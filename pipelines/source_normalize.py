"""Normalize source rasters to internally-tiled LERC COGs.

Assigns the horizontal CRS and nodata from metadata.json (via ``-a_srs``/
``-a_nodata`` — NOT reprojection, so no vertical/geoid shift; the warp to Web
Mercator happens later in aggregation) and writes the COG with a shoal-biased
overview pyramid.

The aggregation warp builds each tile at the FINEST source's resolution, so a source finer
than that grid (CUDEM 1/9 arc-second into a z13 tile is ~2.2x) is decimated on read. With
overviews present that read starts from a per-cell MAX level instead of the full-res grid:
measured on a Chesapeake CUDEM tile, the warp halves (2.9s -> 1.5s) and the output moves
+0.11 m shallower on average, +5.9 m at the shoals it stops drowning.
"""

import argparse
import os
import subprocess
import sys
from glob import glob

import rasterio

import utils


# Lossless ZSTD compression (level 9 default): ~4% smaller than DEFLATE and
# ~2.7x faster on GEBCO Int16, and it's in the stock Ubuntu/Homebrew GDAL builds
# (unlike LERC). The predictor is chosen per file: 3 (floating-point) only works
# on Float32/64; integer rasters (e.g. GEBCO's Int16) need 2 (horizontal differencing).
COG_OPTS = ["-co", "BLOCKSIZE=512", "-co", "SPARSE_OK=YES",
            "-co", "BIGTIFF=IF_NEEDED", "-co", "COMPRESS=ZSTD", "-co", "NUM_THREADS=ALL_CPUS"]


def predictor_for(filepath):
    with rasterio.open(filepath) as src:
        dtype = src.dtypes[0]
    if dtype in ("float32", "float64"):
        return "3"
    return "2" if "int" in dtype else "1"


def normalize_file(filepath, crs, nodata):
    tmp = filepath + ".norm.tif"
    args = []
    if crs:
        args += ["-a_srs", crs]
    if nodata is not None:
        args += ["-a_nodata", str(nodata)]
    with utils.shoal_cog_source(filepath, " ".join(args)) as src:
        subprocess.run(["gdal_translate", "-of", "COG", *COG_OPTS,
                        "-co", f"PREDICTOR={predictor_for(filepath)}",
                        "-co", "OVERVIEWS=FORCE_USE_EXISTING", src, tmp], check=True)
    os.replace(tmp, filepath)


def main():
    p = argparse.ArgumentParser(description="Assign CRS/nodata and rewrite as a ZSTD COG.")
    p.add_argument("source")
    p.add_argument("--crs", help="horizontal CRS to assign (e.g. EPSG:4269)")
    p.add_argument("--nodata", help="nodata value to assign")
    a = p.parse_args()
    filepaths = sorted(glob(f"store/source/{a.source}/*.tif"))
    print(f"{a.source}: normalize {len(filepaths)} file(s) (crs={a.crs} nodata={a.nodata})")
    for filepath in filepaths:
        normalize_file(filepath, a.crs, a.nodata)


def _check():
    """The normalized COG's shoal-biased pyramid, against a synthetic 2048 px shoal field:
    a one-pixel -2 m shoal on a -50 m bed, an all-nodata block, and a block that is nodata
    except for one shallow pixel. Every level must (a) equal the max of its children,
    (b) be pointwise >= the same level built with OVERVIEW_RESAMPLING=AVERAGE, (c) keep the
    all-nodata block nodata while the 3-nodata-plus-a-shoal block reads the shoal. A file with
    no declared nodata gets no pyramid at all."""
    import shutil
    import tempfile

    import numpy as np
    from rasterio.transform import from_origin

    ND, N = -9999.0, 2048
    d = tempfile.mkdtemp()
    try:
        arr = np.full((N, N), -50.0, dtype="float32")
        arr[100, 100] = -2.0                       # an isolated shoal among -50 m
        arr[200:204, 200:204] = ND                 # an all-nodata block
        arr[300:304, 300:304] = ND
        arr[303, 303] = -5.0                       # nodata must not swallow this one
        src = f"{d}/a.tif"
        with rasterio.open(src, "w", driver="GTiff", height=N, width=N, count=1, dtype="float32",
                           nodata=ND, crs="EPSG:4326", transform=from_origin(0, 1, 1e-4, 1e-4)) as f:
            f.write(arr, 1)
        avg = f"{d}/avg.tif"
        subprocess.run(["gdal_translate", "-q", "-of", "COG", "-co", "BLOCKSIZE=512",
                        "-co", "OVERVIEW_RESAMPLING=AVERAGE", src, avg], check=True)
        normalize_file(src, "EPSG:4326", ND)

        with rasterio.open(src) as got, rasterio.open(avg) as ref:
            assert got.overviews(1) == [2, 4], got.overviews(1)
            assert got.crs.to_epsg() == 4326 and got.nodata == ND, (got.crs, got.nodata)
            prev = arr
            for factor in got.overviews(1):
                h = w = N // factor
                lvl = got.read(1, out_shape=(h, w))
                # (a) exactly the max of the level above, nodata excluded
                q = prev.reshape(h, 2, w, 2)
                want = np.where(q == ND, -np.inf, q).max(axis=(1, 3))
                want[np.isinf(want)] = ND
                assert np.array_equal(lvl, want.astype("float32")), factor
                # (b) never deeper than the average pyramid at the same pixel
                a = ref.read(1, out_shape=(h, w))
                both = (lvl != ND) & (a != ND)
                assert (lvl[both] >= a[both] - 1e-6).all(), \
                    f"level {factor}: {int((lvl[both] < a[both] - 1e-6).sum())} px deeper than AVERAGE"
                assert lvl[100 // factor, 100 // factor] == -2.0, (factor, "shoal drowned")
                assert lvl[200 // factor, 200 // factor] == ND, (factor, "all-nodata block")
                assert lvl[303 // factor, 303 // factor] == -5.0, (factor, "nodata swallowed a shoal")
                prev = lvl
            # the AVERAGE pyramid is what the assertions are worth: it drowns the shoal
            assert ref.read(1, out_shape=(N // 4, N // 4))[25, 25] < -40.0

        # Odd dimensions: levels must be ceil-sized like GDAL's own, and the padded edge block
        # must max over only the real pixel it covers rather than reading the pad as data.
        odd = f"{d}/odd.tif"
        oarr = np.full((1023, 1025), -50.0, dtype="float32")
        oarr[:, -1] = -3.0  # the odd trailing column, which every edge block must carry up
        with rasterio.open(odd, "w", driver="GTiff", height=1023, width=1025, count=1,
                           dtype="float32", nodata=ND, crs="EPSG:4326",
                           transform=from_origin(0, 1, 1e-4, 1e-4)) as f:
            f.write(oarr, 1)
        normalize_file(odd, "EPSG:4326", ND)
        with rasterio.open(odd) as got:
            assert got.overviews(1) == [2, 4], got.overviews(1)
            for factor, size in ((2, (512, 513)), (4, (256, 257))):
                lvl = got.read(1, out_shape=size)
                assert lvl.shape == size, (factor, lvl.shape)
                assert (lvl[:, -1] == -3.0).all(), (factor, "odd edge column lost its shoal")
                assert (lvl[:, :-1] == -50.0).all(), (factor, "pad leaked into the interior")

        bare = f"{d}/b.tif"
        with rasterio.open(bare, "w", driver="GTiff", height=N, width=N, count=1, dtype="float32",
                           crs="EPSG:4326", transform=from_origin(0, 1, 1e-4, 1e-4)) as f:
            f.write(arr, 1)
        normalize_file(bare, "EPSG:4326", None)
        with rasterio.open(bare) as got:
            assert got.overviews(1) == [], got.overviews(1)
        print("source_normalize.py self-check ok")
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        _check()
    else:
        main()
