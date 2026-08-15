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

import config
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
    return _raster_info(src)[0]


def _raster_info(src):
    """(ground pixel size in metres, width px, height px)."""
    info = json.loads(subprocess.run(["gdalinfo", "-json", src],
                                     capture_output=True, text=True, check=True).stdout)
    width, height = info.get("size", [0, 0])
    gt = info.get("geoTransform")
    if not gt or not gt[1]:
        return COARSEN_TARGET_M, width, height
    wkt = info.get("coordinateSystem", {}).get("wkt", "").lstrip()
    geographic = wkt.upper().startswith(("GEOGCRS", "GEOGCS"))
    return (abs(gt[1]) * 111_320 if geographic else abs(gt[1])), width, height


def _decimated_size(width, height, pct):
    """The decimated raster's size in PIXELS, never smaller than 1x1.

    Sized in pixels rather than passed to gdal_translate as a percentage: a small raster times a
    small percentage rounds to a fraction, and `-outsize 2% 2%` on a 27 px tile asks for 0.54 px
    and fails the whole polygon job. A footprint only needs the tile to survive as SOME pixels."""
    return max(1, round(width * pct / 100)), max(1, round(height * pct / 100))


def polygonize_tif(source, filename):
    src = f"store/source/{source}/{filename}"
    mask = f"store/polygon/{source}/{filename}"
    native_m, width, height = _raster_info(src)
    pct = _decimate_pct(native_m)
    calc_src = src
    if pct < 100:
        out_w, out_h = _decimated_size(width, height, pct)
        # -r nearest drops sub-target speckle, which is what we want for a footprint.
        # ponytail: switch to a coverage-preserving resample if a genuinely sparse source ever
        # draws under-covered — no such source today.
        calc_src = f"{mask}.small.tif"
        utils.run_command(
            f'GDAL_CACHEMAX=1024 gdal_translate -outsize {out_w} {out_h} -r nearest '
            f'{src} {calc_src}', silent=SILENT)
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
    return [row[0] for row in config.source_files(source)]


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
    import shutil
    import tempfile

    assert _decimate_pct(1) == 1        # 1 m native -> 1% (~100 m)
    assert _decimate_pct(10) == 10
    assert _decimate_pct(100) == 100    # already at the target -> no-op
    assert _decimate_pct(450) == 100    # coarser than the target -> never upsampled

    # A tile small enough that its decimated size rounds below one pixel still has to produce a
    # raster: uk_cco's 27 px NT9550 asked gdal_translate for 0.54 px and failed the whole source.
    assert _decimated_size(27, 27, 2) == (1, 1), _decimated_size(27, 27, 2)
    assert _decimated_size(1, 1, 1) == (1, 1)
    assert _decimated_size(4000, 2000, 2) == (80, 40)  # the ordinary case is unchanged
    assert _decimated_size(150, 30, 2) == (3, 1), "each axis floors independently"

    if not (shutil.which("gdal_translate") and shutil.which("gdal_polygonize.py")):
        print("source_polygonize.py ok (tiny-raster path skipped — no GDAL CLI)")
        return
    # …and the whole real path over such a tile, since the failure was in the shell-out.
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    d = tempfile.mkdtemp()
    cwd = os.getcwd()
    try:
        os.chdir(d)
        sid, name = "_poly_tiny", "tiny.tif"
        utils.create_folder(f"store/source/{sid}")
        utils.create_folder(f"store/polygon/{sid}")
        with rasterio.open(f"store/source/{sid}/{name}", "w", driver="GTiff", height=27,
                           width=27, count=1, dtype="float32", nodata=-9999.0,
                           crs="EPSG:27700", transform=from_origin(0, 54, 2, 2)) as dst:
            dst.write(np.full((27, 27), -5.0, dtype="float32"), 1)
        polygonize_tif(sid, name)
        out = f"store/polygon/{sid}/{name}.gpkg"
        assert os.path.isfile(out), "a tiny tile must still polygonize"
        info = json.loads(subprocess.run(["ogrinfo", "-json", out],
                                         capture_output=True, text=True, check=True).stdout)
        assert info["layers"][0]["featureCount"] >= 1, info["layers"][0]["featureCount"]
    finally:
        os.chdir(cwd)
        shutil.rmtree(d, ignore_errors=True)
    print("source_polygonize.py ok (incl. a sub-pixel decimation)")


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
