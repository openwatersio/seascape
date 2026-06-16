"""Plain HTTP download of a source's file_list.txt URLs into store/source/<id>/.

Uses requests (no shell) so query-string URLs with ``&`` work. Other access
patterns are their own steps a recipe picks instead: source_download_s3 (/vsis3/
clip), source_download_erddap (griddap subset), source_unzip (extract archives
this step fetched).
"""

import os
import sys

import config
import utils

# Only trust a URL's trailing extension when it names a real data/archive format;
# otherwise (e.g. the DDM weblink ending in ...html?...) save as .tif — GDAL reads
# by content, not by name.
DATA_EXTS = {"tif", "tiff", "zip", "nc", "asc", "xyz", "img", "gz", "7z", "grd"}


def ext_for(url):
    last = url.split("?")[0].split("#")[0].rsplit("/", 1)[-1]
    ext = last.rsplit(".", 1)[-1].lower() if "." in last else ""
    return ext if ext in DATA_EXTS else "tif"


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: source_download.py <source-id>")
    source = sys.argv[1]
    urls = config.file_list(source)
    if not urls:
        sys.exit(f"no URLs in {config.SOURCES_DIR}/{source}/file_list.txt")
    os.makedirs(f"store/source/{source}", exist_ok=True)
    print(f"downloading {source}: {len(urls)} url(s)")
    for i, url in enumerate(urls):
        dest = f"store/source/{source}/{source}_{i}.{ext_for(url)}"
        print(f"  [{i}] {url} -> {dest}")
        utils.http_download(url, dest)


if __name__ == "__main__":
    main()
