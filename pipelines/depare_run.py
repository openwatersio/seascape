"""Depth-area polygons (ENC DEPARE) as a fork off each aggregation tile's merged DEM.

The vector twin of the raster depth shading — the ENC model, where DEPARE partitions the
whole water surface. Three feature kinds share the one `depare` layer, distinguished by
their attributes (the fill just switches on them), so bands, drying, and unknown-depth
water compose without extra layers or archives:

  1. depth bands — water partitioned into ranges between the charted isobath levels, each
     polygon carrying its range as drval1/drval2 (ENC DEPARE, positive-down metres) and a
     `sys` tag (m ladder / ft fathom curves). drval1 >= 0.
  2. drying — the green foreshore, DEPARE with a NEGATIVE drval1 (ENC-true: drying = DEPARE,
     DRVAL1 < 0): drval1 = -DRYING_CAP, drval2 = 0 (one band for now). Derived from the SAME
     gdal_contour -p pass as the bands: the metre ladder carries DRYING_CAP as an extra
     positive level, so its [0, DRYING_CAP] bucket is the foreshore and shares its 0 m
     seaward edge with the shoal band's amax=0 edge — the same ring, so tippecanoe's
     --detect-shared-borders simplifies both identically with no crack. Cut to effective
     water (seaward of the OSM land line, but kept inside mapped inland water — the
     ICW/tidal-channel case), see generate().
  3. nodata — water OSM maps as a polygon but the DEM holds no depth for (a #24-cleared
     lake, unsurveyed margins beside a fairway). NO drval1/drval2 — absence IS the encoding
     (MVT has no null; the fill's "no drval1" case renders S-52's NODATA fill) — plus a
     `kind` passthrough of the Overture subtype (river/lake/canal/reservoir).

The three are pairwise disjoint by construction — bands are the DEM's water pixels
(amax <= 0), drying is the disjoint [0, DRYING_CAP] bucket ∩ effective water, and nodata is
the mapped water MINUS both — so style ordering is cosmetic. The `rank` sort attribute
survives only as a stable tie-breaker at an incidental simplification-wobble edge (nodata 0
< bands 1 < drying 2: real depth over no-data, foreshore over the shoal band it abuts).

sys multiplexing: the bands duplicate per sys (the ladders differ), but drying and nodata
are unit-independent, so they ship ONCE with NO `sys` — the style filters them by drval
semantics (drval1 < 0 / no drval1), not by sys, and showing them in both m and ft modes
needs no duplication. Halves their feature bytes vs a per-sys copy for identical pixels.
Drying rides the metre pass only (the cap is a metre level) and is emitted once.

gdal_contour -p buckets the same merged, smoothed DEM the contour lines trace, with the
same contour generator — band edges and contour lines coincide by construction. Geometry
stays raw — no Chaikin, no shapely simplify: adjacent partitions share edges, and
per-feature smoothing treats the shared chain differently in each polygon, opening
see-through cracks between bands. tippecanoe --detect-shared-borders simplifies shared
borders identically per zoom instead. (Shoal band edges still match the drawn contour
lines, which skip Chaikin in the navigable band; deep lines smooth away from the raw
edge by design — an invisible sliver between near-white deep tints.)

Per tile: bands (gdal_contour -p at DEPARE_LEVELS / DEPARE_LEVELS_FT, drop land, drval/sys)
+ drying (the metre ladder's [0, DRYING_CAP] bucket ∩ effective water, drval1 < 0) + nodata
(inland-water polygons minus the DEM's water coverage and the drying) -> clip to the
unbuffered tile bbox in shapely (polygon-only by construction, see _polys) -> 4326 ->
store/depare/{stem}.fgb. Same seam contract as contours: deterministic on the buffered grid,
so neighbouring tiles' features abut exactly at the clip line. bundle() tippecanoes them into
a `depare` layer pmtiles; the contours tile-join folds it into vector.pmtiles like soundings.
"""

import os
import subprocess
import sys

import mercantile
import rasterio

import config
import contour_run
import utils

# The bands' zoom floor (matches the style's depth-areas/contour-lines minzoom):
# partitions can't be level-thinned per zoom like the lines' CONTOUR_TIERS (dropping
# one leaves a hole), so the floor is the low-zoom cost control — the raster depth
# shading carries z<6.
MIN_ZOOM = 6

# DEPARE_LEVELS derives from CONTOUR_LEVELS, which the style hand-mirrors (style/index.ts
# DEPARE_LADDER_M/FT) — warn when an env override diverges the bands from the style. Upgrade
# path: generate the style constants from config instead of mirroring.
if config.CONTOUR_LEVELS != config.CONTOUR_LEVELS_DEFAULT:
    print("WARNING: CONTOUR_LEVELS overridden — depare bands will diverge from the style's "
          "hand-mirrored DEPARE_LADDER_M/FT (style/index.ts); update it to match.",
          file=sys.stderr)

# Fill draw order for the `rank` sort attribute (a style fill-sort-key draws higher on top).
# All three kinds are disjoint by construction, so rank is cosmetic — a stable tie-breaker at
# an incidental simplification-wobble edge: real depth (bands) over no-data (nodata), and
# drying over the shoal band it abuts along their shared 0 m seam.
NODATA_RANK = 0
BAND_RANK = 1
DRYING_RANK = 2

# Sliver filter: the vector edges (OSM water outline, effective-land cut) and the raster
# depth-band edge (pixel-staircased) never coincide exactly, so `water minus coverage` (nodata)
# and `bucket minus land` (drying) both leave crumbs along every near-coincident boundary. Drop
# any polygon smaller than this many DEM pixels to clear the registration noise. Sized from
# measurement, not a guess: over a whole ICW macrotile the crumbs top out at ~3 px (95% are thin,
# compactness < 0.2), so 4 px removes them all — while real intertidal flats and small unsurveyed
# ponds bottom out around 4 px and survive. The old 64 px was tuned for nodata lakes alone and
# culled ~1000 genuine small drying flats (only ~2% of the area, but most of the visible detail).
# Ceiling: a pure area gate can't tell a LONG thin ribbon (1 px × 100 px ≈ 100 px area) from a
# compact real flat; none occurred here (the difference yields corner fragments, not ribbons), but
# a width/compactness gate is the targeted tool if thin ribbons ever appear. Env-tunable.
SLIVER_MIN_PX = float(os.environ.get("SLIVER_MIN_PX", "4"))


def _polys(geom):
    """Every non-empty Polygon inside a geometry, recursing into Multi/GeometryCollection and
    dropping the line/point slivers a clip or make_valid can leave — so the output layer is
    uniformly polygon (FlatGeobuf rejects a mixed layer)."""
    t = geom.geom_type
    if t == "Polygon":
        return [] if geom.is_empty else [geom]
    if t in ("MultiPolygon", "GeometryCollection"):
        return [p for g in geom.geoms for p in _polys(g)]
    return []


def valid_union(geoms):
    """unary_union after make_valid per input. gdal_contour's polygon mode can emit a
    self-touching ring (GEOS raises "side location conflict" on a raw union of it); make_valid
    splits it into valid parts first. A no-op on already-valid geometry (bands are make_valid'd
    per feature; this guards the bucket / coverage / mask unions)."""
    from shapely import make_valid
    from shapely.ops import unary_union
    return unary_union([make_valid(g) for g in geoms])


def partitions(dem, levels, raw_fgb):
    """Water/foreshore partitions off `dem`: gdal_contour -p buckets the DEM between `levels`,
    tagging each bucket its range amin/amax -> drval1/drval2 (ENC: shallow/deep bound,
    positive-down metres). Returns the full bucketed GeoDataFrame in the DEM's CRS; the caller
    selects depth bands (amax <= 0), the [0, DRYING_CAP] drying bucket (0 < amax <= cap), and
    drops land (amax above the shallowest positive level)."""
    import geopandas as gpd
    fl = " ".join(str(l) for l in levels)
    contour_run._run(
        f"gdal_contour -q -p -amin amin -amax amax -fl {fl} -f FlatGeobuf {dem} {raw_fgb}",
        "gdal_contour -p")
    g = gpd.read_file(raw_fgb)
    # Select on amax, NOT amin: GDAL 3.8's polygon mode writes a garbage amin (0) on the
    # deepest bucket, which then read as land and vanished — amax is correct on every version.
    # So drval1 keys off amax; drval2 (off amin) is right for the interior bands but unreliable
    # on that deepest bucket, and the drying emit uses a literal drval2 = 0 anyway.
    g["drval1"] = 0.0 - g["amax"]  # 0.0 - keeps the shoalest bound 0.0, not -0.0
    g["drval2"] = 0.0 - g["amin"]
    return g


def _depare_dem(dem, tile_obj, tmp, label):
    """Partition any DEM covering the tile's buffered extent into depth-area / drying / nodata
    rows. Returns (final_path, count) inside ``tmp``, or None when there is no water."""
    import geopandas as gpd
    import landmask
    from shapely import make_valid
    from shapely.geometry import box

    clip = box(*mercantile.xy_bounds(tile_obj))  # unbuffered, tile-aligned (EPSG:3857)
    with rasterio.open(dem) as d:
        b, res = d.bounds, abs(d.transform.a)
    bbox = (b.left, b.bottom, b.right, b.top)  # the DEM's full (buffered) extent, EPSG:3857
    buffered = box(*bbox)
    min_area = SLIVER_MIN_PX * res * res       # slivers where a vector edge meets the raster shore
    rows = []

    # ── depth bands + drying ── the metre + fathom partition ladders, each off one gdal_contour -p
    # pass, clipped in shapely (not ogr2ogr -clipsrc — the GDAL-3.8 GeometryCollection trap). The
    # metre pass carries DRYING_CAP as an extra positive level, so it ALSO yields the [0, cap]
    # drying bucket, whose 0 m seaward edge is the same ring as the shoal band's amax=0 edge. The
    # metre bands' pre-clip (buffered) union is the water-coverage footprint the nodata pass
    # subtracts; both ladders cover the same water pixels, so the metre union stands for it.
    coverage_geoms = []
    drying_geoms = []
    for sys_tag, levels in (("m", config.DEPARE_LEVELS + [config.DRYING_CAP]),
                            ("ft", config.DEPARE_LEVELS_FT)):
        g = partitions(dem, levels, f"{tmp}/depare-raw-{sys_tag}.fgb")
        if not len(g):
            continue
        bands = g[g["amax"] <= 0]  # water; amax > 0 is drying (m) or land, both handled below
        for r in bands.itertuples():
            for p in _polys(make_valid(r.geometry).intersection(clip)):
                rows.append({"geometry": p, "drval1": r.drval1, "drval2": r.drval2,
                             "sys": sys_tag, "rank": BAND_RANK})
        if sys_tag == "m":
            coverage_geoms = list(bands.geometry)
            # The [0, DRYING_CAP] bucket, keyed on amax alone: 0 and the cap are discrete levels
            # and every other level is negative, so 0 < amax <= cap uniquely picks it regardless
            # of the garbage amin. Land above the cap (amax > cap) is dropped.
            drying_geoms = list(g[(g["amax"] > 0) & (g["amax"] <= config.DRYING_CAP)].geometry)
    coverage = valid_union(coverage_geoms) if coverage_geoms else None

    # Inland-water feed, read once by bbox (the nodata pass iterates its features for `kind`; the
    # drying cut unions its geometry). Optional: absent -> no water term (today's land-only gate).
    water_src = landmask.water_path()
    water = None
    if landmask._present(water_src):
        w = gpd.read_file(water_src, bbox=bbox)
        if len(w):
            water = w.to_crs("EPSG:3857")
    water_geom = valid_union(list(water.geometry)) if water is not None else None

    # ── drying ── fold the [0, DRYING_CAP] foreshore in as DEPARE with a negative drval1. Cut the
    # landward side by EFFECTIVE land = OSM land ∖ OSM inland water — the load-bearing point: the
    # osmdata land product does NOT punch inland water out as holes, so a tidal channel OSM maps as
    # a water polygon sits INSIDE the land coverage; cutting by raw land would delete the drying
    # flats in and along that channel (the ICW/tidal-river failure). effective_water = (NOT land)
    # OR water, so drying = bucket.difference(land) ∪ bucket.intersection(water) — matching the
    # raster gate (rasterize burns land=1 then water=0) without materialising land ∖ water. Absent
    # land.fgb -> no landward cut (degrade; land.fgb is effectively always present); absent
    # water.fgb -> effective_water = NOT land (the union term is empty). Geometry stays RAW like the
    # bands so the shared 0 m edge aligns; clip in shapely; the min-area filter drops seam slivers.
    drying_area = None
    if drying_geoms:
        bucket = valid_union(drying_geoms)
        land_src = landmask.path()
        land_geom = None
        if landmask._present(land_src):
            land = gpd.read_file(land_src, bbox=bbox)
            if len(land):
                land_geom = valid_union(list(land.to_crs("EPSG:3857").geometry))
        if land_geom is None:
            effective = bucket  # no land coverage here -> nothing to cut
        else:
            effective = bucket.difference(land_geom)
            if water_geom is not None:
                effective = valid_union([effective, bucket.intersection(water_geom)])
        effective = make_valid(effective)
        if not effective.is_empty:
            drying_area = effective  # subtracted from nodata below (over the buffered extent)
            for p in _polys(effective.intersection(clip)):
                if p.area >= min_area:
                    rows.append({"geometry": p, "drval1": -config.DRYING_CAP, "drval2": 0.0,
                                 "sys": None, "kind": None, "rank": DRYING_RANK})

    # ── nodata ── inland water we hold no depth for: the OSM water polygons (bbox-read, clipped to
    # the buffered tile) MINUS the water-coverage footprint (depth bands ∪ drying) — a #24-cleared
    # lake the merge left as nodata produces no band, so its whole polygon survives; a surveyed lake
    # nets to slivers the min-area filter drops. No drval (absence is the encoding) + a `kind`
    # passthrough. Ocean has no water polygon, so it gains nothing. Skipped when no water feed.
    if water is not None:
        subtract = [g for g in (coverage, drying_area) if g is not None]
        subtract = valid_union(subtract) if subtract else None
        for r in water.itertuples():
            geom = make_valid(r.geometry).intersection(buffered)
            if subtract is not None:
                geom = geom.difference(subtract)
            geom = geom.intersection(clip)  # same seam contract as the bands
            kind = getattr(r, "kind", None)
            for p in _polys(geom):
                if p.area >= min_area:
                    rows.append({"geometry": p, "drval1": None, "drval2": None,
                                 "sys": None, "kind": kind, "rank": NODATA_RANK})

    if not rows:
        print(f"depare: no water in tile bbox for {label}")
        return None

    # A mixed schema across the three kinds: a row omits a key it doesn't carry, geopandas writes
    # the gap as NULL (NaN for float drval, None for str sys/kind), and FlatGeobuf -> tippecanoe
    # encode that as an ABSENT MVT property — so nodata truly has no drval1, the fill's switch key.
    final = f"{tmp}/depare-final.fgb"
    gpd.GeoDataFrame(rows, crs="EPSG:3857").to_crs("EPSG:4326").to_file(final, driver="FlatGeobuf")
    return final, len(rows)


def tile(stem):
    """The per-stem Snakemake job: partition one stem from a BUFFERED mosaic window, smoothed at
    read with the one shared f(depth, zoom), output at store/depare/<stem>.fgb (depare also reads
    the land + water masks). A waterless tile writes a 0-byte sentinel; bundling filters empties by
    size."""
    import shutil
    import tempfile

    import mosaic
    import smooth
    z, x, y, child_z = (int(a) for a in stem.split("-"))
    out = f"store/depare/{stem}.fgb"
    tmp = tempfile.mkdtemp(prefix=f"depare-{stem}-")  # local scratch; publish crosses to the store
    dem = mosaic.window_dem(stem, f"{tmp}/dem.tiff")
    if not os.environ.get("SKIP_SMOOTH"):
        smooth.smooth_tiff(dem)
    res = _depare_dem(dem, mercantile.Tile(x=x, y=y, z=z), tmp, stem)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    if res:
        final, n = res
        os.replace(final, out)
        print(f"depare tile {stem}: {n} polygons")
    else:
        open(out, "w").close()
        print(f"depare tile {stem}: empty")
    shutil.rmtree(tmp)


# ── bundle ───────────────────────────────────────────────────────────────────

def _tippecanoe(fgbs, maxz, out):
    """tippecanoe the per-tile depare FGBs into `out` (layer `depare`, z MIN_ZOOM..maxz).
    --detect-shared-borders keeps shared partition edges identical through per-zoom simplification
    (no cracks between bands); --coalesce-smallest-as-needed, never --drop-densest: a dropped
    partition is a tint hole, a coalesced one mis-tints a sub-pixel blob. drval1/drval2 are Real
    (numeric MVT; absent on nodata, the fill's switch key); rank is FlatGeobuf Integer64, so
    -T rank:int keeps it numeric (else it lands as a string, like the contour depth ints)."""
    subprocess.run(
        ["tippecanoe", "-o", out, "-f", "-l", "depare",
         "-n", "Depth areas", "-A", utils.ATTRIBUTION,
         "-Z", str(MIN_ZOOM), "-z", str(maxz), "-P", "-q",
         "--detect-shared-borders", "--coalesce-smallest-as-needed",
         "--simplification", os.environ.get("DEPARE_SIMPLIFICATION", "8"),
         "-y", "drval1", "-y", "drval2", "-y", "sys", "-y", "kind", "-y", "rank",
         "-T", "rank:int", *fgbs],
        check=True)


def bundle_stable():
    """Depare bundle: tippecanoe the per-stem FGBs for the covering into store/bundle/depare.pmtiles.
    A 0-byte per-tile file is a legitimately waterless tile (filtered by size); a MISSING one is an
    incomplete build (require_stable_complete). Snakemake decides when to invoke, so this always
    rebuilds."""
    import mosaic
    stems = mosaic.covering_stems()
    files = [f"store/depare/{s}.fgb" for s in stems]
    contour_run.require_stable_complete("depare", stems, files)
    fgbs = [f for f in files if os.path.getsize(f) > 0]
    out = "store/bundle/depare.pmtiles"
    utils.create_folder("store/bundle")
    own_max = max((int(os.path.basename(f).split(".")[0].rsplit("-", 1)[1]) for f in fgbs), default=0)
    maxz = contour_run.bundle_maxz_stable(own_max)
    _tippecanoe(fgbs, maxz, out)
    print(f"depare bundle (stable): {out} (z{MIN_ZOOM}-{maxz}, {len(fgbs)} FGBs)")


def _check():
    """gdal_contour -p buckets on a synthetic DEM: land above the cap dropped, the metre ladder's
    extra DRYING_CAP level yields the [0, DRYING_CAP] drying bucket (selected by 0 < amax <= cap,
    never amin), each water depth lands in its ladder bucket, the water bands are pairwise disjoint
    and jointly cover the water, the ladders ascend and end at 0, and the buckets are deterministic
    (the seam contract reduces to this). The effective-land drying cut is exercised end-to-end
    against real masks in test_engine.check_depare_drying."""
    import tempfile

    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    from shapely.geometry import Point
    from shapely.ops import unary_union

    for levels in (config.DEPARE_LEVELS, config.DEPARE_LEVELS_FT):
        assert levels == sorted(levels) and levels[-1] == 0, "levels must ascend and end at 0"
    assert config.DRYING_CAP > 0, "DRYING_CAP must be a positive level above 0"

    # valid_union must make_valid before unioning: gdal_contour emits self-touching rings that
    # poison a raw union ("side location conflict"). A bowtie is invalid on every GEOS version,
    # and a raw union of it stays invalid — valid_union must return valid geometry.
    from shapely.geometry import Polygon
    bowtie = Polygon([(0, 0), (2, 2), (2, 0), (0, 2), (0, 0)])
    assert not bowtie.is_valid and valid_union([bowtie]).is_valid, \
        "valid_union must make_valid before union (guards the contour side-location-conflict fix)"

    d = tempfile.mkdtemp()
    h = w = 60
    res = 100.0
    tr = from_origin(0, h * res, res, res)  # top-left origin, EPSG:3857
    cap = config.DRYING_CAP
    levels_m = config.DEPARE_LEVELS + [cap]
    # Top-down: land above the cap (dropped), a [0, cap] foreshore (the drying bucket), then four
    # water bands stepping deeper — each 10 rows, values strictly inside a bucket. Step transitions
    # interpolate through the intervening levels, so extra sliver partitions are expected and fine.
    dem = np.full((h, w), cap + 50, dtype="float32")     # rows 0-9: land above the cap
    dem[10:20, :] = 2.0                                  # rows 10-19: [0, cap] foreshore -> drying
    for i, v in enumerate([-1.0, -7.0, -25.0, -150.0]):
        dem[(i + 2) * 10:(i + 3) * 10, :] = v            # rows 20-59: four water bands
    p = f"{d}/dem.tif"
    with rasterio.open(p, "w", driver="GTiff", height=h, width=w, count=1, dtype="float32",
                       nodata=-9999, crs="EPSG:3857", transform=tr) as dst:
        dst.write(dem, 1)

    g = partitions(p, levels_m, f"{d}/raw.fgb")
    bands = g[g["amax"] <= 0]
    drying = g[(g["amax"] > 0) & (g["amax"] <= cap)]
    assert len(bands) and (bands["drval1"] >= 0).all(), "water bands must have drval1 >= 0"
    assert (bands["drval1"] < bands["drval2"]).all(), "drval1 must be the shallow bound"

    def bucket_at(gdf, row):
        pt = Point(tr * (w / 2 + 0.5, row + 0.5))
        hit = gdf[gdf.covers(pt)]
        assert len(hit) == 1, f"exactly one partition must cover row {row}, got {len(hit)}"
        return (hit.iloc[0]["drval1"], hit.iloc[0]["drval2"])

    assert bucket_at(bands, 25) == (0.0, 2.0), "-1 m must land in the [0,2] bucket"
    assert bucket_at(bands, 35) == (5.0, 10.0), "-7 m must land in the [5,10] bucket"
    assert bucket_at(bands, 45) == (20.0, 30.0), "-25 m must land in the [20,30] bucket"
    assert bucket_at(bands, 55) == (100.0, 200.0), "-150 m must land in the [100,200] bucket"

    # The drying bucket: the [0, cap] foreshore, NOT a water band, NOT the above-cap land.
    fore = Point(tr * (w / 2 + 0.5, 15.5))
    land = Point(tr * (w / 2 + 0.5, 5.5))
    assert len(drying) == 1 and drying.covers(fore).any(), "the [0, cap] foreshore is the drying bucket"
    assert not bands.covers(fore).any(), "the foreshore is not a water band (amax > 0)"
    assert not drying.covers(land).any() and not bands.covers(land).any(), \
        "land above the cap is dropped from bands and drying alike"

    # The fill contract for the water bands: pairwise disjoint (sum of areas == union area) and
    # jointly covering the water (union area == the 40 water rows, ± the interpolated band edges).
    union = unary_union(list(bands.geometry))
    assert abs(bands.geometry.area.sum() - union.area) < 1e-6 * union.area, "bands overlap"
    water = 40 * w * res * res
    assert abs(union.area - water) < 1.5 * w * res * res, \
        f"bands must tile the water ({union.area:.0f} vs {water:.0f})"

    # Fathom-curve set (no cap — drying rides the metre ladder only): -7 m sits between 3 fm and 5 fm.
    gft = partitions(p, config.DEPARE_LEVELS_FT, f"{d}/raw-ft.fgb")
    d1, d2 = bucket_at(gft[gft["amax"] <= 0], 35)
    assert abs(d1 - 3 * 1.8288) < 1e-6 and abs(d2 - 5 * 1.8288) < 1e-6, (d1, d2)

    # Deterministic: same DEM -> byte-identical buckets (the drying bucket included).
    g2 = partitions(p, levels_m, f"{d}/raw2.fgb")
    assert sorted(x.wkb for x in g.geometry) == sorted(x.wkb for x in g2.geometry), \
        "partitions not deterministic"

    # A uniform-0 DEM (terrain exactly at datum) yields NO depth band — it falls in the
    # [0, DRYING_CAP] drying bucket, so a merge-filled-0 area tints as drying foreshore, never a
    # false shoal. (The cleared-lake NODATA path is separate: gdal_contour skips NODATA pixels, so
    # a genuinely-unfilled lake interior carries no bucket and renders as nodata — see
    # check_depare_water. Only a thin 0-filled rim at a cleared lake's edge lands in this bucket.)
    flat = np.zeros((h, w), dtype="float32")
    fp = f"{d}/flat.tif"
    with rasterio.open(fp, "w", driver="GTiff", height=h, width=w, count=1, dtype="float32",
                       nodata=-9999, crs="EPSG:3857", transform=tr) as dst:
        dst.write(flat, 1)
    flat_g = partitions(fp, levels_m, f"{d}/flat-raw.fgb")
    assert len(flat_g[flat_g["amax"] <= 0]) == 0, \
        "a uniform-0 surface must produce no depth band (it's the drying bucket, not a shoal tint)"
    print(f"depare_run self-check ok ({len(bands)} m-bands, {len(drying)} drying, "
          f"{len(gft[gft['amax'] <= 0])} ft-bands)")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["tile"] and len(a) == 2:
        tile(a[1])
    elif a == ["bundle", "--stable"]:
        bundle_stable()
    elif a[:1] == ["check"]:
        _check()
    else:
        sys.exit("usage: depare_run.py tile <stem> | bundle --stable | check")
