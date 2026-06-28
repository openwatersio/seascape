"""Shared core for *streaming* sources — COG tile collections already published on a
public bucket, registered WITHOUT downloading.

Instead of bulk-fetching the bytes, read each tile's *header* via GDAL ``/vsicurl/`` and
record its 3857 bounds in ``store/source/<id>/bounds.csv`` with the ``/vsicurl/`` path
itself as the "filename". The aggregation stage then range-reads only the COG blocks it
needs straight over public HTTPS (``config.source_path`` passes the ``/vsicurl/`` path
through — no credentials, so it coexists with the signed-free R2 reads of locally-prepared
sources). No normalize (tiles are already COGs with CRS + nodata), no polygonize/tarball
(a streaming source has no local bytes to redistribute).

Two enumeration shapes pick a front-end CLI. A flat text urllist
(``source_register_remote_urllist``, CUDEM) has no per-tile metadata, so it
``register_tiles`` — opening each header to read bounds + size. A tile-scheme GeoPackage
(``source_register_remote_geopkg``, NOAA S-102) already *is* the index (footprint geometry +
resolution per tile), so it derives the bounds rows itself and calls ``write_bounds`` directly,
skipping ~7k header round-trips.
"""

import os
import sys

import rasterio
from rasterio.warp import transform_bounds

import utils


def to_vsicurl(url):
    """An http(s)/s3:// URL -> a GDAL ``/vsicurl/`` path (public range reads, no creds)."""
    if url.startswith("s3://"):
        bucket, key = url[len("s3://"):].split("/", 1)
        url = f"https://{bucket}.s3.amazonaws.com/{key}"
    return "/vsicurl/" + url


def bounds_3857(src):
    left, bottom, right, top = transform_bounds(src.crs, "EPSG:3857", *src.bounds)
    if right - left > 0.9 * 2 * utils.X_MAX_3857:  # antimeridian flip (e.g. Aleutians)
        left, right = right, left
    return left, bottom, right, top


def write_bounds(source, rows):
    """Write store/source/<source>/bounds.csv from rows of
    (vsicurl_path, left, bottom, right, top, width, height) — bounds in EPSG:3857. The covering
    re-filters precisely from these bounds, so a generous upstream BBOX prefilter is fine."""
    os.makedirs(f"store/source/{source}", exist_ok=True)
    with open(f"store/source/{source}/bounds.csv", "w") as f:
        f.write("filename,left,bottom,right,top,width,height\n")
        for path, left, bottom, right, top, width, height in rows:
            f.write(f"{path},{left},{bottom},{right},{top},{width},{height}\n")
    print(f"{source}: wrote {len(rows)} tiles to bounds.csv")


def _prev_bounds(source):
    """Previous bounds.csv as {vsicurl_path: row}, for incremental re-register. Empty if absent."""
    path = f"store/source/{source}/bounds.csv"
    if not os.path.isfile(path):
        return {}
    rows = {}
    with open(path) as f:
        next(f, None)  # header
        for line in f:
            parts = line.rstrip("\n").split(",")
            if len(parts) == 7:
                rows[parts[0]] = tuple(parts)  # keyed on the /vsicurl filename
    return rows


def register_tiles(source, urls):
    """Header-read each tile via /vsicurl -> 3857 bounds + pixel size -> bounds.csv. For sources
    with no metadata index (a flat urllist, e.g. CUDEM); a GeoPackage-indexed source builds rows
    from the index and calls write_bounds directly, skipping these per-tile reads.

    Incremental: a tile URL already in the previous bounds.csv is reused as-is, so a re-register
    only opens headers for *newly-added* URLs (a urllist's tile URLs are stable — CUDEM names
    encode lat/lon + version — so a rebuild reads ~0 headers). Delete bounds.csv to force a full
    refetch."""
    prev = _prev_bounds(source)
    rows, reads = [], 0
    for url in urls:
        path = to_vsicurl(url)
        if path in prev:
            rows.append(prev[path])
            continue
        with rasterio.open(path) as src:
            if src.crs is None:
                sys.exit(f"crs not defined on {path}")
            left, bottom, right, top = bounds_3857(src)
            rows.append((path, left, bottom, right, top, src.width, src.height))
        reads += 1
        if reads % 100 == 0:
            print(f"  read {reads} new headers")
    write_bounds(source, rows)
    print(f"{source}: {len(rows)} tiles ({reads} newly read, {len(rows) - reads} reused)")


def _check():
    import shutil
    assert to_vsicurl("s3://b/k/x.tif") == "/vsicurl/https://b.s3.amazonaws.com/k/x.tif"
    assert to_vsicurl("https://h.example/x.tif") == "/vsicurl/https://h.example/x.tif"

    # incremental: a tile already in bounds.csv is reused, never re-fetched. If reuse broke,
    # register_tiles would try to open the (unreachable) header and fail — so this also asserts
    # offline behavior.
    src = "_remote_selfcheck"
    os.makedirs(f"store/source/{src}", exist_ok=True)
    url = "https://h.example/dem/t.tif"
    row = f"{to_vsicurl(url)},1.0,2.0,3.0,4.0,10,20"
    with open(f"store/source/{src}/bounds.csv", "w") as f:
        f.write("filename,left,bottom,right,top,width,height\n" + row + "\n")
    register_tiles(src, [url])  # cached -> no header read
    with open(f"store/source/{src}/bounds.csv") as f:
        out = f.read().splitlines()
    assert out[1] == row, out
    shutil.rmtree(f"store/source/{src}")
    print("source_remote.py self-check ok")


if __name__ == "__main__":
    _check()
