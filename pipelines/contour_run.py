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

SKIP_CONTOUR_SMOOTH = os.environ.get("SKIP_CONTOUR_SMOOTH", "")
CHAIKIN_ITERATIONS = 5
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


def smooth_and_enrich(in_fgb, out_fgb, tol, smooth):
    """Chaikin-smooth (in 3857), drop deep micro-loop stipple, add depth_abs_m."""
    import geopandas as gpd
    gdf = gpd.read_file(in_fgb)
    if smooth:
        gdf["geometry"] = [_smooth_geom(g, tol, CHAIKIN_ITERATIONS) for g in gdf.geometry]
    gdf["depth_abs_m"] = (-gdf["depth_m"]).round().astype(int)
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
    _run(f"gdal_contour -q -fl {levels} -a depth_m -f FlatGeobuf {dem} {raw}", "gdal_contour")
    if feature_count(raw) == 0:
        print(f"contour: no ocean features for {filename}")
        return

    smoothed = f"{tmp}/contour-smooth.fgb"
    smooth_and_enrich(raw, smoothed, tol=get_resolution(child_z), smooth=not SKIP_CONTOUR_SMOOTH)

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
# every level shows. Mirrors the viewer's CONTOUR_TIERS in index.js.
CONTOUR_TIERS = [
    (5, [-200, -1000, -2000, -4000]),
    (7, [-200, -500, -1000, -2000, -3000, -4000, -5000, -6000, -8000, -10000]),
    (9, [-50, -100, -150, -200, -500, -1000, -1500, -2000, -3000, -4000, -5000, -6000, -8000, -10000]),
    (11, [-10, -20, -30, -40, -50, -75, -100, -150, -200, -500, -1000, -1500, -2000, -3000, -4000, -5000, -6000, -8000, -10000]),
]


def _per_zoom_filter():
    """Build tippecanoe's -j filter (exclusive $zoom bands) from CONTOUR_TIERS."""
    bands, lo = [], 0
    for hi, depths in CONTOUR_TIERS:
        bands.append(["all", [">=", "$zoom", lo], ["<", "$zoom", hi], ["in", "depth_m", *depths]])
        lo = hi
    bands.append(["all", [">=", "$zoom", lo]])  # native+ zoom: every level
    return json.dumps({"*": ["any", *bands]})


PER_ZOOM_FILTER = _per_zoom_filter()


def _tippecanoe(fgbs, minz, maxz, out):
    subprocess.run(
        ["tippecanoe", "-o", out, "-f", "-l", "contours",
         "-n", "Bathymetric contours", "-A", utils.ATTRIBUTION,
         "-Z", str(minz), "-z", str(maxz), "-P", "-q", "--drop-densest-as-needed",
         "--simplification", "4",  # geometric vertex thinning at low zoom (full detail at maxz)
         "-y", "depth_m", "-y", "depth_abs_m", "-j", PER_ZOOM_FILTER, *fgbs],
        check=True)


def _global_maxz(fgbs):
    return max(int(f.split("/")[-1].replace(".fgb", "").split("-")[3]) for f in fgbs)


def bundle(shard=None):
    """tippecanoe the local contour FGBs into pmtiles. Whole set (contours.pmtiles)
    by default; with a shard index → contours-shard-{shard}.pmtiles, which
    bundle_merge() tile-joins (a single global tippecanoe blows the 6 h job cap at
    planet scale). The CI pulls only this shard's FGB slice and writes the GLOBAL maxz
    to store/contour-maxz.txt, so every shard tiles to the same depth and tile-joins
    cleanly; a local whole-set build derives maxz from the FGBs it has."""
    fgbs = sorted(glob("store/contour/*.fgb"))
    if not fgbs:
        print("contour bundle: no contour FGBs")
        return
    maxzfile = "store/contour-maxz.txt"
    maxz = int(open(maxzfile).read().strip()) if os.path.isfile(maxzfile) else _global_maxz(fgbs)
    utils.create_folder("store/bundle")
    out = "store/bundle/contours.pmtiles" if shard is None else f"store/bundle/contours-shard-{shard}.pmtiles"
    _tippecanoe(fgbs, 0, maxz, out)
    print(f"contour {'bundle' if shard is None else f'shard {shard}'}: {out} (z0-{maxz}, {len(fgbs)} FGBs)")


def bundle_merge():
    """tile-join the per-shard contour pmtiles into one contours.pmtiles (-pk keeps
    every feature; the shards are disjoint FGB slices unioned per tile)."""
    shards = sorted(glob("store/bundle/contours-shard-*.pmtiles"))
    if not shards:
        print("contour merge: no shard pmtiles")
        return
    subprocess.run(["tile-join", "-o", "store/bundle/contours.pmtiles", "-f", "-pk", *shards], check=True)
    print(f"contour merge: store/bundle/contours.pmtiles ({len(shards)} shards)")


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
    print("contour_run ring-drop self-check ok")


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
