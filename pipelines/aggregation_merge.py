"""Merge the reprojected source layers of one aggregation tile.

Vendored from mapterhorn (BSD-3) — the priority nodata-fill + localized Gaussian
seam feather. Streams 512-windows (plus the buffer overlap): start from the
best source, fill remaining nodata from lower-priority sources, then feather only
across the valid/invalid boundary so source seams don't show as elevation steps.
Interiors are untouched. A final guard restores any pixel the feather flips from
>= 0 to < 0 back to its pre-blend value, so seam smoothing can never manufacture
water on land — the stage invariant that lets the source-blind post-merge land
re-clamp be dropped. This reconciles nothing vertically — datum handling is
upstream in source_datum.
"""

import json
import os
from glob import glob

import numpy as np
import rasterio
from scipy import ndimage

import utils

NODATA = -9999


def merge(filepath):
    filename = filepath.split("/")[-1]
    z, x, y, child_z = (int(a) for a in filename.replace("-aggregation.csv", "").split("-"))
    tmp_folder = filepath.replace("-aggregation.csv", "-tmp")  # beside the CSV — ULID or stable layout alike

    done_filepath = f"{tmp_folder}/merge-done"
    if os.path.isfile(done_filepath):
        print(f"merge {filename} already done...")
        return
    if not os.path.isfile(f"{tmp_folder}/reprojection.json"):
        print(f"{filepath} reprojection not done yet...")
        return

    tiffs = sorted(glob(f"{tmp_folder}/*.tiff"), key=lambda p: int(p.split("/")[-1].split("-")[0]))
    if len(tiffs) == 0:
        raise ValueError(f"failed to read tifs of {filepath}")
    if len(tiffs) == 1:
        utils.run_command(f"touch {done_filepath}")
        return

    with open(f"{tmp_folder}/reprojection.json") as f:
        buffer_pixels = json.load(f)["buffer_pixels"]

    tile_size = 512
    overlap = buffer_pixels
    with rasterio.env.Env(GDAL_CACHEMAX=256):
        with rasterio.open(tiffs[0]) as src:
            height, width, profile = src.height, src.width, src.profile
        profile.update(tiled=True, blockxsize=512, blockysize=512)
        output_path = f"{tmp_folder}/{len(tiffs)}-3857.tiff"

        with rasterio.open(output_path, "w", **profile) as dst:
            for oy in range(0, height, tile_size):
                for ox in range(0, width, tile_size):
                    y_start, y_end = max(0, oy - overlap), min(height, oy + tile_size + overlap)
                    x_start, x_end = max(0, ox - overlap), min(width, ox + tile_size + overlap)
                    window = rasterio.windows.Window(x_start, y_start, x_end - x_start, y_end - y_start)

                    with rasterio.open(tiffs[0]) as src:
                        merged_tile = np.nan_to_num(src.read(1, window=window), nan=NODATA)

                    if NODATA in merged_tile:
                        binary_mask = (merged_tile != NODATA).astype("int32")
                        boundary_tile = binary_mask.astype(bool) & ~ndimage.binary_erosion(binary_mask)

                        for tiff in tiffs[1:]:
                            with rasterio.open(tiff) as src:
                                current_tile = np.nan_to_num(src.read(1, window=window), nan=NODATA)
                            copy_mask = (merged_tile == NODATA) & (current_tile != NODATA)
                            merged_tile[copy_mask] = current_tile[copy_mask]
                            if NODATA not in merged_tile:
                                break
                            binary_mask = (merged_tile != NODATA).astype("int32")
                            boundary_tile |= binary_mask.astype(bool) & ~ndimage.binary_erosion(binary_mask)

                        boundary_tile[0, :] = boundary_tile[-1, :] = 0
                        boundary_tile[:, 0] = boundary_tile[:, -1] = 0
                        binary_mask = (merged_tile != NODATA).astype("int32")
                        boundary_tile &= ndimage.binary_erosion(binary_mask).astype(bool)

                        if 1 in boundary_tile:
                            merged_tile[merged_tile == NODATA] = 0
                            # Snapshot AFTER the nodata fill: a truly-uncovered pixel must enter the
                            # guard as 0 (not the -9999 sentinel), so the ">= 0" test below sees it
                            # too — else a 0-filled hole the feather then pulls negative slips past a
                            # >= 0 predicate and fabricates a false water rim around a data hole.
                            pre_feather = merged_tile.copy()
                            truncate = 4
                            sigma = max(int(overlap / truncate) - 1, 1)
                            bb = ndimage.gaussian_filter(boundary_tile.astype(float), sigma=sigma, truncate=truncate)
                            bb /= (1.0 / (np.sqrt(2 * np.pi) * sigma))
                            bb = np.clip(bb, 0, 1)
                            bb = 3 * bb ** 2 - 2 * bb ** 3  # smoothstep
                            blurred = ndimage.gaussian_filter(merged_tile, sigma=sigma, truncate=truncate)
                            merged_tile = bb * blurred + (1 - bb) * merged_tile
                            # Stage rule: the seam feather may not manufacture water. Wherever the
                            # blend pulled a pixel that entered >= 0 (clamped land, real land topo,
                            # or a filled hole) below 0, restore its pre-feather value. This makes
                            # the feather guarantee its own sign invariant, which is what lets the
                            # source-blind post-merge land re-clamp be deleted: no downstream fork
                            # sees a false negative rim where clamped land abuts water, and trusted
                            # land keeps its true elevation instead of being re-flattened to 0.
                            flipped = (pre_feather >= 0) & (merged_tile < 0)
                            merged_tile = np.where(flipped, pre_feather, merged_tile)

                    crop_y_start = overlap if oy > 0 else 0
                    crop_y_end = merged_tile.shape[0] - (overlap if y_end < height else 0)
                    crop_x_start = overlap if ox > 0 else 0
                    crop_x_end = merged_tile.shape[1] - (overlap if x_end < width else 0)
                    out_window = rasterio.windows.Window(ox, oy, crop_x_end - crop_x_start, crop_y_end - crop_y_start)
                    dst.write(merged_tile[crop_y_start:crop_y_end, crop_x_start:crop_x_end], 1, window=out_window)

    utils.run_command(f"touch {done_filepath}")
