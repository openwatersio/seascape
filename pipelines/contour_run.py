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


def generate(filepath):
    agg_id, filename = filepath.split("/")[-2:]
    z, x, y, child_z = (int(a) for a in filename.replace("-aggregation.csv", "").split("-"))
    tile = mercantile.Tile(x=x, y=y, z=z)
    tmp = f"store/aggregation/{agg_id}/{z}-{x}-{y}-{child_z}-tmp"
    name = f"{z}-{x}-{y}-{child_z}"
    dem = f"{tmp}/{len(glob(f'{tmp}/*.tiff')) - 1}-3857.tiff"
    if not os.path.exists(dem):
        print(f"contour: no merged DEM for {filename}")
        return

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
        print(f"contour: no ocean features for {filename}")
        return

    smoothed = f"{tmp}/contour-smooth.fgb"
    smooth_and_enrich(sources, smoothed, tol=get_resolution(child_z))

    b = mercantile.xy_bounds(tile)  # unbuffered, tile-aligned (EPSG:3857)
    clipped = f"{tmp}/contour-clip.fgb"
    _run(f"ogr2ogr -f FlatGeobuf -overwrite -nlt PROMOTE_TO_MULTI "
         f"-clipsrc {b.left} {b.bottom} {b.right} {b.top} {clipped} {smoothed}", "ogr2ogr clip")
    if feature_count(clipped) == 0:
        print(f"contour: no features in tile bbox for {filename}")
        return

    # Tile-keyed (not agg_id-scoped) so clean tiles' contours persist across
    # incremental runs, exactly like store/pmtiles — bundle() globs them all.
    utils.create_folder("store/contour")
    out = f"store/contour/{name}.fgb"
    _run(f"ogr2ogr -f FlatGeobuf -overwrite -nlt PROMOTE_TO_MULTI -t_srs EPSG:4326 {out} {clipped}",
         "ogr2ogr reproject")
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
        bands.append(["all", [">=", "$zoom", lo], ["<", "$zoom", hi], ["==", "sys", "m"], ["in", "depth_m", *depths]])
        bands.append(["all", [">=", "$zoom", lo], ["<", "$zoom", hi], ["==", "sys", "ft"], [">=", "depth_abs_m", min_abs]])
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
         "-j", PER_ZOOM_FILTER, *fgbs],
        check=True)


def _global_maxz(fgbs):
    return max(int(f.split("/")[-1].replace(".fgb", "").split("-")[3]) for f in fgbs)


def _stems_maxz(stems):
    """Max child_z across covering stems ({z}-{x}-{y}-{child_z})."""
    return max(int(s.rsplit("-", 1)[1]) for s in stems)


def bundle_maxz(own_max):
    """The tileset maxzoom EVERY vector layer bundles to (contours, soundings,
    drying). They tile-join into one vector.pmtiles, whose maxzoom is the max
    across layers — a layer bundled only to its own regional max silently
    vanishes from deeper tiles (drying stopped at z11 while contours ran to
    z14, so the Æbelø flats rendered as bare land above z11). Use the shared
    global contour maxz (store/contour-maxz.txt, written by the CI shard jobs)
    when present, else the current covering's max child_z, else the caller's
    own files' max."""
    maxzfile = "store/contour-maxz.txt"
    if os.path.isfile(maxzfile):
        with open(maxzfile) as f:
            return max(int(f.read().strip()), own_max)
    stems = _current_stems()
    if stems:
        return max(own_max, _stems_maxz(stems))
    return own_max


# ── source coverage (provenance) layer ───────────────────────────────────────
# Tile each source's union footprint (store/polygon/<id>.gpkg, from the source stage)
# into a `coverage` layer alongside `contours` in the same pmtiles, so the viewer can
# show which source covers a clicked point — ROADMAP Milestone 5.3, "tile straight from
# the coverage polygons." GEBCO (the global base) declares no max_zoom, so it's skipped;
# only regional footprints get a polygon.

BASE_SOURCE = "gebco"  # the global fallback; its footprint is the whole planet, never an overlay


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
    polygons are present locally (e.g. a CI contour job that only pulled FGB slices)."""
    valid = set(config.sources())
    zmax = _source_maxzooms()
    feats = []
    for gpkg in sorted(glob("store/polygon/*.gpkg")):
        sid = gpkg.split("/")[-1].replace(".gpkg", "")
        if sid not in valid or sid == BASE_SOURCE:  # orphan polygon, or the global base — skip
            continue
        meta = config.load_metadata(sid)
        out = subprocess.run(
            ["ogr2ogr", "-f", "GeoJSON", "/vsistdout/", gpkg,
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


def _coverage_pmtiles(maxz):
    """tippecanoe the source footprints into store/bundle/coverage.pmtiles (layer
    `coverage`, z0..maxz, footprints kept whole). None when no footprints are local."""
    src = _coverage_geojson()
    if not src:
        return None
    out = "store/contour/coverage.pmtiles"  # intermediate (not store/bundle, which seed.sh ships)
    subprocess.run(
        ["tippecanoe", "-o", out, "-f", "-l", "coverage", "-n", "Source coverage",
         "-A", utils.ATTRIBUTION, "-Z", "0", "-z", str(maxz), "-P", "-q",
         "--no-tile-size-limit", src], check=True)
    return out


def _finalize_contours(archives, maxz):
    """tile-join the layer archives (a local build's contour pmtiles, or the CI shards —
    which carry contours, soundings, AND drying slices) + the coverage layer + the
    whole-set soundings/drying pmtiles (local path, when their bundles ran first) into
    store/bundle/vector.pmtiles. ONE join: tile-join rewrites every tile of the whole
    archive, so folding each sparse layer in afterwards re-paid the planet-wide join
    per layer (~90 min each in CI). -pk keeps every feature of every layer; a layer
    whose pmtiles isn't present locally is simply not joined."""
    cov = _coverage_pmtiles(maxz)
    layers = [p for p in [cov, "store/bundle/soundings.pmtiles", "store/bundle/drying.pmtiles"]
              if p and os.path.isfile(p)]
    subprocess.run(["tile-join", "-o", "store/bundle/vector.pmtiles", "-f", "-pk",
                    *archives, *layers], check=True)
    return cov is not None


def _current_stems():
    """{z}-{x}-{y}-{child_z} of every tile in the newest covering — the only contour
    FGBs that belong in the bundle. None when no covering is present locally (the CI
    contour-shard job reads a pre-pruned R2 set without pulling the covering), so the
    caller falls back to every FGB it has."""
    ids = utils.get_aggregation_ids()
    if not ids:
        return None
    csvs = glob(f"store/aggregation/{ids[-1]}/*-aggregation.csv")
    return {c.split("/")[-1].replace("-aggregation.csv", "") for c in csvs} or None


def _live_fgbs(fgbs, stems):
    """Drop FGBs orphaned by a covering re-tiling. A source's footprint/maxzoom shift
    re-tiles its area (different z/x/y/child_z), but sync has no --delete, so the
    superseded FGB lingers; bundling it alongside the new tiling draws two overlapping
    contour sets. Keep only current-covering stems; keep all when stems is None."""
    if stems is None:
        return fgbs
    return [f for f in fgbs if f.split("/")[-1].replace(".fgb", "") in stems]


def bundle(shard=None):
    """tippecanoe the local contour FGBs into pmtiles. Whole set (vector.pmtiles)
    by default; with a shard index → contours-shard-{shard}.pmtiles, which
    bundle_merge() tile-joins (a single global tippecanoe blows the 6 h job cap at
    planet scale). The CI pulls only this shard's FGB slice and writes the GLOBAL maxz
    to store/contour-maxz.txt, so every shard tiles to the same depth and tile-joins
    cleanly; a local whole-set build derives maxz from the FGBs it has."""
    fgbs = _live_fgbs(sorted(glob("store/contour/*.fgb")), _current_stems())
    if not fgbs:
        print("contour bundle: no contour FGBs")
        return
    maxz = bundle_maxz(_global_maxz(fgbs))
    utils.create_folder("store/bundle")
    if shard is not None:  # CI: lines only; coverage joins once at the merge step
        out = f"store/bundle/contours-shard-{shard}.pmtiles"
        _tippecanoe(fgbs, 0, maxz, out)
        print(f"contour shard {shard}: {out} (z0-{maxz}, {len(fgbs)} FGBs)")
        return
    # Local whole-set: tippecanoe the lines, then fold in the coverage layer.
    lines = "store/contour/contours-lines.pmtiles"
    _tippecanoe(fgbs, 0, maxz, lines)
    cov = _finalize_contours([lines], maxz)
    os.remove(lines)
    print(f"contour bundle: store/bundle/vector.pmtiles (z0-{maxz}, {len(fgbs)} FGBs"
          f"{', + coverage layer' if cov else ''})")


def bundle_merge():
    """tile-join the per-shard pmtiles — contours, soundings, drying (each shard job
    bundles its slice of all three) — + the coverage layer into one vector.pmtiles
    (-pk keeps every feature; the shards are disjoint file slices unioned per tile)."""
    shards = sorted(glob("store/bundle/*-shard-*.pmtiles"))
    if not shards:
        print("contour merge: no shard pmtiles")
        return
    maxzfile = "store/contour-maxz.txt"  # the CI shard path always writes this (shared global maxz)
    if not os.path.isfile(maxzfile):
        raise SystemExit("contour merge: store/contour-maxz.txt missing (the shard jobs write it)")
    cov = _finalize_contours(shards, int(open(maxzfile).read().strip()))
    print(f"contour merge: store/bundle/vector.pmtiles ({len(shards)} shard archives"
          f"{', + coverage layer' if cov else ''})")


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
    # orphan-FGB filter: keep only current-covering stems; passthrough when stems is None
    fgbs = ["store/contour/4-5-6-8.fgb", "store/contour/11-300-400-13.fgb"]
    assert _live_fgbs(fgbs, {"11-300-400-13"}) == ["store/contour/11-300-400-13.fgb"]
    assert _live_fgbs(fgbs, None) == fgbs
    # the shared bundle maxzoom reads child_z off covering stems
    assert _stems_maxz({"4-5-6-8", "11-300-400-13"}) == 13
    # navigable-band contours skip Chaikin (never bow a shoal deeper); deeper ones smooth
    assert _chaikin_iters(10) == 0 and _chaikin_iters(NAV_SMOOTH_MAX_M) == 0
    assert _chaikin_iters(NAV_SMOOTH_MAX_M + 1) == CHAIKIN_ITERATIONS
    # metre and feet/fathom isobaths each get their own per-zoom bands in the tippecanoe filter
    assert '["==", "sys", "m"]' in PER_ZOOM_FILTER and '["==", "sys", "ft"]' in PER_ZOOM_FILTER
    # every tier level must exist in the generated contour set (else it filters to nothing)
    assert all(d in config.CONTOUR_LEVELS for _, depths in CONTOUR_TIERS for d in depths)
    print("contour_run ring-drop + orphan-filter self-check ok")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["bundle"]:
        bundle()
    elif a[:1] == ["bundle-shard"]:
        bundle(int(a[1]))
    elif a[:1] == ["bundle-merge"]:
        bundle_merge()
    elif a[:1] == ["check"]:
        _check()
    else:
        sys.exit("usage: contour_run.py bundle | bundle-shard <i> | bundle-merge | check")
