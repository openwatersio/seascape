"""Contours as a fork off each aggregation tile's merged DEM.

Reads a window of the persisted mosaic (smoothed at read by smooth.py), so contours
come from one continuous surface. GDAL runs as a subprocess; geopandas/shapely do the
Chaikin smoothing.

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

import collections
import json
import os
import re
import shutil
import tempfile
import subprocess
import sys
from glob import glob

import mercantile

import aggregation_covering
import config
import landmask
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


def _lines(geom):
    """Every non-empty LineString inside a geometry, recursing into Multi/GeometryCollection and
    dropping the point/polygon slivers a clip or make_valid can leave (the line analog of
    depare_run._polys) — so the output layer stays uniformly line (FlatGeobuf rejects a mixed layer)."""
    t = geom.geom_type
    if t == "LineString":
        return [] if geom.is_empty else [geom]
    if t in ("MultiLineString", "GeometryCollection"):
        return [ln for g in geom.geoms for ln in _lines(g)]
    return []


def _clip_to_bbox(in_fgb, out_fgb, clip):
    """Clip the smoothed contours to the tile bbox in shapely (EPSG:3857), keeping only line pieces
    — ogr2ogr -clipsrc emits a GeometryCollection here that the FlatGeobuf write rejects (the GDAL
    GeometryCollection trap, same as depare's shapely clip). Returns the surviving feature count."""
    import geopandas as gpd
    from shapely import make_valid
    from shapely.geometry import MultiLineString
    gdf = gpd.read_file(in_fgb)
    clipped = [MultiLineString(parts) if parts else None  # PROMOTE_TO_MULTI: uniform line layer
               for parts in (_lines(make_valid(g).intersection(clip)) for g in gdf.geometry)]
    gdf["geometry"] = clipped
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    gdf.to_file(out_fgb, driver="FlatGeobuf")
    return len(gdf)


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


def smooth_and_enrich(sources, out_fgb, tol):
    """Concatenate the metre + feet contour sets (each `(fgb, sys)`), tag `sys`, Chaikin-smooth
    (in 3857, nav-band skip so smoothing never understates a shoal), drop deep micro-loop stipple,
    and add depth_abs_m / depth_ft / depth_fm. Feet features sit on whole-fathom depths so their
    depth_ft/depth_fm round clean; the viewer labels metre features in metres, feet features in
    feet or fathoms."""
    import geopandas as gpd
    import pandas as pd
    parts = []
    for fgb, sys in sources:
        g = gpd.read_file(fgb)
        g["sys"] = sys
        parts.append(g)
    gdf = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=parts[0].crs)
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


def tile(stem):
    """The per-stem Snakemake job: contour one stem from a BUFFERED mosaic window, smoothed at read
    with the one shared f(depth, zoom), output at store/contour/<stem>.fgb. A featureless tile
    writes a 0-byte sentinel so the engine sees a complete output; bundling filters empties by
    size."""
    import mosaic
    import smooth
    z, x, y, child_z = (int(a) for a in stem.split("-"))
    out = f"store/contour/{stem}.fgb"
    tmp = tempfile.mkdtemp(prefix=f"contour-{stem}-")  # local scratch; publish crosses to the store
    dem = mosaic.window_dem(stem, f"{tmp}/dem.tiff")
    if not os.environ.get("SKIP_SMOOTH"):
        smooth.smooth_tiff(dem)
    # Deep isobaths must track the depth-area band edges, which come from this same coarsened surface.
    if child_z >= smooth.DEEP_COARSEN_MIN_CHILD_Z:
        smooth.deep_coarsen(dem)
    # After smoothing: it smears sea negatives back across clamp-flattened islands, and
    # the warp-time clamp's centre-sampled mask misses narrow rims — either puts
    # isobaths on land.
    landmask.clamp_dem_to_land(dem)
    final = _contour_dem(dem, mercantile.Tile(x=x, y=y, z=z), child_z, tmp, stem)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    if final:
        utils.publish(final, out)  # scratch and store are separate filesystems
        print(f"contour tile {stem}: {feature_count(out)} features")
    else:
        open(out, "w").close()
        print(f"contour tile {stem}: empty")
    shutil.rmtree(tmp)


def _contour_dem(dem, tile_obj, child_z, tmp, label):
    """gdal_contour -> smooth/enrich -> clip -> reproject, off any DEM covering the tile's
    buffered extent. Returns the final FGB path inside ``tmp``, or None when featureless."""
    levels = " ".join(str(l) for l in config.CONTOUR_LEVELS)
    raw = f"{tmp}/contour-raw.fgb"
    _run(f"gdal_contour -q -fl {levels} -a depth_m -f FlatGeobuf {dem} {raw}", "gdal_contour m")
    sources = [(raw, "m")]

    # A second set at the fathom curves for feet/fathom charts (same DEM, tagged sys=ft).
    levels_ft = " ".join(str(l) for l in config.CONTOUR_LEVELS_FT)
    raw_ft = f"{tmp}/contour-raw-ft.fgb"
    _run(f"gdal_contour -q -fl {levels_ft} -a depth_m -f FlatGeobuf {dem} {raw_ft}", "gdal_contour ft")
    if feature_count(raw_ft) > 0:
        sources.append((raw_ft, "ft"))

    if sum(feature_count(f) for f, _ in sources) == 0:
        print(f"contour: no ocean features for {label}")
        return None

    smoothed = f"{tmp}/contour-smooth.fgb"
    smooth_and_enrich(sources, smoothed, tol=get_resolution(child_z))

    from shapely.geometry import box
    b = mercantile.xy_bounds(tile_obj)  # unbuffered, tile-aligned (EPSG:3857)
    clipped = f"{tmp}/contour-clip.fgb"
    if _clip_to_bbox(smoothed, clipped, box(b.left, b.bottom, b.right, b.top)) == 0:
        print(f"contour: no features in tile bbox for {label}")
        return None

    # Reproject inside tmp; the caller owns the atomic move into its lane's name.
    final = f"{tmp}/contour-final.fgb"
    _run(f"ogr2ogr -f FlatGeobuf -overwrite -nlt PROMOTE_TO_MULTI -t_srs EPSG:4326 {final} {clipped}",
         "ogr2ogr reproject")
    return final


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


def contour_minzoom(sys_tag, depth_m):
    """Per-curve tippecanoe.minzoom: the first CONTOUR_TIERS band that shows the curve (native+ if
    none). The leaf-safe replacement for the old -j $zoom PER_ZOOM_FILTER — a variable-depth leaf
    freezes at its own zoom, so zoom gating must ride each feature, not the $zoom expression. Metre
    isobaths match a tier's hand-picked levels; feet/fathom curves mirror by depth (shown once at
    least as deep as the shallowest metre curve the band shows)."""
    lo = 0
    for hi, depths in CONTOUR_TIERS:
        if sys_tag == "m":
            shown = round(depth_m) in depths
        else:  # ft: at least as deep as the shallowest metre curve shown in this band
            shown = depth_m <= -min(-d for d in depths)
        if shown:
            return lo
        lo = hi
    return lo  # native+ band ([lo, inf)); lo is the last tier ceiling


def _stems_maxz(stems):
    """Max child_z across covering stems ({z}-{x}-{y}-{child_z})."""
    return max(int(s.rsplit("-", 1)[1]) for s in stems)


def _vector_maxz(own_max):
    """The join's -z: the covering's max child_z (native depth), so a variable-depth leaf can
    reach it, else the caller's own inputs' max. The covering is always present (Snakemake refuses
    to run without it)."""
    import mosaic
    return max(own_max, _stems_maxz(mosaic.covering_stems()))


# The vector shard grid. Defaults to the macrotile grid itself — one covering stem per cell, the
# finest seam-safe grain, so the worst cell job is one stem's content (the raster overlays' coarser
# OVERLAY_SPLIT_Z grid would swallow a whole dense region into one serial cell run). Env-overridable
# for experiments; must stay <= macrotile_z so every stem maps into exactly one cell.
VECTOR_SPLIT_Z = int(os.environ.get("VECTOR_SPLIT_Z", "0")) or utils.macrotile_z


def _vector_cell_of(z, x, y):
    return f"{VECTOR_SPLIT_Z}-{x >> (z - VECTOR_SPLIT_Z)}-{y >> (z - VECTOR_SPLIT_Z)}"


def vector_covering_cells(stems):
    """{cell: [covering stems]} grouping the covering stems by their VECTOR_SPLIT_Z cell. Each
    covering stem roots at macrotile_z >= VECTOR_SPLIT_Z, so it maps to exactly ONE cell; every
    stem also feeds the shallow run. build.smk derives the vector_cell wildcard set (and each
    cell's member stems) from this."""
    if utils.macrotile_z < VECTOR_SPLIT_Z:
        raise SystemExit(f"VECTOR_SPLIT_Z ({VECTOR_SPLIT_Z}) exceeds macrotile_z "
                         f"({utils.macrotile_z}) — covering stems root below the shard grid")
    cells = collections.defaultdict(list)
    for s in stems:
        z, x, y, _cz = (int(a) for a in s.split("-"))
        cells[_vector_cell_of(z, x, y)].append(s)
    return {c: sorted(cells[c]) for c in cells}


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
    """{source: resolved native/capped maxzoom}, independent of a planet covering."""
    return aggregation_covering.source_maxzooms()


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


def require_stable_complete(layer, stems, files):
    """The no-holes gate: every covering stem must have its per-stem file on disk — a 0-byte file
    is a legitimately empty tile, a MISSING one is an incomplete build. The only bundle-time gate;
    Snakemake owns freshness."""
    missing = [s for s, f in zip(stems, files) if not os.path.exists(f)]
    if missing:
        raise SystemExit(
            f"{layer} incomplete: {len(missing)} of {len(stems)} covering tiles have no per-tile "
            f"file (an interrupted build) — e.g. {', '.join(missing[:5])}"
            f"{' …' if len(missing) > 5 else ''}")


# ── cell-subtree sharded vector bundle → sparse vector.pmtiles ─────────────────────────────────
# The one joint --generate-variable-depth-tile-pyramid run is split into a global shallow run plus
# one variable-depth run per populated VECTOR_SPLIT_Z cell, tile-joined into vector.pmtiles (the
# serial variable-depth tiler then runs once per cell concurrently, not once over all content — see
# the design note in docs/plans/2026-07-14-native-resolution.md):
#   - shallow: plain dense -Z0 -z(VECTOR_SPLIT_Z-1) over all three layers, filtered to features whose
#     tippecanoe.minzoom <= VECTOR_SPLIT_Z-1; owns every z < VECTOR_SPLIT_Z tile.
#   - cell:    -Z VECTOR_SPLIT_Z -z(cell max child_z) with variable depth over that cell's covering stems;
#     owns its z >= VECTOR_SPLIT_Z subtree. Layers stay joint within each run so the layer-vanishing bug
#     (separately-tiled layers leafing at different depths) stays dead.
# Tile ownership is disjoint by construction, so the join is pure concatenation (tile-join -pk).
# All zoom gating rides as per-feature tippecanoe.minzoom: contour tiers via contour_minzoom,
# depare's z6 floor as a uniform minzoom, soundings' pyramid levels unchanged. The archive is
# SPARSE — the Worker overzooms ancestors (Part 1); manifest.vector.max_zoom is what turns that on.

# Union of the three layers' MVT attributes; the FlatGeobuf/GeoJSON Integer64 columns need :int so
# they don't land as strings. depth_m stays untyped (contours carry an int level, soundings a float
# depth — tippecanoe detects per layer).
# depare's kind stays in the FGBs but OUT of the tiles: nothing reads it (the style keys on
# sys/drval1 presence and sorts by rank), and it measured -3.9% on the dominant coarse-stem class.
VECTOR_ATTRS = ["depth_m", "depth_abs_m", "sys", "depth_ft", "depth_fm",
                "drval1", "drval2", "rank"]
VECTOR_TYPES = ["depth_abs_m:int", "depth_ft:int", "depth_fm:int", "rank:int"]
DEPARE_MINZOOM = 6  # depare's zoom floor, now per-feature (was depare_run's run-level -Z)
# Down-zoom partition-gap tolerance: a parent tile quantizes geometry to the same 4096-cell grid over
# 4× the area of each child, so band-union areas differ by a few percent (measured ≤2.7% either way)
# purely from that. The gap check is one-sided (only a SHRINK — children covering less than the parent
# — is a hole) with margin over that noise; a gross partition hole (a whole band missing from the
# children, e.g. the 50% test case) clears it easily, while sub-band holes are caught by the id
# completeness check. Not the 2% overlap tolerance, which is same-tile (no cross-zoom quantization).
DEPARE_GAP_TOL = 0.10

# Feature ids must be globally unique across the shallow run AND every per-cell run (each run is a
# separate process, so no shared counter): the join concatenates all archives, and a collision would
# let a dropped feature's completeness id be satisfied by an unrelated survivor. Each run gets a
# disjoint [base, base + ID_STRIDE) block — shallow at base 0, cell i (sorted) at (i+1)*ID_STRIDE.
# 2**40 (~1.1e12) dwarfs any single run's feature count; ~1024 z-cells * 2**40 stays well inside a
# 64-bit id (tippecanoe preserves ids as unsigned 64-bit).
ID_STRIDE = 1 << 40


def _json_scalar(v):
    """A GeoJSON-safe scalar: unwrap numpy/pandas scalars; None for missing (None/NaN) so the
    property stays ABSENT in the MVT — depare nodata truly carries no drval1, the fill's switch."""
    if v is None:
        return None
    if hasattr(v, "item"):
        try:
            v = v.item()
        except (ValueError, AttributeError):
            pass
    if isinstance(v, float) and v != v:  # NaN
        return None
    return v


# tippecanoe simplifies away lines/polygons that shrink below ~a pixel at a tile's zoom; even at the
# finest (leaf = -z) zoom a sub-pixel sliver is dropped — a legitimate loss, not the regression. So a
# line/polygon is REQUIRED to survive (enters the completeness set) only above this many pixels at
# maxz; below it, it's exempt. Measured on the real preview: 22 contour fragments dropped, all
# ≤1.5 px, while the smallest required curve is ~6 px — 4 px sits clear of both. Points (soundings)
# are never simplified away, so they are always required.
COMPLETENESS_MIN_PX = 4


def _leaf_pixel_deg(maxz):
    """One MVT pixel at the leaf zoom, in degrees of longitude (tile extent 4096)."""
    return 360.0 / (2 ** maxz) / 4096


def _survives_leaf(geom, min_deg):
    """Whether tippecanoe must keep this geometry at the leaf zoom (so completeness can require it).
    Points always; lines by length; polygons by linear size sqrt(area) — all in degrees, min_deg being
    COMPLETENESS_MIN_PX leaf pixels. Anisotropy (lon vs lat degrees) is inside the 4-px margin."""
    t = geom.geom_type
    if "Point" in t:
        return True
    if "Polygon" in t:
        return geom.area ** 0.5 >= min_deg
    return geom.length >= min_deg


def _fgb_to_seq(fgbs, cols, minzoom_fn, out_path, layer, identity_fn, id0, maxz, max_minzoom=None,
                min_minzoom=None):
    """Stream per-tile FGBs → one GeoJSONSeq (newline-delimited features) carrying a per-feature
    tippecanoe.minzoom — FGB can't hold the tippecanoe extension, so the run reads GeoJSON like
    soundings already do. One tile in memory at a time (macrotile-sized). Every WRITTEN feature gets
    a unique GeoJSON id counting up from id0 (disjoint per run/cell) — tippecanoe preserves ids, so
    the self-check proves each input survived. When max_minzoom is set (the shallow run's -z), a
    feature whose minzoom exceeds it is dropped at write time — it belongs to a deeper cell, never a
    shallow tile; the explicit filter is what makes this our contract, not tippecanoe's clamp.
    Returns (next_id, {id: identity} for the completeness set) — only above-leaf-pixel features enter
    it (sub-pixel slivers are exempt, since tippecanoe drops them legitimately)."""
    import geopandas as gpd
    from shapely.geometry import mapping
    min_deg = COMPLETENESS_MIN_PX * _leaf_pixel_deg(maxz)
    fid = id0
    ids = {}
    with open(out_path, "w") as fh:
        for fgb in fgbs:
            g = gpd.read_file(fgb)
            for r in g.itertuples():
                props = {}
                for c in cols:
                    v = _json_scalar(getattr(r, c, None))
                    if v is not None:
                        props[c] = v
                mz = minzoom_fn(props)
                if max_minzoom is not None and mz > max_minzoom:
                    continue
                if min_minzoom is not None and mz < min_minzoom:
                    # A cell run's zoom floor: a variable-depth pyramid can decide to LEAF above the
                    # run's -Z and silently drop the frozen content — clamping every feature's minzoom
                    # to the floor makes the #397 leaf guard force subdivision down to it instead.
                    # The sub-floor zooms these features would have shown at belong to the shallow run.
                    mz = min_minzoom
                feat = {"type": "Feature", "id": fid,
                        "tippecanoe": {"minzoom": mz},
                        "properties": props, "geometry": mapping(r.geometry)}
                fh.write(json.dumps(feat))
                fh.write("\n")
                if _survives_leaf(r.geometry, min_deg):
                    ids[fid] = f"{layer} {identity_fn(props)}"
                fid += 1
    return fid, ids


def _soundings_to_seq(gjs, out_path, id0, max_minzoom=None, min_minzoom=None):
    """Concatenate the per-tile sounding FeatureCollections into one GeoJSONSeq, features passed
    through untouched except a unique GeoJSON id (counting up from id0) for the completeness check —
    each already carries its own tippecanoe.minzoom (pyramid level). When max_minzoom is set (the
    shallow run's -z), a sounding whose minzoom exceeds it is dropped at write time (it rides a
    deeper cell). Returns (next_id, {id: identity})."""
    fid = id0
    ids = {}
    with open(out_path, "w") as fh:
        for gj in gjs:
            for ft in json.load(open(gj)).get("features", []):
                mz = ft.get("tippecanoe", {}).get("minzoom", 0)
                if max_minzoom is not None and mz > max_minzoom:
                    continue
                if min_minzoom is not None and mz < min_minzoom:
                    ft.setdefault("tippecanoe", {})["minzoom"] = min_minzoom  # cell zoom floor, see _fgb_to_seq
                ft["id"] = fid
                fh.write(json.dumps(ft))
                fh.write("\n")
                ids[fid] = f"sounding depth={ft.get('properties', {}).get('depth_m')}"
                fid += 1
    return fid, ids


def _tippecanoe_run(layers, minz, maxz, out, variable_depth):
    """ONE tippecanoe run over the named layer seqs → out (pmtiles). `variable_depth` adds
    --generate-variable-depth-tile-pyramid (the per-cell runs; the shallow run stays a plain dense
    pyramid). Every other flag is IDENTICAL across both run kinds so tile content at a given zoom
    matches the old single joint run. Global --coalesce-smallest-as-needed (merges, never drops: a
    dropped depare polygon is a partition hole; contours coalesce cleanly, proven by the STEP-0
    gate) since every as-needed strategy is invocation-wide. --no-simplification-of-shared-nodes
    keeps partition seams crack-free through per-zoom simplification. -q is dropped so tippecanoe's
    progress rides the per-rule log (the joint run's 14 h of invisibility motivated this)."""
    cmd = ["tippecanoe", "-o", out, "-f"]
    if variable_depth:
        cmd.append("--generate-variable-depth-tile-pyramid")
    cmd += ["-n", "Open Waters Bathymetry", "-A", utils.ATTRIBUTION,
            "-Z", str(minz), "-z", str(maxz), "-P",
            "--coalesce-smallest-as-needed", "--no-simplification-of-shared-nodes",
            # -S alone ALSO applies at maxzoom (~76 m tolerance at z10), which cut isobaths across
            # islands the DEM-level land clamp had already routed around — pin maxzoom near-lossless
            # so leaf tiles keep the clamped shoreline. Env-tunable to dial on a re-bundle.
            "--simplification", os.environ.get("VECTOR_SIMPLIFICATION", "8"),
            "--simplification-at-maximum-zoom",
            os.environ.get("VECTOR_SIMPLIFICATION_MAXZOOM", "1")]
    # --detect-shared-borders drives tippecanoe's wagyu exit-106 hole-placement crash on dense
    # DEPARE geometry (mapbox/tippecanoe#761, unfixed in felt v2.80.0). Its documented successor
    # --no-simplification-of-shared-nodes (above) keeps shared edges crack-free without the crash;
    # this guard fails the build if the old flag is ever reintroduced.
    assert "--detect-shared-borders" not in cmd, "vector bundle must not use --detect-shared-borders (wagyu exit-106)"
    for a in VECTOR_ATTRS:
        cmd += ["-y", a]
    for t in VECTOR_TYPES:
        cmd += ["-T", t]
    for name, seq in layers:
        cmd += ["-L", f"{name}:{seq}"]
    kind = "variable-depth" if variable_depth else "shallow"
    with utils.log_group(f"vector tippecanoe {kind} ({len(layers)} layers, z{minz}-{maxz})"):
        utils.run_monitored(cmd, f"vector tippecanoe {kind}", out)


def _empty_archive(out):
    """Declare an empty vector output: a 0-byte file, the same sentinel the per-tile forks use for
    an empty tile — the join skips it by size, so an all-empty cell (or a shallow run with no
    below-VECTOR_SPLIT_Z content) still satisfies the DAG without minting a spurious archive."""
    utils.create_folder(os.path.dirname(out))
    open(out, "w").close()


def _build_seqs_and_run(stems, minz, maxz, id_base, variable_depth, out, max_minzoom=None,
                        min_minzoom=None):
    """Build the three layer GeoJSONSeqs from `stems` (ids from a disjoint block at id_base), then
    one tippecanoe run → out. Returns the {layer: {id: identity}} completeness map of the WRITTEN,
    above-leaf-pixel features. An all-empty input writes a 0-byte `out` and returns empty maps.
    require_stable_complete gates the caller; here 0-byte per-tile files are legitimately empty."""
    depare_on = not os.environ.get("SKIP_DEPARE")
    cfiles = [f"store/contour/{s}.fgb" for s in stems]
    sfiles = [f"store/soundings/{s}.geojson" for s in stems]
    dfiles = [f"store/depare/{s}.fgb" for s in stems] if depare_on else []
    cfgbs = [f for f in cfiles if os.path.getsize(f) > 0]
    sgjs = [f for f in sfiles if os.path.getsize(f) > 0]
    dfgbs = [f for f in dfiles if os.path.getsize(f) > 0]

    cseq = utils.vector_scratch("contours.geojsons")
    sseq = utils.vector_scratch("soundings.geojsons")
    dseq = utils.vector_scratch("depare.geojsons")
    nid = id_base
    cids = sids = dids = {}
    try:
        nid, cids = _fgb_to_seq(cfgbs, ("depth_m", "depth_abs_m", "sys", "depth_ft", "depth_fm"),
                                lambda p: contour_minzoom(p["sys"], float(p["depth_m"])), cseq,
                                "contour", lambda p: f"sys={p.get('sys')} depth_m={p.get('depth_m')}",
                                nid, maxz, max_minzoom, min_minzoom)
        nid, sids = _soundings_to_seq(sgjs, sseq, nid, max_minzoom, min_minzoom)
        nid, dids = _fgb_to_seq(dfgbs, ("drval1", "drval2", "sys", "rank"),
                                lambda p: DEPARE_MINZOOM, dseq,
                                "depare", lambda p: f"drval1={p.get('drval1')} sys={p.get('sys')}",
                                nid, maxz, max_minzoom, min_minzoom)
        layers = [(name, seq) for name, seq, ids in
                  (("contours", cseq, cids), ("soundings", sseq, sids), ("depare", dseq, dids)) if ids]
        if layers:
            _tippecanoe_run(layers, minz, maxz, out, variable_depth)
        else:
            _empty_archive(out)
    finally:
        for seq in (cseq, sseq, dseq):
            if os.path.exists(seq):
                os.remove(seq)
    return {"contours": cids, "soundings": sids, "depare": dids}


SHALLOW = "store/bundle/vector-shallow.pmtiles"


def _cell_archive(cell):
    return f"store/bundle/vector-cell-{cell}.pmtiles"


def _cell_sidecar(cell):
    return f"store/bundle/vector-cell-{cell}.ids.json"


def bundle_shallow_stable():
    """The global shallow vector run: plain dense -Z0 -z(VECTOR_SPLIT_Z-1) over all three layers, filtered
    to features whose tippecanoe.minzoom <= VECTOR_SPLIT_Z-1 → store/bundle/vector-shallow.pmtiles. Owns
    every z < SPLIT_Z tile of the join. require_stable_complete gates every covering stem first (a
    MISSING per-tile file is an interrupted build; a 0-byte one is an empty tile). Snakemake owns
    freshness. The shallow features are a subset of some cell's features, so their ids don't join the
    completeness set — the join checks the per-cell sidecars."""
    import bundle
    import mosaic
    stems = mosaic.covering_stems()
    require_stable_complete("contour", stems, [f"store/contour/{s}.fgb" for s in stems])
    require_stable_complete("soundings", stems, [f"store/soundings/{s}.geojson" for s in stems])
    if not os.environ.get("SKIP_DEPARE"):
        require_stable_complete("depare", stems, [f"store/depare/{s}.fgb" for s in stems])
    utils.create_folder("store/bundle")
    maxz = VECTOR_SPLIT_Z - 1
    ids = _build_seqs_and_run(stems, 0, maxz, 0, False, SHALLOW, max_minzoom=maxz)
    n = {k: len(v) for k, v in ids.items()}
    print(f"vector shallow bundle (stable): {SHALLOW} (z0-{maxz}; {n})")


def bundle_cell_stable(cell):
    """One variable-depth vector run for a SPLIT_Z cell: -Z SPLIT_Z -z(cell max child_z) over the
    cell's covering stems → store/bundle/vector-cell-<cell>.pmtiles, plus a sidecar of its expected
    completeness ids the join collects. Owns the cell's z >= SPLIT_Z subtree. An all-empty cell
    writes a 0-byte archive + empty sidecar (the join skips it). The id base is a disjoint block from
    a stable sort of the covering's cells, so ids never collide with the shallow run or another
    cell."""
    import bundle
    import mosaic
    covering = mosaic.covering_stems()
    cells = vector_covering_cells(covering)
    if cell not in cells:
        raise SystemExit(f"vector cell {cell}: not a populated cell of the covering")
    stems = cells[cell]
    require_stable_complete("contour", stems, [f"store/contour/{s}.fgb" for s in stems])
    require_stable_complete("soundings", stems, [f"store/soundings/{s}.geojson" for s in stems])
    if not os.environ.get("SKIP_DEPARE"):
        require_stable_complete("depare", stems, [f"store/depare/{s}.fgb" for s in stems])
    utils.create_folder("store/bundle")
    id_base = (sorted(cells).index(cell) + 1) * ID_STRIDE
    cell_maxz = _stems_maxz(stems)
    ids = _build_seqs_and_run(stems, VECTOR_SPLIT_Z, cell_maxz, id_base, True, _cell_archive(cell),
                              min_minzoom=VECTOR_SPLIT_Z)
    with open(_cell_sidecar(cell), "w") as f:
        json.dump(ids, f)
    n = {k: len(v) for k, v in ids.items()}
    print(f"vector cell bundle (stable): {_cell_archive(cell)} (z{VECTOR_SPLIT_Z}-{cell_maxz}; {n})")


def bundle_join_stable():
    """tile-join the shallow archive + every populated cell archive → store/bundle/vector.pmtiles,
    the single served sparse pyramid the Worker overzooms (Part 1). Tile ownership is disjoint
    (shallow owns z < SPLIT_Z, each cell its z >= SPLIT_Z subtree), so the join is pure
    concatenation — -pk keeps every tile (per-cell runs already enforced size limits; tile-join must
    not silently skip). The completeness self-check runs on the FINAL archive against the union of
    the per-cell sidecars' expected ids."""
    import mosaic
    covering = mosaic.covering_stems()
    cellmap = vector_covering_cells(covering)
    cells = sorted(cellmap)
    if not os.path.exists(SHALLOW):
        raise SystemExit(f"vector join: missing {SHALLOW} — run the shallow bundle first")
    expected = {"contours": {}, "soundings": {}, "depare": {}}
    by_maxz = []  # (maxz, path) — tile-join takes the OUTPUT header's maxzoom from its FIRST input,
    # so the deepest archive must lead or every deeper tile goes invisible to header-ranged readers
    # (the self-check, the Worker's TileJSON) while still occupying bytes in the file.
    if os.path.getsize(SHALLOW) > 0:
        by_maxz.append((VECTOR_SPLIT_Z - 1, SHALLOW))
    for c in cells:
        arch = _cell_archive(c)
        if not os.path.exists(arch):
            raise SystemExit(f"vector join: missing cell archive {arch} — run the cell bundles first")
        if os.path.getsize(arch) == 0:
            continue  # empty cell (0-byte sentinel) — no tiles, no expected ids
        by_maxz.append((_stems_maxz(cellmap[c]), arch))
        with open(_cell_sidecar(c)) as f:
            for layer, m in json.load(f).items():
                expected[layer].update((int(k), v) for k, v in m.items())
    archives = [p for _, p in sorted(by_maxz, key=lambda t: -t[0])]
    if not archives:
        raise SystemExit("vector join: no vector features in any shallow or cell archive")

    vec = "store/bundle/vector.pmtiles"
    utils.create_folder("store/bundle")
    cmd = ["tile-join", "-o", vec, "-f", "-pk",
           "-n", "Open Waters Bathymetry", "-A", utils.ATTRIBUTION, *archives]
    with utils.log_group(f"vector tile-join ({len(archives)} archives)"):
        utils.run_monitored(cmd, "vector tile-join", vec)

    maxz = _vector_maxz(0)
    expected = {k: v for k, v in expected.items() if v}
    _vector_selfcheck(vec, maxz, expected)
    print(f"vector bundle (stable): {vec} (z0-{maxz}; {len(archives)} archives, "
          f"{sum(len(v) for v in expected.values())} ids)")


def _decode_tile(vec, z, x, y):
    """{layer: [feature, ...]} for one tile of a pmtiles archive, via tippecanoe-decode (XYZ)."""
    out = subprocess.run(["tippecanoe-decode", "-c", vec, str(z), str(x), str(y)],
                         capture_output=True, text=True).stdout
    layers = collections.defaultdict(list)
    for line in out.splitlines():
        line = line.strip().rstrip(",")
        if not line.startswith("{"):
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if o.get("type") == "Feature":
            layers[o.get("tippecanoe", {}).get("layer", "?")].append(o)
    return layers


# A decoded feature line carries its id right after the type; anchor on the type so a future
# property named "id" (there is none today) can't be mistaken for it.
_FEATURE_ID_RE = re.compile(r'"type": "Feature", "id": (\d+)')


def _seen_ids(vec):
    """Union of feature ids across EVERY tile of the archive, from ONE streaming whole-archive
    tippecanoe-decode (measured 0.85 s / 4 MB preview → 60 MB decoded; the pass streams, so memory is
    the distinct-id set, not the JSON). Each input feature is written at its native leaf tile, so a
    present id proves the feature survived; a feature dropped by leafing above -z is in NO tile."""
    # ponytail: cost is O(features summed over all zooms) and the id set is ~1 int/feature; at planet
    # scale both shrink ~4^Δz-fold by scoping the decode to leaf tiles (no children in the pmtiles
    # directory) — every feature already lives at its leaf, so the union is unchanged. Not needed yet.
    seen = set()
    p = subprocess.Popen(["tippecanoe-decode", vec], stdout=subprocess.PIPE, text=True)
    for line in p.stdout:
        m = _FEATURE_ID_RE.search(line)
        if m:
            seen.add(int(m.group(1)))
    if p.wait() != 0:  # a partial decode would undercount and fail completeness for the wrong reason
        raise SystemExit(f"vector self-check: tippecanoe-decode failed on {vec}")
    return seen


def _mbands(feats):
    """Decoded depare metre depth-area bands: sys=m with a real drval1 — INCLUDING the 0-m band.
    The `drval1 is not None and >= 0` test is deliberate: `drval1 or -1` treated 0 as missing and
    dropped the 0-m band from the partition checks (the confirmed bug)."""
    return [f for f in feats
            if f["properties"].get("sys") == "m"
            and f["properties"].get("drval1") is not None
            and f["properties"]["drval1"] >= 0]


def _vector_selfcheck(vec, maxz, expected=None, per_zoom=6):
    """Completeness + partition guard on the sparse archive. PRIMARY check (when `expected` — the
    {layer: {id: identity}} maps the seq-builders return): every input feature id must survive
    somewhere in the archive, per layer. COMPLETE (not sampled), and it is what catches a contour,
    sounding, OR whole depare polygon that variable-depth dropped — the exact regression this PR
    prevents — because a dropped feature appears in no tile and its id is missing from the union.
    Robust to legitimate per-zoom minzoom thinning and to --coalesce-smallest-as-needed (a feature
    that only coalesces at a low zoom still appears at its full-detail native leaf; one present
    nowhere is a real drop). Plus the sampled complementary checks: no depare below its z6 floor, no
    contour below its tier minzoom, soundings present, depare m-bands pairwise disjoint (coalesce
    displaced nothing), and the partition conserved down-zoom (a parent m-band union ≈ its 4
    children's — a shrink means a gap opened deeper; whole missing polygons are already caught above)."""
    from pmtiles.reader import Reader, MmapSource, all_tiles
    from shapely.geometry import shape
    from shapely.ops import unary_union
    byz = collections.defaultdict(list)
    with open(vec, "r+b") as f:
        reader = Reader(MmapSource(f))
        for tile_tuple, _ in all_tiles(reader.get_bytes):
            z, x, y = tile_tuple
            byz[z].append((x, y))
    present = {z: set(xys) for z, xys in byz.items()}
    problems, n_soundings = [], 0

    if expected is not None:
        seen = _seen_ids(vec)
        for layer, idmap in expected.items():
            missing = [i for i in idmap if i not in seen]
            if missing:
                sample = "; ".join(idmap[i] for i in missing[:5])
                problems.append(f"{layer}: {len(missing)} of {len(idmap)} input features dropped "
                                f"(present in no tile) — e.g. {sample}")

    def mband_area(feats):
        geoms = [shape(f["geometry"]).buffer(0) for f in _mbands(feats)]
        return unary_union(geoms).area if geoms else 0.0

    for z in sorted(byz):
        ts = byz[z]
        for x, y in ts[:: max(1, len(ts) // per_zoom)]:
            L = _decode_tile(vec, z, x, y)
            n_soundings += len(L.get("soundings", []))
            if z < DEPARE_MINZOOM and L.get("depare"):
                problems.append(f"z{z} {x}/{y}: {len(L['depare'])} depare below z{DEPARE_MINZOOM}")
            for feat in L.get("contours", []):
                p = feat["properties"]
                d, s = p.get("depth_m"), p.get("sys")
                if d is not None and s and contour_minzoom(s, float(d)) > z:
                    problems.append(f"z{z} {x}/{y}: contour depth_m={d} sys={s} below minzoom "
                                    f"{contour_minzoom(s, float(d))}")
                    break
            mbands = _mbands(L.get("depare", []))
            if mbands:
                geoms = [shape(f["geometry"]).buffer(0) for f in mbands]
                asum, uni = sum(g.area for g in geoms), unary_union(geoms).area
                if uni and (asum - uni) / uni >= 0.02:
                    problems.append(f"z{z} {x}/{y}: depare m-bands overlap {(asum - uni) / uni:.2%} "
                                    f"(coalesce displaced a partition)")
                # partition conserved into z+1: a present-children parent's band union ≈ its 4
                # children's combined union; a one-sided SHRINK (children covering less) is a gap that
                # opened at the deeper zoom (a grow is just finer-grid quantization, not a hole).
                kids = [(2 * x, 2 * y), (2 * x + 1, 2 * y), (2 * x, 2 * y + 1), (2 * x + 1, 2 * y + 1)]
                if z + 1 <= maxz and uni and all(k in present.get(z + 1, ()) for k in kids):
                    child = sum(mband_area(_decode_tile(vec, z + 1, kx, ky).get("depare", []))
                                for kx, ky in kids)
                    if (uni - child) / uni >= DEPARE_GAP_TOL:
                        problems.append(f"z{z} {x}/{y}: depare band area {uni:.3g} shrank to children "
                                        f"{child:.3g} ({(uni - child) / uni:.2%} gap opened deeper)")
    if n_soundings == 0:
        problems.append("no soundings in any sampled tile (the layer vanished)")
    if problems:
        raise SystemExit("vector self-check FAILED:\n  " + "\n  ".join(problems[:20]))
    ids_note = f", {sum(len(m) for m in expected.values())} ids complete" if expected else ""
    print(f"vector self-check ok ({sum(len(v) for v in byz.values())} tiles, "
          f"z{min(byz)}-{max(byz)}, {n_soundings} sampled soundings{ids_note})")


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
    # the run's -z reads child_z off covering stems
    assert _stems_maxz({"4-5-6-8", "11-300-400-13"}) == 13
    # cell grouping: covering stems route to their VECTOR_SPLIT_Z cell; a cell's child_z drives -z.
    sz = VECTOR_SPLIT_Z
    a_stem, b_stem = f"{utils.macrotile_z}-0-0-{utils.macrotile_z}", f"{utils.macrotile_z}-100-100-{utils.macrotile_z + 3}"
    cells = vector_covering_cells([a_stem, b_stem])
    assert all(c.startswith(f"{sz}-") for c in cells), cells
    assert cells[_vector_cell_of(utils.macrotile_z, 100, 100)] == [b_stem]
    assert _stems_maxz(cells[_vector_cell_of(utils.macrotile_z, 100, 100)]) == utils.macrotile_z + 3
    # id blocks are disjoint: shallow at 0, cell i (sorted) at (i+1)*STRIDE — no block overlaps
    order = sorted(vector_covering_cells([a_stem, b_stem]))
    bases = [0] + [(i + 1) * ID_STRIDE for i in range(len(order))]
    assert all(b2 - b1 >= ID_STRIDE for b1, b2 in zip(bases, bases[1:])), bases
    # navigable-band contours skip Chaikin (never bow a shoal deeper); deeper ones smooth
    assert _chaikin_iters(10) == 0 and _chaikin_iters(NAV_SMOOTH_MAX_M) == 0
    assert _chaikin_iters(NAV_SMOOTH_MAX_M + 1) == CHAIKIN_ITERATIONS
    # per-feature minzoom reproduces the CONTOUR_TIERS thinning (the leaf-safe replacement for -j).
    # A shallow metre curve only shows at native+ (last ceiling); a deep one from the first tier
    # shows at z0; a fathom curve is gated by depth like the deep metre ones.
    ceilings = [hi for hi, _ in CONTOUR_TIERS]
    assert contour_minzoom("m", -2) == ceilings[-1]              # -2 in no tier -> native+
    assert contour_minzoom("m", -4000) == 0                      # -4000 in the first tier -> z0
    assert contour_minzoom("m", -50) == 7 and contour_minzoom("m", -10) == 9
    assert contour_minzoom("ft", -0.5) == ceilings[-1]          # shallower than any band's floor
    assert contour_minzoom("ft", -4000) == 0                     # deeper than the first band's floor
    # every tier level must exist in the generated contour set (else it gates nothing)
    assert all(d in config.CONTOUR_LEVELS for _, depths in CONTOUR_TIERS for d in depths)
    print("contour_run ring-drop + minzoom self-check ok")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["tile"] and len(a) == 2:
        tile(a[1])
    elif a == ["bundle-shallow", "--stable"]:
        bundle_shallow_stable()
    elif a[:1] == ["bundle-cell"] and len(a) == 3 and a[2] == "--stable":
        bundle_cell_stable(a[1])
    elif a == ["bundle-join", "--stable"]:
        bundle_join_stable()
    elif a[:1] == ["coverage"]:
        coverage_bundle()
    elif a[:1] == ["check"]:
        _check()
    else:
        sys.exit("usage: contour_run.py tile <stem> | bundle-shallow --stable | "
                 "bundle-cell <cell> --stable | bundle-join --stable | coverage | check")
