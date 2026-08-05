"""Normalize source rasters to internally-tiled LERC COGs.

Assigns the horizontal CRS and nodata from metadata.json (via ``-a_srs``/
``-a_nodata`` — NOT reprojection, so no vertical/geoid shift; the warp to Web
Mercator happens later in aggregation) and writes the COG with a shoal-biased
overview pyramid.

The aggregation warp builds each tile at the FINEST source's resolution, so a source finer
than that grid (CUDEM 1/9 arc-second into a z13 tile is ~2.2x) is decimated on read. With
overviews present that read starts from a class-aware shoal level instead of the full-res grid:
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
COG_OPTS = ["-co", "BLOCKSIZE=512", "-co", "SPARSE_OK=YES", "-co", "BIGTIFF=IF_NEEDED",
            "-co", "COMPRESS=ZSTD",
            # not ALL_CPUS: prep normalizes files in parallel, so the compressor pool is
            # per-worker (utils.GDAL_WORKER_THREADS)
            "-co", f"NUM_THREADS={utils.GDAL_WORKER_THREADS}"]


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


def _reduce_ref(prev, nodata, cap):
    """The class-aware reduction spelled out independently of utils, for the self-check to assert
    against: (level, all-children-water mask). A block with any water-domain child reduces over
    those alone; an all-land block over its valid children; an all-nodata block stays nodata."""
    import numpy as np
    h, w = prev.shape[0] // 2, prev.shape[1] // 2
    q = prev[:h * 2, :w * 2].reshape(h, 2, w, 2)
    good = (q != nodata) & (q == q)
    wet = good & (q <= cap)
    land_max = np.where(good, q, -np.inf).max(axis=(1, 3))
    wet_max = np.where(wet, q, -np.inf).max(axis=(1, 3))
    lvl = np.where(wet.any(axis=(1, 3)), wet_max, land_max)
    lvl[~good.any(axis=(1, 3))] = nodata
    return lvl.astype(prev.dtype), (wet | ~good).all(axis=(1, 3)) & good.any(axis=(1, 3))


def _check():
    """The normalized COG's class-aware shoal pyramid.

    Fixture 1, a 2048 px all-water shoal field (a one-pixel -2 m shoal on a -50 m bed, an all-nodata
    block, and a block that is nodata except for one shallow pixel): every level must (a) match the
    class-aware reduction of the level above, (b) be pointwise >= the same level built with
    OVERVIEW_RESAMPLING=AVERAGE wherever the block is entirely water-domain — the only domain in
    which comparing to an average of everything under the block means anything — and (c) keep the
    all-nodata block nodata while the 3-nodata-plus-a-shoal block reads the shoal.

    Fixture 2, a mixed coastline (land, drying flat, dredged channel) cut by a one-pixel channel:
    the channel never turns positive at any level down to a single pixel, the drying flat stays in
    the drying domain, and all-land blocks stay land.

    A file with no declared nodata gets no pyramid at all."""
    import shutil
    import tempfile

    import numpy as np
    from rasterio.transform import from_origin

    import config

    ND, N, CAP = -9999.0, 2048, config.DRYING_CAP
    d = tempfile.mkdtemp()

    def write(path, a):
        with rasterio.open(path, "w", driver="GTiff", height=a.shape[0], width=a.shape[1], count=1,
                           dtype="float32", nodata=ND, crs="EPSG:4326",
                           transform=from_origin(0, 1, 1e-4, 1e-4)) as f:
            f.write(a, 1)
        return path

    try:
        arr = np.full((N, N), -50.0, dtype="float32")
        arr[100, 100] = -2.0                       # an isolated shoal among -50 m
        arr[200:204, 200:204] = ND                 # an all-nodata block
        arr[300:304, 300:304] = ND
        arr[303, 303] = -5.0                       # nodata must not swallow this one
        src = write(f"{d}/a.tif", arr)
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
                want, allwet = _reduce_ref(prev, ND, CAP)
                assert np.array_equal(lvl, want), factor
                assert allwet[(prev != ND).reshape(h, 2, w, 2).any(axis=(1, 3))].all(), \
                    "fixture 1 is all water: every valid block must be water-domain"
                a = ref.read(1, out_shape=(h, w))
                both = (lvl != ND) & (a != ND) & allwet
                assert (lvl[both] >= a[both] - 1e-6).all(), \
                    f"level {factor}: {int((lvl[both] < a[both] - 1e-6).sum())} px deeper than AVERAGE"
                assert lvl[100 // factor, 100 // factor] == -2.0, (factor, "shoal drowned")
                assert lvl[200 // factor, 200 // factor] == ND, (factor, "all-nodata block")
                assert lvl[303 // factor, 303 // factor] == -5.0, (factor, "nodata swallowed a shoal")
                prev = lvl
            # the AVERAGE pyramid is what the assertions are worth: it drowns the shoal
            assert ref.read(1, out_shape=(N // 4, N // 4))[25, 25] < -40.0

        # A coastline in one footprint: land above, a drying flat at the water's edge, a dredged
        # basin below, and a one-pixel channel cut through the land into it. CHAN sits in the same
        # 2x2 block as land at every level, so it is the case a plain max would close.
        CHAN = 1020
        coast = np.full((N, N), -10.0, dtype="float32")
        coast[:1024, :] = 30.0                     # land, well above the cap
        coast[1024:1040, :] = 1.0                  # drying flat
        coast[:1040, CHAN] = -10.0                 # the channel, one pixel wide
        cpath = write(f"{d}/coast.tif", coast)
        normalize_file(cpath, "EPSG:4326", ND)
        with rasterio.open(cpath) as got:
            prev = coast
            for factor in got.overviews(1):
                lvl = got.read(1, out_shape=(N // factor, N // factor))
                want, _ = _reduce_ref(prev, ND, CAP)
                assert np.array_equal(lvl, want), factor
                prev = lvl
        # A block is pure land only while it stays clear of the channel column (2**level <= CHAN)
        # and above the drying flat (2**level <= 1024).
        level, step = 0, coast
        while 2 ** level < 1024:
            step, _ = _reduce_ref(step, ND, CAP)
            level += 1
            assert step[0, CHAN >> level] == -10.0, \
                (f"level {2 ** level}: the channel through the land closed", step[0, CHAN >> level])
            assert step[1024 >> level, 0] == 1.0, \
                (f"level {2 ** level}: the drying flat left the drying domain", step[1024 >> level, 0])
            if 2 ** level <= CHAN:
                assert step[0, 0] == 30.0, \
                    (f"level {2 ** level}: an all-land block must stay land", step[0, 0])

        # One pixel of water through solid land, reduced by the REAL kernel to a single pixel: the
        # channel is the only water in its block at every level, and land never takes a block.
        chan = np.full((N, N), 30.0, dtype="float32")
        chan[:, CHAN] = -10.0
        step, level = write(f"{d}/chan.tif", chan), 0
        prev = chan
        while True:
            nxt = f"{d}/chan-{level + 1}.tif"
            w, h = utils._block_reduce(step, nxt, ND)
            level += 1
            with rasterio.open(nxt) as f:
                lvl = f.read(1)
            want, _ = _reduce_ref(prev, ND, CAP)
            assert np.array_equal(lvl, want), f"level {2 ** level} diverged from the class rule"
            assert (lvl[:, CHAN >> level] == -10.0).all(), \
                (f"level {2 ** level}: a one-pixel channel closed — "
                 f"{int((lvl[:, CHAN >> level] > CAP).sum())} px went land")
            if 2 ** level <= CHAN:
                assert (lvl[:, 0] == 30.0).all(), f"level {2 ** level}: land lost its own blocks"
            if max(w, h) == 1:
                break
            step, prev = nxt, lvl
        assert level == 11, f"the reduction must run to a single pixel, stopped at level {level}"

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
