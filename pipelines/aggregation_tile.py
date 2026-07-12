"""Cut one aggregation tile's merged DEM into single-zoom Terrarium PMTiles.

Vendored from mapterhorn (BSD-3). child_z is derived from the de-buffered raster
width; each 512x512 window is Terrarium-encoded via utils.save_terrarium_tile
(our encode.py — conservative per-zoom quantization) and packed into one
single-zoom PMTiles in the z7-sharded store/pmtiles.
"""

import json
import math
import os
from concurrent.futures import ThreadPoolExecutor
from glob import glob

import mercantile
import rasterio

import keys
import utils

NODATA = -9999

# Threads for the per-tile Terrarium/WebP encode. rasterio's block reads and
# imagecodecs.webp_encode both release the GIL, so the ~43 ms/tile encode of the
# 4096 z14 tiles parallelizes without process-spawn overhead. All threads share
# the process's one open DEM; each task holds only its own 512x512 window (~1 MB),
# so the added memory ceiling is workers x ~1 MB — negligible against the tile's
# multi-GB merged DEM.
ENCODE_THREADS = min(os.cpu_count() or 1, 8)


def create_tile(i, j, tiff_filepath, out_filepath, buffer_pixels):
    col_start = i * 512 + buffer_pixels
    row_start = j * 512 + buffer_pixels
    window = rasterio.windows.Window(col_off=col_start, row_off=row_start, width=512, height=512)
    with rasterio.open(tiff_filepath) as src:
        subdata = src.read(1, window=window, out_shape=(512, 512))
    subdata[subdata == NODATA] = 0
    utils.save_terrarium_tile(subdata, out_filepath)


def create_tiles(tmp_folder, aggregation_tile, tiff_filepath, buffer_pixels):
    with rasterio.open(tiff_filepath) as src:
        assert src.block_shapes[0] == (512, 512)
        horizontal_block_count = (src.width - 2 * buffer_pixels) / 512
        assert math.floor(horizontal_block_count) == horizontal_block_count
        child_z = aggregation_tile.z + int(math.log2(horizontal_block_count))
    span = 2 ** (child_z - aggregation_tile.z)
    x_min = aggregation_tile.x * span
    y_min = aggregation_tile.y * span
    # Each task writes its own {z}-{x}-{y}.webp; create_archive re-globs and packs
    # tiles in sorted tile-id order, so encode order can't change the archive bytes.
    # Waiting on futures in submission order re-raises the first failure like the serial loop.
    with ThreadPoolExecutor(max_workers=ENCODE_THREADS) as pool:
        futures = [
            pool.submit(create_tile, i, j, tiff_filepath, f"{tmp_folder}/{child_z}-{x}-{y}.webp", buffer_pixels)
            for i, x in enumerate(range(x_min, x_min + span))
            for j, y in enumerate(range(y_min, y_min + span))
        ]
        for future in futures:
            future.result()


def main(filepath, out_filepath):
    """Tile the merged DEM into single-zoom Terrarium PMTiles at ``out_filepath`` — the caller's
    content-addressed name (``store/pmtiles/<stem>-<key>.pmtiles``). The archive is built in the
    tile's tmp folder and published with an atomic rename, so ``out_filepath`` only ever appears
    complete (a crash mid-write leaves the temp, and the fork reads stale next run)."""
    aggregation_id, filename = filepath.split("/")[-2:]
    z, x, y, child_z = (int(a) for a in filename.replace("-aggregation.csv", "").split("-"))
    tmp_folder = f"store/aggregation/{aggregation_id}/{z}-{x}-{y}-{child_z}-tmp"

    if not os.path.isfile(f"{tmp_folder}/merge-done"):
        print("merge not done yet...")
        return

    with open(f"{tmp_folder}/reprojection.json") as f:
        buffer_pixels = json.load(f)["buffer_pixels"]

    num_tiffs = len(glob(f"{tmp_folder}/*.tiff"))
    tiff_filepath = f"{tmp_folder}/{num_tiffs - 1}-3857.tiff"

    aggregation_tile = mercantile.Tile(x=x, y=y, z=z)
    create_tiles(tmp_folder, aggregation_tile, tiff_filepath, buffer_pixels)
    tmp_archive = f"{tmp_folder}/tile.pmtiles"   # not *.webp, so create_archive's glob skips it
    utils.create_archive(tmp_folder, tmp_archive)
    keys.publish(tmp_archive, out_filepath)      # atomic rename into the content name
