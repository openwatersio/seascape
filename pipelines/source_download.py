"""Plain HTTP download of a source's file_list.txt URLs into store/source/<id>/.

Uses requests (no shell) so query-string URLs with ``&`` work. Other access
patterns are their own steps a recipe picks instead: source_register_remote_urllist
/ source_register_remote_geopkg (/vsicurl streaming refs), source_unzip (extract
archives this step fetched).
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


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: source_download.py <source-id>")
    source = sys.argv[1]
    urls = config.file_list(source)
    if not urls:
        sys.exit(f"no URLs in {config.SOURCES_DIR}/{source}/file_list.txt")
    os.makedirs(f"store/source/{source}", exist_ok=True)
    print(f"downloading {source}: {len(urls)} url(s)")
    skipped = 0
    for i, url in enumerate(urls):
        dest = f"store/source/{source}/{source}_{i}.{ext_for(url)}"
        # A finished file is skipped, so a re-run resumes instead of re-pulling
        # everything (http_download is atomic — dest only exists complete; a zip
        # sniffed by fix_archive_ext lands under the .zip name). No checksum —
        # rm the dir to force a clean refetch.
        if os.path.exists(dest) or os.path.exists(dest.rsplit(".", 1)[0] + ".zip"):
            skipped += 1
            continue
        print(f"  [{i}] {url} -> {dest}")
        utils.http_download(url, dest)
        fix_archive_ext(dest)
    if skipped:
        print(f"  skipped {skipped} already-downloaded file(s)")


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
    print("source_download.py self-check ok")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        _check()
    else:
        main()
