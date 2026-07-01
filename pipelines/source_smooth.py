"""Denoise speckly sources with a gaussian blur over water (satellite-derived bathymetry).

Allen Coral Atlas's raw 10 m SDB is per-pixel speckly. The merged-DEM smooth (smooth.py) can't
remove it: that blur is slope-gated to PRESERVE steep gradients, and speckle *is* steep, so the
noise survives into contour generation — a 2048 px all-water SDB window contours to ~1.55 M
segments at 1 m intervals (gdal_contour then runs for tens of minutes and blows the sqlite blob
limit). A small gaussian at the source removes the speckle — which is below SDB's ~±1-2 m vertical
accuracy anyway, and real shoals span many pixels and survive it — so contours follow real
bathymetry. Run after source_datum, before source_normalize:

    source_datum <id> ...
    source_smooth <id> [--sigma 4]
    source_normalize <id> ...

Measured on that window (25 fine levels): raw 1.55 M features -> sigma=4 ~3.3 k, sigma=8 ~0.6 k.
A bias-shallow (one-sided) blur was tried first but barely helped (~740 k) — SDB speckle is
symmetric, so the shallow half alone still explodes. Only water (elevation < 0) is blurred; land
and nodata clamp to 0 so they don't drag the edge, and the nodata mask is preserved exactly, so
bounds / coverage are unchanged.

ponytail: whole-tile read (tile_raster pre-splits to <=16k px ~ a few GB); window it like smooth.py
if a source ships larger single rasters.
"""

import argparse
import os
import sys
from glob import glob

import numpy as np
import rasterio
from scipy.ndimage import gaussian_filter


def smooth_array(dem, sigma, nodata, max_depth=None):
    water = (dem != nodata) & (dem < 0) if nodata is not None else (dem < 0)
    work = np.where(water, dem, 0.0).astype("float32")  # clamp nodata/land to 0 (smooth.py trick)
    blur = gaussian_filter(work, sigma=sigma, mode="nearest") if sigma else work  # sigma=0 → cutoff only
    out = np.where(water, blur, dem).astype(dem.dtype)  # only water blurred; mask + land preserved
    if max_depth and nodata is not None:
        # SDB is only reliable to ~1.5x Secchi depth; drop water deeper than the cutoff to nodata so
        # the merge feathers in the coarse-but-reliable base (GEBCO) below it — the "ACA shallow,
        # GEBCO deep" blend. Gates on the smoothed value, so it misses deep noise that reads
        # false-shallow (upgrade to a GEBCO-gated merge, or a depth-weighted feather, for that).
        out = np.where(water & (out < -max_depth), out.dtype.type(nodata), out)
    return out


def smooth_file(path, sigma, max_depth=None):
    with rasterio.open(path) as src:
        profile = src.profile
        dem = src.read(1)
        nodata = src.nodata
    out = smooth_array(dem.astype("float32"), sigma, nodata, max_depth).astype(profile["dtype"])
    tmp = path + ".smooth.tif"
    profile.update(driver="GTiff", tiled=True, blockxsize=512, blockysize=512, compress="deflate")
    with rasterio.open(tmp, "w", **profile) as dst:
        dst.write(out, 1)
    os.replace(tmp, path)


def main():
    p = argparse.ArgumentParser(description="Gaussian-denoise (over water) a source's tifs; optionally mask deep water.")
    p.add_argument("source")
    p.add_argument("--sigma", type=float, default=4.0, help="gaussian sigma in pixels (default 4; 0 = cutoff only)")
    p.add_argument("--max-depth", type=float, default=0.0,
                   help="drop water deeper than this many metres to nodata (0 = off) — masks SDB "
                        "past its reliable range so the merge fills the coarse base below it")
    a = p.parse_args()
    paths = sorted(glob(f"store/source/{a.source}/*.tif"))
    print(f"{a.source}: gaussian denoise sigma={a.sigma} max_depth={a.max_depth or 'off'} on {len(paths)} file(s)")
    for path in paths:
        smooth_file(path, a.sigma, a.max_depth or None)


def _check():
    """Speckle removed over water; land/nodata untouched; mask preserved; deep water cut to nodata."""
    rng = np.random.default_rng(0)
    nodata = 0.0
    dem = (-10 + rng.normal(0, 2, (64, 64))).astype("float32")  # noisy 10 m water
    dem[:8, :] = nodata   # a nodata band
    dem[-8:, :] = 5.0     # land (positive)
    out = smooth_array(dem, sigma=3.0, nodata=nodata)
    w = (dem != nodata) & (dem < 0)
    assert out[w].std() < dem[w].std(), (out[w].std(), dem[w].std())  # water denoised
    assert np.all(out[:8, :] == nodata), "nodata untouched"
    assert np.all(out[-8:, :] == 5.0), "land untouched"
    assert np.all((out != nodata) == (dem != nodata)), "nodata mask preserved"

    # max_depth cutoff: a shallow patch is kept, a deep patch is dropped to nodata
    d2 = np.full((16, 16), -8.0, dtype="float32")   # shallow water
    d2[8:, :] = -40.0                               # deep water -> should be masked out
    o2 = smooth_array(d2, sigma=0.0, nodata=nodata, max_depth=18.0)  # sigma=0 → cutoff only
    assert np.all(o2[:8, :] == -8.0), "shallow water kept"
    assert np.all(o2[8:, :] == nodata), "water deeper than 18 m dropped to nodata"
    print("source_smooth.py self-check ok")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        _check()
    else:
        main()
