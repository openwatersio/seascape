"""Stage 3 — per-zoom Terrarium terrain render from the MOSAIC.

Each output zoom renders by reading the MOSAIC at that zoom's resolution
  -> depth/zoom-gated smooth AT THAT RESOLUTION (smooth.smooth_array — the ONE smoothing
     function the contour fork also calls, so isobaths agree with the shading)
  -> land sentinel (clamp to DRYING_CAP+1 so decoded v > DRYING_CAP reads land/out-of-scope) +
     land-side 0 nudge (land-side exact-0 -> +LSB so decoded 0 stays unambiguously water)
  -> Terrarium encode (encode.py's conservative, bias-shallow quantization)
  -> single-zoom pmtiles bundle.py concatenates (store/pmtiles/<stem>.pmtiles).

Reading the mosaic's nodata-aware `average` overviews at each zoom and smoothing there gives
f(depth, zoom) a real metre meaning at every level (the zoom-tier contour design assumes this).
Consequence: coarse zooms are not a strict decimation of fine zooms, so features may shift slightly
between levels — invisible in shading, intended for contours.

Two pyramids: the mosaic COGs' internal overviews are the *access* pyramid (unsmoothed
truth, `average`-resampled) — a decimated GTI read picks each tile's overview automatically, so the
rule here is ALWAYS read at target resolution, never full-res-then-decimate. This module builds the
*served* pyramid: smoothed, encoded, display-conditioned. z0-~z4 read the mosaic's own GTI-
registered planet z8 COG (the <Overview> in mosaic.gti), not thousands of tile-COG top overviews —
that fallthrough is automatic in the GTI decimated read too.

Reads the mosaic by ABSOLUTE path through the SYSTEM gdal (the toolchain the pipeline shells to),
not rasterio's bundled GDAL — the GTI's RELATIVE IndexDataset/Overview refs resolve
differently across GDAL versions, so a windowed read must go through the same gdal that wrote the
.gti. MOSAIC_VSI_BASE points the tile `location`s at a bucket in CI; unset = local abspath, which
is what a laptop / this render uses. The mosaic MUST already be built + indexed
(mosaic.py index --stable) before this runs — the terrain_render rule gates on mosaic_index.

build.smk derives the render stems (render_stems) at parse time and schedules one
`terrain.py render <stem>` job per stem, writing store/pmtiles/<stem>.pmtiles.

  python terrain.py render <stem>   render one stem from the mosaic to the plain flat name
  python terrain.py --check         self-check (renders a synthetic mosaic tile, asserts the pyramid)
"""

import math
import os
import shutil
import tempfile
import sys

import mercantile
import numpy as np
import rasterio

import aggregation_reproject
import config
import encode
import landmask
import mosaic
import smooth
import utils

NODATA = -9999


# ── the mosaic GTI, read through the system gdal by absolute path ──────────────────────────────

def gti_abspath():
    """The absolute mosaic.gti path the system gdal opens. A regional/dev build streams the
    published planet GTI by setting MOSAIC_GTI to a /vsicurl URL; unset = the local store's
    pointer (what a laptop `snakemake preview` and the build box both use — the box hydrates the
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


def window_tiles(stem):
    """The per-stem read set for cz>=8 renders: intersecting tiles buffered by the smooth halo
    in meters at cz res — which outgrows the fixed macrotile buffer at coarse zooms."""
    cz = int(stem.split("-")[3])
    return mosaic.intersecting_tiles(stem, _halo_px() * aggregation_reproject.get_resolution(cz))


def _read_window(anchor, cz, halo, out_tif, src=None):
    """Warp a buffered window of the mosaic — the anchor tile's 3857 bounds expanded by `halo`
    px on every side — to `out_tif` at zoom `cz` resolution, through the SYSTEM gdal. `-r average`
    (the anti-alias prefilter decimation needs): a decimated read picks
    each mosaic tile's own `average` overview, and z<=8 falls through to the planet z8 COG the .gti
    registers — never a full-res-then-decimate. Exact grid registration: -te/-tr snap the window to
    the global 3857 grid so the interior origin is EXACTLY the anchor's mercantile bounds (a slip
    ceils the virtual extent to a 1-px overhang). -dstnodata fills the beyond-extent halo with the
    one sentinel."""
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
        f"-co NUM_THREADS=ALL_CPUS {_q(src or gti_abspath())} {out_tif}",
        f"terrain window {anchor.z}-{anchor.x}-{anchor.y}@z{cz}")
    return core, halo


def _q(path):
    """Quote a path for the shell (the GTI abspath can contain spaces, e.g. a worktree path)."""
    return "'" + path.replace("'", "'\\''") + "'"


# ── render one stem ────────────────────────────────────────────────────────────────────────────

# The published raster carries depth only: `v < 0` is bathymetry, and the non-negative domain is
# three flat codes — 0 unknown-depth water, 1 drying foreshore, 2 land. Flat codes have no slope,
# so client hillshade shows nothing on land (no halo ring at 0-filled lakes, no fake topo relief);
# all three are exact multiples of the 0.25 m quantize floor, so they decode exactly at every zoom.
UNKNOWN, DRYING, LAND = 0.0, 1.0, 2.0


def _encode_tile(i, j, src_path, halo, out_webp, cz, mask_path=None):
    """One 512-tile of the smoothed window -> a lossless Terrarium webp at zoom `cz`. The window's
    interior starts at `halo`; nodata (a build-edge hole the mosaic legitimately carries — GEBCO
    fills the ocean, so this is only the beyond-build fringe) resolves to 0 for the served render,
    which has no transparency."""
    col = i * 512 + halo
    row = j * 512 + halo
    win = rasterio.windows.Window(col, row, 512, 512)
    with rasterio.open(src_path) as src:
        data = src.read(1, window=win)
        dims = (src.height, src.width)
    # GEBCO (priority 0, global) is inside the mosaic, so any surviving interior nodata is genuinely
    # UNCOVERED — no source has it. Resolving it to 0 m = shoreline/datum, which is shallow and
    # chart-safe (never a false DEEP); the served Terrarium has no transparency, so it must be some
    # value, and shoaling it is the conservative choice. (In practice this is only the beyond-build
    # fringe; true interior holes don't occur while GEBCO blankets the planet.)
    data[data == NODATA] = 0
    pos = data > 0
    if mask_path is not None:
        with rasterio.open(mask_path) as m:
            if (m.height, m.width) != dims:  # same guard as the landmask clamps
                raise ValueError(f"land mask {(m.height, m.width)} != window {dims} for {src_path}")
            land = m.read(1, window=win) == 1
        # Water-side (0, cap] is genuine drying — the 4th-quadrant clamp zeroed coarse fakes at
        # warp, so only trusted-source foreshore survives there. Everything else positive (land
        # topo, land-side lowland, tall unmapped rock over water) is land, as is land-side exact-0
        # (clamped polders); water-side exact-0 stays the unknown code.
        drying = pos & ~land & (data <= config.DRYING_CAP)
        data[pos] = LAND
        data[drying] = DRYING
        data[(data == 0) & land] = LAND
    else:
        data[pos] = LAND  # maskless (coarse stems / no feed): drying is sub-pixel there anyway
    utils.save_terrarium_tile(data, out_webp)  # utils parses zoom from the {cz}-{x}-{y}.webp name


def _render(stem, out_pmtiles):
    """Render one stem `z-x-y-cz` from the mosaic into `out_pmtiles`:
    read the buffered window at cz res -> smooth per-zoom -> cut the interior into the span*span
    512-tiles -> encode -> pack. Published atomically."""
    z, x, y, cz = (int(a) for a in stem.split("-"))
    anchor = mercantile.Tile(x=x, y=y, z=z)
    span = 2 ** (cz - z)
    halo = _halo_px()

    tmp = tempfile.mkdtemp(prefix=f"terrain-{stem}-")  # local scratch; publish crosses to the store
    window = f"{tmp}/window.tif"
    # cz>=8 reads a throwaway VRT of the halo-buffered tile set, so a render never waits on the
    # planet-wide GTI; cz<8 needs the GTI's planet-z8-COG fall-through. MOSAIC_GTI still wins.
    src = None
    if cz >= 8 and not os.environ.get("MOSAIC_GTI"):
        src = f"{tmp}/window.vrt"
        tiles = " ".join(mosaic.tile_artifact(s) for s in window_tiles(stem))
        aggregation_reproject._run(f"gdalbuildvrt -q -overwrite -resolution highest {src} {tiles}",
                                   f"terrain vrt {stem}")
    # The RAM peak the terrain_render rule reserves (build.smk mem_gb=weight) is here — the gdalwarp
    # window read + the smooth hold the multi-GB window array (a native macrotile window is 32768²
    # float32). The engine schedules one process per stem, so the cgroup cap bounds each in isolation.
    core, halo = _read_window(anchor, cz, halo, window, src)
    if not os.environ.get("SKIP_SMOOTH"):
        smooth.smooth_tiff(window)  # the ONE f(depth,zoom); halo makes interior identical to whole

    # Rasterize the combined land mask on the window's exact grid so _encode_tile can nudge
    # land-side exact-0 pixels (clamped polders, beyond-build land fringe) to land, keeping decoded
    # 0 unambiguously water. Same #24 machinery, degrades to no-nudge when no mask is published.
    # Regional windows only (cz>=8, the per-tile VRT path): a planet-scale window's -spat clip
    # streams the entire vector mask (GBs) instead of an indexed subset. At z<=7 a land-side
    # exact-0 area is a few pixels; a rasterized planet-z8 mask artifact is the upgrade path.
    mask = None
    if cz >= 8 and landmask._present(landmask.path()):
        mask = f"{tmp}/landmask.tif"
        with rasterio.open(window) as w:
            b, wres = w.bounds, w.res[0]
        landmask.rasterize((b.left, b.bottom, b.right, b.top), wres, mask)

    x_min, y_min = x * span, y * span
    from concurrent.futures import ThreadPoolExecutor
    threads = min(os.cpu_count() or 1, 8)
    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = [
            pool.submit(_encode_tile, i, j, window, halo, f"{tmp}/{cz}-{tx}-{ty}.webp", cz, mask)
            for i, tx in enumerate(range(x_min, x_min + span))
            for j, ty in enumerate(range(y_min, y_min + span))
        ]
        for f in futures:
            f.result()

    tmp_archive = f"{tmp}/terrain.pmtiles"  # not *.webp, so create_archive's glob skips it
    utils.create_archive(tmp, tmp_archive)
    utils.publish(tmp_archive, out_pmtiles)
    shutil.rmtree(tmp)


# ── the render covering: native aggregation stems + coalesced overview parents ──────────────────
# Enumerates the multi-zoom stem set bundle.py groups (native + overviews); every stem RENDERS from
# the mosaic. _simplified bounds a stem's pixel span to 2**num_overviews*512 so no render window is
# unbounded; render_stems is the pure, parse-time cascade build.smk derives its outputs from.

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


def _config():
    """The served-render config the terrain_render rule carries as a param: the smoothing knobs
    (unless SKIP_SMOOTH) and the encode's quantization constants + zoom-tiling floors. A SMOOTH_* /
    quantization change reruns every terrain stem and nothing upstream (the payoff)."""
    cfg = {
        "quant_full_zoom": encode.FULL_RESOLUTION_ZOOM,
        "shallow_rel": encode.SHALLOW_REL, "shallow_min_step": encode.SHALLOW_MIN_STEP,
        "macrotile_z": utils.macrotile_z, "num_overviews": utils.num_overviews,
        "resample": aggregation_reproject.RESAMPLE, "drying_cap": config.DRYING_CAP,
    }
    if not os.environ.get("SKIP_SMOOTH"):
        cfg["smooth"] = {
            "sigma": smooth.DEM_SIGMA, "sigma_deep": smooth.DEM_SIGMA_DEEP,
            "mask_sigma": smooth.MASK_SIGMA, "slope_low": smooth.SLOPE_LOW,
            "slope_high": smooth.SLOPE_HIGH, "depth_full": smooth.DEPTH_FULL,
            "depth_smooth": smooth.DEPTH_SMOOTH, "block": smooth.BLOCK}
    return cfg


def render_stems(covering_stems):
    """The render-stem cascade, PURE: native render stems are the covering stems; overview parents
    coalesce per child zoom cascading to z0 (a parent added at one zoom is an extent the next-coarser
    pass reads). Parse-time cheap: mercantile only, no store reads — build.smk derives the
    terrain_render outputs from this."""
    out = set(covering_stems)
    for child_zoom in reversed(range(1, 32)):
        extents = []
        for s in out:
            z, x, y, cz = (int(a) for a in s.split("-"))
            if cz == child_zoom:
                extents.append(mercantile.Tile(x=x, y=y, z=z))
        if not extents:
            continue
        for simplified in _simplified(extents, child_zoom):
            out.add(f"{simplified.z}-{simplified.x}-{simplified.y}-{child_zoom - 1}")
    return sorted(out)


def render(stem):
    """The stage-3 Snakemake job: render one stem from the mosaic (via the local GTI mosaic_index
    wrote) to store/pmtiles/<stem>.pmtiles — a flat name, not sharded (bundling globs it)."""
    out = f"store/pmtiles/{stem}.pmtiles"
    os.makedirs("store/pmtiles", exist_ok=True)
    _render(stem, out)
    print(f"terrain render {stem}: {out}", flush=True)


def _check():
    """Derive the render stems from a one-tile covering (native + overview cascade), render a
    synthetic single-tile mosaic at two zooms, and assert exact grid
    registration (served tiles match the mercantile grid), the served pyramid decodes to the
    mosaic's depth, and the mosaic's nodata hole resolves to a served value (no transparency)."""
    import json
    import tempfile

    import imagecodecs
    from pmtiles.reader import MmapSource, Reader, all_tiles
    from rasterio.transform import from_origin

    saved_env = {k: os.environ.pop(k, None) for k in ("MOSAIC_GTI", "SKIP_SMOOTH")}
    cwd = os.getcwd()
    d = tempfile.mkdtemp()
    try:
        os.chdir(d)
        # A z8 mosaic tile at child_z=10 (span 4 -> 2048 px native), constant -30 m with a nodata
        # hole in one corner, built as a real COG with average overviews (mosaic.tile's shape).
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
        cog = f"store/mosaic/tiles/{stem}.tif"
        utils.run_command(f"gdal_translate -q -of COG -a_nodata {NODATA} -co RESAMPLING=AVERAGE "
                          f"-co OVERVIEW_RESAMPLING=AVERAGE -co BLOCKSIZE=512 {raw} {cog}")
        os.remove(raw)
        os.makedirs("store/aggregation")
        with open("store/aggregation/covering.txt", "w") as f:
            f.write(stem + "\n")  # window_tiles resolves the cz>=8 VRT read set from the covering
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

        # render_stems (pure): the native stem plus the coalesced overview parents down to z0.
        stems = render_stems([stem])
        assert stem in stems, "native stem must be a render stem"
        assert any(s.endswith("-9") for s in stems), "an overview parent (z9) must be planned"

        # render the native stem to its PLAIN name; assert the tiles register on the mercantile grid
        render(stem)
        cpath = f"store/pmtiles/{stem}.pmtiles"
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
        render(ostem)
        with open(f"store/pmtiles/{ostem}.pmtiles", "r+b") as f:
            ot = dict(all_tiles(Reader(MmapSource(f)).get_bytes))
        assert ot, "overview render produced no tiles"
        allpx = np.concatenate([encode.decode(imagecodecs.webp_decode(v).astype("float32")).ravel()
                                for v in ot.values()])
        wet = allpx[allpx < -1]  # the covered water (0 = beyond-build fill / nodata; ignore)
        assert len(wet) and abs(float(np.median(wet)) - (-30.0)) < 3.0, \
            f"overview served depth must track the mosaic (-30 m), median {np.median(wet):.1f}"

        # _encode_tile's render-path classification. A window with a land-topo block, a land-side
        # lowland, a land-side 0, a water-side drying height, and a water-side 0, plus a matching
        # land mask: every non-negative pixel decodes to its exact code at this zoom — land topo,
        # land lowland, and land-0 all 2; water-side (0, cap] exactly 1; water-side 0 exactly 0 —
        # and measured depth passes through untouched.
        win = np.full((512, 512), -30.0, dtype="float32")
        win[100:120, 100:120] = 5000.0  # land topo block
        win[0, 0], win[0, 1], win[1, 0] = 12.0, 0.0, 0.0  # land lowland, land-side 0, water-side 0
        win[5, 5] = 4.0  # water-side drying height (trusted-source foreshore)
        wt, mkt = f"{d}/win.tif", f"{d}/winmask.tif"
        wtr = from_origin(b.left, b.top, res, res)
        with rasterio.open(wt, "w", driver="GTiff", height=512, width=512, count=1, dtype="float32",
                           crs="EPSG:3857", transform=wtr) as dst:
            dst.write(win, 1)
        mk = np.zeros((512, 512), dtype="uint8")
        mk[100:120, 100:120] = 1
        mk[0, 0], mk[0, 1] = 1, 1  # land at the topo block + lowland + land-0; water elsewhere
        with rasterio.open(mkt, "w", driver="GTiff", height=512, width=512, count=1, dtype="uint8",
                           crs="EPSG:3857", transform=wtr) as dst:
            dst.write(mk, 1)
        _encode_tile(0, 0, wt, 0, f"{cz}-0-0.webp", cz, mkt)
        with open(f"{cz}-0-0.webp", "rb") as f:
            dec = encode.decode(imagecodecs.webp_decode(f.read()).astype("float32"))
        assert dec[110, 110] == LAND and dec[0, 0] == LAND and dec[0, 1] == LAND, \
            ("land topo / lowland / land-0 must all decode the exact land code",
             dec[110, 110], dec[0, 0], dec[0, 1])
        assert dec[5, 5] == DRYING, ("water-side (0, cap] must decode the drying code", dec[5, 5])
        assert dec[1, 0] == UNKNOWN, ("water-side 0 must stay exactly 0", dec[1, 0])
        assert abs(dec[200, 200] - (-30.0)) < 0.5, ("measured depth must pass through", dec[200, 200])
        print("terrain.py self-check ok")
    finally:
        os.chdir(cwd)
        for kk, vv in saved_env.items():
            if vv is not None:
                os.environ[kk] = vv
        shutil.rmtree(d, ignore_errors=True)


def main(argv):
    if argv[:1] == ["render"] and len(argv) == 2:
        render(argv[1])
    elif argv[:1] == ["--check"]:
        _check()
    else:
        sys.exit("usage: terrain.py <render <stem> | --check>")


if __name__ == "__main__":
    main(sys.argv[1:])
