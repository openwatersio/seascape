"""Fetch the NOAA NCEI Great Lakes Bathymetry grids and extract each lake's DEM.

Each lake is a .tar.gz on ngdc.noaa.gov holding ``<lake>_lld/<lake>_lld.tif`` (+ .prj/.tfw):
a 3 arc-sec (~90 m) NAD83 grid, elevation on each lake's **Low Water Datum** (LWD — a
low-water chart-datum analog), so the lake bed is negative and the surrounding land positive.
These are full open-lake grids, so they cover both the US and Canadian sides (the lakes are
single water bodies — this fills the Canadian Great Lakes that had no source).

file_list.txt holds five tarball URLs (Superior, Michigan, Huron, Erie, Ontario) — which
cover all six Great Lakes water bodies: NGDC's Erie grid is "Lake Erie and Lake Saint
Clair", so Lake St. Clair rides along in erie_lld (no separate St. Clair grid exists). The
recipe then drops land above LWD (source_datum --clamp-positive → lake-only) and assigns
EPSG:4269 in normalize (the CRS ships as a .prj sidecar we don't keep). No login, ~220 MB.
"""

import os
import shutil
import sys
import tarfile

import config
import utils


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: source_download_greatlakes.py <source-id>")
    source = sys.argv[1]
    urls = config.file_list(source)
    if not urls:
        sys.exit(f"no URLs in {config.SOURCES_DIR}/{source}/file_list.txt")
    out_dir = f"store/source/{source}"
    os.makedirs(out_dir, exist_ok=True)

    for i, url in enumerate(urls):
        tgz = f"{out_dir}/_{i}.tar.gz"
        print(f"downloading {url}")
        utils.http_download(url, tgz)
        with tarfile.open(tgz) as t:
            tifs = [m for m in t.getmembers() if m.name.lower().endswith("_lld.tif")]
            if len(tifs) != 1:
                sys.exit(f"expected exactly one *_lld.tif in {url}, found {len(tifs)}")
            name = os.path.basename(tifs[0].name)
            with t.extractfile(tifs[0]) as src, open(f"{out_dir}/{name}", "wb") as dst:
                shutil.copyfileobj(src, dst)  # stream to disk, don't buffer the whole raster
            print(f"  extracted {name}")
        os.remove(tgz)


if __name__ == "__main__":
    main()
