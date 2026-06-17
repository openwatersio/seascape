"""Reproject each source group of one aggregation tile to EPSG:3857.

Vendored from mapterhorn (BSD-3). For each (source, maxzoom) group, most-important
first: build a VRT, warp to 3857 at the group's maxzoom resolution with cubicspline
and dstnodata -9999, then short-circuit once the accumulated result has no nodata
(highest-res source wins; lower ones only fill gaps). A halo buffer is always added
so contour lines stay continuous across tile seams; the raster output crops it back.

Internal paths only (store/aggregation tmp + source filenames from our bounds.csv);
shells out via utils.run_command.
"""

import json
import os
import subprocess
import sys

import mercantile
import numpy as np
import rasterio

import config
import utils

# Streaming sources (CUDEM off NOAA, the locally-prepared sources off public R2) are
# all read via /vsicurl over public HTTPS — no credentials in the read path, so no
# AWS env to set here. config.source_path resolves each filename to its /vsicurl URL.

SILENT = True
NODATA = -9999

# Bathymetry note: cubicspline can ring near steep escarpments; set
# AGG_RESAMPLE=bilinear to switch if that shows.
RESAMPLE = os.environ.get("AGG_RESAMPLE", "cubicspline")


def band_select(source):
    """`-b N ` (trailing space) if the source pins a band, else ''. BlueTopo is 3-band
    (elevation/uncertainty/contributor); band 1 is elevation."""
    band = config.load_metadata(source).get("band")
    return f"-b {band} " if band else ""


def create_virtual_raster(tmp_folder, i, source_items):
    source = source_items[0]["source"]
    vrt = f"{tmp_folder}/{i}.vrt"
    listpath = f"{tmp_folder}/{i}-file-list.txt"
    with open(listpath, "w") as f:
        for item in source_items:
            f.write(config.source_path(source, item["filename"]) + "\n")
    utils.run_command(f"gdalbuildvrt -overwrite {band_select(source)}-input_file_list {listpath} {vrt}", silent=SILENT)
    return vrt


def per_tile_vrts(tmp_folder, i, source_items):
    """One single-band VRT per tile, for a `mixed_crs` source (per-tile UTM zones, e.g.
    BlueTopo). gdalbuildvrt refuses to merge differing CRS into one VRT — it silently
    drops the off-CRS tiles, holing zone seams — so each tile gets its own VRT and
    gdalwarp (which does reproject per input) mosaics them into 3857 in warp_mixed."""
    source = source_items[0]["source"]
    bsel = band_select(source)
    vrts = []
    for j, item in enumerate(source_items):
        path = config.source_path(source, item["filename"])
        vrt = f"{tmp_folder}/{i}-{j}.vrt"
        utils.run_command(f"gdalbuildvrt -overwrite {bsel}{vrt} {path}", silent=SILENT)
        vrts.append(vrt)
    return vrts


def warp_mixed(inputs, out_tif, zoom, aggregation_tile, buffer):
    """gdalwarp several heterogeneous-CRS inputs into one 3857 GTiff mosaic. A warped
    VRT can't span source CRSs (it has one), so warp straight to a raster — each input
    is reprojected from its own UTM zone, so a zone-crossing tile keeps every source.
    No value transform: streamed sources skip source_datum, MLLW->MSL is the Phase 5
    VDatum job; nan source-nodata maps to NODATA via -dstnodata."""
    left, bottom, right, top = mercantile.xy_bounds(aggregation_tile)
    left, bottom, right, top = left - buffer, bottom - buffer, right + buffer, top + buffer
    res = get_resolution(zoom)
    _run(f"GDAL_CACHEMAX=512 gdalwarp -overwrite -t_srs EPSG:3857 -tr {res} {res} "
         f"-te {left} {bottom} {right} {top} -r {RESAMPLE} -dstnodata {NODATA} -co TILED=YES "
         f"{' '.join(inputs)} {out_tif}",
         f"gdalwarp(mixed) {out_tif}")


def get_resolution(zoom):
    bounds = mercantile.xy_bounds(mercantile.Tile(x=0, y=0, z=zoom))
    return (bounds.right - bounds.left) / 512


def _run(cmd, what):
    # Check the exit code, not stderr: gdal writes non-fatal warnings (e.g.
    # "Several coordinate operations" for datum transforms like 4269->3857) to
    # stderr, which must not be treated as a failure.
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if proc.returncode != 0:
        raise Exception(f"{what} failed (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}")


def create_warp(vrt, vrt_3857, zoom, aggregation_tile, buffer):
    left, bottom, right, top = mercantile.xy_bounds(aggregation_tile)
    left, bottom, right, top = left - buffer, bottom - buffer, right + buffer, top + buffer
    res = get_resolution(zoom)
    _run(f"gdalwarp -of vrt -overwrite -t_srs EPSG:3857 -tr {res} {res} "
         f"-te {left} {bottom} {right} {top} -r {RESAMPLE} -dstnodata {NODATA} {vrt} {vrt_3857}",
         f"gdalwarp {vrt}")


def translate(in_filepath, out_filepath):
    _run("GDAL_CACHEMAX=512 gdal_translate -of COG -co BIGTIFF=IF_NEEDED -co ADD_ALPHA=YES "
         f"-co OVERVIEWS=NONE -co SPARSE_OK=YES -co BLOCKSIZE=512 -co COMPRESS=NONE {in_filepath} {out_filepath}",
         f"gdal_translate {in_filepath}")


def contains_nodata_pixels(filepath):
    with rasterio.env.Env(GDAL_CACHEMAX=64):
        with rasterio.open(filepath) as src:
            block = 1024
            for row in range(0, src.height, block):
                for col in range(0, src.width, block):
                    window = rasterio.windows.Window(col, row,
                                                     min(block, src.width - col),
                                                     min(block, src.height - row))
                    data = np.nan_to_num(src.read(1, window=window), nan=NODATA)
                    if NODATA in data:
                        return True
    return False


def reproject(filepath):
    aggregation_id, filename = filepath.split("/")[-2:]
    z, x, y, child_z = (int(a) for a in filename.replace("-aggregation.csv", "").split("-"))
    aggregation_tile = mercantile.Tile(x=x, y=y, z=z)

    tmp_folder = f"store/aggregation/{aggregation_id}/{z}-{x}-{y}-{child_z}-tmp"
    utils.create_folder(tmp_folder)
    metadata_filepath = f"{tmp_folder}/reprojection.json"
    if os.path.isfile(metadata_filepath):
        print(f"reproject {filename} already done...")
        return

    grouped = utils.get_grouped_source_items(filepath)
    maxzoom = grouped[0][0]["maxzoom"]
    resolution = get_resolution(maxzoom)

    # Always buffer (even single-source) so the merged DEM has a halo for contour
    # seam continuity (lines traced through the overlap, then clipped to the tile
    # bbox). Raster crops it out via the buffer_pixels offset, so this is free for
    # raster.
    buffer_pixels = int(utils.macrotile_buffer_3857 / resolution)
    buffer_3857_rounded = buffer_pixels * resolution

    for i, source_items in enumerate(grouped):
        out_tiff = f"{tmp_folder}/{i}-3857.tiff"
        if config.load_metadata(source_items[0]["source"]).get("mixed_crs"):
            # Per-tile UTM zones: warp the per-tile VRTs straight to a raster (gdalwarp
            # reprojects each input), then the same translate makes the COG.
            merged = f"{tmp_folder}/{i}-3857-merged.tif"
            warp_mixed(per_tile_vrts(tmp_folder, i, source_items), merged,
                       maxzoom, aggregation_tile, buffer_3857_rounded)
            translate(merged, out_tiff)
        else:
            vrt = create_virtual_raster(tmp_folder, i, source_items)
            vrt_3857 = f"{tmp_folder}/{i}-3857.vrt"
            create_warp(vrt, vrt_3857, maxzoom, aggregation_tile, buffer_3857_rounded)
            translate(vrt_3857, out_tiff)
        if len(grouped) > 1 and not contains_nodata_pixels(out_tiff):
            break

    with open(metadata_filepath, "w") as f:
        json.dump({"buffer_pixels": buffer_pixels}, f, indent=2)


def _check():
    """warp_mixed must keep tiles from *different* CRSs that gdalbuildvrt would drop one
    of. Two boxes straddling the UTM 17N/18N boundary (~78W): the 3857 mosaic of both
    must have strictly more valid pixels than either alone -> the off-zone tile survived."""
    import tempfile
    from rasterio.transform import from_origin

    d = tempfile.mkdtemp()

    def utm_box(path, epsg, west_e, north_n, val, n=120, res=100):
        arr = np.full((n, n), val, dtype="float32")
        with rasterio.open(path, "w", driver="GTiff", height=n, width=n, count=1,
                           dtype="float32", nodata=NODATA, crs=f"EPSG:{epsg}",
                           transform=from_origin(west_e, north_n, res, res)) as dst:
            dst.write(arr, 1)

    a, b = f"{d}/a_z17.tif", f"{d}/b_z18.tif"
    utm_box(a, 32617, 748000, 4438000, 10.0)  # ~ -78.1, 40.1 in UTM 17N
    utm_box(b, 32618, 240000, 4438000, 20.0)  # ~ -78.0, 40.1 in UTM 18N
    tile = mercantile.tile(-78.0, 40.0, 8)    # ~1.4deg window, both boxes inside

    def valid(path):
        with rasterio.open(path) as src:
            arr = src.read(1)
        return int(np.count_nonzero((arr != NODATA) & ~np.isnan(arr)))

    both, just_a = f"{d}/both.tif", f"{d}/a.tif"
    warp_mixed([a, b], both, 9, tile, 0)
    warp_mixed([a], just_a, 9, tile, 0)
    va, vboth = valid(just_a), valid(both)
    assert vboth > va > 0, (va, vboth)

    # run_command must RAISE on a failed gdalbuildvrt (not swallow it) — the bug that let a
    # missing VRT reach gdalwarp as a baffling "No such file" and kill an hour-long shard.
    miss = f"{d}/miss.vrt"
    try:
        utils.run_command(f"gdalbuildvrt -overwrite {miss} {d}/does-not-exist.tif")
        assert False, "expected run_command to raise on a failed gdalbuildvrt"
    except RuntimeError:
        assert not os.path.exists(miss)
    print(f"aggregation_reproject.py self-check ok (valid pixels: A={va}, A+B={vboth})")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        _check()
    else:
        sys.exit("aggregation_reproject is a library; run with --check for the self-check")
