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


STREAM_BATCH = 100_000  # features per refine batch; a z15 window's set doesn't fit in RAM


def _refine_stream(sources, out_fgb, tol, clip):
    """Enrich, Chaikin-smooth, deep ring-drop, and bbox-clip the raw contour sets, one feature
    batch at a time, appending to a GeoJSONSeq (4326) that a final ogr2ogr converts to the
    multi-promoted FGB. Clip in shapely, keeping only line pieces — ogr2ogr -clipsrc emits a
    GeometryCollection here that the FlatGeobuf write rejects (the GDAL GeometryCollection trap,
    same as depare's shapely clip). Returns the written feature count."""
    import geopandas as gpd
    from shapely import make_valid
    from shapely.geometry import MultiLineString

    seq = out_fgb + ".geojsons"
    written = 0
    for fgb, sys_tag in sources:
        total = feature_count(fgb)
        for off in range(0, total, STREAM_BATCH):
            g = gpd.read_file(fgb, rows=slice(off, min(off + STREAM_BATCH, total)))
            g["sys"] = sys_tag
            g["depth_abs_m"] = (-g["depth_m"]).round().astype(int)
            g["depth_ft"] = (-g["depth_m"] / 0.3048).round().astype(int)
            g["depth_fm"] = (-g["depth_m"] / 1.8288).round().astype(int)
            g["geometry"] = [_smooth_geom(geom, tol, _chaikin_iters(d))
                             for geom, d in zip(g.geometry, g["depth_abs_m"])]
            deep = g["depth_abs_m"] >= DEEP_CUTOFF_M
            if deep.any():
                g.loc[deep, "geometry"] = [_drop_small_rings(geom, MIN_RING_AREA_M2)
                                           for geom in g.loc[deep, "geometry"]]
            g = g[g.geometry.notna() & ~g.geometry.is_empty]
            if len(g):
                g["geometry"] = [MultiLineString(parts) if parts else None
                                 for parts in (_lines(make_valid(geom).intersection(clip))
                                               for geom in g.geometry)]
                g = g[g.geometry.notna() & ~g.geometry.is_empty]
            if len(g) == 0:
                continue
            g.to_crs(4326).to_file(seq, driver="GeoJSONSeq", mode="a" if written else "w")
            written += len(g)
    if written:
        # Same 200 MB per-feature GeoJSON ceiling as the depare sink (see _RowSink.finish):
        # a dense stem's isobath can exceed it.
        _run(f"ogr2ogr --config OGR_GEOJSON_MAX_OBJ_SIZE 0 "
             f"-f FlatGeobuf -overwrite -nlt PROMOTE_TO_MULTI {out_fgb} {seq}",
             "ogr2ogr contours")
        os.remove(seq)
    return written


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


def tile(stem):
    """The per-stem Snakemake job: contour one stem from a BUFFERED mosaic window, smoothed at read
    with the one shared f(depth, zoom), output at store/contour/<stem>.fgb. A featureless tile
    writes a 0-byte sentinel so the engine sees a complete output; bundling filters empties by
    size."""
    z, x, y, child_z = (int(a) for a in stem.split("-"))
    out = f"store/contour/{stem}.fgb"
    tmp = tempfile.mkdtemp(prefix=f"contour-{stem}-")  # local scratch; publish crosses to the store
    # Private copy of the shared smoothed window: the clamp mutates in place, and the
    # smoothing already smeared sea negatives back across islands — without the re-cut,
    # isobaths land on shore.
    dem = f"{tmp}/dem.tiff"
    shutil.copyfile(f"store/window/{stem}.tif", dem)
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

    from shapely.geometry import box
    b = mercantile.xy_bounds(tile_obj)  # unbuffered, tile-aligned (EPSG:3857)
    final = f"{tmp}/contour-final.fgb"
    if _refine_stream(sources, final, get_resolution(child_z),
                      box(b.left, b.bottom, b.right, b.top)) == 0:
        print(f"contour: no features in tile bbox for {label}")
        return None
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


# The vector shard "grid" is the covering itself: aggregation tiles are a quadtree PARTITION of
# the plane (roots from seed_z up to macrotile_z — coarse ocean stems root shallower than z8), so
# each stem's own root tile is its cell. One stem per cell, the finest seam-safe grain: worst cell
# job = worst stem, and ownership below the shallow band is disjoint because the roots partition.
# VECTOR_SPLIT_Z caps the SHALLOW run (z0..SPLIT-1) and floors the cell runs' -Z; env-overridable.
VECTOR_SPLIT_Z = int(os.environ.get("VECTOR_SPLIT_Z", "0")) or utils.macrotile_z


def _vector_cell_of(z, x, y):
    if z >= VECTOR_SPLIT_Z:
        return f"{VECTOR_SPLIT_Z}-{x >> (z - VECTOR_SPLIT_Z)}-{y >> (z - VECTOR_SPLIT_Z)}"
    return f"{z}-{x}-{y}"  # a coarse root IS its cell (the covering partitions, so it's unique)


def vector_covering_cells(stems):
    """{cell: [covering stems]} — each covering stem's root tile is its own cell (1:1); every
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
# one variable-depth run per populated VECTOR_SPLIT_Z cell, merged into vector.pmtiles (the serial
# variable-depth tiler then runs once per cell concurrently, not once over all content — see the
# design note in docs/plans/2026-07-14-native-resolution.md):
#   - shallow: plain dense -Z0 -z(VECTOR_SPLIT_Z-1) over all three layers, filtered to features whose
#     tippecanoe.minzoom <= VECTOR_SPLIT_Z-1; owns every z < VECTOR_SPLIT_Z tile.
#   - cell:    -Z VECTOR_SPLIT_Z -z(cell max child_z) with variable depth over that cell's covering stems,
#     then rewritten to keep ONLY its z >= VECTOR_SPLIT_Z owned subtree (fringe tiles a neighbour's edge
#     buffer bled in are dropped). Layers stay joint within each run so the layer-vanishing bug
#     (separately-tiled layers leafing at different depths) stays dead.
# After the fringe filter tile ownership is STRUCTURALLY disjoint (shallow owns z < VECTOR_SPLIT_Z,
# each cell exactly its own subtree), so the join is `pmtiles merge` (go-pmtiles) — a pure concat that
# refuses overlapping inputs, replacing tile-join's boundary-tile MERGE (slow at planet scale + the
# PMTiles-writer corruption felt/tippecanoe#278). All zoom gating rides as per-feature
# tippecanoe.minzoom: contour tiers via contour_minzoom, depare's z6 floor as a uniform minzoom,
# soundings' pyramid levels unchanged. The archive is SPARSE — the Worker overzooms ancestors
# (Part 1); manifest.vector.max_zoom is what turns that on.

# Union of the three layers' MVT attributes; the FlatGeobuf/GeoJSON Integer64 columns need :int so
# they don't land as strings. depth_m stays untyped (contours carry an int level, soundings a float
# depth — tippecanoe detects per layer).
# depare's kind stays in the FGBs but OUT of the tiles: nothing reads it (the style keys on
# sys/drval1 presence and sorts by rank), and it measured -3.9% on the dominant coarse-stem class.
VECTOR_ATTRS = ["depth_m", "depth_abs_m", "sys", "depth_ft", "depth_fm",
                "drval1", "drval2", "rank"]
VECTOR_TYPES = ["depth_abs_m:int", "depth_ft:int", "depth_fm:int", "rank:int"]
DEPARE_MINZOOM = 6  # depare's zoom floor, now per-feature (was depare_run's run-level -Z)
# Geometric self-check thresholds, calibrated on the NY-harbor archive (2026-07-29). Decoded tiles
# include their buffer ring, and a parent's ring spans 2× its children's in shared-map units, so
# band content living only in the parent's ring (the next-deeper band just past the tile edge)
# reads as a huge down-zoom "gap"; parent and child zooms also quantize/simplify the same edge
# differently. So every comparison clips to the unbuffered tile extent, erodes past the wobble,
# and flags residual BLOBS rather than area ratios. Measured noise floor after clip+erode:
# gap 8.1k child-px², overlap 103 px²; a real defect (a displaced or lost band polygon — the 50%
# negative fixture is ~33M px²) sits orders of magnitude above these.
DEPARE_ERODE_PX = 2.0          # erosion before differencing, in child-tile grid units
DEPARE_GAP_MAX_PX2 = 32_768    # residual blob ≥ ~181×181 child px (0.2% of a tile) is a hole
DEPARE_OVERLAP_MAX_PX2 = 512   # eroded pairwise band intersection ≥ this is a displacement

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

LEAF_DETAIL = 12  # tippecanoe's default tile detail: 2**12 = 4096 grid units per tile

# The shallow run's -z is an OVERVIEW zoom — z >= VECTOR_SPLIT_Z is served from the cell archives, so
# no shallow tile is ever rendered at leaf scale. At LEAF_DETAIL a planet z6/z7 depare tile runs
# 0.5–2 MB, past tippecanoe's 500 KB ceiling, and the overflow path (--coalesce-smallest-as-needed)
# merges a band's polygon onto a deeper neighbour: a partition hole and a shoal-bias lie. 1024 units
# still resolves a quarter of a screen pixel at z7 (~306 m against a ~1.2 km pixel).
VECTOR_SHALLOW_DETAIL = int(os.environ.get("VECTOR_SHALLOW_DETAIL", "10"))


def _leaf_pixel_deg(maxz, detail=LEAF_DETAIL):
    """One MVT grid unit at the leaf zoom, in degrees of longitude (tile extent 2**detail)."""
    return 360.0 / (2 ** maxz) / (2 ** detail)


def _tile_grid_deg(z):
    """One MVT grid unit of a tile of the JOINED archive, in degrees of longitude. z below the split
    comes from the shallow run at VECTOR_SHALLOW_DETAIL, the rest from the per-cell runs at
    LEAF_DETAIL. Every geometric tolerance below is expressed in grid units, so it has to follow the
    grid of the tile it measures — a coarser tile quantizes its band edges proportionally further."""
    return 360.0 / 2 ** z / 2 ** (VECTOR_SHALLOW_DETAIL if z < VECTOR_SPLIT_Z else LEAF_DETAIL)


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
                min_minzoom=None, leaf_detail=LEAF_DETAIL):
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
    min_deg = COMPLETENESS_MIN_PX * _leaf_pixel_deg(maxz, leaf_detail)
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
    """Stream the per-tile line-delimited sounding features (`.geojsons`, one Feature per line) into
    one GeoJSONSeq, passed through untouched except a unique GeoJSON id (counting up from id0) for the
    completeness check — each already carries its own tippecanoe.minzoom (pyramid level). One line in
    memory at a time (no whole-file json.load). When max_minzoom is set (the shallow run's -z), a
    sounding whose minzoom exceeds it is dropped at write time (it rides a deeper cell). Returns
    (next_id, {id: identity})."""
    fid = id0
    ids = {}
    with open(out_path, "w") as fh:
        for gj in gjs:
            with open(gj) as src:
                for raw in src:
                    raw = raw.strip()
                    if not raw:
                        continue
                    ft = json.loads(raw)
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
    """ONE tippecanoe run over the named layer seqs → out (pmtiles). `variable_depth` marks the
    per-cell runs, whose -z IS the display leaf: they add --generate-variable-depth-tile-pyramid and
    pin maxzoom near-lossless. The shallow run is a plain dense pyramid whose every zoom, -z
    included, is an overview, so it drops that pin and writes at VECTOR_SHALLOW_DETAIL. The rest is
    shared: --coalesce-smallest-as-needed (an as-needed strategy is invocation-wide; of the ones on
    offer it merges rather than drops, but a merge still displaces a depare band, so the run must
    stay under the tile ceiling on its own and never reach for it), and
    --no-simplification-of-shared-nodes, which keeps partition seams crack-free through per-zoom
    simplification. -q is dropped so tippecanoe's progress rides the per-rule log (the joint run's
    14 h of invisibility motivated this)."""
    cmd = ["tippecanoe", "-o", out, "-f"]
    if variable_depth:
        cmd.append("--generate-variable-depth-tile-pyramid")
    cmd += ["-n", "Open Waters Bathymetry", "-A", utils.ATTRIBUTION,
            "-Z", str(minz), "-z", str(maxz), "-P",
            "--coalesce-smallest-as-needed", "--no-simplification-of-shared-nodes",
            "--simplification", os.environ.get("VECTOR_SIMPLIFICATION", "8")]
    if variable_depth:
        # -S alone ALSO applies at maxzoom (~76 m tolerance at z10), which cut isobaths across
        # islands the DEM-level land clamp had already routed around — pin maxzoom near-lossless
        # so leaf tiles keep the clamped shoreline. Env-tunable to dial on a re-bundle.
        cmd += ["--simplification-at-maximum-zoom",
                os.environ.get("VECTOR_SIMPLIFICATION_MAXZOOM", "1")]
    else:
        cmd += ["-d", str(VECTOR_SHALLOW_DETAIL), "-D", str(VECTOR_SHALLOW_DETAIL)]
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
    sfiles = [f"store/soundings/{s}.geojsons" for s in stems]
    dfiles = [f"store/depare/{s}.fgb" for s in stems] if depare_on else []
    cfgbs = [f for f in cfiles if os.path.getsize(f) > 0]
    sgjs = [f for f in sfiles if os.path.getsize(f) > 0]
    dfgbs = [f for f in dfiles if os.path.getsize(f) > 0]

    cseq = utils.vector_scratch("contours.geojsons")
    sseq = utils.vector_scratch("soundings.geojsons")
    dseq = utils.vector_scratch("depare.geojsons")
    nid = id_base
    cids = sids = dids = {}
    detail = LEAF_DETAIL if variable_depth else VECTOR_SHALLOW_DETAIL
    try:
        nid, cids = _fgb_to_seq(cfgbs, ("depth_m", "depth_abs_m", "sys", "depth_ft", "depth_fm"),
                                lambda p: contour_minzoom(p["sys"], float(p["depth_m"])), cseq,
                                "contour", lambda p: f"sys={p.get('sys')} depth_m={p.get('depth_m')}",
                                nid, maxz, max_minzoom, min_minzoom, detail)
        nid, sids = _soundings_to_seq(sgjs, sseq, nid, max_minzoom, min_minzoom)
        nid, dids = _fgb_to_seq(dfgbs, ("drval1", "drval2", "sys", "rank"),
                                lambda p: DEPARE_MINZOOM, dseq,
                                "depare", lambda p: f"drval1={p.get('drval1')} sys={p.get('sys')}",
                                nid, maxz, max_minzoom, min_minzoom, detail)
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


def _filter_owned_subtree(archive, cell):
    """Rewrite `archive` in place keeping ONLY tiles in `cell`'s owned z/x/y subtree: a tile (z,x,y)
    is owned by cell (cz,cx,cy) iff z >= cz and x >> (z-cz) == cx and y >> (z-cz) == cy (cz is the
    cell id's OWN zoom — covering cells root as coarse as seed_z, not VECTOR_SPLIT_Z). Fringe tiles — copies a NEIGHBOUR cell's edge buffer
    bled into this cell's area — are DROPPED, making the per-cell archives structurally disjoint so
    the join's `pmtiles merge` (which refuses overlapping inputs) is a pure concat. Safe: per-stem
    fork inputs are pre-clipped at cell boundaries, so a feature's EXTENT never crosses into a
    neighbour's tile — only its render buffer does, and the owning tile's own buffer already covers
    the shared edge. Preserves the source header's tile type/compression/bounds + the tippecanoe
    layer-schema metadata; the pmtiles Writer recomputes the zoom bounds from the surviving tiles.
    Streams tile-by-tile — a dense cell's tile bytes can run to GBs, so nothing is buffered; the
    Writer needs ascending tile ids, which a clustered archive (tippecanoe's output) yields."""
    from pmtiles.reader import Reader, MmapSource, all_tiles
    from pmtiles.writer import Writer
    from pmtiles.tile import zxy_to_tileid
    cz, cx, cy = (int(a) for a in cell.split("-"))
    tmp = archive + ".owned"
    written = dropped = 0
    with open(archive, "rb") as src, open(tmp, "wb") as dst:
        reader = Reader(MmapSource(src))
        header, metadata = reader.header(), reader.metadata()
        writer = Writer(dst)
        last_tid = -1
        for (z, x, y), data in all_tiles(reader.get_bytes):
            if z < cz or (x >> (z - cz)) != cx or (y >> (z - cz)) != cy:
                dropped += 1
                continue
            tid = zxy_to_tileid(z, x, y)
            if tid <= last_tid:
                raise SystemExit(f"vector cell {cell}: archive is not clustered (tile ids out of "
                                 "order) — cannot stream the fringe filter")
            writer.write_tile(tid, data)
            last_tid = tid
            written += 1
        hdr = {k: header[k] for k in ("tile_type", "tile_compression",
                                      "min_lon_e7", "min_lat_e7", "max_lon_e7", "max_lat_e7",
                                      "center_zoom", "center_lon_e7", "center_lat_e7")}
        writer.finalize(hdr, metadata)  # recomputes min/max_zoom from the surviving tiles
    if not written:
        # A non-empty cell always owns the tiles its features live in; nothing owned means every tile
        # was fringe — a mis-sharded stem. Investigate, don't paper over (per the PR contract).
        os.remove(tmp)
        raise SystemExit(f"vector cell {cell}: no tiles in its own subtree after the fringe filter "
                         "— a mis-sharded stem")
    os.replace(tmp, archive)
    print(f"vector cell {cell}: fringe filter kept {written} tiles, dropped {dropped}")


def _archive_metadata(archive):
    from pmtiles.reader import Reader, MmapSource
    with open(archive, "rb") as f:
        return Reader(MmapSource(f)).metadata()


def _union_layers_lead(archives, lead):
    """Write `archives[0]` to `lead` with its `vector_layers` widened to the union across every
    shard, so the merged archive advertises exactly the layers its tiles carry.

    Necessary because pmtiles merge copies the layer schema from its FIRST input only and no shard
    is guaranteed every layer: a constant-depth cell emits no contours, and a z8-rooted stem's
    soundings (coarsest display zoom = the stem's own z) never clear the shallow run's
    -z(VECTOR_SPLIT_Z-1) filter. An under-declared archive reads as a dropped layer to anything
    trusting the metadata (ab_check.py compares exactly this set). Patching a COPY, not the shallow
    rule's own output, keeps that output's provenance intact.

    Also logs each shard's tippecanoe `strategies` block (its own per-zoom drop/coalesce accounting)
    and tilestats layer counts — merge keeps only the lead's metadata, and this is the record of
    what each shard did under tile-size pressure."""
    layers = {}
    for arch in archives:
        meta = _archive_metadata(arch)
        name = os.path.basename(arch)
        strat = {z: s for z, s in enumerate(meta.get("strategies", [])) if s}
        counts = {l.get("layer"): l.get("count") for l in
                  meta.get("tilestats", {}).get("layers", [])}
        print(f"vector shard {name}: tilestats {counts}; strategies {strat}")
        for layer in meta.get("vector_layers", []):
            have = layers.setdefault(layer["id"], dict(layer))
            have["fields"] = {**layer.get("fields", {}), **have.get("fields", {})}
            for key, widen in (("minzoom", min), ("maxzoom", max)):
                if key in layer:
                    have[key] = widen(have[key], layer[key]) if key in have else layer[key]
    if not layers:
        raise SystemExit("vector join: no shard declares a vector layer — every shard is empty")
    meta = {**_archive_metadata(archives[0]), "vector_layers": list(layers.values())}
    shutil.copyfile(archives[0], lead)
    meta_path = lead + ".json"
    with open(meta_path, "w") as f:
        json.dump(meta, f)
    utils.run_command(f"pmtiles edit --metadata={meta_path} {lead}")


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
    require_stable_complete("soundings", stems, [f"store/soundings/{s}.geojsons" for s in stems])
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
    require_stable_complete("soundings", stems, [f"store/soundings/{s}.geojsons" for s in stems])
    if not os.environ.get("SKIP_DEPARE"):
        require_stable_complete("depare", stems, [f"store/depare/{s}.fgb" for s in stems])
    utils.create_folder("store/bundle")
    id_base = (sorted(cells).index(cell) + 1) * ID_STRIDE
    cell_maxz = _stems_maxz(stems)
    ids = _build_seqs_and_run(stems, VECTOR_SPLIT_Z, cell_maxz, id_base, True, _cell_archive(cell),
                              min_minzoom=VECTOR_SPLIT_Z)
    # Drop the fringe tiles a neighbour's edge buffer bled in, so the cells are disjoint for the merge.
    if os.path.getsize(_cell_archive(cell)) > 0:
        _filter_owned_subtree(_cell_archive(cell), cell)
        # Census THIS archive against THIS cell's ids: a drop caught here names the guilty cell and
        # stage (cell run or fringe filter), instead of surfacing as an anonymous miss in the join's
        # whole-planet census.
        seen = _seen_ids(_cell_archive(cell))
        for layer, idmap in ids.items():
            missing = [i for i in idmap if i not in seen]
            if missing:
                sample = "; ".join(idmap[i] for i in missing[:5])
                raise SystemExit(f"vector cell {cell}: {layer}: {len(missing)} of {len(idmap)} input "
                                 f"features in no tile after the cell run + fringe filter — e.g. {sample}")
    utils.write_if_changed(_cell_sidecar(cell), json.dumps(ids))
    n = {k: len(v) for k, v in ids.items()}
    print(f"vector cell bundle (stable): {_cell_archive(cell)} (z{VECTOR_SPLIT_Z}-{cell_maxz}; {n})")


def bundle_join_stable():
    """Merge the shallow archive + every populated (fringe-filtered) cell archive → the single served
    store/bundle/vector.pmtiles, the sparse pyramid the Worker overzooms (Part 1). Tile ownership is
    STRUCTURALLY disjoint — the shallow run owns every z < VECTOR_SPLIT_Z tile, and each cell archive
    was rewritten (bundle_cell_stable) to keep only its own z >= VECTOR_SPLIT_Z subtree — so the join
    is `pmtiles merge` (go-pmtiles): a pure concat that REFUSES overlapping inputs, replacing
    tile-join's boundary-tile MERGE (slow at planet scale + the PMTiles-writer corruption
    felt/tippecanoe#278). merge copies the layer-schema metadata from its FIRST input, so the lead is
    a patched copy declaring every shard's layers (see _union_layers_lead). The completeness
    self-check runs on the FINAL merged archive against the union of the per-cell sidecars'
    expected ids."""
    import mosaic
    covering = mosaic.covering_stems()
    cellmap = vector_covering_cells(covering)
    cells = sorted(cellmap)
    if not os.path.exists(SHALLOW):
        raise SystemExit(f"vector join: missing {SHALLOW} — run the shallow bundle first")
    expected = {"contours": {}, "soundings": {}, "depare": {}}
    archives = []  # shallow leads (planet bounds); order is otherwise irrelevant — pmtiles merge
    # takes the header zoom bounds from the UNION of all inputs, not the first.
    if os.path.getsize(SHALLOW) > 0:
        archives.append(SHALLOW)
    for c in cells:
        arch = _cell_archive(c)
        if not os.path.exists(arch):
            raise SystemExit(f"vector join: missing cell archive {arch} — run the cell bundles first")
        if os.path.getsize(arch) == 0:
            continue  # empty cell (0-byte sentinel) — no tiles, no expected ids
        archives.append(arch)
        with open(_cell_sidecar(c)) as f:
            for layer, m in json.load(f).items():
                expected[layer].update((int(k), v) for k, v in m.items())
    if not archives:
        raise SystemExit("vector join: no vector features in any shallow or cell archive")

    vec = "store/bundle/vector.pmtiles"
    utils.create_folder("store/bundle")
    lead = vec + ".lead"  # archives[0] carrying every shard's layer — see _union_layers_lead
    try:
        _union_layers_lead(archives, lead)
        cmd = ["pmtiles", "merge", lead, *archives[1:], vec]  # go-pmtiles: inputs, then output last
        with utils.log_group(f"vector pmtiles merge ({len(archives)} archives)"):
            utils.run_monitored(cmd, "vector pmtiles merge", vec)
    finally:
        # a full copy of the shallow archive; the build volume has no room to orphan one on failure
        for scratch in (lead, lead + ".json"):
            if os.path.exists(scratch):
                os.remove(scratch)

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
    # -f: variable-depth leaves are written WITHOUT polygon cleaning by design (tippecanoe
    # README; the Worker cleans on serve), and tile-scale rounding can flip a clipped sliver
    # ring's sign — strict decode hard-exits on that (EXIT_IMPOSSIBLE, "Polygon begins with an
    # inner ring"), failing completeness over geometry this check never asserts. Forced decode
    # emits the identical id stream; a nonzero exit still means a genuinely unreadable archive.
    p = subprocess.Popen(["tippecanoe-decode", "-f", vec], stdout=subprocess.PIPE, text=True)
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
    displaced nothing), and the partition conserved down-zoom (no interior blob of a parent's m-band
    union uncovered by its 4 children — a hole opened deeper; whole missing polygons are already
    caught above). Geometric comparisons clip to the unbuffered tile extent and erode before
    differencing — see the DEPARE_*_PX2 calibration note."""
    import mercantile
    from pmtiles.reader import Reader, MmapSource, all_tiles
    from shapely.geometry import shape, box
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

    def tile_box(z, x, y):
        b = mercantile.bounds(x, y, z)
        return box(b.west, b.south, b.east, b.north)

    def mband_union(feats, clip):
        geoms = [shape(f["geometry"]).buffer(0) for f in _mbands(feats)]
        return unary_union(geoms).intersection(clip) if geoms else None

    def blob_px2(geom, px):
        """Largest single part of `geom`, in px² of the given grid unit — blobs, not ribbons."""
        parts = getattr(geom, "geoms", [geom])
        return max((p.area for p in parts if not p.is_empty), default=0.0) / px ** 2

    for z in sorted(byz):
        ts = byz[z]
        px = _tile_grid_deg(z)
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
                clip = tile_box(z, x, y)
                eroded = [g for g in (shape(f["geometry"]).buffer(0).intersection(clip)
                                      .buffer(-1.5 * px) for f in mbands) if not g.is_empty]
                for i in range(len(eroded)):
                    for j in range(i + 1, len(eroded)):
                        if not eroded[i].intersects(eroded[j]):
                            continue
                        ov = blob_px2(eroded[i].intersection(eroded[j]), px)
                        if ov >= DEPARE_OVERLAP_MAX_PX2:
                            problems.append(f"z{z} {x}/{y}: depare m-bands overlap in a {ov:.0f} px² "
                                            f"blob (coalesce displaced a partition)")
                # partition conserved into z+1: every interior blob of the parent's band union must
                # be covered by its 4 children's — an uncovered blob is a hole (gap) opened deeper.
                kids = [(2 * x, 2 * y), (2 * x + 1, 2 * y), (2 * x, 2 * y + 1), (2 * x + 1, 2 * y + 1)]
                if z + 1 <= maxz and all(k in present.get(z + 1, ()) for k in kids):
                    childpx = _tile_grid_deg(z + 1)
                    pu = mband_union(mbands, clip)
                    resid = pu.buffer(-DEPARE_ERODE_PX * childpx) if pu is not None else None
                    if resid is not None and not resid.is_empty:
                        ku = [g for g in (mband_union(_decode_tile(vec, z + 1, kx, ky).get("depare", []),
                                                      clip) for kx, ky in kids) if g is not None]
                        if ku:
                            resid = resid.difference(unary_union(ku))
                        hole = blob_px2(resid, childpx)
                        if hole >= DEPARE_GAP_MAX_PX2:
                            problems.append(f"z{z} {x}/{y}: depare band coverage lost in a "
                                            f"{hole:.0f} child-px² blob (gap opened deeper)")
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
    # a coarse root (shallower than the split) is its own cell, verbatim
    assert list(vector_covering_cells([f"{max(sz - 2, 0)}-3-3-{sz}"])) == [f"{max(sz - 2, 0)}-3-3"]
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
