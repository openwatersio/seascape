"""Bulk-fetch every file named by a *filelist* — a text manifest of one URL per line.

For sources published as a tile collection with a maintained URL manifest (e.g.
NOAA CUDEM's ``urllist8483.txt``: 942 tile URLs across all US coastal regions).
``file_list.txt`` holds the URL(s) of the *filelist(s)*; this step downloads each
manifest, then downloads every file it names into ``store/source/<id>/``. Pointing
at NOAA's manifest (vs vendoring ~942 lines) means coverage tracks their periodic
re-tiling automatically. The download is idempotent — a finished file is skipped,
so a re-run after a flaky tile resumes instead of re-pulling everything.
"""

import os
import sys

import requests

import config
import utils
from source_download import ext_for


def filelist_urls(text):
    """File URLs in a downloaded manifest: non-blank, non-comment lines."""
    return [l.strip() for l in text.splitlines()
            if l.strip() and not l.lstrip().startswith("#")]


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: source_download_filelist.py <source-id>")
    source = sys.argv[1]
    manifests = config.file_list(source)
    if not manifests:
        sys.exit(f"no filelist URLs in {config.SOURCES_DIR}/{source}/file_list.txt")
    os.makedirs(f"store/source/{source}", exist_ok=True)

    urls = []
    for m in manifests:
        print(f"reading filelist {m}")
        r = requests.get(m, timeout=60)
        r.raise_for_status()
        urls += filelist_urls(r.text)
    print(f"downloading {source}: {len(urls)} file(s) from {len(manifests)} filelist(s)")

    for i, url in enumerate(urls):
        dest = f"store/source/{source}/{source}_{i}.{ext_for(url)}"
        if os.path.exists(dest):
            continue
        print(f"  [{i}/{len(urls)}] {url} -> {dest}")
        # http_download is atomic (.part + rename) and resumes a crashed fetch, so
        # the skip above only ever sees complete files. No checksum — rm the dir to
        # force a clean refetch.
        utils.http_download(url, dest)


if __name__ == "__main__":
    main()
