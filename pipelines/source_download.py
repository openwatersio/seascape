"""Plain HTTP download of a source's file_list.txt URLs into store/download/<id>/.

The download phase: raw bytes land in the cache dir (store/download/<id>/) that later
phases never mutate, so re-running normalize is network-free. Idempotent — a URL whose
cache file already exists is skipped unless --force is passed. Does not unzip or delete.

Uses requests (no shell) so query-string URLs with ``&`` work. Other access
patterns are their own steps a recipe picks instead: source_mirror (volatile
public tile collections, registered against the store's object mirror),
source_unzip (extract archives this step fetched into the source work dir).
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


def fix_archive_ext(dest):
    """Rename to .zip when the bytes are a zip but the URL gave no extension to say so
    (e.g. Defra's survey tile API serves .../survey/.../2/SS6045 as application/zip).
    ext_for can't tell from the URL, so source_unzip would miss it. GDAL still reads
    tif/nc by content regardless — only archives need the right name for the unzip step."""
    if dest.endswith(".zip"):
        return dest
    with open(dest, "rb") as f:
        if f.read(4) != b"PK\x03\x04":
            return dest
    zdest = dest.rsplit(".", 1)[0] + ".zip"
    os.replace(dest, zdest)
    return zdest


def cached(dest):
    """The existing cache file for this dest, or None. Accounts for fix_archive_ext's
    rename of the ext_for "tif" fallback to .zip (an extensionless URL that served zip
    bytes): only a .tif/.tiff dest is legitimately satisfied by a .zip sibling, so a
    stray .zip never shadows a .nc/.asc/... at the same index if the URL list changes."""
    if os.path.exists(dest):
        return dest
    if dest.rsplit(".", 1)[-1].lower() in ("tif", "tiff"):
        zdest = dest.rsplit(".", 1)[0] + ".zip"
        if os.path.exists(zdest):
            return zdest
    return None


def main(force=False):
    source = sys.argv[1]
    urls = config.file_list(source)
    if not urls:
        sys.exit(f"no URLs in {config.SOURCES_DIR}/{source}/file_list.txt")
    out = f"store/download/{source}"
    os.makedirs(out, exist_ok=True)
    print(f"downloading {source}: {len(urls)} url(s) -> {out}/")
    for i, url in enumerate(urls):
        dest = f"{out}/{source}_{i}.{ext_for(url)}"
        hit = cached(dest)
        if hit and not force:
            print(f"  [{i}] cached {hit}")
            continue
        print(f"  [{i}] {url} -> {dest}")
        utils.http_download(url, dest)
        fix_archive_ext(dest)


def _check():
    import tempfile
    assert ext_for("https://x/y/SS6045?subscription-key=dspui") == "tif"  # extensionless URL
    assert ext_for("https://x/y/a.zip?q=1") == "zip"
    d = tempfile.mkdtemp()
    zpath = os.path.join(d, "s_0.tif")  # zip bytes saved under the .tif fallback name
    with open(zpath, "wb") as f:
        f.write(b"PK\x03\x04rest")
    assert fix_archive_ext(zpath) == os.path.join(d, "s_0.zip")
    tpath = os.path.join(d, "s_1.tif")  # real tif keeps its name
    with open(tpath, "wb") as f:
        f.write(b"II*\x00")
    assert fix_archive_ext(tpath) == tpath
    # cached(): a .tif dest is satisfied by the .zip sibling fix_archive_ext left behind
    assert cached(zpath) == os.path.join(d, "s_0.zip")  # zpath was renamed to .zip above
    assert cached(tpath) == tpath                        # plain tif, present
    assert cached(os.path.join(d, "missing.tif")) is None
    # a stray .zip sibling must NOT shadow a non-tif dest at the same index (s_0.zip exists)
    assert cached(os.path.join(d, "s_0.nc")) is None
    print("source_download.py self-check ok")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        _check()
    elif len(sys.argv) < 2 or sys.argv[1].startswith("-"):
        sys.exit("usage: source_download.py <source-id> [--force]")
    else:
        main(force="--force" in sys.argv[2:])
