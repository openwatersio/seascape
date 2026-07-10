"""Shared helpers for *remotely-enumerated* sources — tile collections published on
a public bucket and registered from header/index reads instead of a bulk download.

``bounds.csv`` IS the registration: one row per tile — a filename plus 3857 bounds
and pixel dims. ``config.source_path`` resolves the filename to a GDAL-openable
path, so rows may be relative store paths (``objects/<key>``, the mirrored-source
shape ``source_mirror`` maintains) or absolute ``/vsi`` URLs (passed through
verbatim, which is what makes a cutover between the two shapes safe in both
directions). These helpers are the pieces any enumeration front-end shares: the
URL->VSI mapping, antimeridian-aware 3857 bounds, the bounds.csv writer, and the
previous-registration reader that makes re-registration incremental.
"""

import os

from rasterio.warp import transform_bounds

import utils


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


def write_bounds(source, rows):
    """Write store/source/<source>/bounds.csv from rows of
    (filename, left, bottom, right, top, width, height) — bounds in EPSG:3857. The covering
    re-filters precisely from these bounds, so a generous upstream prefilter is fine."""
    os.makedirs(f"store/source/{source}", exist_ok=True)
    with open(f"store/source/{source}/bounds.csv", "w") as f:
        f.write("filename,left,bottom,right,top,width,height\n")
        for path, left, bottom, right, top, width, height in rows:
            f.write(f"{path},{left},{bottom},{right},{top},{width},{height}\n")
    print(f"{source}: wrote {len(rows)} tiles to bounds.csv")


def _prev_bounds(source):
    """Previous bounds.csv as {filename: row}, for incremental re-register. Empty if
    absent. Keyed on the filename column — the contract carry-forward depends on: a
    re-registration reuses a row (skipping its header read) only when it writes the
    exact same filename shape."""
    path = f"store/source/{source}/bounds.csv"
    if not os.path.isfile(path):
        return {}
    rows = {}
    with open(path) as f:
        next(f, None)  # header
        for line in f:
            parts = line.rstrip("\n").split(",")
            if len(parts) == 7:
                rows[parts[0]] = tuple(parts)
    return rows


def _check():
    import shutil
    assert to_vsicurl("s3://b/k/x.tif") == "/vsicurl/https://b.s3.amazonaws.com/k/x.tif"
    assert to_vsicurl("https://h.example/x.tif") == "/vsicurl/https://h.example/x.tif"

    # antimeridian: a normal box passes through untouched; a ~full-width span flips
    # to the wrapped left>right convention (what split_at_antimeridian splits).
    XM = utils.X_MAX_3857
    assert wrap_antimeridian(-100.0, 100.0) == (-100.0, 100.0)
    left, right = wrap_antimeridian(-0.9999 * XM, 0.9999 * XM)
    assert left > right, (left, right)

    # write/read round-trip: _prev_bounds keys rows on the filename column — the
    # contract source_mirror's zero-network carry-forward depends on.
    src = "_remote_selfcheck"
    write_bounds(src, [("objects/dem/t.tif", 1.0, 2.0, 3.0, 4.0, 10, 20)])
    prev = _prev_bounds(src)
    assert prev["objects/dem/t.tif"] == ("objects/dem/t.tif", "1.0", "2.0", "3.0", "4.0", "10", "20"), prev
    shutil.rmtree(f"store/source/{src}")
    print("source_remote.py self-check ok")


if __name__ == "__main__":
    _check()
