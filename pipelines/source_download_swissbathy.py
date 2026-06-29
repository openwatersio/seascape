"""Fetch one swissBATHY3D lake (swisstopo) and mosaic its ASCII-grid tiles to a tif.

Each lake's STAC asset is a single ``*.esriasciigrid.zip`` holding hundreds of small
``swissBATHY3D_CHLV95_LN02_<x>_<y>.asc`` tiles (1-2 m, EPSG:2056 / CH1903+ LV95, bed
elevation in the LN02 height datum — metres above sea level). source_unzip only knows
``.tif``; AAIGrid tiles carry no CRS, so this step extracts every ``.asc``, mosaics them
into one GeoTIFF via a VRT (source_normalize then assigns --crs EPSG:2056), and drops
the tiles. Per lake the bed is on the LN02 datum, so a per-lake source_datum --offset
to the lake surface level makes the bed negative (the surface ~0).

file_list.txt holds the single esriasciigrid.zip URL for the lake.
"""

import glob
import os
import sys

import config
import utils


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: source_download_swissbathy.py <source-id>")
    source = sys.argv[1]
    urls = config.file_list(source)
    if not urls:
        sys.exit(f"no URLs in {config.SOURCES_DIR}/{source}/file_list.txt")
    out_dir = f"store/source/{source}"
    tiles_dir = f"{out_dir}/asc"
    os.makedirs(tiles_dir, exist_ok=True)

    zpath = f"{out_dir}/{source}.zip"
    print(f"downloading {source}: {urls[0]}")
    utils.http_download(urls[0], zpath)

    import zipfile
    with zipfile.ZipFile(zpath) as z:
        members = [n for n in z.namelist() if n.lower().endswith(".asc")]
        print(f"  {len(members)} .asc tiles")
        for name in members:
            with open(f"{tiles_dir}/{os.path.basename(name)}", "wb") as f:
                f.write(z.read(name))
    os.remove(zpath)

    # Mosaic the tiles into one GTiff (no -a_srs; source_normalize assigns EPSG:2056).
    ascs = sorted(glob.glob(f"{tiles_dir}/*.asc"))
    listfile = f"{out_dir}/tiles.txt"
    with open(listfile, "w") as f:
        f.write("\n".join(ascs) + "\n")
    vrt = f"{out_dir}/{source}.vrt"
    tif = f"{out_dir}/{source}.tif"
    print(f"  mosaicking {len(ascs)} tiles -> {tif}")
    utils.run_command(f"gdalbuildvrt -overwrite -input_file_list {listfile} {vrt}", silent=False)
    utils.run_command(
        f"gdal_translate -q -of GTiff -a_nodata -9999 -co TILED=YES -co COMPRESS=DEFLATE "
        f"-co NUM_THREADS=ALL_CPUS {vrt} {tif}", silent=False)

    os.remove(vrt)
    os.remove(listfile)
    for a in ascs:
        os.remove(a)
    os.rmdir(tiles_dir)


if __name__ == "__main__":
    main()
