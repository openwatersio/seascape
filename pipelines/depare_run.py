"""Depth-area polygons (ENC DEPARE) as a fork off each aggregation tile's merged DEM.

The vector twin of the raster depth shading: water partitioned into bands between the
charted isobath levels, each polygon carrying its depth range as drval1/drval2 (ENC
DEPARE semantics, positive-down metres). A plain fill layer tints them — crisp band
edges at any zoom, and client-side safety recolouring that snaps to the next-deeper
charted level (the ECDIS model) with ordinary expressions.

gdal_contour -p buckets the same merged, smoothed DEM the contour lines trace, with the
same contour generator — band edges and contour lines coincide by construction. Two
bucket sets, like the two contour sets: the metre ladder and the fathom curves (sys=ft).
Partitions, not nested "deeper than" bands: exactly one polygon covers each water pixel,
so a translucent fill composites once and the tint stays exact.

Geometry stays raw — no Chaikin, no shapely simplify: adjacent partitions share edges,
and per-feature smoothing treats the shared chain differently in each polygon, opening
see-through cracks between bands. tippecanoe --detect-shared-borders simplifies shared
borders identically per zoom instead. (Shoal band edges still match the drawn contour
lines, which skip Chaikin in the navigable band; deep lines smooth away from the raw
edge by design — an invisible sliver between near-white deep tints.)

Per tile: gdal_contour -p at DEPARE_LEVELS / DEPARE_LEVELS_FT -> drop land buckets
(amin >= 0) -> drval1/drval2/sys -> clip to the unbuffered tile bbox in shapely
(polygon-only by construction, see drying_run) -> 4326 -> store/depare/{stem}.fgb.
Same seam contract as contours/drying: deterministic on the buffered grid, so
neighbouring tiles' partitions abut exactly at the clip line — invisible to fills,
which never stroke their boundary. bundle() tippecanoes them into a `depare` layer
pmtiles (or a per-shard slice in CI); the contours tile-join folds it into
vector.pmtiles like soundings/drying.
"""

import os
import subprocess
import sys
from glob import glob

import mercantile

import config
import contour_run
import drying_run
import utils

# The bands' zoom floor (matches the style's depth-areas/contour-lines minzoom):
# partitions can't be level-thinned per zoom like the lines' CONTOUR_TIERS (dropping
# one leaves a hole), so the floor is the low-zoom cost control — the raster depth
# shading carries z<6.
MIN_ZOOM = 6


def partitions(dem, levels, raw_fgb):
    """Water partitions off `dem`: gdal_contour -p buckets between `levels` -> drop land
    (amin >= 0; 0 is always a level, so land is exactly the buckets above it) -> add
    drval1/drval2 (ENC: shallow/deep bound, positive-down metres). GeoDataFrame in the
    DEM's CRS."""
    import geopandas as gpd
    fl = " ".join(str(l) for l in levels)
    contour_run._run(
        f"gdal_contour -q -p -amin amin -amax amax -fl {fl} -f FlatGeobuf {dem} {raw_fgb}",
        "gdal_contour -p")
    g = gpd.read_file(raw_fgb)
    g = g[g["amin"] < 0].copy()
    g["drval1"] = 0.0 - g["amax"]  # 0.0 - keeps the shoalest bound 0.0, not -0.0
    g["drval2"] = 0.0 - g["amin"]
    return g


def generate(filepath):
    import geopandas as gpd
    import pandas as pd
    from shapely import make_valid
    from shapely.geometry import box

    agg_id, filename = filepath.split("/")[-2:]
    z, x, y, child_z = (int(a) for a in filename.replace("-aggregation.csv", "").split("-"))
    tile = mercantile.Tile(x=x, y=y, z=z)
    tmp = f"store/aggregation/{agg_id}/{z}-{x}-{y}-{child_z}-tmp"
    stem = f"{z}-{x}-{y}-{child_z}"
    dem = f"{tmp}/{len(glob(f'{tmp}/*.tiff')) - 1}-3857.tiff"
    if not os.path.exists(dem):
        print(f"depare: no merged DEM for {filename}")
        return

    parts = []
    for sys_tag, levels in (("m", config.DEPARE_LEVELS), ("ft", config.DEPARE_LEVELS_FT)):
        g = partitions(dem, levels, f"{tmp}/depare-raw-{sys_tag}.fgb")
        if len(g):
            g["sys"] = sys_tag
            parts.append(g)
    if not parts:
        print(f"depare: no water for {filename}")
        return
    gdf = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=parts[0].crs)

    # Clip in shapely, not ogr2ogr -clipsrc (the GDAL-3.8 GeometryCollection trap — see
    # drying_run): make_valid + intersect + keep polygonal parts only, so the layer is
    # uniformly polygon by construction.
    clip = box(*mercantile.xy_bounds(tile))  # unbuffered, tile-aligned (EPSG:3857)
    rows = [{"geometry": p, "drval1": r.drval1, "drval2": r.drval2, "sys": r.sys}
            for r in gdf.itertuples()
            for p in drying_run._polys(make_valid(r.geometry).intersection(clip))]
    if not rows:
        print(f"depare: no water in tile bbox for {filename}")
        return

    utils.create_folder("store/depare")  # tile-keyed, so clean tiles persist across incremental runs
    out = f"store/depare/{stem}.fgb"
    gpd.GeoDataFrame(rows, crs="EPSG:3857").to_crs("EPSG:4326").to_file(out, driver="FlatGeobuf")
    print(f"depare: {filename} -> {len(rows)} polygons")


# ── bundle ───────────────────────────────────────────────────────────────────

def bundle(shard=None):
    """tippecanoe the per-tile depare FGBs into store/bundle/depare.pmtiles (layer `depare`,
    folded into vector.pmtiles by the contours tile-join, same as soundings/drying), z
    MIN_ZOOM..shared maxz (see contour_run.bundle_maxz). With a shard index →
    depare-shard-{shard}.pmtiles from this shard's local slice, like the other vector
    layers. --detect-shared-borders keeps shared partition edges identical through
    per-zoom simplification (no cracks between bands); --coalesce-smallest-as-needed,
    never --drop-densest: a dropped partition is a tint hole, a coalesced one mis-tints
    a sub-pixel blob. Orphan filter as contours/drying."""
    fgbs = contour_run._live_fgbs(sorted(glob("store/depare/*.fgb")), contour_run._current_stems())
    if not fgbs:
        print("depare bundle: no depare FGBs")
        return
    maxz = contour_run.bundle_maxz(
        max(int(f.split("/")[-1].replace(".fgb", "").split("-")[3]) for f in fgbs))
    utils.create_folder("store/bundle")
    out = "store/bundle/depare.pmtiles" if shard is None \
        else f"store/bundle/depare-shard-{shard}.pmtiles"
    subprocess.run(
        ["tippecanoe", "-o", out, "-f", "-l", "depare",
         "-n", "Depth areas", "-A", utils.ATTRIBUTION,
         "-Z", str(MIN_ZOOM), "-z", str(maxz), "-P", "-q",
         "--detect-shared-borders", "--coalesce-smallest-as-needed",
         "--simplification", os.environ.get("DEPARE_SIMPLIFICATION", "8"),
         "-y", "drval1", "-y", "drval2", "-y", "sys", *fgbs],
        check=True)
    print(f"depare bundle: {out} (z{MIN_ZOOM}-{maxz}, {len(fgbs)} FGBs)")


def _check():
    """gdal_contour -p buckets on a synthetic DEM: land dropped, each depth lands in its
    ladder bucket (drval1/drval2 = the enclosing levels), partitions are pairwise disjoint
    and jointly cover the water (the fill contract: no holes, no overlaps), the level
    ladders ascend and end at 0 (gdal_contour -fl requires ascending; 0 closes the shoalest
    band), and the output is deterministic (the seam contract reduces to this)."""
    import tempfile

    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    from shapely.geometry import Point
    from shapely.ops import unary_union

    for levels in (config.DEPARE_LEVELS, config.DEPARE_LEVELS_FT):
        assert levels == sorted(levels) and levels[-1] == 0, "levels must ascend and end at 0"

    d = tempfile.mkdtemp()
    h = w = 50
    res = 100.0
    tr = from_origin(0, h * res, res, res)  # top-left origin, EPSG:3857
    # Land on top, then four water bands stepping deeper — each 10 rows, values chosen to
    # sit strictly inside a ladder bucket. Step transitions interpolate through the
    # intervening levels, so extra sliver partitions between bands are expected and fine.
    dem = np.full((h, w), 5.0, dtype="float32")
    for i, v in enumerate([-1.0, -7.0, -25.0, -150.0]):
        dem[(i + 1) * 10:(i + 2) * 10, :] = v
    p = f"{d}/dem.tif"
    with rasterio.open(p, "w", driver="GTiff", height=h, width=w, count=1, dtype="float32",
                       nodata=-9999, crs="EPSG:3857", transform=tr) as dst:
        dst.write(dem, 1)

    g = partitions(p, config.DEPARE_LEVELS, f"{d}/raw.fgb")
    assert len(g) and (g["drval1"] >= 0).all(), "land buckets must be dropped"
    assert (g["drval1"] < g["drval2"]).all(), "drval1 must be the shallow bound"

    def bucket_at(gdf, row):
        pt = Point(tr * (w / 2 + 0.5, row + 0.5))
        hit = gdf[gdf.covers(pt)]
        assert len(hit) == 1, f"exactly one partition must cover row {row}, got {len(hit)}"
        return (hit.iloc[0]["drval1"], hit.iloc[0]["drval2"])

    assert bucket_at(g, 15) == (0.0, 2.0), "-1 m must land in the [0,2] bucket"
    assert bucket_at(g, 25) == (5.0, 10.0), "-7 m must land in the [5,10] bucket"
    assert bucket_at(g, 35) == (20.0, 30.0), "-25 m must land in the [20,30] bucket"
    assert bucket_at(g, 45) == (100.0, 200.0), "-150 m must land in the [100,200] bucket"
    assert not g.covers(Point(tr * (w / 2 + 0.5, 5.5))).any(), "no partition may cover land"

    # The fill contract: pairwise disjoint (sum of areas == union area) and jointly
    # covering the water (union area == the water rows, ± the interpolated land edge).
    union = unary_union(list(g.geometry))
    assert abs(g.geometry.area.sum() - union.area) < 1e-6 * union.area, "partitions overlap"
    water = (h - 10) * w * res * res
    assert abs(union.area - water) < 1.5 * w * res * res, \
        f"partitions must tile the water ({union.area:.0f} vs {water:.0f})"

    # Fathom-curve set: -7 m sits between the 3 fm and 5 fm curves.
    gft = partitions(p, config.DEPARE_LEVELS_FT, f"{d}/raw-ft.fgb")
    d1, d2 = bucket_at(gft, 25)
    assert abs(d1 - 3 * 1.8288) < 1e-6 and abs(d2 - 5 * 1.8288) < 1e-6, (d1, d2)

    # Deterministic: same DEM -> byte-identical buckets.
    g2 = partitions(p, config.DEPARE_LEVELS, f"{d}/raw2.fgb")
    assert sorted(x.wkb for x in g.geometry) == sorted(x.wkb for x in g2.geometry), \
        "partitions not deterministic"
    print(f"depare_run self-check ok ({len(g)} m-partitions, {len(gft)} ft-partitions)")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["bundle"]:
        bundle()
    elif a[:1] == ["bundle-shard"]:
        bundle(int(a[1]))
    elif a[:1] == ["check"]:
        _check()
    else:
        sys.exit("usage: depare_run.py bundle | bundle-shard <i> | check")
