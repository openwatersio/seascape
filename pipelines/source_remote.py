"""Shared helpers for *remotely-enumerated* sources — tile collections published on
a public bucket and registered from header/index reads instead of a bulk download.

The catalog item (``seascape:files``) IS the registration: one row per tile — a filename
plus 3857 bounds and pixel dims. ``config.source_path`` resolves the filename to a
GDAL-openable path, so rows may be relative store paths (``objects/<key>``, the raw-source
shape ``source_mirror`` maintains) or absolute ``/vsi`` URLs (passed through verbatim,
which is what makes a cutover between the two shapes safe in both directions).

A raw registration is two processes: ``source_mirror`` builds the rows (header reads +
carry-forward) and hands them to ``source_catalog`` via an uncommitted ``.rows.csv`` — the
rows come from thousands of network reads, too expensive to recompute during catalog
assembly. These helpers are the pieces the front-ends share: the URL->VSI mapping,
antimeridian-aware 3857 bounds, the row handoff I/O, and the previous-registration reader
(the catalog's ``seascape:files``) that makes re-registration incremental.
"""

import os

from rasterio.warp import transform_bounds

import config
import utils

# source_mirror -> source_catalog handoff (raw rows, consumed + deleted by source_catalog).
HANDOFF = ".rows.csv"


def to_vsicurl(url):
    """An http(s)/s3:// URL -> a GDAL ``/vsicurl/`` path (public range reads, no creds)."""
    if url.startswith("s3://"):
        bucket, key = url[len("s3://"):].split("/", 1)
        url = f"https://{bucket}.s3.amazonaws.com/{key}"
    return "/vsicurl/" + url


def wrap_antimeridian(left, right):
    """3857 (left, right) -> the wrapped convention left>right when the extent
    crosses 180°: a reprojected span of ~the whole world means the tile's vertices
    landed on both ±X_MAX (e.g. the Aleutians), and the covering's
    split_at_antimeridian expects the wrapped form, not a full-globe box."""
    if right - left > 0.9 * 2 * utils.X_MAX_3857:
        return right, left
    return left, right


def bounds_3857(src):
    left, bottom, right, top = transform_bounds(src.crs, "EPSG:3857", *src.bounds)
    left, right = wrap_antimeridian(left, right)
    return left, bottom, right, top


def write_rows(source, rows):
    """Write the raw-source row handoff store/source/<source>/.rows.csv from rows of
    (filename, left, bottom, right, top, width, height) — bounds in EPSG:3857.
    source_catalog consumes + deletes it, folding the rows into the catalog's seascape:files."""
    os.makedirs(f"store/source/{source}", exist_ok=True)
    with open(f"store/source/{source}/{HANDOFF}", "w") as f:
        f.write("filename,left,bottom,right,top,width,height\n")
        for path, left, bottom, right, top, width, height in rows:
            f.write(f"{path},{left},{bottom},{right},{top},{width},{height}\n")
    print(f"{source}: wrote {len(rows)} tiles to {HANDOFF}")


def read_rows(path):
    """Parse a 7-column registration CSV (the .rows.csv handoff) into typed rows. Empty if
    the file is absent."""
    if not os.path.isfile(path):
        return []
    rows = []
    with open(path) as f:
        next(f, None)  # header
        for line in f:
            parts = line.rstrip("\n").split(",")
            if len(parts) == 7:
                rows.append((parts[0], float(parts[1]), float(parts[2]), float(parts[3]),
                             float(parts[4]), int(parts[5]), int(parts[6])))
    return rows


def _prev_files(source):
    """The PREVIOUS registration as {filename: row}, for incremental re-register. Reads the
    previous catalog item's seascape:files via config.source_files (which falls back to a
    legacy bounds.csv when the item predates phase 4). Empty when neither exists. Keyed on the
    filename column — the carry-forward contract: a re-registration reuses a row (skipping its
    header read) only when it writes the exact same filename shape."""
    return {r[0]: r for r in config.source_files(source)}


def _check():
    import json
    import shutil
    import tempfile
    assert to_vsicurl("s3://b/k/x.tif") == "/vsicurl/https://b.s3.amazonaws.com/k/x.tif"
    assert to_vsicurl("https://h.example/x.tif") == "/vsicurl/https://h.example/x.tif"

    # antimeridian: a normal box passes through untouched; a ~full-width span flips
    # to the wrapped left>right convention (what split_at_antimeridian splits).
    XM = utils.X_MAX_3857
    assert wrap_antimeridian(-100.0, 100.0) == (-100.0, 100.0)
    left, right = wrap_antimeridian(-0.9999 * XM, 0.9999 * XM)
    assert left > right, (left, right)

    d = tempfile.mkdtemp()
    cwd = os.getcwd()
    try:
        os.chdir(d)
        config._catalog_cache.clear()
        # write/read round-trip on the handoff, keyed + typed.
        src = "_remote_selfcheck"
        write_rows(src, [("objects/dem/t.tif", 1.0, 2.0, 3.0, 4.0, 10, 20)])
        got = read_rows(f"store/source/{src}/{HANDOFF}")
        assert got == [("objects/dem/t.tif", 1.0, 2.0, 3.0, 4.0, 10, 20)], got

        # _prev_files reads the previous registration from the catalog's seascape:files,
        # keyed on the filename column (the zero-network carry-forward contract).
        with open(f"store/source/{src}/catalog.json", "w") as f:
            json.dump({"properties": {"seascape:files": [
                ["objects/dem/t.tif", 1.0, 2.0, 3.0, 4.0, 10, 20]]}}, f)
        prev = _prev_files(src)
        assert prev["objects/dem/t.tif"] == ("objects/dem/t.tif", 1.0, 2.0, 3.0, 4.0, 10, 20), prev
    finally:
        os.chdir(cwd)
        config._catalog_cache.clear()
        shutil.rmtree(d, ignore_errors=True)
    print("source_remote.py self-check ok")


if __name__ == "__main__":
    _check()
