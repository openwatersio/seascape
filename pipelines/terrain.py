"""Stage 3 — per-zoom Terrarium terrain render from the MOSAIC.

Replaces BOTH the aggregate's native-zoom terrain fork (old aggregation_tile call) AND the
2x2-average overview pyramid (old downsampling.py). The model the production-build plan mandates:

  each output zoom renders by reading the MOSAIC at that zoom's resolution
  -> depth/zoom-gated smooth AT THAT RESOLUTION (smooth.smooth_array — the ONE smoothing
     function the contour fork also calls, so isobaths agree with the shading)
  -> Terrarium encode (encode.py's conservative, bias-shallow quantization)
  -> single-zoom pmtiles bundle.py already understands (store/pmtiles/<stem>-<key12>.pmtiles).

Why this replaces the 2x2 average: the old path smoothed ONCE at native res then box-averaged the
ENCODED tiles down, so a sigma had no defined ground size at coarse zoom. Reading the mosaic's
nodata-aware `average` overviews at each zoom and smoothing there gives f(depth, zoom) a real
metre meaning at every level (the zoom-tier contour design assumes this). Accepted consequence
(plan): coarse zooms are no longer a strict decimation of fine zooms, so features may shift
slightly between levels — invisible in shading, already intended for contours.

The two pyramids (5a): the mosaic COGs' internal overviews are the *access* pyramid (unsmoothed
truth, `average`-resampled) — a decimated GTI read picks each tile's overview automatically, so the
rule here is ALWAYS read at target resolution, never full-res-then-decimate. This module builds the
*served* pyramid: smoothed, encoded, display-conditioned. z0-~z4 read the mosaic's own GTI-
registered planet z8 COG (the <Overview> in mosaic.gti), not thousands of tile-COG top overviews —
that fallthrough is automatic in the GTI decimated read too.

Reads the mosaic by ABSOLUTE path through the SYSTEM gdal (the 3.13 toolchain the pipeline shells
to), not rasterio's bundled 3.10 — 5a found the GTI's RELATIVE IndexDataset/Overview refs resolve
differently across GDAL versions, so a windowed read must go through the same gdal that wrote the
.gti (see mosaic._check). MOSAIC_VSI_BASE points the tile `location`s at a bucket in CI; unset =
local abspath, which is what a laptop / this render uses. The mosaic MUST already be built +
indexed (mosaic.py index) before this runs — the Justfile orders `aggregate -> mosaic-index ->
terrain`.

  python terrain.py cover     plan the render stems (native + coalesced overviews) for the covering
  python terrain.py run       render every stale stem from the mosaic (one machine, a process pool)
  python terrain.py --check    self-check (renders a synthetic mosaic tile, asserts the pyramid)
"""

import math
import os
import shutil
import sys
import time
from functools import lru_cache
from glob import glob
from multiprocessing import get_context

import mercantile
import numpy as np
import rasterio

import aggregation_reproject
import encode
import keys
import mosaic
import scheduler
import smooth
import utils

NODATA = -9999

# Code the served terrain render depends on: the smoothing (the ONE f(depth,zoom)), the encode
# (bias-shallow quantization), the mosaic reader (whose module bytes + resolution math shape the
# window), and this renderer. A change to any re-keys the render — but NOT the mosaic (the stage
# split: a smoothing/quantization change re-renders from a fully cached mosaic, no re-merge).
TERRAIN_MODULES = ["smooth", "encode", "aggregation_reproject", "mosaic", "utils", "terrain"]


# ── the mosaic GTI, read through the system gdal by absolute path ──────────────────────────────

def gti_abspath():
    """The absolute mosaic.gti path the system gdal opens. A regional/dev build streams the
    published planet GTI by setting MOSAIC_GTI to a /vsicurl URL; unset = the local store's
    pointer (what `just planet` on a laptop and the build box both use — the box hydrates the
    mosaic/ prefix, so the .gti's RELATIVE index/overview refs resolve on local disk)."""
    env = os.environ.get("MOSAIC_GTI")
    if env:
        return env
    return os.path.abspath(mosaic.gti_path())


# The smoothing needs real neighbours across every 512-tile cut inside a render stem AND across
# stem boundaries, so each windowed read carries a halo = smooth's gaussian truncation radius
# (buffer the input); the render then keeps only the interior tiles (restrict the output). The
# halo reads into neighbouring mosaic tiles through the GTI (continuous truth); beyond the built
# area it fills nodata, which the interior never sees.
def _halo_px():
    return int(math.ceil(smooth.TRUNCATE * max(
        smooth.DEM_SIGMA, smooth.DEM_SIGMA_DEEP, smooth.MASK_SIGMA))) + 1


def _read_window(anchor, cz, halo, out_tif):
    """Warp a buffered window of the mosaic — the anchor tile's 3857 bounds expanded by `halo`
    px on every side — to `out_tif` at zoom `cz` resolution, through the SYSTEM gdal. `-r average`
    (the plan's default, and the anti-alias prefilter decimation needs): a decimated read picks
    each mosaic tile's own `average` overview, and z<=8 falls through to the planet z8 COG the .gti
    registers — never a full-res-then-decimate. Exact grid registration: -te/-tr snap the window to
    the global 3857 grid so the interior origin is EXACTLY the anchor's mercantile bounds (5a's
    ceil-overhang trap). -dstnodata fills the beyond-extent halo with the one sentinel.

    Shallow-bias option (plan, deferred): `average` moves an isolated shoal deeper. If the shading
    itself should bias shallow in the navigable band (<=~30 m), a shallowest-in-window reducer
    there (average standing in deep water) is the tool — a cartographic call beyond this pass; the
    conservative signal at coarse zoom is carried by soundings/depare meanwhile."""
    res = aggregation_reproject.get_resolution(cz)
    span = 2 ** (cz - anchor.z)
    core = span * 512
    b = mercantile.xy_bounds(anchor)
    left = b.left - halo * res
    top = b.top + halo * res
    right = left + (core + 2 * halo) * res
    bottom = top - (core + 2 * halo) * res
    aggregation_reproject._run(
        f"GDAL_CACHEMAX=512 gdalwarp -q -overwrite -r average -t_srs EPSG:3857 "
        f"-tr {res} {res} -te {left} {bottom} {right} {top} -dstnodata {NODATA} "
        f"-of GTiff -co TILED=YES -co BLOCKXSIZE=512 -co BLOCKYSIZE=512 -co COMPRESS=ZSTD -co PREDICTOR=3 "
        f"-co NUM_THREADS=ALL_CPUS {_q(gti_abspath())} {out_tif}",
        f"terrain window {anchor.z}-{anchor.x}-{anchor.y}@z{cz}")
    return core, halo


def _q(path):
    """Quote a path for the shell (the GTI abspath can contain spaces, e.g. a worktree path)."""
    return "'" + path.replace("'", "'\\''") + "'"


# ── render one stem ────────────────────────────────────────────────────────────────────────────

def _encode_tile(i, j, src_path, halo, out_webp, cz):
    """One 512-tile of the smoothed window -> a lossless Terrarium webp at zoom `cz`. The window's
    interior starts at `halo`; nodata (a build-edge hole the mosaic legitimately carries — GEBCO
    fills the ocean, so this is only the beyond-build fringe) resolves to 0 for the served render,
    which has no transparency."""
    col = i * 512 + halo
    row = j * 512 + halo
    with rasterio.open(src_path) as src:
        data = src.read(1, window=rasterio.windows.Window(col, row, 512, 512))
    # GEBCO (priority 0, global) is inside the mosaic, so any surviving interior nodata is genuinely
    # UNCOVERED — no source has it. Resolving it to 0 m = shoreline/datum, which is shallow and
    # chart-safe (never a false DEEP); the served Terrarium has no transparency, so it must be some
    # value, and shoaling it is the conservative choice. (In practice this is only the beyond-build
    # fringe; true interior holes don't occur while GEBCO blankets the planet.)
    data[data == NODATA] = 0
    utils.save_terrarium_tile(data, out_webp)  # utils parses zoom from the {cz}-{x}-{y}.webp name


def _render(stem, out_pmtiles):
    """Render one stem `z-x-y-cz` from the mosaic into `out_pmtiles` (the caller's content name):
    read the buffered window at cz res -> smooth per-zoom -> cut the interior into the span*span
    512-tiles -> encode -> pack. Published atomically."""
    z, x, y, cz = (int(a) for a in stem.split("-"))
    anchor = mercantile.Tile(x=x, y=y, z=z)
    span = 2 ** (cz - z)
    halo = _halo_px()

    tmp = f"store/terrain-tmp/{stem}"
    utils.create_folder(tmp)
    window = f"{tmp}/window.tif"
    # Reserve this stem's memory weight across the RAM peak: the gdalwarp window read + the smooth
    # hold the multi-GB window array (a native macrotile window is 32768² float32). The budget
    # bounds how many dense windows warp+smooth at once so a spawn Pool sized to cores can't OOM.
    # The per-512-tile encode below reads small windows off the on-disk smoothed tiff — light, so it
    # runs outside the reservation (scheduler.reserve is try/finally: a warp/smooth error frees it).
    with scheduler.reserve(stem):
        core, halo = _read_window(anchor, cz, halo, window)
        if not os.environ.get("SKIP_SMOOTH"):
            smooth.smooth_tiff(window)  # the ONE f(depth,zoom); halo makes interior identical to whole

    x_min, y_min = x * span, y * span
    from concurrent.futures import ThreadPoolExecutor
    threads = min(os.cpu_count() or 1, 8)
    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = [
            pool.submit(_encode_tile, i, j, window, halo, f"{tmp}/{cz}-{tx}-{ty}.webp", cz)
            for i, tx in enumerate(range(x_min, x_min + span))
            for j, ty in enumerate(range(y_min, y_min + span))
        ]
        for f in futures:
            f.result()

    tmp_archive = f"{tmp}/terrain.pmtiles"  # not *.webp, so create_archive's glob skips it
    utils.create_archive(tmp, tmp_archive)
    keys.publish(tmp_archive, out_pmtiles)
    shutil.rmtree(tmp)
    scheduler.log_peak(stem)  # whole-render peak RSS vs weight — factor-tuning data


# ── the render covering: native aggregation stems + coalesced overview parents ──────────────────
# Ported from the old downsampling `cover` cascade (which planned the overview parents by coalescing
# aggregation extents). Here it enumerates the SAME multi-zoom stem set bundle.py groups (native +
# overviews), but every stem RENDERS from the mosaic — there are no "children" to average, so the
# marker CSV carries no filenames, only the stem's identity. get_simplified_extents bounds a stem's
# pixel span to 2**num_overviews*512 so no render window is unbounded.

def _covering_id():
    ids = utils.get_aggregation_ids()
    if not ids:
        sys.exit("terrain: no covering — run `just cover` first")
    return ids[-1]


def _extents_at(aid, zoom):
    """Every render extent already planned at `zoom` — the aggregation stems (native) and any
    terrain markers written for that zoom (coarser parents the cascade already emitted)."""
    out = []
    for pattern in (f"*-*-*-{zoom}-aggregation.csv", f"*-*-*-{zoom}-terrain.csv"):
        for fp in glob(f"store/aggregation/{aid}/{pattern}"):
            z, x, yy = (int(a) for a in fp.split("/")[-1].split("-")[:3])
            out.append(mercantile.Tile(x=x, y=yy, z=z))
    return out


def _simplified(extents, zoom):
    simplified = []
    for unlimited in mercantile.simplify(extents):
        if unlimited.z == zoom:
            simplified.append(mercantile.parent(unlimited, zoom=zoom - 1))
        elif unlimited.z >= zoom - utils.num_overviews:
            simplified.append(unlimited)
        else:
            simplified += list(mercantile.children(unlimited, zoom=zoom - utils.num_overviews))
    return simplified


def _marker(aid, stem):
    return f"store/aggregation/{aid}/{stem}-terrain.csv"


def cover():
    """Plan the render stems. Native stems come straight from the aggregation CSVs (each renders at
    its own child_z); overview parents are the coalesced cascade down to z0. Writes one
    `<stem>-terrain.csv` marker per render stem — a work item for `run`, no children inside."""
    aid = _covering_id()
    utils.run_command(f"rm -f store/aggregation/{aid}/*-terrain.csv")
    # native: one marker per aggregation stem (the finest render, at child_z).
    for fp in glob(f"store/aggregation/{aid}/*-aggregation.csv"):
        stem = fp.split("/")[-1].replace("-aggregation.csv", "")
        with open(_marker(aid, stem), "w") as f:
            f.write("# terrain render marker (native)\n")
    # overviews: coalesce extents at each child_zoom into parents one zoom coarser, cascading down
    # (a parent written here is itself an extent the next-coarser pass reads — the pyramid to z0).
    for child_zoom in reversed(range(1, 32)):
        extents = _extents_at(aid, child_zoom)
        if not extents:
            continue
        seen = set()
        for simplified in _simplified(extents, child_zoom):
            pstem = f"{simplified.z}-{simplified.x}-{simplified.y}-{child_zoom - 1}"
            if pstem in seen:
                continue
            seen.add(pstem)
            with open(_marker(aid, pstem), "w") as f:
                f.write("# terrain render marker (overview)\n")


# ── keys, dirty, run ────────────────────────────────────────────────────────────────────────────

def _aggregation_mosaic_keys(aid):
    """{aggregation stem -> its mosaic tile key}. The mosaic tile a stem reads IS keyed by
    mosaic.mosaic_key(csv); a re-merged/re-precedenced tile gets a new mosaic key here -> a new
    terrain key -> a re-render. Cached per covering read isn't needed — one call per `run`."""
    out = {}
    for fp in glob(f"store/aggregation/{aid}/*-aggregation.csv"):
        stem = fp.split("/")[-1].replace("-aggregation.csv", "")
        out[stem] = mosaic.mosaic_key(fp)
    return out


@lru_cache(maxsize=None)
def _tile_bounds(stem):
    """mercantile lon/lat bounds of a 'z-x-y-cz' stem, memoised. `_intersecting_keys` runs once per
    render stem over the SAME aggregation tiles, so without the cache a planet build recomputes each
    tile's projected bounds thousands of times — the cost Copilot flagged. The cache makes the
    expensive `mercantile.bounds` O(unique tiles); the per-stem loop is then only cheap float
    compares. (A macrotile-grid spatial index would cut the loop to O(stems × 9) too, but the
    compares are already sub-second at planet scale — deferred until measured to dominate.)"""
    z, x, y, _cz = (int(a) for a in stem.split("-"))
    return mercantile.bounds(mercantile.Tile(x=x, y=y, z=z))


def _intersecting_keys(stem, agg_keys):
    """The mosaic tile keys a render stem reads INTO — every aggregation tile whose mercantile tile
    overlaps OR EDGE/CORNER-TOUCHES the stem's anchor. The touch case is load-bearing for chart
    safety: _read_window buffers the anchor by a halo (smooth's truncation radius) and blends those
    haloed pixels into the served EDGE band, so the immediate 8-neighbour tiles genuinely shape the
    output — they must enter the key or a neighbour-only re-merge leaves a stale seam depth. Hence
    INCLUSIVE bounds (<=/>=): adjacent tiles share exact grid coordinates (the east neighbour's west
    == the anchor's east), and the halo (65 px) is far under the 512 px minimum tile width, so it
    never reaches past the immediate neighbours — the touch test is both necessary and sufficient.
    Sorted, so the terrain key is order-independent."""
    ab = _tile_bounds(stem)
    keyset = []
    for astem, k in agg_keys.items():
        bb = _tile_bounds(astem)
        # lon/lat bbox overlap-or-touch (tiles share a global grid); >=/<= so an edge/corner
        # neighbour the halo reads is included, not just a strictly-overlapping ancestor/descendant.
        if bb.west <= ab.east and bb.east >= ab.west and bb.south <= ab.north and bb.north >= ab.south:
            keyset.append(k)
    return sorted(set(keyset))


def _config():
    """The served-render config in the key: the smoothing knobs (unless SKIP_SMOOTH) and the
    encode's quantization constants + zoom-tiling floors. A SMOOTH_* / quantization change re-keys
    every terrain stem and nothing upstream (the payoff)."""
    cfg = {
        "quant_full_zoom": encode.FULL_RESOLUTION_ZOOM,
        "shallow_rel": encode.SHALLOW_REL, "shallow_min_step": encode.SHALLOW_MIN_STEP,
        "macrotile_z": utils.macrotile_z, "num_overviews": utils.num_overviews,
        "resample": aggregation_reproject.RESAMPLE,
    }
    if not os.environ.get("SKIP_SMOOTH"):
        cfg["smooth"] = {
            "sigma": smooth.DEM_SIGMA, "sigma_deep": smooth.DEM_SIGMA_DEEP,
            "mask_sigma": smooth.MASK_SIGMA, "slope_low": smooth.SLOPE_LOW,
            "slope_high": smooth.SLOPE_HIGH, "depth_full": smooth.DEPTH_FULL,
            "depth_smooth": smooth.DEPTH_SMOOTH, "block": smooth.BLOCK}
    return cfg


def terrain_key(stem, agg_keys):
    return keys.stage_key(_intersecting_keys(stem, agg_keys), TERRAIN_MODULES,
                          {**_config(), "product": "terrain"})


def _artifact(stem):
    z, x, y, _cz = (int(a) for a in stem.split("-"))
    return f"{utils.get_pmtiles_folder(x, y, z)}/{stem}.pmtiles"


def _render_stems(aid):
    """Every render stem in the covering: native (aggregation) + overview (terrain markers)."""
    stems = set()
    for fp in glob(f"store/aggregation/{aid}/*-aggregation.csv"):
        stems.add(fp.split("/")[-1].replace("-aggregation.csv", ""))
    for fp in glob(f"store/aggregation/{aid}/*-terrain.csv"):
        stems.add(fp.split("/")[-1].replace("-terrain.csv", ""))
    return stems


def dirty_stems():
    """Render stems whose content-addressed pmtiles is absent under its current key (self-heal, or
    the mosaic tile(s) it reads / the smooth+encode config moved). FORCE_REBUILD makes every stem
    stale. Heaviest-first (child_z desc) balances the pool."""
    aid = _covering_id()
    agg_keys = _aggregation_mosaic_keys(aid)
    out = []
    for stem in _render_stems(aid):
        if not keys.fork_fresh(_artifact(stem), terrain_key(stem, agg_keys)):
            out.append(stem)
    return sorted(out, key=lambda s: (-int(s.split("-")[3]), s))


def _run_one(stem_and_key):
    stem, key = stem_and_key
    art = _artifact(stem)
    keys.supersede(art)  # clear last build's key before the atomic publish (crash -> reads stale)
    _render(stem, keys.content_path(art, key))
    return stem


def run():
    """Render every stale stem from the mosaic on one machine. SPAWN, not fork: this module inits
    GDAL at import (rasterio), and GDAL is not fork-safe (same reason as the old downsampling pool).
    Cap workers via TERRAIN_PROCESSES (each holds one window — a native macrotile is multi-GB); the
    build box sizes it from RAM. unset/0 = all cores."""
    aid = _covering_id()
    agg_keys = _aggregation_mosaic_keys(aid)
    stems = dirty_stems()
    if not stems:
        print("terrain: nothing to render")
        return
    work = [(s, terrain_key(s, agg_keys)) for s in stems]
    # Pool size = cores (TERRAIN_PROCESSES, unset/0 = all cores); peak RAM is bounded separately by a
    # shared GB budget (TERRAIN_MEM_BUDGET_GB) each worker reserves across the window warp+smooth in
    # _render, so the densest native windows can't all peak at once. Budget unset/0 = plain pool.
    # The budget's Manager proxies pickle into the SPAWN workers via the Pool initializer.
    procs = int(os.environ.get("TERRAIN_PROCESSES", "0")) or None
    budget = int(os.environ.get("TERRAIN_MEM_BUDGET_GB", "0"))
    mgr, pool_kwargs = scheduler.pool_kwargs(budget)
    print(f"terrain: rendering {len(work)} stem(s) from the mosaic...")
    done, last = 0, time.monotonic()
    try:
        with get_context("spawn").Pool(procs, **pool_kwargs) as pool:
            for _stem in pool.imap_unordered(_run_one, work, chunksize=1):
                done += 1
                if time.monotonic() - last > 30:
                    print(f"  {done}/{len(work)} stems rendered", flush=True)
                    last = time.monotonic()
    finally:
        if mgr is not None:
            mgr.shutdown()
    print(f"terrain: {done} stem(s) rendered")


def _check():
    """Render two zooms of a synthetic single-tile mosaic and assert: exact grid registration
    (the served tiles match the mercantile grid), the served pyramid decodes to the mosaic's depth,
    nodata resolves to a served value (no hole), and the terrain key is stable / moves on a mosaic-
    tile-key change / a smooth-config change (the stage split's whole point)."""
    import json
    import tempfile

    import imagecodecs
    from pmtiles.reader import MmapSource, Reader, all_tiles
    from rasterio.transform import from_origin

    saved_env = {k: os.environ.pop(k, None) for k in ("MOSAIC_GTI", "FORCE_REBUILD", "SKIP_SMOOTH")}
    cwd = os.getcwd()
    d = tempfile.mkdtemp()
    try:
        os.chdir(d)
        # A z8 mosaic tile at child_z=10 (span 4 -> 2048 px native), constant -30 m with a nodata
        # hole in one corner, built as a real COG with average overviews (mosaic.produce's shape).
        z, x, y, cz = 8, 75, 96, 10
        stem = f"{z}-{x}-{y}-{cz}"
        res = aggregation_reproject.get_resolution(cz)
        core = 4 * 512
        b = mercantile.xy_bounds(mercantile.Tile(x=x, y=y, z=z))
        arr = np.full((core, core), -30.0, dtype="float32")
        arr[-200:, -200:] = NODATA
        os.makedirs("store/mosaic/tiles")
        raw = "store/mosaic/tiles/raw.tif"
        with rasterio.open(raw, "w", driver="GTiff", height=core, width=core, count=1,
                           dtype="float32", nodata=NODATA, crs="EPSG:3857",
                           transform=from_origin(b.left, b.top, res, res)) as dst:
            dst.write(arr, 1)
        cog = f"store/mosaic/tiles/{stem}-abc123abc123.tif"
        utils.run_command(f"gdal_translate -q -of COG -a_nodata {NODATA} -co RESAMPLING=AVERAGE "
                          f"-co OVERVIEW_RESAMPLING=AVERAGE -co BLOCKSIZE=512 {raw} {cog}")
        os.remove(raw)
        # a minimal GTI over the one tile (absolute location; a real build's mosaic.py writes this)
        gj = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {
            "location": os.path.abspath(cog), "resx": res, "resy": res},
            "geometry": {"type": "Polygon", "coordinates": [[[b.left, b.bottom], [b.right, b.bottom],
                         [b.right, b.top], [b.left, b.top], [b.left, b.bottom]]]}}]}
        with open("store/mosaic/idx.geojson", "w") as f:
            json.dump(gj, f)
        utils.run_command("ogr2ogr -f Parquet -a_srs EPSG:3857 store/mosaic/idx.parquet store/mosaic/idx.geojson")
        with open(mosaic.gti_path(), "w") as f:
            f.write("<GDALTileIndexDataset>\n  <IndexDataset>idx.parquet</IndexDataset>\n"
                    "  <LocationField>location</LocationField>\n  <SRS>EPSG:3857</SRS>\n"
                    f"  <NoDataValue>{NODATA}</NoDataValue>\n</GDALTileIndexDataset>\n")

        # a covering with the one aggregation tile, so keys resolve the real mosaic key
        import config
        saved_dir = config.SOURCES_DIR
        config.SOURCES_DIR = "sources"
        config._catalog_cache.clear()
        os.makedirs("sources/gebco")
        with open("sources/gebco/metadata.json", "w") as f:
            json.dump({"name": "gebco", "priority": 0, "max_zoom": 10}, f)
        aid = "01TERRAINTERRAINTERRAINTE"
        os.makedirs(f"store/aggregation/{aid}")
        csv = f"store/aggregation/{aid}/{stem}-aggregation.csv"
        with open(csv, "w") as f:
            f.write("source,filename,maxzoom\ngebco,gebco_0.tif,10\n")

        cover()
        stems = _render_stems(aid)
        assert stem in stems, "native stem must be a render stem"
        assert any(s.endswith("-9") for s in stems), "an overview parent (z9) must be planned"

        agg_keys = _aggregation_mosaic_keys(aid)
        k = terrain_key(stem, agg_keys)
        config._catalog_cache.clear()
        assert terrain_key(stem, agg_keys) == k, "terrain key must be stable across a no-op recompute"

        # render the native stem; assert the served tiles register on the mercantile grid and decode
        cpath = keys.content_path(_artifact(stem), k)
        _render(stem, cpath)
        with open(cpath, "r+b") as f:
            tiles = dict(all_tiles(Reader(MmapSource(f)).get_bytes))
        span = 4
        assert len(tiles) == span * span, f"native render must emit {span*span} z{cz} tiles, got {len(tiles)}"
        for (tz, tx, ty), payload in tiles.items():
            assert tz == cz and x * span <= tx < x * span + span and y * span <= ty < y * span + span, \
                f"served tile {(tz,tx,ty)} off the mercantile grid for {stem}"
            elev = encode.decode(imagecodecs.webp_decode(payload).astype("float32"))
            assert elev.shape == (512, 512)
        # the corner tile carries the nodata hole -> served as 0 (no transparency), NOT left nodata
        corner = tiles[(cz, x * span + span - 1, y * span + span - 1)]
        cel = encode.decode(imagecodecs.webp_decode(corner).astype("float32"))
        assert (cel == 0).any(), "the mosaic's nodata hole must resolve to a served value (0), not a hole"
        # an INTERIOR tile (no hole, no build edge) must decode to exactly the mosaic depth — the
        # served pyramid tracks the truth; the smooth pull toward 0 only shows at the hole/edge.
        inner = encode.decode(imagecodecs.webp_decode(tiles[(cz, x * span + 1, y * span + 1)])
                              .astype("float32"))
        assert (np.abs(inner - (-30.0)) < 1e-3).all(), \
            f"interior served depth must track the mosaic (-30 m), got {np.unique(inner)[:5]}"

        # render an overview stem too (reads the mosaic's average overview); it must decode to ~-30
        ostem = next(s for s in stems if s.endswith("-9"))
        oc = keys.content_path(_artifact(ostem), terrain_key(ostem, agg_keys))
        _render(ostem, oc)
        with open(oc, "r+b") as f:
            ot = dict(all_tiles(Reader(MmapSource(f)).get_bytes))
        assert ot, "overview render produced no tiles"
        allpx = np.concatenate([encode.decode(imagecodecs.webp_decode(v).astype("float32")).ravel()
                                for v in ot.values()])
        wet = allpx[allpx < -1]  # the covered water (0 = beyond-build fill / nodata; ignore)
        assert len(wet) and abs(float(np.median(wet)) - (-30.0)) < 3.0, \
            f"overview served depth must track the mosaic (-30 m), median {np.median(wet):.1f}"

        # key sensitivity: a mosaic-tile-key change re-keys terrain; a smooth-config change too
        agg_keys2 = dict(agg_keys); agg_keys2[stem] = "deadbeefdead"
        assert terrain_key(stem, agg_keys2) != k, "a mosaic tile key change must move the terrain key"

        # TWO-TILE fixture (the single-tile store structurally can't cover this): the anchor's
        # 65 px halo reads its EDGE NEIGHBOUR's mosaic tile and blends it into the served seam band,
        # so a neighbour-only re-merge MUST move the anchor's terrain key (else a stale seam depth —
        # a chart-safety bug). anchor 8-75-96-14 + east neighbour 8-76-96-14 (shares the anchor's
        # east edge) + a far tile 8-200-96-14 that neither overlaps nor touches.
        anc, nbr, far = "8-75-96-14", "8-76-96-14", "8-200-96-14"
        two = {anc: "1111aaaa1111", nbr: "2222bbbb2222"}
        base2 = terrain_key(anc, two)
        assert two[nbr] in _intersecting_keys(anc, two), "the edge neighbour must be an intersecting tile"
        bumped = {**two, nbr: "3333cccc3333"}  # the neighbour re-merged (new mosaic key)
        assert terrain_key(anc, bumped) != base2, \
            "a neighbour-only mosaic re-key must move the anchor's terrain key (the halo reads it)"
        assert terrain_key(anc, {**two, far: "4444dddd4444"}) == base2, \
            "a non-touching far tile must NOT enter the anchor's terrain key"

        saved_sigma = smooth.DEM_SIGMA_DEEP
        smooth.DEM_SIGMA_DEEP = saved_sigma + 1
        try:
            assert terrain_key(stem, agg_keys) != k, "a smooth-config change must move the terrain key"
        finally:
            smooth.DEM_SIGMA_DEEP = saved_sigma
        config.SOURCES_DIR = saved_dir
        print("terrain.py self-check ok")
    finally:
        os.chdir(cwd)
        for kk, vv in saved_env.items():
            if vv is not None:
                os.environ[kk] = vv
        shutil.rmtree(d, ignore_errors=True)


def main(argv):
    if argv == ["cover"]:
        cover()
    elif argv == ["run"]:
        run()
    elif argv[:1] == ["--check"]:
        _check()
    else:
        sys.exit("usage: terrain.py <cover | run | --check>")


if __name__ == "__main__":
    main(sys.argv[1:])
