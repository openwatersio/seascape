"""Build store/polygon/<id>.gpkg — the source's coverage footprint.

Vendored from mapterhorn (BSD-3). Rasterizes a 1-valued mask per file,
polygonizes it, merges all files, and dissolves to a single union polygon. The
covering uses these footprints; a future provenance/footprint vector layer
(ROADMAP.md Milestone 5) can tile them straight.

Note: shells out via utils.run_command; inputs are our own source ids/filenames
(filenames are sanitized upstream), not untrusted input.
"""

import math
import sys
import os
from multiprocessing import Pool
import shutil

import rasterio

import utils

SILENT = True

# Long-edge ceiling for the mask a file is polygonized from. Footprints only steer
# the tile covering and the coverage layer — both far coarser than native pixels —
# but a pixel-exact mask of a ragged swath grid (an AusSeabed transit corridor:
# thousands of nodata holes) takes minutes per file and unions into a ~20 MB
# polygon. Shrinking with `-r max` DILATES: any block with one valid pixel stays
# covered, so the footprint over-approximates and coverage is never lost. The
# ceiling costs fuzz of extent/1024 (~hundreds of m on an ocean corridor, a few
# tile-pixels on a harbor survey); a per-source knob is the upgrade if a source
# ever needs exact edges.
MASK_MAX_PX = 1024


def polygonize_tif(source, filename):
    src = f"store/source/{source}/{filename}"
    mask = f"store/polygon/{source}/{filename}"
    with rasterio.open(src) as r:
        factor = math.ceil(max(r.width, r.height) / MASK_MAX_PX)
        size = (max(1, r.width // factor), max(1, r.height // factor))
    if factor > 1:
        small = mask + ".small.tif"
        utils.run_command(
            f'GDAL_CACHEMAX=1024 gdalwarp -q -overwrite -ts {size[0]} {size[1]} '
            f'-r max {src} {small}', silent=SILENT)
        src = small
    utils.run_command(
        f'GDAL_CACHEMAX=1024 gdal_calc.py -A {src} '
        f'--outfile={mask} --calc="A*0+1" --type=Byte --overwrite', silent=SILENT)
    utils.run_command(
        f'GDAL_CACHEMAX=1024 gdal_polygonize.py {mask} -b 1 -f "GPKG" '
        f'store/polygon/{source}/{filename}.gpkg -overwrite', silent=SILENT)
    os.remove(mask)
    if factor > 1:
        os.remove(src)


def get_filenames(source):
    with open(f"store/source/{source}/bounds.csv") as f:
        lines = [l.strip() for l in f.readlines()[1:]]
    return [line.split(",")[0] for line in lines]


def polygonize_source(source, processes):
    filenames = get_filenames(source)
    utils.create_folder(f"store/polygon/{source}/")
    with Pool(processes) as pool:
        pool.starmap(polygonize_tif, [(source, fn) for fn in filenames], chunksize=1)


def merge_source(source):
    filenames = get_filenames(source)
    merged = f"store/polygon/{source}/merged.gpkg"
    if os.path.isfile(merged):
        os.remove(merged)
    # Reproject every per-file footprint to EPSG:4326 before union. Each polygon is
    # emitted in its tif's native CRS; for a mixed_crs source (per-file UTM zones,
    # e.g. noaa_estuarine) merging without -t_srs would union UTM metres with degrees
    # into a nonsensical polygon. 4326 is the common frame the covering/footprint
    # layer expect; single-CRS sources are unaffected (one reprojection to lon/lat).
    utils.run_command(f"ogr2ogr -f GPKG -t_srs EPSG:4326 {merged} store/polygon/{source}/{filenames[0]}.gpkg", silent=False)
    for j, filename in enumerate(filenames[1:]):
        if j % 100 == 0:
            print(f"{j:_} / {len(filenames):_}")
        utils.run_command(
            f"ogr2ogr -f GPKG -t_srs EPSG:4326 -update -append {merged} "
            f"store/polygon/{source}/{filename}.gpkg -nln out -addfields", silent=True)
    union = f"store/polygon/{source}.gpkg"
    if os.path.isfile(union):
        os.remove(union)
    utils.run_command(
        f'ogr2ogr -f GPKG {union} {merged} -nln union -dialect sqlite '
        f'-sql "SELECT ST_Union(ST_MakeValid(geom)) AS geom FROM out"', silent=False)


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: source_polygonize.py <source-id> <processes>")
    source, processes = sys.argv[1], int(sys.argv[2])
    print(f"polygonizing {source} with {processes} processes...")
    polygonize_source(source, processes)
    merge_source(source)
    shutil.rmtree(f"store/polygon/{source}")


def _check():
    """A sparse raster wide enough to trigger the mask downsample keeps every
    valid speck in its footprint (dilation, not erosion)."""
    import json
    import subprocess
    import tempfile
    import numpy as np
    from rasterio.transform import from_origin
    from shapely.geometry import shape, Point

    tmp = tempfile.mkdtemp()
    cwd = os.getcwd()
    os.chdir(tmp)
    try:
        os.makedirs("store/source/_synth")
        arr = np.full((3000, 3000), -9999.0, dtype="float32")
        arr[10:13, 10:13] = -5.0        # speck near one corner
        arr[2900:2903, 2900:2903] = -7.0  # speck near the other
        with rasterio.open("store/source/_synth/_synth_0.tif", "w", driver="GTiff",
                           height=3000, width=3000, count=1, dtype="float32",
                           nodata=-9999.0, crs="EPSG:4326",
                           transform=from_origin(0.0, 3.0, 0.001, 0.001)) as d:
            d.write(arr, 1)
        utils.create_folder("store/polygon/_synth/")
        polygonize_tif("_synth", "_synth_0.tif")
        geo = subprocess.run(
            ["ogr2ogr", "-f", "GeoJSON", "/vsistdout/", "store/polygon/_synth/_synth_0.tif.gpkg"],
            capture_output=True, check=True).stdout
        polys = [shape(f["geometry"]) for f in json.loads(geo)["features"]]
        for x, y in [(0.0115, 2.9885), (2.9015, 0.0985)]:  # speck centers in map coords
            assert any(p.contains(Point(x, y)) for p in polys), (x, y)
        print("source_polygonize.py self-check ok")
    finally:
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        _check()
    else:
        main()
