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
import json
import subprocess
from multiprocessing import Pool
import shutil

import utils

SILENT = True

# The footprint feeds ONLY the coverage layer, which reads it with -simplify 0.001 (~100 m) and
# tiles to z8. So polygonize a mask decimated to ~this resolution: at native resolution a dense
# coastal source's speckle explodes into millions of tiny polygons, and merge_source's single
# ST_Union over them becomes an unbounded single-threaded GEOS job (nz_coastal: 11 M features, 20 GB).
COARSEN_TARGET_M = 100


def _decimate_pct(native_m, target_m=COARSEN_TARGET_M):
    """gdal_translate -outsize percentage to bring native_m ground resolution to ~target_m,
    capped at 100 so a source already coarser than the target is never upsampled."""
    return min(100, max(1, round(native_m / target_m * 100)))


def _native_res_m(src):
    """The source's ground pixel size in metres (degrees converted at the equator — footprint-
    grade, exact resolution is discarded downstream). Falls back to the target (no decimation)."""
    info = json.loads(subprocess.run(["gdalinfo", "-json", src],
                                     capture_output=True, text=True, check=True).stdout)
    gt = info.get("geoTransform")
    if not gt or not gt[1]:
        return COARSEN_TARGET_M
    wkt = info.get("coordinateSystem", {}).get("wkt", "").lstrip()
    geographic = wkt.upper().startswith(("GEOGCRS", "GEOGCS"))
    return abs(gt[1]) * 111_320 if geographic else abs(gt[1])


def polygonize_tif(source, filename):
    src = f"store/source/{source}/{filename}"
    mask = f"store/polygon/{source}/{filename}"
    pct = _decimate_pct(_native_res_m(src))
    calc_src = src
    if pct < 100:
        # -r nearest drops sub-target speckle, which is what we want for a footprint.
        # ponytail: switch to a coverage-preserving resample if a genuinely sparse source ever
        # draws under-covered — no such source today.
        calc_src = f"{mask}.small.tif"
        utils.run_command(
            f'GDAL_CACHEMAX=1024 gdal_translate -outsize {pct}% {pct}% -r nearest {src} {calc_src}',
            silent=SILENT)
    utils.run_command(
        f'GDAL_CACHEMAX=1024 gdal_calc.py -A {calc_src} '
        f'--outfile={mask} --calc="A*0+1" --type=Byte --overwrite', silent=SILENT)
    utils.run_command(
        f'GDAL_CACHEMAX=1024 gdal_polygonize.py {mask} -b 1 -f "GPKG" '
        f'store/polygon/{source}/{filename}.gpkg -overwrite', silent=SILENT)
    os.remove(mask)
    if calc_src != src:
        os.remove(calc_src)


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


def _check():
    assert _decimate_pct(1) == 1        # 1 m native -> 1% (~100 m)
    assert _decimate_pct(10) == 10
    assert _decimate_pct(100) == 100    # already at the target -> no-op
    assert _decimate_pct(450) == 100    # coarser than the target -> never upsampled
    print("source_polygonize.py ok")


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--check":
        return _check()
    if len(sys.argv) != 3:
        sys.exit("usage: source_polygonize.py <source-id> <processes>")
    source, processes = sys.argv[1], int(sys.argv[2])
    print(f"polygonizing {source} with {processes} processes...")
    polygonize_source(source, processes)
    merge_source(source)
    shutil.rmtree(f"store/polygon/{source}")


if __name__ == "__main__":
    main()
