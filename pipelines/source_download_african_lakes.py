"""Fetch the GLWNB-2020 African Great Lakes bathymetry and extract the lake DEMs.

The DEMs ship as a single .7z (Bathymetric_Rasters.7z) inside the Harvard Dataverse
dataset doi:10.7910/DVN/ITCOGT — four Analytical rasters: Victoria, Albert, Edward,
George. Extracted with py7zr (a Python 7z library) rather than GDAL's ``/vsi7z``:
``/vsi7z`` needs a GDAL built with the libarchive backend, which the CI image's apt
``gdal-bin`` lacks — and this is a Python fetcher, so a Python extractor is the portable
choice.

Per-lake CRS differs (Victoria = Africa Lambert Conformal Conic / ESRI:102024, no EPSG
code but a full WKT; Albert/George = UTM 36N / EPSG:32636; Edward = UTM 35S / EPSG:32735)
→ mixed_crs source: the extracted GeoTIFFs keep their embedded CRS, so source_normalize
runs WITHOUT --crs and aggregation reprojects each from its own frame. Values are water
DEPTH (positive down, 0 at the lake surface) per Hamilton et al. 2022, Sci. Data →
source_datum --negate, no offset.

file_list.txt holds the single Dataverse access URL for the .7z.
"""

import os
import shutil
import sys

import py7zr

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
    out_dir = f"store/download/{source}"
    os.makedirs(out_dir, exist_ok=True)

    archive = f"{out_dir}/Bathymetric_Rasters.7z"
    print(f"downloading {source}: {urls[0]}")
    utils.http_download(urls[0], archive)

    # Extract the four Analytical rasters with py7zr (portable; no /vsi7z). They're already
    # GeoTIFFs carrying each lake's CRS, so no gdal_translate — rename into place and let
    # source_normalize make the final COG.
    targets = [f"{ARCHIVE_DIR}/{lake}_Analytical_ras.tif" for lake in LAKES]
    print(f"  extracting {len(targets)} rasters")
    with py7zr.SevenZipFile(archive, "r") as z:
        z.extract(path=out_dir, targets=targets)
    for lake in LAKES:
        os.replace(f"{out_dir}/{ARCHIVE_DIR}/{lake}_Analytical_ras.tif", f"{out_dir}/{lake}.tif")
    shutil.rmtree(f"{out_dir}/{ARCHIVE_DIR}", ignore_errors=True)
    os.remove(archive)


if __name__ == "__main__":
    main()
