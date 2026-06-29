"""Fetch the GLWNB-2020 African Great Lakes bathymetry and extract the lake DEMs.

The DEMs ship as a single .7z (Bathymetric_Rasters.7z) inside the Harvard Dataverse
dataset doi:10.7910/DVN/ITCOGT — four Analytical rasters: Victoria, Albert, Edward,
George. We don't have a 7z binary, but GDAL's ``/vsi7z/`` reads members directly, so
each lake is translated straight out of the archive into store/source/<id>/.

Per-lake CRS differs (Victoria = Africa Lambert Conformal Conic / ESRI:102024, no
EPSG code but a full WKT; Albert/George = UTM 36N / EPSG:32636; Edward = UTM 35S /
EPSG:32735) → mixed_crs source: gdal_translate keeps each file's embedded CRS, so
source_normalize runs WITHOUT --crs and aggregation reprojects each from its own
frame. Values are water DEPTH (positive down, 0 at the lake surface) per the paper
(Hamilton et al. 2022, Sci. Data) → source_datum --negate, no offset.

file_list.txt holds the single Dataverse access URL for the .7z.
"""

import os
import sys

import config
import utils

# Member names inside the archive (one Analytical raster per lake).
LAKES = ["Lake_Victoria", "Lake_Albert", "Lake_Edward", "Lake_George"]
ARCHIVE_DIR = "Bathymetric_Rasters"


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: source_download_african_lakes.py <source-id>")
    source = sys.argv[1]
    urls = config.file_list(source)
    if not urls:
        sys.exit(f"no URLs in {config.SOURCES_DIR}/{source}/file_list.txt")
    out_dir = f"store/source/{source}"
    os.makedirs(out_dir, exist_ok=True)

    archive = f"{out_dir}/Bathymetric_Rasters.7z"
    print(f"downloading {source}: {urls[0]}")
    utils.http_download(urls[0], archive)

    for lake in LAKES:
        member = f"/vsi7z/{{{archive}}}/{ARCHIVE_DIR}/{lake}_Analytical_ras.tif"
        tif = f"{out_dir}/{lake}.tif"
        print(f"  extract {lake}")
        # System GDAL CLI (not rasterio's bundled libgdal) for /vsi7z; keeps the
        # embedded per-lake CRS (no -a_srs). source_normalize makes the final COG.
        utils.run_command(
            f"gdal_translate -q -of GTiff -co TILED=YES -co COMPRESS=DEFLATE {member} {tif}",
            silent=False,
        )
    os.remove(archive)


if __name__ == "__main__":
    main()
