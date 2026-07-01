"""Tile large source rasters in store/source/<id>/ into smaller tiles, dropping all-nodata tiles.

A recipe step for sources whose raw download is one raster too large for the per-file
`source_*` steps to hold in memory (`source_datum` reads a whole band via rasterio;
`source_polygonize` via gdal_calc). A ~23 GB Allen Coral Atlas regional GeoTIFF or the ~32 GB
GSC Pacific DEM would OOM. Run it after `source_download`, before `source_datum`:

    source_download <id>
    tile_raster <id>            # split each store/source/<id>/*.tif into tiles, drop empties
    source_datum <id> ...

Memory-safe: each tile is cut with a windowed `gdal_translate -srcwin` (block streaming) and
emptiness is tested on a windowed mask read — the whole raster is never materialized. Source
CRS / nodata / dtype carry over (the values stay PRISTINE; `source_normalize` re-asserts CRS+nodata,
`source_datum` applies any unit/datum transform). Each input raster is replaced by its tiles
(`tile_<base>_<row>_<col>.tif`); already-tiled outputs are skipped so a rerun is idempotent.

ponytail: gdal_translate per tile (not gdal_retile) so emptiness is judged before a tile is
written — no all-nodata tiles created then pruned.
"""

import argparse
import os
import subprocess
import sys
from glob import glob

import rasterio
from rasterio.windows import Window


def tile_file(path, size):
    """Split one raster into tile_<base>_RR_CC.tif beside it; return the kept tile names."""
    base = os.path.splitext(os.path.basename(path))[0]
    out_dir = os.path.dirname(path) or "."
    with rasterio.open(path) as src:
        width, height = src.width, src.height
    ncols = (width + size - 1) // size
    nrows = (height + size - 1) // size
    kept = []
    for r in range(nrows):
        for c in range(ncols):
            col_off, row_off = c * size, r * size
            w = min(size, width - col_off)
            h = min(size, height - row_off)
            with rasterio.open(path) as src:  # reopen per tile so only this window is read
                if not src.read_masks(1, window=Window(col_off, row_off, w, h)).any():
                    continue  # all-nodata tile (open ocean) — skip
            name = f"tile_{base}_{r:02d}_{c:02d}.tif"
            subprocess.run(
                ["gdal_translate", "-q", "-of", "GTiff",
                 "-srcwin", str(col_off), str(row_off), str(w), str(h),
                 "-co", "TILED=YES", "-co", "COMPRESS=ZSTD", "-co", "BIGTIFF=IF_NEEDED",
                 path, os.path.join(out_dir, name)],
                check=True)
            kept.append(name)
    return kept


def tile_source(source, size):
    paths = [p for p in sorted(glob(f"store/source/{source}/*.tif"))
             if not os.path.basename(p).startswith("tile_")]  # skip our own outputs (idempotent rerun)
    print(f"{source}: tiling {len(paths)} file(s) at {size}px")
    for p in paths:
        kept = tile_file(p, size)
        os.remove(p)  # replace the monolith with its tiles
        print(f"  {os.path.basename(p)} -> {len(kept)} non-empty tile(s)")


def main():
    p = argparse.ArgumentParser(description="Tile large rasters in store/source/<id>/, dropping all-nodata tiles.")
    p.add_argument("source")
    p.add_argument("--size", type=int, default=16384, help="tile edge in pixels (default 16384)")
    a = p.parse_args()
    tile_source(a.source, a.size)


def _check():
    """Self-check: an all-nodata quadrant is dropped; data tiles keep their values."""
    import tempfile
    import numpy as np
    from rasterio.transform import from_origin

    d = tempfile.mkdtemp()
    path = os.path.join(d, "big.tif")
    arr = np.zeros((4, 4), dtype="int16")           # 2x2 grid of 2x2 tiles, nodata=0
    arr[0:2, 0:2] = [[5, 10], [3, 0]]               # only the top-left tile has valid data
    with rasterio.open(path, "w", driver="GTiff", height=4, width=4, count=1, dtype="int16",
                       nodata=0, crs="EPSG:4326", transform=from_origin(0, 4, 1, 1)) as dst:
        dst.write(arr, 1)

    kept = tile_file(path, size=2)
    assert kept == ["tile_big_00_00.tif"], kept     # other three tiles are all-nodata -> dropped
    with rasterio.open(os.path.join(d, "tile_big_00_00.tif")) as t:
        o = t.read(1)
        assert t.nodata == 0, t.nodata              # nodata carried over
    assert o[0, 0] == 5 and o[0, 1] == 10 and o[1, 0] == 3, o
    print("tile_raster.py self-check ok")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        _check()
    else:
        main()
