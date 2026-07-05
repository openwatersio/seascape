"""Drying areas (green foreshore) as a fork off each aggregation tile's merged DEM.

Charts tint the foreshore green: seabed above chart datum that covers and uncovers with the
tide. The data already flows through the pipeline (S-102 drying heights and CUDEM intertidal
become positive elevations, nothing masks them), but a raster depth ramp can't tell a +0.5 m
drying flat from +0.5 m of dry land — separating them needs the high-water shoreline, which is
exactly the OSM land mask Phase 1 introduced. OSM's coastline is mapped at high water and the
DEM is low-water-referenced, so a drying area is the chart definition: elevation in
[0, DRYING_CAP] AND seaward of the land polygons (mask == 0) — above low water, below high water.

A sibling of contour_run/soundings_run: it consumes the same merged, smoothed DEM plus the
land-mask raster the reproject clamp already rasterized for this tile. Per tile: build a Byte
drying mask off (DEM, land mask) -> polygonize -> clip to the unbuffered tile bbox -> 4326 ->
store/drying/{stem}.fgb. Same seam contract as contours: the mask and DEM are deterministic on
the buffered grid, so neighbouring tiles' halos polygonize identically and polygon edges meet at
the clip. bundle() tippecanoes them into a `drying` layer pmtiles; the contour merge's single
tile-join folds it into vector.pmtiles (run bundle before the merge).
"""

import os
import subprocess
import sys
from glob import glob

import mercantile
import numpy as np
import rasterio

import config
import contour_run
import landmask
import utils


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


def drying_mask(dem_path, mask_path, cap):
    """Whole-tile Byte drying mask (1 = foreshore) built in windows, so the multi-GB Float32 DEM
    is never held whole — only this uint8 result (a quarter the size). 1 where a valid DEM pixel
    is above chart datum but no deeper than the tide range (0 <= elev <= cap) AND seaward of the
    land polygons (mask == 0). read_masks so nodata/alpha pixels drop out; the >= 0 term also
    excludes the DEM's -9999 nodata by itself. Returns (uint8 array, transform).

    Ceiling: the uint8 mask is materialised whole for polygonize (rasterio.features.shapes needs
    one array). It is a quarter the DEM's bytes, but a deep coastal macrotile is still ~1 GB;
    upgrade path is a windowed streaming polygonize (gdal_polygonize) if that ever OOMs a shard.
    """
    with rasterio.open(dem_path) as d, rasterio.open(mask_path) as m:
        if (d.height, d.width) != (m.height, m.width):
            raise ValueError(
                f"land mask {(m.height, m.width)} != DEM {(d.height, d.width)} for {dem_path} "
                "(rasterize -te/-tr must match the merged DEM grid)")
        out = np.zeros((d.height, d.width), dtype="uint8")
        block = 2048
        for row in range(0, d.height, block):
            for col in range(0, d.width, block):
                win = rasterio.windows.Window(
                    col, row, min(block, d.width - col), min(block, d.height - row))
                elev = d.read(1, window=win)
                valid = d.read_masks(1, window=win) != 0
                land = m.read(1, window=win)
                hit = valid & (elev >= 0) & (elev <= cap) & (land == 0)
                out[row:row + hit.shape[0], col:col + hit.shape[1]] = hit
        return out, d.transform


def polygons(mask_arr, transform):
    """Polygonize the 1-pixels of a drying mask into shapely polygons (EPSG:3857). shapes() is
    rasterio's binding to the same GDAL polygonizer gdal_polygonize uses; mask= excludes the
    0-pixels so every yielded feature is a foreshore blob (its DN is 1, discarded)."""
    from rasterio.features import shapes
    from shapely.geometry import shape
    return [shape(g) for g, _v in shapes(mask_arr, mask=mask_arr, transform=transform)]


def smooth_polygons(geoms, tol):
    """De-staircase the polygonize output with topology-preserving Douglas-Peucker (shapely's
    default simplify). It drops the 1-pixel steps while keeping every vertex within `tol` of the
    true raster edge — a bounded-fidelity generalisation, unlike Chaikin corner-cutting, which
    self-intersects at a thin foreshore neck, biases the polygon inward, and rounds genuinely
    straight edges. Run in 3857 (tol in metres) BEFORE the tile clip so adjacent tiles de-staircase
    their shared halo identically and the edges still meet. A staircase collapses to a clean
    diagonal; a real straight edge stays straight.

    Curve ceiling: DP leaves generalised polylines (slightly angular), not smooth curves — fine for
    a soft foreshore fill. For true curves the right place is the raster (blur the mask / extract an
    iso-contour with marching squares), which is smooth and valid by construction; not worth it yet.
    """
    return [p for g in geoms for p in _polys(g.simplify(tol))]


def generate(filepath):
    import geopandas as gpd
    agg_id, filename = filepath.split("/")[-2:]
    z, x, y, child_z = (int(a) for a in filename.replace("-aggregation.csv", "").split("-"))
    tile = mercantile.Tile(x=x, y=y, z=z)
    tmp = f"store/aggregation/{agg_id}/{z}-{x}-{y}-{child_z}-tmp"
    stem = f"{z}-{x}-{y}-{child_z}"
    dem = f"{tmp}/{len(glob(f'{tmp}/*.tiff')) - 1}-3857.tiff"
    if not os.path.exists(dem):
        print(f"drying: no merged DEM for {filename}")
        return

    # Reuse the land-mask raster the reproject clamp already burned for this tile (same buffered
    # grid as the DEM). If a tile carried no flagged coarse source there's no cached mask, so
    # rasterize one on the DEM's own grid — drying always needs the mask (fails loudly if the
    # LANDMASK source is unreadable, like the clamp).
    mask = f"{tmp}/landmask.tif"
    if not os.path.exists(mask):
        with rasterio.open(dem) as d:
            b, res = d.bounds, d.res[0]
        landmask.rasterize((b.left, b.bottom, b.right, b.top), res, mask)

    arr, transform = drying_mask(dem, mask, config.DRYING_CAP)
    geoms = polygons(arr, transform)
    if not geoms:
        print(f"drying: no foreshore for {filename}")
        return
    geoms = smooth_polygons(geoms, tol=abs(transform.a) * 1.5)  # transform.a = pixel width (3857 m)

    # Same seam contract as contours: de-staircase over the buffered grid, then clip to the
    # unbuffered tile bbox (deterministic input -> byte-identical halo -> edges meet). Clip in
    # shapely, NOT ogr2ogr -clipsrc: on a fractal coastline the clip can emit a GeometryCollection
    # (polygon + a boundary-grazing edge) that a (multi)polygon FlatGeobuf layer refuses to write
    # ("Mismatched geometry type", the planet-build failure) — and that surfaces only on the
    # container's GDAL 3.8, not a newer local GDAL, so it can't be caught locally. make_valid + clip
    # + _polys keeps only the polygonal parts of the RESULT, so the output is uniformly polygon by
    # construction, version-independently. Reproject with geopandas in the same write.
    from shapely import make_valid
    from shapely.geometry import box
    clip = box(*mercantile.xy_bounds(tile))  # unbuffered, tile-aligned (EPSG:3857)
    kept = [p for g in geoms for p in _polys(make_valid(g).intersection(clip))]
    if not kept:
        print(f"drying: no foreshore in tile bbox for {filename}")
        return

    utils.create_folder("store/drying")  # tile-keyed, so clean tiles persist across incremental runs
    out = f"store/drying/{stem}.fgb"
    gpd.GeoDataFrame(geometry=kept, crs="EPSG:3857").to_crs("EPSG:4326").to_file(out, driver="FlatGeobuf")
    print(f"drying: {filename} -> {len(kept)} polygons")


# ── bundle ───────────────────────────────────────────────────────────────────

def bundle():
    """tippecanoe the per-tile drying FGBs into store/bundle/drying.pmtiles (layer `drying`).
    Sparse coastal polygons (not sharded, like soundings); the orphan filter drops FGBs left
    from a re-tiled covering, same as contours/soundings."""
    fgbs = contour_run._live_fgbs(sorted(glob("store/drying/*.fgb")), contour_run._current_stems())
    if not fgbs:
        print("drying bundle: no drying FGBs")
        return
    # Shared tileset maxzoom (see contour_run.bundle_maxz): tiling only to this
    # layer's own regional max would truncate it out of deeper joined tiles.
    maxz = contour_run.bundle_maxz(
        max(int(f.split("/")[-1].replace(".fgb", "").split("-")[3]) for f in fgbs))
    utils.create_folder("store/bundle")
    subprocess.run(
        ["tippecanoe", "-o", "store/bundle/drying.pmtiles", "-f", "-l", "drying",
         "-n", "Drying areas", "-A", utils.ATTRIBUTION, "-Z", "0", "-z", str(maxz),
         "-P", "-q", "--drop-densest-as-needed",
         "--simplification", os.environ.get("DRYING_SIMPLIFICATION", "8"), *fgbs],
        check=True)
    print(f"drying bundle: store/bundle/drying.pmtiles (z0-{maxz}, {len(fgbs)} FGBs)")


def _check():
    """Drying mask + polygonize on a synthetic DEM/mask grid: only foreshore (0<=elev<=cap,
    seaward of land) turns 1; land, deep water, and above-cap topo stay 0; and the mask is
    deterministic across a shifted grid (the seam contract)."""
    import tempfile
    from rasterio.transform import from_origin

    d = tempfile.mkdtemp()
    NODATA = -9999.0
    cap = config.DRYING_CAP
    h = w = 40
    res = 100.0
    tr = from_origin(0, h * res, res, res)  # top-left origin, EPSG:3857

    # Land = the top half (rows < 20); water = the bottom half.
    land = np.zeros((h, w), dtype="uint8")
    land[:20, :] = 1
    # DEM: deep -50 everywhere, then a foreshore band (+2, in-band) on rows 25..29, an above-cap
    # patch (cap+50) on rows 32..35, one nodata cell, and the SAME +2 up in the land half.
    dem = np.full((h, w), -50.0, dtype="float32")
    dem[25:30, :] = 2.0          # water, in-band  -> drying
    dem[32:36, :] = cap + 50     # water, above cap -> not drying
    dem[27, 0] = NODATA          # nodata in the band -> not drying (excluded by validity)
    dem[5:10, :] = 2.0           # land, in-band   -> NOT drying (mask == 1)

    dem_p, mask_p = f"{d}/dem.tif", f"{d}/mask.tif"
    with rasterio.open(dem_p, "w", driver="GTiff", height=h, width=w, count=1, dtype="float32",
                       nodata=NODATA, crs="EPSG:3857", transform=tr) as dst:
        dst.write(dem, 1)
    with rasterio.open(mask_p, "w", driver="GTiff", height=h, width=w, count=1, dtype="uint8",
                       nodata=0, crs="EPSG:3857", transform=tr) as dst:
        dst.write(land, 1)

    arr, transform = drying_mask(dem_p, mask_p, cap)
    assert arr[27, 5] == 1, "water in-band foreshore must be drying"
    assert arr[27, 0] == 0, "nodata in the band must not be drying"
    assert arr[7, 5] == 0, "in-band elevation under land must not be drying"
    assert arr[34, 5] == 0, "above-cap topo must not be drying"
    assert arr[2, 5] == 0 and arr[38, 5] == 0, "deep water / bare land must not be drying"
    # Pixelwise-pure: same inputs -> byte-identical mask (the seam contract reduces to this once
    # the DEM/mask are deterministic, which merge/smooth/landmask already assert). The real
    # adjacent-tile seam is exercised end-to-end in test_engine.check_drying.
    assert np.array_equal(arr, drying_mask(dem_p, mask_p, cap)[0]), "drying mask not deterministic"

    from shapely.geometry import Point
    geoms = polygons(arr, transform)
    assert geoms, "the foreshore band must polygonize to at least one polygon"
    band_pt = Point(transform * (5.5, 27.5))   # centre of a water in-band cell
    land_pt = Point(transform * (5.5, 7.5))     # centre of an in-band LAND cell
    assert any(g.covers(band_pt) for g in geoms), "a drying polygon must cover the foreshore band"
    assert not any(g.covers(land_pt) for g in geoms), "no drying polygon may cover land"

    # De-staircasing must still cover the band interior and exclude land (DP keeps the axis-aligned
    # rectangle a rectangle — a real straight edge is NOT rounded, which is the point).
    sm = smooth_polygons(geoms, tol=abs(transform.a) * 1.5)
    assert any(g.covers(band_pt) for g in sm), "de-staircased drying must still cover the band"
    assert not any(g.covers(land_pt) for g in sm), "de-staircased drying must still exclude land"

    def axis_frac(gs):
        segs = []
        for g in gs:
            for p in (g.geoms if g.geom_type == "MultiPolygon" else [g]):
                d = np.diff(np.asarray(p.exterior.coords), axis=0)
                segs += list((np.abs(d[:, 0]) < 1e-6) | (np.abs(d[:, 1]) < 1e-6))
        return np.mean(segs) if segs else 0.0
    # A real 45° pixel staircase must collapse to a near-diagonal (few axis-aligned segments),
    # where Chaikin's failure mode (thin-neck self-intersection) also lived.
    diag = np.array([[1 if 10 <= i + j <= 14 else 0 for j in range(30)] for i in range(30)], "uint8")
    dgeoms = polygons(diag, from_origin(0, 3000, 100, 100))
    draw, dsm = axis_frac(dgeoms), axis_frac(smooth_polygons(dgeoms, tol=150))
    assert draw > 0.9 and dsm < 0.6, f"a staircase must de-stair (axis-aligned {draw:.2f} -> {dsm:.2f})"

    # Regression for the planet-build OGR "Mismatched geometry type" (which only surfaced on the
    # container's GDAL 3.8): the shapely clip must keep the layer uniformly polygon by construction.
    # (1) a self-touching ring heals + clips to valid polygons; (2) a GeometryCollection — what a
    # fractal-coastline clip produces — reduces to polygons only, dropping line/point slivers.
    from shapely import make_valid
    from shapely.geometry import GeometryCollection, LineString, Point, Polygon, box
    bowtie = Polygon([(0, 0), (4, 4), (0, 4), (4, 0), (0, 0)])  # self-intersecting
    parts = _polys(make_valid(bowtie).intersection(box(-1, -1, 5, 5)))
    assert parts and all(p.geom_type == "Polygon" and p.is_valid and not p.is_empty for p in parts), \
        "a self-touching ring must reduce to valid, writable polygons"
    mixed = GeometryCollection([Polygon([(0, 0), (2, 0), (2, 2), (0, 2)]),
                                LineString([(3, 3), (4, 4)]), Point(5, 5)])
    assert [p.geom_type for p in _polys(mixed)] == ["Polygon"], \
        "a GeometryCollection must reduce to polygons only (drop line/point slivers)"

    print(f"drying_run self-check ok (foreshore pixels {int(arr.sum())}, {len(geoms)} polygons; "
          f"staircase de-staired axis-aligned {draw:.2f} -> {dsm:.2f})")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["bundle"]:
        bundle()
    elif a[:1] == ["check"]:
        _check()
    else:
        sys.exit("usage: drying_run.py bundle | check")
