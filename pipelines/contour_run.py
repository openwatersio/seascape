"""Contours as a fork off each aggregation tile's merged DEM.

Reuses the best-available merged DEM the aggregation stage builds (already
slope-smoothed by smooth.py), so contours come from one continuous surface. GDAL
runs as a subprocess; geopandas/shapely do the Chaikin smoothing.

Each aggregation tile contours its full-res merged DEM, so every source shows at
all zooms (coarse GEBCO is overzoomed by the renderer above its native z9; CUDEM
adds detail where present). Within a tile the merge is feathered, so the
CUDEM->GEBCO footprint transition is continuous; the residual coarse/fine
difference at the tile edge is fundamental (you can't draw GEBCO at CUDEM's
resolution) and is softened by the smoothing.

Per tile: gdal_contour (3857) -> Chaikin smooth + enrich (depth_abs_m)
-> clip to the unbuffered tile bbox -> reproject to EPSG:4326.

(A disjoint base/regional zoom-band split was tried to make lines join at z9, but
it hid GEBCO above z9 — not worth losing GEBCO at high zoom. Smoothing is the
real seam mitigation.)
"""

import json
import os
import subprocess
import sys
from glob import glob

import mercantile

import config
import keys
import utils
from aggregation_reproject import get_resolution

CHAIKIN_ITERATIONS = 5
# Skip Chaikin in the navigable band: corner-cutting bows a contour toward the deep
# side at shallow-convex bends, which shrinks a shoal (unsafe). Shallow lines keep
# their raw gdal_contour geometry (sub-pixel simplify only); deeper contours smooth
# for looks. Mirrors smooth.DEPTH_FULL / the ECDIS safety-contour depth.
NAV_SMOOTH_MAX_M = int(os.environ.get("CONTOUR_NAV_SMOOTH_MAX", "30"))


def _chaikin_iters(depth_abs_m):
    return 0 if depth_abs_m <= NAV_SMOOTH_MAX_M else CHAIKIN_ITERATIONS


# Drop spurious tiny closed contours (abyssal stipple — micro-loops around bumps
# near a deep contour value) at deep levels only. Shallow rings are navigable
# shoals and are kept untouched (IHO safe-side: never drop a charted shoal). Areas
# in m² (geometry is EPSG:3857 here).
DEEP_CUTOFF_M = int(os.environ.get("CONTOUR_RING_DEEP_CUTOFF", "1000"))
MIN_RING_AREA_M2 = float(os.environ.get("CONTOUR_RING_MIN_AREA", "16e6"))  # ~16 km²

# The style hand-mirrors the DEFAULT metre ladder (style/index.ts DEPARE_LADDER_M/FT), so an
# env-overridden CONTOUR_LEVELS silently diverges the tiles from the style. Warn loudly, once
# per process; upgrade path is generating the style constants from config instead of mirroring.
if config.CONTOUR_LEVELS != config.CONTOUR_LEVELS_DEFAULT:
    print("WARNING: CONTOUR_LEVELS overridden — isobaths/depth-areas will diverge from the "
          "style's hand-mirrored DEPARE_LADDER_M/FT (style/index.ts); update it to match.",
          file=sys.stderr)


def _run(cmd, what):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if p.returncode != 0:
        raise Exception(f"{what} failed (exit {p.returncode}):\n{p.stdout}\n{p.stderr}")


def feature_count(fgb):
    out = subprocess.run(["ogrinfo", "-so", "-al", fgb], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "Feature Count" in line:
            return int(line.split(":")[1].strip())
    return 0


# ── Chaikin smoothing (ported from scripts/smooth-contours, via shapely) ─────

def _chaikin(coords, iterations):
    import numpy as np
    pts = np.asarray(coords, float)
    for _ in range(iterations):
        if len(pts) < 3:
            break
        q = 0.75 * pts[:-1] + 0.25 * pts[1:]
        r = 0.25 * pts[:-1] + 0.75 * pts[1:]
        out = np.empty((2 * len(q), 2))
        out[0::2], out[1::2] = q, r
        out[0], out[-1] = pts[0], pts[-1]  # preserve endpoints (lines must not drift)
        pts = out
    return pts


def _smooth_geom(geom, tol, iterations):
    from shapely.geometry import LineString, MultiLineString
    if geom.geom_type == "LineString":
        g = geom.simplify(tol) if tol > 0 else geom
        coords = list(g.coords)
        return LineString(_chaikin(coords, iterations)) if len(coords) >= 3 else geom
    if geom.geom_type == "MultiLineString":
        return MultiLineString([_smooth_geom(p, tol, iterations) for p in geom.geoms])
    return geom


def _drop_small_rings(geom, min_area):
    """Drop closed-ring parts enclosing < min_area m²; keep open lines. None if empty."""
    from shapely.geometry import MultiLineString, Polygon
    parts = [geom] if geom.geom_type == "LineString" else list(geom.geoms)
    kept = [p for p in parts
            if not (len(p.coords) > 3 and p.coords[0] == p.coords[-1]
                    and Polygon(p.coords).area < min_area)]
    if not kept:
        return None
    return kept[0] if len(kept) == 1 else MultiLineString(kept)


def smooth_and_enrich(raw_fgb, out_fgb, tol):
    """Tag each reassembled contour with every system whose ladder includes its depth (a level in
    both the metre and fathom ladders yields one feature per system), Chaikin-smooth (in 3857,
    nav-band skip so smoothing never understates a shoal), drop deep micro-loop stipple, and add
    depth_abs_m / depth_ft / depth_fm. Feet features sit on whole-fathom depths so their
    depth_ft/depth_fm round clean; the viewer labels metre features in metres, feet in feet/fathoms."""
    import geopandas as gpd
    import pandas as pd
    g = gpd.read_file(raw_fgb)
    m = g[g["depth_m"].isin(config.CONTOUR_LEVELS)].assign(sys="m")
    ft = g[g["depth_m"].isin(config.CONTOUR_LEVELS_FT)].assign(sys="ft")
    gdf = gpd.GeoDataFrame(pd.concat([m, ft], ignore_index=True), crs=g.crs)
    gdf["depth_abs_m"] = (-gdf["depth_m"]).round().astype(int)
    gdf["depth_ft"] = (-gdf["depth_m"] / 0.3048).round().astype(int)
    gdf["depth_fm"] = (-gdf["depth_m"] / 1.8288).round().astype(int)
    gdf["geometry"] = [_smooth_geom(g, tol, _chaikin_iters(d))
                       for g, d in zip(gdf.geometry, gdf["depth_abs_m"])]
    deep = gdf["depth_abs_m"] >= DEEP_CUTOFF_M
    if deep.any():
        gdf.loc[deep, "geometry"] = [_drop_small_rings(g, MIN_RING_AREA_M2)
                                     for g in gdf.loc[deep, "geometry"]]
        gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    gdf.to_file(out_fgb, driver="FlatGeobuf")


# gdal_contour holds ALL of its input raster's line geometry in RAM, so one dense 32768 px coastal
# tile peaks ~12-14 GB in a single subprocess — the aggregate's whole memory peak, why a few heavy
# tiles exhaust RAM while cores idle. merge and smooth already window for this; contour was the one
# heavy stage still run whole. _contour_blocks windows it (see its docstring).
CONTOUR_BLOCK = int(os.environ.get("CONTOUR_BLOCK", "8192"))  # core block side (px)
CONTOUR_HALO = 2                                              # read overlap (px); contour marching is cell-local, so 1 neighbour row suffices to draw the seam segment
CONTOUR_SNAP = 1e-3                                           # reassembly snap grid (m, 3857); << pixel, >> float noise


def _reassemble(fragments, out):
    """Rejoin the block fragments a seam split back into whole contour lines, writing `out`. Per
    depth level: snap fragment coordinates to a fine grid (a seam's two blocks compute its crossing
    to the last ULP, and the halo can duplicate a corner bridge, so "the same" endpoint differs by
    ~1e-9), union to flatten and dedupe, then line_merge to stitch each seam-split line whole.
    Genuinely separate contours at a level don't touch, so they stay separate. Runs BEFORE
    smooth_and_enrich so Chaikin smooths each whole line as one piece — smoothing the raw fragments
    would kink the geometry at every block seam (the regression this exists to prevent)."""
    import geopandas as gpd
    import pandas as pd
    from shapely import get_parts, line_merge, set_precision, union_all
    gdf = pd.concat([gpd.read_file(f) for f in fragments], ignore_index=True)
    rows = []
    for depth, grp in gdf.groupby("depth_m"):
        merged = line_merge(union_all(set_precision(grp.geometry.values, CONTOUR_SNAP)))
        rows.extend({"depth_m": depth, "geometry": g} for g in get_parts(merged))
    gpd.GeoDataFrame(rows, crs=gdf.crs).to_file(out, driver="FlatGeobuf")


def _contour_blocks(dem, levels, tmp, tag):
    """Contour `dem` into ONE FGB, bounding gdal_contour's RAM by running it per core block. Returns
    the FGB path, or None if the DEM yields no contours. A DEM within one block contours directly
    (byte-identical to the old single call). A larger tile contours per CONTOUR_BLOCK core block
    (halo-padded reads so a seam segment still draws, each clipped back to its core so halo
    duplicates drop), then _reassemble stitches the seam-split fragments back into whole lines before
    the caller smooths them."""
    import rasterio
    from rasterio.windows import Window
    from rasterio.windows import bounds as window_bounds
    with rasterio.open(dem) as ds:
        width, height, transform = ds.width, ds.height, ds.transform
    if width <= CONTOUR_BLOCK and height <= CONTOUR_BLOCK:
        raw = f"{tmp}/contour-raw-{tag}.fgb"
        _run(f"gdal_contour -q -fl {levels} -a depth_m -f FlatGeobuf {dem} {raw}", f"gdal_contour {tag}")
        return raw if feature_count(raw) > 0 else None
    fragments = []
    n = 0
    for oy in range(0, height, CONTOUR_BLOCK):
        for ox in range(0, width, CONTOUR_BLOCK):
            cw, ch = min(CONTOUR_BLOCK, width - ox), min(CONTOUR_BLOCK, height - oy)
            rx0, ry0 = max(0, ox - CONTOUR_HALO), max(0, oy - CONTOUR_HALO)
            rx1, ry1 = min(width, ox + cw + CONTOUR_HALO), min(height, oy + ch + CONTOUR_HALO)
            vrt = f"{tmp}/cblk-{tag}-{n}.vrt"
            rawb = f"{tmp}/cblk-{tag}-{n}-raw.fgb"
            clipped = f"{tmp}/cblk-{tag}-{n}.fgb"
            # -of VRT -srcwin is a metadata-only window (no data copy), keeping the DEM geotransform
            _run(f"gdal_translate -q -of VRT -srcwin {rx0} {ry0} {rx1 - rx0} {ry1 - ry0} {dem} {vrt}",
                 f"gdal_translate contour block {tag}")
            _run(f"gdal_contour -q -fl {levels} -a depth_m -f FlatGeobuf {vrt} {rawb}",
                 f"gdal_contour {tag}")
            left, bottom, right, top = window_bounds(Window(ox, oy, cw, ch), transform)
            _run(f"ogr2ogr -f FlatGeobuf -overwrite -nlt PROMOTE_TO_MULTI "
                 f"-clipsrc {left} {bottom} {right} {top} {clipped} {rawb}",
                 f"ogr2ogr contour block clip {tag}")
            if feature_count(clipped) > 0:
                fragments.append(clipped)
            n += 1
    if not fragments:
        return None
    out = f"{tmp}/contour-raw-{tag}.fgb"
    _reassemble(fragments, out)
    return out


def generate(filepath, out):
    """Contour one aggregation tile's merged DEM into ``out`` (the caller's content-addressed FGB
    name). Writes ``out`` atomically only when there ARE features — an all-land / all-deep tile
    returns having written nothing, and the caller records the empty marker. The caller superseded
    this stem's old-key siblings before calling, so a crash here leaves the fork reading stale."""
    agg_id, filename = filepath.split("/")[-2:]
    z, x, y, child_z = (int(a) for a in filename.replace("-aggregation.csv", "").split("-"))
    tile = mercantile.Tile(x=x, y=y, z=z)
    tmp = f"store/aggregation/{agg_id}/{z}-{x}-{y}-{child_z}-tmp"
    dem = f"{tmp}/{len(glob(f'{tmp}/*.tiff')) - 1}-3857.tiff"
    if not os.path.exists(dem):
        print(f"contour: no merged DEM for {filename}")
        return

    # Contour the metre + fathom ladders in ONE pass over the union of levels — halving the
    # per-block contour/clip/reassemble work vs a pass each. smooth_and_enrich splits each line
    # back to its system(s) by depth membership.
    levels = " ".join(str(l) for l in sorted(set(config.CONTOUR_LEVELS) | set(config.CONTOUR_LEVELS_FT)))
    raw = _contour_blocks(dem, levels, tmp, "all")
    if not raw:
        print(f"contour: no ocean features for {filename}")
        return

    smoothed = f"{tmp}/contour-smooth.fgb"
    smooth_and_enrich(raw, smoothed, tol=get_resolution(child_z))

    b = mercantile.xy_bounds(tile)  # unbuffered, tile-aligned (EPSG:3857)
    clipped = f"{tmp}/contour-clip.fgb"
    _run(f"ogr2ogr -f FlatGeobuf -overwrite -nlt PROMOTE_TO_MULTI "
         f"-clipsrc {b.left} {b.bottom} {b.right} {b.top} {clipped} {smoothed}", "ogr2ogr clip")
    if feature_count(clipped) == 0:
        print(f"contour: no features in tile bbox for {filename}")
        return

    # Reproject into the tmp folder, then atomically publish into the content name — the content
    # file only ever appears complete (a crash mid-reproject leaves the temp, reads stale).
    final = f"{tmp}/contour-final.fgb"
    _run(f"ogr2ogr -f FlatGeobuf -overwrite -nlt PROMOTE_TO_MULTI -t_srs EPSG:4326 {final} {clipped}",
         "ogr2ogr reproject")
    keys.publish(final, out)
    print(f"contour: {filename} -> {feature_count(out)} features")


# ── bundle ───────────────────────────────────────────────────────────────────

# Scale-dependent contour interval: coarse isobaths zoomed out, finer zoomed in
# (charts thin the deep, not the shelf — abyssal contours stipple into noise at
# small scale). (zoom_ceiling, depths_m shown below it); at/above the last ceiling
# every level shows. Each list must be a subset of config.CONTOUR_LEVELS.
CONTOUR_TIERS = [
    (5, [-200, -1000, -2000, -4000]),
    (7, [-200, -500, -1000, -2000, -3000, -4000]),
    (9, [-50, -100, -200, -300, -500, -1000, -2000, -3000, -4000, -5000, -6000, -8000, -10000]),
    (11, [-10, -20, -30, -50, -100, -200, -300, -500, -1000, -2000, -3000, -4000, -5000, -6000, -8000, -10000]),
]


def _per_zoom_filter():
    """tippecanoe's -j filter (exclusive $zoom bands). Metre isobaths (sys=m) keep the hand-picked
    CONTOUR_TIERS per zoom. Feet/fathom isobaths (sys=ft) mirror that thinning by depth — at each
    zoom they show only curves at least as deep as the shallowest metre curve shown, so deep
    fathom curves drop out at low zoom like the metre deep ones. Native+ zoom shows every curve."""
    bands, lo = [], 0
    for hi, depths in CONTOUR_TIERS:
        min_abs = min(-d for d in depths)  # shallowest metre curve shown in this band
        # Both bands test depth_m (gdal_contour's Real attribute): -j comparisons are
        # type-strict, and the enriched int columns (depth_abs_m et al.) reach the filter
        # as strings via the FlatGeobuf Integer64 path — a numeric test on them matches
        # nothing, which silently dropped every ft curve below native zoom.
        bands.append(["all", [">=", "$zoom", lo], ["<", "$zoom", hi], ["==", "sys", "m"], ["in", "depth_m", *depths]])
        bands.append(["all", [">=", "$zoom", lo], ["<", "$zoom", hi], ["==", "sys", "ft"], ["<=", "depth_m", -min_abs]])
        lo = hi
    bands.append(["all", [">=", "$zoom", lo]])  # native+ zoom: every curve, both systems
    return json.dumps({"*": ["any", *bands]})


PER_ZOOM_FILTER = _per_zoom_filter()


def _tippecanoe(fgbs, minz, maxz, out):
    subprocess.run(
        ["tippecanoe", "-o", out, "-f", "-l", "contours",
         "-n", "Bathymetric contours", "-A", utils.ATTRIBUTION,
         "-Z", str(minz), "-z", str(maxz), "-P", "-q", "--drop-densest-as-needed",
         # Aggressive low-zoom vertex thinning (tolerance scales with zoom distance from
         # maxz, so maxz detail is untouched). Env-tunable to dial on a re-bundle.
         "--simplification", os.environ.get("CONTOUR_SIMPLIFICATION", "8"),
         "-y", "depth_m", "-y", "depth_abs_m", "-y", "sys", "-y", "depth_ft", "-y", "depth_fm",
         # FlatGeobuf Integer64 attributes otherwise land in the MVT as strings.
         "-T", "depth_abs_m:int", "-T", "depth_ft:int", "-T", "depth_fm:int",
         "-j", PER_ZOOM_FILTER, *fgbs],
        check=True)


def _global_maxz(fgbs):
    return max(int(keys.stem_of(f).split("-")[3]) for f in fgbs)


def _stems_maxz(stems):
    """Max child_z across covering stems ({z}-{x}-{y}-{child_z})."""
    return max(int(s.rsplit("-", 1)[1]) for s in stems)


def bundle_maxz(own_max):
    """The tileset maxzoom EVERY vector layer bundles to (contours, soundings,
    depare). They tile-join into one vector.pmtiles, whose maxzoom is the max
    across layers — a layer bundled only to its own regional max silently
    vanishes from deeper tiles (the drying flats, folded into depare, once stopped
    at z11 while contours ran to z14 and rendered as bare land above z11). Use the current
    covering's max child_z (so every layer tiles to the same depth and tile-joins cleanly),
    else the caller's own files' max."""
    stems = _current_stems()
    if stems:
        return max(own_max, _stems_maxz(stems))
    return own_max


# ── source coverage (provenance) layer ───────────────────────────────────────
# Tile each source's union footprint (store/polygon/<id>.gpkg, from the source stage)
# into its own store/bundle/coverage.pmtiles, so the viewer can show which source
# covers a clicked point. Its own tileset, NOT a layer in vector.pmtiles: a joined
# archive shares one zoom range, and sea-sized fill polygons tiled to the contours'
# maxzoom mint millions of deep-ocean tiles, while tiled lower the layer vanishes
# above its maxzoom (MapLibre overzooms a missing *tile*, never a missing *layer*).
# Standalone, the renderer overzooms it past COVERAGE_MAX_ZOOM on its own. GEBCO
# (the global base) declares no max_zoom, so it's skipped; only regional footprints
# get a polygon.

BASE_SOURCE = "gebco"  # the global fallback; its footprint is the whole planet, never an overlay

# z8's 4096-cell MVT grid resolves ~37 m and the footprints are already simplified
# to 0.001° (~100 m) — fidelity is simplify-bound, not zoom-bound — while the z8
# tile address space caps the archive at 65k tiles.
COVERAGE_MAX_ZOOM = int(os.environ.get("COVERAGE_MAX_ZOOM", "8"))


def _source_maxzooms():
    """{source: native maxzoom} from the newest covering's aggregation CSVs — the pipeline's
    authoritative per-source zoom (resolution-derived, capped/floored). Empty when no covering
    is local (a CI contour job). Note: metadata.json's max_zoom is only an optional *cap*, so
    most regional sources don't carry it — read the real zoom from the covering instead."""
    ids = utils.get_aggregation_ids()
    if not ids:
        return {}
    mz = {}
    for csv in glob(f"store/aggregation/{ids[-1]}/*-aggregation.csv"):
        with open(csv) as f:
            for line in f.readlines()[1:]:
                s, _fn, z = line.strip().split(",")
                mz[s] = max(mz.get(s, 0), int(z))
    return mz


def _coverage_geojson():
    """Combine every regional source's simplified footprint into one GeoJSON FC with
    {source_id, source_name, source_maxzoom}; write it and return the path. None if no
    polygons are present locally (coverage_bundle turns that into a hard failure).

    BBOX (W,S,E,N lon/lat, the same regional-build env the covering uses) pushes a -spat
    filter onto each footprint read, so a preview builds only the region's coverage instead of
    tippecanoeing every footprint whole (the large intertidal-survey unions — uk_surfzone,
    infomar — dominate that cost). Footprints are EPSG:4326, so BBOX maps straight to -spat.
    Unset (a planet/CI build) → the whole world."""
    valid = set(config.sources())
    zmax = _source_maxzooms()
    bbox = os.environ.get("BBOX", "").strip()
    spat = ["-spat", *(c.strip() for c in bbox.split(","))] if bbox else []
    feats = []
    for gpkg in sorted(glob("store/polygon/*.gpkg")):
        sid = gpkg.split("/")[-1].replace(".gpkg", "")
        if sid not in valid or sid == BASE_SOURCE:  # orphan polygon, or the global base — skip
            continue
        meta = config.load_metadata(sid)
        out = subprocess.run(
            ["ogr2ogr", "-f", "GeoJSON", "/vsistdout/", gpkg, *spat,
             "-simplify", "0.001", "-lco", "COORDINATE_PRECISION=5"],
            capture_output=True, text=True, check=True).stdout
        for f in json.loads(out).get("features", []):
            # source_maxzoom drives the viewer's deepest-wins pick on overlap (priority isn't
            # folded in — a higher-priority shallower source would mis-attribute; rare today).
            f["properties"] = {"source_id": sid, "source_name": meta.get("name", sid),
                               "source_maxzoom": zmax.get(sid) or meta.get("max_zoom") or 0}
            feats.append(f)
    if not feats:
        return None
    path = "store/contour/coverage.geojson"
    utils.create_folder("store/contour")
    with open(path, "w") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f)
    return path


def coverage_bundle():
    """tippecanoe the source footprints into store/bundle/coverage.pmtiles (layer
    `coverage`, z0..COVERAGE_MAX_ZOOM, footprints kept whole). Fails when no
    footprints are local: a planet build without them ships a dead provenance
    layer — the silent drop is exactly how the layer went missing from every
    published build."""
    src = _coverage_geojson()
    if not src:
        raise SystemExit("coverage: no footprints in store/polygon/ — pull "
                         "bathymetry/polygon/*.gpkg or prepare a source first")
    utils.create_folder("store/bundle")
    out = "store/bundle/coverage.pmtiles"
    subprocess.run(
        ["tippecanoe", "-o", out, "-f", "-l", "coverage", "-n", "Source coverage",
         "-A", utils.ATTRIBUTION, "-Z", "0", "-z", str(COVERAGE_MAX_ZOOM), "-P", "-q",
         "--no-tile-size-limit", src], check=True)
    print(f"coverage bundle: {out} (z0-{COVERAGE_MAX_ZOOM})")


def _finalize_contours(archives):
    """tile-join the contour lines pmtiles + the soundings/depare pmtiles (bundled first)
    into store/bundle/vector.pmtiles. ONE join: tile-join rewrites every tile of the whole
    archive, so folding each sparse layer in afterwards re-paid the planet-wide join
    per layer (~90 min each). -pk keeps every feature of every layer; a layer
    whose pmtiles isn't present locally is simply not joined. Coverage is its own
    tileset (coverage_bundle), not a layer here. Drying is no longer its own layer —
    it folds into `depare` (a DEPARE band with negative drval1)."""
    layers = [p for p in ["store/bundle/soundings.pmtiles",
                          "store/bundle/depare.pmtiles"]
              if os.path.isfile(p)]
    subprocess.run(["tile-join", "-o", "store/bundle/vector.pmtiles", "-f", "-pk",
                    *archives, *layers], check=True)


def _current_stems():
    """{z}-{x}-{y}-{child_z} of every tile in the newest covering — the only contour
    FGBs that belong in the bundle. None when no covering is present locally, so the
    caller falls back to every FGB it has."""
    ids = utils.get_aggregation_ids()
    if not ids:
        return None
    csvs = glob(f"store/aggregation/{ids[-1]}/*-aggregation.csv")
    return {c.split("/")[-1].replace("-aggregation.csv", "") for c in csvs} or None


def _live_fgbs(fgbs, stems):
    """Drop FGBs orphaned by a covering re-tiling. A source's footprint/maxzoom shift re-tiles its
    area (different z/x/y/child_z), but the store keeps no --delete, so the superseded FGB lingers;
    bundling it alongside the new tiling draws two overlapping contour sets. Keep only
    current-covering stems (parsed off the content-addressed name); keep all when stems is None.
    Non-content-addressed debris (a torn logical write) is dropped either way."""
    live = [f for f in fgbs if keys.is_content_name(f)]
    if stems is None:
        return live
    return [f for f in live if keys.stem_of(f) in stems]


def verify_vector_complete(layer, paths):
    """Every stem in the current covering must carry this fork's artifact OR its .empty marker, or
    the layer has a silent hole — the vector twin of bundle.verify_complete (a tile whose fork
    never completed would simply vanish from vector.pmtiles). build.yml enforces the same invariant
    via the store manifest's hard-fail, which happens to run before the vector bundlers — but that
    is step ORDER; this gate makes each bundler SELF-enforcing, so a reordered workflow or a local
    run fails loudly instead of publishing a hole. Runs before each bundler's fresh-skip, so a
    fresh key can't paper over a gap. No covering locally (ad-hoc bundling of whatever exists)
    skips the gate; a fork hard-skipped at aggregate time (SKIP_CONTOURS et al.) left no markers,
    so bundling it fails here — correct: you asked to bundle a fork you never built."""
    stems = _current_stems()
    if stems is None:
        return
    have = {keys.stem_of(p) for p in paths}
    have |= {keys.stem_of(p) for p in glob(f"store/{layer}/*.empty") if keys.is_content_name(p)}
    missing = sorted(stems - have)
    if missing:
        raise SystemExit(
            f"{layer} incomplete: {len(missing)} of {len(stems)} covering tiles have neither an "
            f"artifact nor an empty marker (a failed/interrupted aggregate run) — e.g. "
            f"{', '.join(missing[:15])}{' …' if len(missing) > 15 else ''}")


# vector.pmtiles depends on every per-tile contour/sounding/depare artifact's key + the three
# layers' modules (the tippecanoe filter + flags live in them) + the env-tunable simplifications.
# A per-tile fork whose key changed (a contour-levels edit) moves this key; an unchanged set skips
# the whole tippecanoe + planet-wide tile-join.
VECTOR_MODULES = ["contour_run", "soundings_run", "depare_run", "utils"]


def _vector_key(maxz):
    stems = _current_stems()
    inputs = []
    for folder, ext in (("contour", "fgb"), ("soundings", "geojson"), ("depare", "fgb")):
        for path in sorted(glob(f"store/{folder}/*.{ext}")):
            if not keys.is_content_name(path):  # legacy/torn debris — never a bundle input
                continue
            stem = keys.stem_of(path)
            if stems is not None and stem not in stems:  # orphan from a re-tiled covering
                continue
            # The key rides in the filename now (empty forks carry only a .empty marker, which
            # this *.fgb / *.geojson glob skips — an empty->non-empty transition adds a content
            # file and moves the key, the meaningful change). Layer folder included: contour and
            # depare are both .fgb, so the folder keeps each input's identity unambiguous.
            inputs.append(os.path.relpath(path, "store"))
    cfg = {"maxz": maxz,
           "contour_simplification": os.environ.get("CONTOUR_SIMPLIFICATION", "8"),
           "depare_simplification": os.environ.get("DEPARE_SIMPLIFICATION", "8")}
    return keys.stage_key(inputs, VECTOR_MODULES, cfg)


def bundle():
    """tippecanoe every contour FGB into contour lines, then tile-join in the already-bundled
    soundings/depare layers → store/bundle/vector.pmtiles (one global tippecanoe + one join;
    maxz is the covering's shared max child_z so every layer tiles to the same depth). Soundings
    and depare must bundle before this — the single tile-join folds their pmtiles in. Skips when
    every per-tile vector artifact's key is unchanged and vector.pmtiles is already on disk (the
    local iterative loop; the box's store/bundle is never hydrated, so a box build always runs)."""
    fgbs = _live_fgbs(sorted(glob("store/contour/*.fgb")), _current_stems())
    verify_vector_complete("contour", fgbs)  # self-enforcing, before the fresh-skip (see the gate)
    vec = "store/bundle/vector.pmtiles"
    if not fgbs:
        # Empty input is a real state, not a no-op: a previously-built vector.pmtiles (and the
        # sidecar vouching for it) must not survive to be pushed as this build's current vector
        # layer. The invalidate discipline, extended to "the current state is nothing".
        for stale in (vec, keys.sidecar(vec)):
            if os.path.isfile(stale):
                os.remove(stale)
        print("contour bundle: no contour FGBs")
        return
    maxz = bundle_maxz(_global_maxz(fgbs))
    vkey = _vector_key(maxz)
    if keys.is_fresh(vec, vkey):
        print("contour bundle: vector inputs unchanged — skip")
        return
    utils.create_folder("store/bundle")
    # Invalidate before writing (the crash rule every keyed writer follows): under FORCE the
    # key is unchanged, so a crash mid-tippecanoe/tile-join would otherwise leave the old
    # sidecar reading a torn vector.pmtiles as fresh forever.
    for stale in (vec, keys.sidecar(vec)):
        if os.path.isfile(stale):
            os.remove(stale)
    lines = "store/contour/contours-lines.pmtiles"
    _tippecanoe(fgbs, 0, maxz, lines)
    _finalize_contours([lines])
    os.remove(lines)
    keys.write_key(vec, vkey)
    print(f"contour bundle: {vec} (z0-{maxz}, {len(fgbs)} FGBs)")


def _check():
    """Deep micro-loops dropped; big deep ring and open lines kept."""
    import numpy as np
    from shapely.geometry import LineString
    def ring(r, n=48):
        t = np.linspace(0, 2 * np.pi, n, endpoint=False)
        pts = [(r * np.cos(a), r * np.sin(a)) for a in t]
        return LineString(pts + [pts[0]])  # explicitly closed
    assert _drop_small_rings(ring(100), MIN_RING_AREA_M2) is None        # ~0.03 km² → drop
    assert _drop_small_rings(ring(3000), MIN_RING_AREA_M2) is not None   # ~28 km²  → keep
    assert _drop_small_rings(LineString([(0, 0), (5e3, 0), (1e4, 5e3)]), MIN_RING_AREA_M2) is not None  # open line kept
    # orphan-FGB filter (content-addressed names): keep only current-covering stems; passthrough
    # when stems is None; a non-content name (legacy/torn) is dropped either way.
    fgbs = ["store/contour/4-5-6-8-aaaaaaaaaaaa.fgb", "store/contour/11-300-400-13-bbbbbbbbbbbb.fgb"]
    assert _live_fgbs(fgbs, {"11-300-400-13"}) == ["store/contour/11-300-400-13-bbbbbbbbbbbb.fgb"]
    assert _live_fgbs(fgbs, None) == fgbs
    assert _live_fgbs(["store/contour/9-1-1-9.fgb"], None) == [], "a legacy logical name is dropped"
    # the shared bundle maxzoom reads child_z off covering stems
    assert _stems_maxz({"4-5-6-8", "11-300-400-13"}) == 13
    # navigable-band contours skip Chaikin (never bow a shoal deeper); deeper ones smooth
    assert _chaikin_iters(10) == 0 and _chaikin_iters(NAV_SMOOTH_MAX_M) == 0
    assert _chaikin_iters(NAV_SMOOTH_MAX_M + 1) == CHAIKIN_ITERATIONS
    # metre and feet/fathom isobaths each get their own per-zoom bands in the tippecanoe filter
    assert '["==", "sys", "m"]' in PER_ZOOM_FILTER and '["==", "sys", "ft"]' in PER_ZOOM_FILTER
    # the filter must only compare depth_m — the int columns are string-typed at -j time
    assert "depth_abs_m" not in PER_ZOOM_FILTER
    # every tier level must exist in the generated contour set (else it filters to nothing)
    assert all(d in config.CONTOUR_LEVELS for _, depths in CONTOUR_TIERS for d in depths)

    # windowed gdal_contour must reconstruct the whole-raster line set with no seam gaps. A line
    # only SPLITS at a block core (total length preserved); a dropped seam segment would shorten
    # it. Contour a diagonal ramp whole and in small blocks (diagonals cross both seam axes), and
    # compare total line length.
    import tempfile
    import geopandas as gpd
    import rasterio
    from rasterio.transform import from_origin
    global CONTOUR_BLOCK
    with tempfile.TemporaryDirectory() as d:
        n = 256
        yy, xx = np.mgrid[0:n, 0:n]
        ramp = ((xx + yy) / (2 * (n - 1)) * 200 - 100).astype("float32")  # anti-diagonal contours
        dem = f"{d}/dem.tif"
        with rasterio.open(dem, "w", driver="GTiff", height=n, width=n, count=1, dtype="float32",
                           crs="EPSG:3857", transform=from_origin(0, n, 1, 1)) as ds:
            ds.write(ramp, 1)
        lv = "-50 0 50"
        whole = f"{d}/whole.fgb"
        _run(f"gdal_contour -q -fl {lv} -a depth_m -f FlatGeobuf {dem} {whole}", "check whole")
        saved, CONTOUR_BLOCK = CONTOUR_BLOCK, 64   # 4x4 blocks over the 256px DEM
        try:
            win = _contour_blocks(dem, lv, d, "chk")   # windowed + reassembled
        finally:
            CONTOUR_BLOCK = saved
        wg = gpd.read_file(win)
        len_whole = float(gpd.read_file(whole).geometry.length.sum())
        assert abs(float(wg.geometry.length.sum()) - len_whole) / len_whole < 0.02, \
            "windowed contour lost length (seam gap?)"
        # reconnection: fragments MUST rejoin into whole lines before smoothing, else a seam-crossing
        # line Chaikin-smooths as pieces (a kink per block seam). The whole-raster contour is one
        # line per component; the reassembled windowed set must carry the SAME count, not extra
        # fragments — length alone can't see this, since splitting preserves length.
        assert len(wg) == feature_count(whole), \
            f"fragments not reconnected: whole={feature_count(whole)} lines, windowed={len(wg)}"
    print("contour_run ring-drop + orphan-filter + windowed-contour self-check ok")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["bundle"]:
        bundle()
    elif a[:1] == ["coverage"]:
        coverage_bundle()
    elif a[:1] == ["check"]:
        _check()
    else:
        sys.exit("usage: contour_run.py bundle | coverage | check")
