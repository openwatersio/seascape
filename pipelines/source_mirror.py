"""Register a *mirrored* source from its enumerated items — a volatile tile collection
published on a public bucket, whose objects get copied into the local store rather than
read from the upstream at aggregate time.

Enumeration (listing/filter/dedupe/shrink-guard) happens upstream in the enumerate
checkpoint; this module is the REGISTRATION half. It reads ``store/source/<id>/items.txt``
(one fetchable object URL per line, all on one public bucket) and writes:

- ``bounds.csv`` — one row per object, a **relative** ``objects/<upstream-key>`` filename
  plus 3857 bounds and pixel dims (``config.source_path`` resolves it against the store;
  upstream key paths preserved 1:1, so provenance stays readable and no rename logic exists);
- ``mirror.txt`` — the full HEALTHY upstream key list, one per line, for whatever process
  maintains the store's copy of those objects (always the FULL list, so a partial prior
  mirror self-heals on the next pass);
- ``mirror-bucket.txt`` — the single bucket those keys live on.

Incremental: a key already registered in the previous ``bounds.csv`` is carried forward with
**zero network reads**; only newly-listed keys get a header read — from the upstream URL, not
the store, because a new object was listed seconds ago and may not be mirrored yet. A
header-read failure is a hard error path, triaged below (carry the previous issue forward or
drop a brand-new broken cell); more than ``BROKEN_TOLERANCE`` such failures aborts, keeping
the previous registration serving rather than a silently thinner one.

Run from pipelines/:  uv run python source_mirror.py <source-id>
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import rasterio
from rasterio.warp import transform_bounds

import config
from source_enumerate import _split_bucket_key, cell_key
from source_remote import _prev_bounds, bounds_3857, to_vsicurl, wrap_antimeridian, write_bounds

# More listed-but-unreadable products than this aborts the registration — the carry-forward
# tolerance must not quietly absorb a half-broken edition.
BROKEN_TOLERANCE = 5


def h5_header(vsi, tries=3):
    """3857 bounds + pixel dims from an HDF5 product's header, via a ``gdalinfo -json``
    *subprocess*. The isolation is the point: bathymetric-surface drivers have segfaulted on
    degenerate 1x1 stub products, and a crash must fail this one probe with a readable error
    instead of killing registration. Bounds come from gdalinfo's ``wgs84Extent`` (its bbox ->
    3857) rather than re-parsing projected corners + WKT — one version-stable JSON field, and
    it matches the corner reprojection to ~mm. Transient fetch errors retry; a persistent
    failure raises — a key listed seconds ago must be readable (see module docstring)."""
    for attempt in range(1, tries + 1):
        proc = subprocess.run(["gdalinfo", "-json", vsi], capture_output=True, text=True)
        if proc.returncode == 0:
            break
        if attempt == tries:
            raise RuntimeError(f"gdalinfo failed on {vsi} (exit {proc.returncode}): "
                               f"{proc.stderr.strip()[-500:]}")
        time.sleep(2 ** attempt)
    info = json.loads(proc.stdout)
    geom = info.get("wgs84Extent") or {}
    rings = geom.get("coordinates", [])
    if geom.get("type") == "MultiPolygon":  # some GDAL builds split the extent at 180
        rings = [ring for poly in rings for ring in poly]
    points = [pt for ring in rings for pt in ring]
    if not points or "size" not in info:
        raise RuntimeError(f"{vsi}: header lacks a georeferenced extent")
    lons, lats = [p[0] for p in points], [p[1] for p in points]
    left, bottom, right, top = transform_bounds(
        "EPSG:4326", "EPSG:3857", min(lons), min(lats), max(lons), max(lats))
    left, right = wrap_antimeridian(left, right)
    width, height = info["size"]
    return left, bottom, right, top, width, height


def tif_header(vsi):
    """3857 bounds + pixel dims from a GeoTIFF header — in-process (the GeoTIFF driver has no
    crash history, so it keeps the cheap path; HDF5 goes through the subprocess isolation)."""
    with rasterio.open(vsi) as src:
        if src.crs is None:
            raise RuntimeError(f"crs not defined on {vsi}")
        left, bottom, right, top = bounds_3857(src)
        return left, bottom, right, top, src.width, src.height


def read_header(bucket, key):
    """Header -> (3857 bounds, pixel dims) for one upstream object, read from the UPSTREAM
    URL, not the store: a new key was listed seconds ago and its bytes may not be mirrored
    yet. A failure here is triaged by register() (see module docstring)."""
    vsi = to_vsicurl(f"https://{bucket}.s3.amazonaws.com/{key}")
    return h5_header(vsi) if key.lower().endswith(".h5") else tif_header(vsi)


def _upstream_key(filename):
    """A previous bounds.csv filename -> its upstream key, for the removed-key diff. Handles
    both this module's relative ``objects/<key>`` rows and the absolute ``/vsicurl/https://…``
    rows the pre-mirror registration wrote, so the first run after cutover diffs honestly."""
    if filename.startswith("objects/"):
        return filename[len("objects/"):]
    m = re.match(r"^/vsicurl/https://[^/]+/(.+)$", filename)
    return m.group(1) if m else filename


def register(source, keys, header):
    """bounds.csv + mirror.txt from the enumerated upstream keys.

    Carry-forward first: a key whose ``objects/<key>`` row is already registered keeps it
    verbatim — zero network reads, so a steady-state refresh touches ~tens of headers, not
    thousands. (Rows in the older absolute-URL shape don't qualify: their pixel dims came from
    an index approximation, so the one-time full header sweep at cutover re-measures every
    product for real.) Removed keys print loudly.

    A listed key whose header read fails PERSISTENTLY (h5_header's retries exhausted — e.g.
    NOAA's 0-byte 102US005WI1DT262297.h5 replacing a deleted good issue) must not freeze the
    other ~4.3k products: when the previous registration has a row for the same cell, that row
    is carried forward — its object lives in our never-delete mirror even when the upstream
    deleted it — and a broken brand-new cell is dropped. Both print loudly; more than
    BROKEN_TOLERANCE such keys aborts (a half-broken edition must not be quietly absorbed cell
    by cell). Broken and carried keys stay OUT of mirror.txt — the object copier must never
    chase an upstream-deleted or empty object."""
    keys = sorted(set(keys))
    prev = _prev_bounds(source)
    removed = sorted({_upstream_key(f) for f in prev} - set(keys))
    for key in removed:
        print(f"REMOVED upstream: {key} (was registered, no longer listed)")

    rows, todo, read_ok = [], [], 0
    healthy = set(keys)  # keys whose objects the copier may chase; broken ones leave
    for key in keys:
        rel = f"objects/{key}"
        if rel in prev:
            rows.append(prev[rel])
        else:
            todo.append((len(rows), key))
            rows.append(None)
    if todo:
        print(f"{source}: reading {len(todo)} new headers from upstream")
        done, lock = 0, threading.Lock()

        def read_one(item):
            i, key = item
            try:
                row = (f"objects/{key}", *header(key))
            except RuntimeError as e:
                return i, key, e  # persistent read failure — triaged below, not fatal here
            nonlocal done
            with lock:
                done += 1
                if done % 100 == 0:
                    print(f"  {done}/{len(todo)} headers")
            return i, row, None

        # network-bound sweep; tune down if an upstream throttles
        workers = int(os.environ.get("MIRROR_HEADER_WORKERS", "48"))
        broken = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for i, row_or_key, err in pool.map(read_one, todo):
                if err is None:
                    rows[i] = row_or_key
                    read_ok += 1
                else:
                    broken.append((i, row_or_key, err))

        if len(broken) > BROKEN_TOLERANCE:
            sys.exit(f"{source}: {len(broken)} listed products are unreadable "
                     f"(> {BROKEN_TOLERANCE}) — upstream looks half-broken; refusing to "
                     "publish over the previous registration")
        prev_by_cell = {cell_key(_upstream_key(rel)): row for rel, row in prev.items()}
        drop = set()
        for i, key, err in sorted(broken):
            # cell-matching is S-102's naming scheme; non-.h5 keys just drop
            carried = prev_by_cell.get(cell_key(key)) if key.lower().endswith(".h5") else None
            healthy.discard(key)
            if carried:
                print(f"BROKEN upstream product: {key} ({err}) — carrying forward "
                      f"{carried[0]} from the mirror")
                rows[i] = carried
            else:
                print(f"BROKEN upstream product: {key} ({err}) — new cell, no previous "
                      "issue to carry; dropping it")
                drop.add(i)
        rows = [r for i, r in enumerate(rows) if i not in drop]

    os.makedirs(f"store/source/{source}", exist_ok=True)
    with open(f"store/source/{source}/mirror.txt", "w") as f:
        f.writelines(key + "\n" for key in sorted(healthy))
    write_bounds(source, rows)
    kept = len(keys) - len(todo)
    n_broken = len(todo) - read_ok if todo else 0
    print(f"{source}: {kept} carried forward, {read_ok} newly read, {n_broken} broken, "
          f"{len(removed)} removed")


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: source_mirror.py <source-id>")
    source = sys.argv[1]
    items = config.items(source)
    if not items:
        sys.exit(f"{source}: no items enumerated — run the enumerate checkpoint first")
    pairs = [_split_bucket_key(u) for u in items]  # (bucket, key) per object URL
    buckets = {b for b, _ in pairs}
    if len(buckets) > 1:
        sys.exit(f"{source}: objects span multiple buckets {sorted(buckets)} — one mirror root per source")
    bucket = buckets.pop()
    keys = sorted({k for _, k in pairs})
    print(f"{source}: {len(keys)} objects on {bucket}")
    register(source, keys, lambda key: read_header(bucket, key))
    # After register() so a guarded/failed run leaves nothing half-written; the mirror process
    # reads the bucket from here instead of re-deriving it from items.txt.
    with open(f"store/source/{source}/mirror-bucket.txt", "w") as f:
        f.write(bucket + "\n")


def _check():
    import shutil

    # URL splitting + previous-filename normalization for the removed-key diff.
    assert _split_bucket_key("s3://b/k/t.tif") == ("b", "k/t.tif")
    assert _upstream_key("objects/dem/x.tif") == "dem/x.tif"
    assert _upstream_key("/vsicurl/https://noaa-s102-pds.s3.amazonaws.com/ed3.0.0/a.h5") == "ed3.0.0/a.h5"

    # register(): carried-forward rows do ZERO network reads (the fake header fn records every
    # call — same offline assertion pattern as source_remote), rows come out relative, and
    # mirror.txt carries the FULL key list, not just new keys.
    src = "_mirror_selfcheck"
    shutil.rmtree(f"store/source/{src}", ignore_errors=True)
    os.makedirs(f"store/source/{src}", exist_ok=True)
    with open(f"store/source/{src}/bounds.csv", "w") as f:
        f.write("filename,left,bottom,right,top,width,height\n"
                "objects/dem/a.tif,1.0,2.0,3.0,4.0,10,20\n")
    reads = []

    def fake_header(key):
        reads.append(key)
        return (0.0, 0.0, 1.0, 1.0, 5, 5)

    register(src, ["dem/a.tif", "dem/b.tif"], fake_header)
    assert reads == ["dem/b.tif"], reads  # only the NEW key was read
    with open(f"store/source/{src}/bounds.csv") as f:
        out = f.read().splitlines()
    assert out[1] == "objects/dem/a.tif,1.0,2.0,3.0,4.0,10,20", out  # carried verbatim
    assert out[2].startswith("objects/dem/b.tif,"), out              # relative path
    with open(f"store/source/{src}/mirror.txt") as f:
        assert f.read().splitlines() == ["dem/a.tif", "dem/b.tif"]
    shutil.rmtree(f"store/source/{src}")

    # Broken-product triage: a persistently-unreadable NEW issue carries the previous cell's
    # row forward (the mirror still holds its object) and stays out of mirror.txt; a broken
    # brand-new cell is dropped; more than BROKEN_TOLERANCE aborts.
    src2 = "_mirror_broken"
    shutil.rmtree(f"store/source/{src2}", ignore_errors=True)
    os.makedirs(f"store/source/{src2}", exist_ok=True)
    with open(f"store/source/{src2}/bounds.csv", "w") as f:
        f.write("filename,left,bottom,right,top,width,height\n"
                "objects/p/102US005AAAAA111111.h5,1.0,2.0,3.0,4.0,10,20\n")

    def broken_header(key):
        if "BROKEN" in key or "NEWCEL" in key:
            raise RuntimeError(f"gdalinfo failed on {key}")
        return (0.0, 0.0, 1.0, 1.0, 5, 5)

    register(src2, ["p/102US005AAAAABROKEN.h5",    # re-issue of AAAAA, unreadable → carry
                    "p/102US005NEWCLNEWCEL.h5",    # broken new cell → dropped
                    "p/102US005CCCCC222222.h5"],   # healthy new
             broken_header)
    with open(f"store/source/{src2}/bounds.csv") as f:
        out2 = f.read()
    assert "objects/p/102US005AAAAA111111.h5,1.0,2.0,3.0,4.0,10,20" in out2, \
        "broken re-issue must carry the previous cell's row"
    assert "BROKEN" not in out2 and "NEWCEL" not in out2, out2
    assert "objects/p/102US005CCCCC222222.h5" in out2, "healthy keys must register"
    with open(f"store/source/{src2}/mirror.txt") as f:
        m = f.read().splitlines()
    assert m == ["p/102US005CCCCC222222.h5"], f"mirror.txt must hold only healthy keys: {m}"
    # ceiling: more broken keys than BROKEN_TOLERANCE refuses the whole registration
    many = [f"p/102US005X{i:04d}BROKEN.h5" for i in range(BROKEN_TOLERANCE + 1)]
    try:
        register(src2, many, broken_header)
        assert False, "expected the broken-product ceiling to exit"
    except SystemExit as e:
        assert "unreadable" in str(e), e
    shutil.rmtree(f"store/source/{src2}")

    # a header-read failure raises (offline: gdalinfo on a path that cannot exist exits
    # nonzero instantly — the same subprocess path a segfaulting driver hits)
    try:
        h5_header("/nonexistent/definitely_not_here.h5", tries=1)
        assert False, "expected h5_header to raise"
    except RuntimeError as e:
        assert "gdalinfo" in str(e), e
    print("source_mirror.py self-check ok")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        _check()
    else:
        main()
