"""Slope-selective DEM smoothing (ported from scripts/smooth-dem + scripts/blur).

Blurs flat areas (abyssal plains, shelves) to cut noise-driven contour
stairstepping while preserving steep detail (canyon walls, seamounts). Applied to
each aggregation tile's merged DEM, so the raster encode and the contour fork
share one smoothed surface. In-memory (the tile is bounded ≤32768px) — numpy
slope + scipy gaussian, so no out-of-core windowing is needed here.

ponytail: sigma is in merged-DEM pixels, so the physical blur scale tracks the
tile's zoom (coarse base tiles blur more in metres, fine regional tiles less) —
which is roughly what we want (coarse data is noisier). Revisit with a
physical-scale sigma if it over/under-blurs. SKIP_SMOOTH=1 disables it.
"""

import glob
import os

import numpy as np
import rasterio
from scipy.ndimage import gaussian_filter

NODATA = -9999

DEM_SIGMA = float(os.environ.get("SMOOTH_DEM_SIGMA", "4"))
MASK_SIGMA = float(os.environ.get("SMOOTH_MASK_SIGMA", "4"))
SLOPE_LOW = float(os.environ.get("SMOOTH_SLOPE_LOW", "1"))    # ≤ this slope (deg): fully blurred
SLOPE_HIGH = float(os.environ.get("SMOOTH_SLOPE_HIGH", "5"))  # ≥ this slope: original kept


def smooth_array(dem, res, nodata=NODATA):
    valid = dem != nodata
    water = valid & (dem < 0)
    # Clamp land/nodata to 0 so they don't drag the blur of nearby ocean pixels.
    work = np.where(water, dem, 0.0).astype("float32")
    blurred = gaussian_filter(work, sigma=DEM_SIGMA, mode="nearest")
    # Slope (degrees) from the clamped surface, accounting for pixel size.
    gy, gx = np.gradient(work, res)
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))
    # flat weight: 1 where flat (→ blurred), 0 where steep (→ original); feathered.
    flat_w = 1.0 - np.clip((slope - SLOPE_LOW) / (SLOPE_HIGH - SLOPE_LOW), 0.0, 1.0)
    flat_w = gaussian_filter(flat_w, sigma=MASK_SIGMA, mode="nearest")
    out = dem * (1.0 - flat_w) + blurred * flat_w
    return np.where(water, out, dem).astype("float32")  # land + nodata untouched


def smooth_tiff(path):
    with rasterio.open(path) as src:
        profile = src.profile
        dem = src.read(1)
        res = src.res[0]
        nodata = src.nodata if src.nodata is not None else NODATA
    out = smooth_array(dem, res, nodata)
    # Re-write as a 512-blocked GTiff (aggregation_tile asserts 512 block shapes).
    profile.update(driver="GTiff", count=1, tiled=True, blockxsize=512, blockysize=512,
                   compress="deflate")
    tmp = path + ".smooth.tif"
    with rasterio.open(tmp, "w", **profile) as dst:
        dst.write(out, 1)
    os.replace(tmp, path)


def smooth_merged(tmp_folder):
    """Smooth the merged DEM of one aggregation tile in place."""
    n = len(glob.glob(f"{tmp_folder}/*.tiff"))
    smooth_tiff(f"{tmp_folder}/{n - 1}-3857.tiff")


def _check():
    """Flat noisy abyssal plain smooths; a steep step is preserved."""
    rng = np.random.default_rng(0)
    flat = -4000 + rng.normal(0, 5, (256, 256)).astype("float32")  # noisy flat
    out = smooth_array(flat, res=300.0)
    assert out.std() < flat.std(), (out.std(), flat.std())  # noise reduced

    step = np.where(np.arange(256)[None, :] < 128, -10.0, -2000.0).astype("float32")
    step = np.broadcast_to(step, (256, 256)).copy()
    out = smooth_array(step, res=10.0)  # 1990 m over 10 m = near-vertical → steep, kept
    assert abs(out[:, 0].mean() - (-10)) < 1 and abs(out[:, -1].mean() - (-2000)) < 1, \
        (out[:, 0].mean(), out[:, -1].mean())
    print("smooth.py self-check ok")


if __name__ == "__main__":
    _check()
