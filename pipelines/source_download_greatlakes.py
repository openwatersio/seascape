"""Fetch the NOAA NCEI Great Lakes Bathymetry grids and extract each lake's DEM.

Each lake is a .tar.gz on ngdc.noaa.gov holding ``<lake>_lld/<lake>_lld.tif`` (+ .prj/.tfw):
a 3 arc-sec (~90 m) NAD83 grid, elevation on each lake's **Low Water Datum** (LWD — a
low-water chart-datum analog), so the lake bed is negative and the surrounding land positive.
These are full open-lake grids, so they cover both the US and Canadian sides (the lakes are
single water bodies — this fills the Canadian Great Lakes that had no source).

file_list.txt holds the six tarball URLs. The recipe then drops land above LWD
(source_datum --clamp-positive → lake-only) and assigns EPSG:4269 in normalize (the CRS
ships as a .prj sidecar we don't keep). No login, ~220 MB total.
"""

import os
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
            member = next(m for m in t.getmembers() if m.name.lower().endswith(".tif"))
            name = os.path.basename(member.name)
            with t.extractfile(member) as src, open(f"{out_dir}/{name}", "wb") as dst:
                dst.write(src.read())
            print(f"  extracted {name}")
        os.remove(tgz)


if __name__ == "__main__":
    main()
