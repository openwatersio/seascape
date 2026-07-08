"""Depth-area polygons (ENC DEPARE) as a fork off each aggregation tile's merged DEM.

The vector twin of the raster depth shading — the ENC model, where DEPARE partitions the
whole water surface. Three feature kinds share the one `depare` layer, distinguished by
their attributes (the fill just switches on them), so bands, drying, and unknown-depth
water compose without extra layers or archives:

  1. depth bands — water partitioned into ranges between the charted isobath levels, each
     polygon carrying its range as drval1/drval2 (ENC DEPARE, positive-down metres) and a
     `sys` tag (m ladder / ft fathom curves). drval1 >= 0.
  2. drying — the green foreshore folds in as DEPARE with a NEGATIVE drval1 (ENC-true:
     drying = DEPARE, DRVAL1 < 0): drval1 = -DRYING_CAP, drval2 = 0 (one band for now).
     drying_run already builds these off the same DEM + land mask, grow/overhang and all,
     so this reads that FGB rather than recompute.
  3. nodata — water OSM maps as a polygon but the DEM holds no depth for (a #24-cleared
     lake, unsurveyed margins beside a fairway). NO drval1/drval2 — absence IS the encoding
     (MVT has no null; the fill's "no drval1" case renders S-52's NODATA fill) — plus a
     `kind` passthrough of the Overture subtype (river/lake/canal/reservoir).

The three are pairwise disjoint by construction — bands come from the DEM's water pixels,
nodata is the mapped water MINUS that coverage (and minus drying), so style ordering is
irrelevant to them. The one exception is drying's grow/overhang, which deliberately
overlaps the shoal band (see drying_run's "close the gap"): that overlap is ACCEPTED and
resolved at render time by a `rank` sort attribute (a fill-sort-key draws higher rank on
top). Ranks: nodata 0 < bands 1 < drying 2 — drying paints over the bands it overhangs,
and bands win over nodata at any incidental simplification-wobble edge (data over no-data).

sys multiplexing: the bands duplicate per sys (the ladders differ), but drying and nodata
are unit-independent, so they ship ONCE with NO `sys` — the style filters them by drval
semantics (drval1 < 0 / no drval1), not by sys, and showing them in both m and ft modes
needs no duplication. Halves their feature bytes vs a per-sys copy for identical pixels.

gdal_contour -p buckets the same merged, smoothed DEM the contour lines trace, with the
same contour generator — band edges and contour lines coincide by construction. Geometry
stays raw — no Chaikin, no shapely simplify: adjacent partitions share edges, and
per-feature smoothing treats the shared chain differently in each polygon, opening
see-through cracks between bands. tippecanoe --detect-shared-borders simplifies shared
borders identically per zoom instead. (Shoal band edges still match the drawn contour
lines, which skip Chaikin in the navigable band; deep lines smooth away from the raw
edge by design — an invisible sliver between near-white deep tints.)

Per tile: bands (gdal_contour -p at DEPARE_LEVELS / DEPARE_LEVELS_FT, drop land, drval/sys)
+ drying (the tile's drying FGB, drval1 < 0) + nodata (inland-water polygons minus the DEM's
water coverage) -> clip to the unbuffered tile bbox in shapely (polygon-only by construction,
see drying_run) -> 4326 -> store/depare/{stem}.fgb. Same seam contract as contours/drying:
deterministic on the buffered grid, so neighbouring tiles' features abut exactly at the clip
line. bundle() tippecanoes them into a `depare` layer pmtiles (or a per-shard slice in CI);
the contours tile-join folds it into vector.pmtiles like soundings.
"""

import os
import subprocess
import sys
from glob import glob

import mercantile
import rasterio

import config
import contour_run
import drying_run
import utils

# The bands' zoom floor (matches the style's depth-areas/contour-lines minzoom):
# partitions can't be level-thinned per zoom like the lines' CONTOUR_TIERS (dropping
# one leaves a hole), so the floor is the low-zoom cost control — the raster depth
# shading carries z<6.
MIN_ZOOM = 6

# Fill draw order for the `rank` sort attribute (a style fill-sort-key draws higher on top).
# Bands and nodata are disjoint, so their order is cosmetic; drying's grow/overhang overlaps
# the shoal band by design and must paint over it, so drying is highest. Bands over nodata is
# the safe choice at any incidental edge overlap — real depth shows through, not "no data".
NODATA_RANK = 0
BAND_RANK = 1
DRYING_RANK = 2

# nodata slivers: the Overture water outline (vector) and the depth-band edge (raster, pixel-
# staircased) never coincide, so `water minus coverage` leaves crumbs along every shared
# boundary. Drop any nodata polygon smaller than this many DEM pixels — it clears the staircase/
# registration noise while genuine unknown-depth water (a whole cleared lake, a canal margin
# beside a surveyed fairway) is orders of magnitude larger and survives. A long thin ribbon where
# the outline and DEM shoreline genuinely disagree by several pixels is kept (it IS water with no
# depth) — the ceiling Part 3 accepts. Env-tunable on a re-bundle.
NODATA_MIN_PX = float(os.environ.get("NODATA_MIN_PX", "64"))


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
    # Water = amax <= 0, NOT amin < 0: GDAL 3.8's polygon mode writes a garbage amin
    # (0) on the deepest bucket, which then read as land and vanished — amax is correct
    # on every version. drval2 off a buggy amin still trips _check's drval1 < drval2.
    g = g[g["amax"] <= 0].copy()
    g["drval1"] = 0.0 - g["amax"]  # 0.0 - keeps the shoalest bound 0.0, not -0.0
    g["drval2"] = 0.0 - g["amin"]
    return g


def generate(filepath):
    import geopandas as gpd
    import landmask
    from shapely import make_valid
    from shapely.geometry import box
    from shapely.ops import unary_union

    agg_id, filename = filepath.split("/")[-2:]
    z, x, y, child_z = (int(a) for a in filename.replace("-aggregation.csv", "").split("-"))
    tile = mercantile.Tile(x=x, y=y, z=z)
    tmp = f"store/aggregation/{agg_id}/{z}-{x}-{y}-{child_z}-tmp"
    stem = f"{z}-{x}-{y}-{child_z}"
    dem = f"{tmp}/{len(glob(f'{tmp}/*.tiff')) - 1}-3857.tiff"
    if not os.path.exists(dem):
        print(f"depare: no merged DEM for {filename}")
        return

    clip = box(*mercantile.xy_bounds(tile))  # unbuffered, tile-aligned (EPSG:3857)
    rows = []

    # ── depth bands ── the metre + fathom partition ladders, each tagged sys, clipped in shapely
    # (not ogr2ogr -clipsrc — the GDAL-3.8 GeometryCollection trap, see drying_run). The pre-clip
    # (buffered) union is the water-coverage footprint the nodata pass subtracts; both ladders
    # cover the same water pixels, so the metre union stands for it.
    coverage_geoms = []
    for sys_tag, levels in (("m", config.DEPARE_LEVELS), ("ft", config.DEPARE_LEVELS_FT)):
        g = partitions(dem, levels, f"{tmp}/depare-raw-{sys_tag}.fgb")
        if not len(g):
            continue
        if sys_tag == "m":
            coverage_geoms = list(g.geometry)
        for r in g.itertuples():
            for p in drying_run._polys(make_valid(r.geometry).intersection(clip)):
                rows.append({"geometry": p, "drval1": r.drval1, "drval2": r.drval2,
                             "sys": sys_tag, "rank": BAND_RANK})
    coverage = unary_union(coverage_geoms) if coverage_geoms else None

    # ── drying ── fold the tile's foreshore in as DEPARE with a negative drval1. drying_run.generate
    # produced these off the same DEM + land mask earlier this run (grow/overhang included), so read
    # that FGB and reproject to the DEM's CRS. No sys (unit-independent); a higher rank so the grown
    # overhang paints over the shoal band it overlaps. Its full grown footprint is also subtracted
    # from nodata below, so nodata stays disjoint from it.
    drying_fgb = f"store/drying/{stem}.fgb"
    drying_area = None
    if os.path.isfile(drying_fgb):
        dry = gpd.read_file(drying_fgb)
        if len(dry):
            dry = dry.to_crs("EPSG:3857")
            drying_area = unary_union(list(dry.geometry))
            for geom in dry.geometry:
                for p in drying_run._polys(make_valid(geom)):  # already clipped to the bbox in drying_run
                    rows.append({"geometry": p, "drval1": -config.DRYING_CAP, "drval2": 0.0,
                                 "sys": None, "rank": DRYING_RANK})

    # ── nodata ── inland water we hold no depth for: the OSM water polygons (bbox-read, clipped to
    # the buffered tile) MINUS the water-coverage footprint (depth bands ∪ the grown drying) — a #24-
    # cleared lake produces no band, so its whole polygon survives; a surveyed lake nets to slivers
    # the min-area filter drops. No drval (absence is the encoding) + a `kind` passthrough. Ocean has
    # no water polygon, so it gains nothing. Skipped cleanly when no water feed is published.
    water_src = landmask.water_path()
    if landmask._present(water_src):
        with rasterio.open(dem) as d:
            b, res = d.bounds, abs(d.transform.a)
        buffered = box(b.left, b.bottom, b.right, b.top)
        subtract = [g for g in (coverage, drying_area) if g is not None]
        subtract = unary_union(subtract) if subtract else None
        min_area = NODATA_MIN_PX * res * res
        water = gpd.read_file(water_src, bbox=(b.left, b.bottom, b.right, b.top))
        if len(water):
            water = water.to_crs("EPSG:3857")
            for r in water.itertuples():
                geom = make_valid(r.geometry).intersection(buffered)
                if subtract is not None:
                    geom = geom.difference(subtract)
                geom = geom.intersection(clip)  # same seam contract as the bands
                kind = getattr(r, "kind", None)
                for p in drying_run._polys(geom):
                    if p.area >= min_area:
                        rows.append({"geometry": p, "drval1": None, "drval2": None,
                                     "sys": None, "kind": kind, "rank": NODATA_RANK})

    if not rows:
        print(f"depare: no water in tile bbox for {filename}")
        return

    utils.create_folder("store/depare")  # tile-keyed, so clean tiles persist across incremental runs
    out = f"store/depare/{stem}.fgb"
    # A mixed schema across the three kinds: a row omits a key it doesn't carry, geopandas writes
    # the gap as NULL (NaN for float drval, None for str sys/kind), and FlatGeobuf -> tippecanoe
    # encode that as an ABSENT MVT property — so nodata truly has no drval1, the fill's switch key.
    gpd.GeoDataFrame(rows, crs="EPSG:3857").to_crs("EPSG:4326").to_file(out, driver="FlatGeobuf")
    print(f"depare: {filename} -> {len(rows)} polygons")


# ── bundle ───────────────────────────────────────────────────────────────────

def bundle(shard=None):
    """tippecanoe the per-tile depare FGBs into store/bundle/depare.pmtiles (layer `depare`,
    folded into vector.pmtiles by the contours tile-join, same as soundings), z
    MIN_ZOOM..shared maxz (see contour_run.bundle_maxz). With a shard index →
    depare-shard-{shard}.pmtiles from this shard's local slice, like the other vector
    layers. --detect-shared-borders keeps shared partition edges identical through
    per-zoom simplification (no cracks between bands); --coalesce-smallest-as-needed,
    never --drop-densest: a dropped partition is a tint hole, a coalesced one mis-tints
    a sub-pixel blob. Orphan filter as contours.

    Attributes: drval1/drval2 (Real -> numeric MVT; absent on nodata features, which is the
    fill's switch key), sys (bands only), kind (nodata only), rank (all — sort key). rank is
    FlatGeobuf Integer64, so -T rank:int keeps it numeric in the MVT (else it lands as a string,
    like the contour depth ints)."""
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
         "-y", "drval1", "-y", "drval2", "-y", "sys", "-y", "kind", "-y", "rank",
         "-T", "rank:int", *fgbs],
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

    # #24 invariant: a uniform-0 DEM (a cleared lake the merge filled to 0) yields NO band, so
    # the whole water polygon survives as nodata in generate(); a shoal (-0.5) still buckets.
    flat = np.zeros((h, w), dtype="float32")
    fp = f"{d}/flat.tif"
    with rasterio.open(fp, "w", driver="GTiff", height=h, width=w, count=1, dtype="float32",
                       nodata=-9999, crs="EPSG:3857", transform=tr) as dst:
        dst.write(flat, 1)
    assert len(partitions(fp, config.DEPARE_LEVELS, f"{d}/flat-raw.fgb")) == 0, \
        "a uniform-0 lake must produce no depth band (so it becomes nodata, not a shoal tint)"
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
