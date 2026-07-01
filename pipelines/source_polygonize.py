"""Build store/polygon/<id>.gpkg — the source's coverage footprint.

Vendored from mapterhorn (BSD-3). Rasterizes a 1-valued mask per file,
polygonizes it, merges all files, and dissolves to a single union polygon. The
covering uses these footprints; a future provenance/footprint vector layer
(ROADMAP.md Milestone 5) can tile them straight.

Note: shells out via utils.run_command; inputs are our own source ids/filenames
(filenames are sanitized upstream), not untrusted input.
"""

import sys
import os
from multiprocessing import Pool
import shutil

import rasterio

import utils

SILENT = True

# Cap the polygonized mask's longest side (pixels). Pixel-exact polygons of a speckly mask — e.g.
# scattered satellite-derived reefs across a 16k-px Allen Coral Atlas tile — blow past gdal/sqlite
# vertex limits ("sqlite3_bind_blob() failed: too big"). Larger masks are downsampled to this first,
# only ever downscaling and taking the max per block so coverage never shrinks (it generalizes
# outward, the conservative direction). The footprint feeds the covering (which aggregation tiles to
# consider); ~native/N precision is ample there. Files already <= this are untouched.
COVERAGE_MAX_PX = 1024


def polygonize_tif(source, filename):
    src_tif = f"store/source/{source}/{filename}"
    mask = f"store/polygon/{source}/{filename}"
    utils.run_command(
        f'GDAL_CACHEMAX=1024 gdal_calc.py -A {src_tif} '
        f'--outfile={mask} --calc="A*0+1" --type=Byte --overwrite', silent=SILENT)
    with rasterio.open(src_tif) as s:
        width, height = s.width, s.height
    factor = max(1, -(-max(width, height) // COVERAGE_MAX_PX))  # ceil-divide -> downscale factor
    if factor > 1:
        coarse = mask + ".coarse.tif"
        utils.run_command(
            f'GDAL_CACHEMAX=1024 gdalwarp -r max '
            f'-ts {max(1, width // factor)} {max(1, height // factor)} '
            f'-overwrite {mask} {coarse}', silent=SILENT)
        os.replace(coarse, mask)
    utils.run_command(
        f'GDAL_CACHEMAX=1024 gdal_polygonize.py {mask} -b 1 -f "GPKG" '
        f'store/polygon/{source}/{filename}.gpkg -overwrite', silent=SILENT)
    os.remove(mask)


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


if __name__ == "__main__":
    main()
