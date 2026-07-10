"""Register a *mirrored* source — a volatile tile collection published on a public
bucket, whose objects get copied into the local store rather than read from the
upstream at aggregate time.

Volatile upstreams republish continuously and non-atomically, so nothing downstream
may depend on their availability: registration writes ``bounds.csv`` rows whose
filenames are **relative** ``objects/<upstream-key>`` paths (``config.source_path``
resolves them against the store — upstream key paths preserved 1:1, so provenance
stays readable and no rename logic exists), plus ``mirror.txt`` — the full upstream
key list, one per line — and ``mirror-bucket.txt`` — the single bucket those keys
live on — for whatever process maintains the store's copy of those objects. The
manifest always carries the FULL list, not just new keys, so a partial prior mirror
self-heals on the next pass.

``file_list.txt`` picks the enumeration shape per line:

- a public-bucket *prefix* URL ending ``/`` — list the prefix (paginated, no creds),
  keep ``.h5`` products, drop the ``_CATALOG/`` sidecars, and dedupe re-issued cells
  (NOAA S-102);
- anything else — fetch it as a flat *urllist* manifest of one tile URL per line,
  keeping ``.tif``/``.tiff`` (NOAA CUDEM).

Incremental: a key already registered in the previous ``bounds.csv`` is carried
forward with **zero network reads**; only new keys get a header read — from the
upstream URL, not the store, because a new object was listed seconds ago and may not
be mirrored yet. A header-read failure is a hard error: either the upstream churned
mid-refresh or it published a broken product, and in both cases the previous
registration must keep serving rather than a silently thinner one. The same
principle gates publication: every removed key is printed loudly, and a shrink
beyond ``SHRINK_TOLERANCE`` refuses to write anything (``MIRROR_ALLOW_SHRINK=1``
overrides, for deliberate upstream repartitions).

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
import requests
from rasterio.warp import transform_bounds

import config
from source_download_filelist import filelist_urls
from source_remote import _prev_bounds, bounds_3857, to_vsicurl, wrap_antimeridian, write_bounds

# Refuse to publish when the registration shrinks by more than this fraction of the
# previous one. A fraction, not an absolute count: the absolute drop ceiling this
# replaces read "7 of 9 regional products vanished" as fine because 7 is a small
# number. Upstreams legitimately prune a few superseded tiles, so a small shrink
# passes; a mass disappearance (repartition, auth change, half-published edition)
# trips the guard and the previous registration keeps serving.
SHRINK_TOLERANCE = 0.05

# S-102 filenames end in a fixed-width 6-character issue field before ``.h5``
# (``102US005JAXEF262297.h5`` -> cell ``102US005JAXEF``, issue ``262297``). Verified
# against the full ed3.0.0 population (4,315 products, 17 distinct issue codes): the
# field is alphanumeric (``262227``…``2622P7``), NOT digits-only — ~200 products end
# letter-then-digit — and cell codes themselves can end in digits (``…FL7G1``), so
# "strip trailing digits" mis-splits both ways; the fixed-width strip is the only
# split that groups every product correctly.
ISSUE_FIELD_LEN = 6

# ListObjectsV2 response fields, matched by regex rather than an XML parser —
# skipping the parser sidesteps XXE/entity expansion on an untrusted payload. The
# live response keeps Key/LastModified adjacent per <Contents>; the \s* tolerates a
# pretty-printed variant without admitting anything between the two fields.
_CONTENTS_RE = re.compile(r"<Key>([^<]+)</Key>\s*<LastModified>([^<]+)</LastModified>")
_TOKEN_RE = re.compile(r"<NextContinuationToken>\s*([^<\s]+)\s*</NextContinuationToken>")


def _next_token(xml):
    """Continuation token of a truncated listing page, or None when the listing is
    complete. A truncated page whose token can't be found is a hard error: returning
    None there would silently enumerate a partial bucket, and the shrink guard is the
    backstop for that, not the contract."""
    if "<IsTruncated>true</IsTruncated>" not in xml:
        return None
    m = _TOKEN_RE.search(xml)
    if not m:
        sys.exit("truncated listing without a parsable NextContinuationToken — "
                 "refusing a partial enumeration")
    return m.group(1)


def _split_bucket_key(url):
    """A public object/prefix URL -> (bucket, key). Virtual-host https form
    (``https://<bucket>.s3[.region].amazonaws.com/<key>``) or ``s3://<bucket>/<key>``."""
    m = re.match(r"^https://([^./]+)\.s3[^/]*\.amazonaws\.com/(.+)$", url)
    if m:
        return m.group(1), m.group(2)
    m = re.match(r"^s3://([^/]+)/(.+)$", url)
    if m:
        return m.group(1), m.group(2)
    sys.exit(f"cannot split bucket/key from {url} — mirrored sources need public-bucket URLs")


def list_prefix(bucket, prefix):
    """Every (key, LastModified) under a public bucket prefix — paginated
    ListObjectsV2, no creds. LastModified rides along because it is the only
    trustworthy recency signal for the cell dedupe below."""
    host = f"https://{bucket}.s3.amazonaws.com"
    items, token = [], None
    while True:
        params = {"list-type": "2", "prefix": prefix}
        if token:
            params["continuation-token"] = token
        r = requests.get(host, params=params, timeout=60)
        r.raise_for_status()
        items += _CONTENTS_RE.findall(r.text)
        token = _next_token(r.text)
        if not token:
            return items


def cell_key(key):
    """The re-issue-stable cell id of an S-102 product key: basename minus ``.h5``
    minus the fixed-width issue field (see ISSUE_FIELD_LEN). A degenerate short name
    keys as itself rather than collapsing into the empty cell."""
    stem = key.rsplit("/", 1)[-1].removesuffix(".h5")
    return stem[:-ISSUE_FIELD_LEN] if len(stem) > ISSUE_FIELD_LEN else stem


def dedupe_cells(items):
    """One product per cell from [(key, LastModified)]: keep the NEWEST LastModified.
    The issue field's encoding is undocumented, so lexical order on it means nothing;
    the listing's own timestamps are the only trustworthy order (ISO-8601 UTC strings,
    so string max == newest). Steady-state the upstream deletes the superseded issue
    and every cell has one product — the dedupe exists for the mid-republish window
    where both issues are listed at once."""
    best = {}
    for key, modified in items:
        cell = cell_key(key)
        if cell not in best or modified > best[cell][1]:
            best[cell] = (key, modified)
    if len(best) < len(items):
        print(f"  deduped {len(items) - len(best)} re-issued cell(s)")
    return sorted(key for key, _ in best.values())


def enumerate_keys(source):
    """-> (bucket, sorted upstream keys) from ``file_list.txt``. Everything must live
    on ONE bucket: ``mirror.txt`` is a flat key list rooted there and the mirrored
    layout preserves upstream keys 1:1, so a second bucket would collide. Enforced,
    not assumed."""
    pairs = []  # (bucket, key)
    for entry in config.file_list(source):
        if entry.endswith("/"):
            bucket, prefix = _split_bucket_key(entry)
            listed = list_prefix(bucket, prefix)
            products = [(k, m) for k, m in listed
                        if k.endswith(".h5") and "/_CATALOG/" not in k]
            print(f"{source}: {len(listed)} keys under {entry}, {len(products)} .h5 products")
            if not products:
                sys.exit(f"{source}: no .h5 products under {entry}")
            pairs += [(bucket, k) for k in dedupe_cells(products)]
        else:
            print(f"{source}: reading urllist {entry}")
            r = requests.get(entry, timeout=60)
            r.raise_for_status()
            # A urllist also names sidecars (tile-index .shp/.shx/.dbf, .vrt, .xml,
            # .pdf, itself); keep only raster tiles.
            tiles = [u for u in filelist_urls(r.text)
                     if u.lower().endswith((".tif", ".tiff"))]
            pairs += [_split_bucket_key(u) for u in tiles]
    buckets = {b for b, _ in pairs}
    if len(buckets) > 1:
        sys.exit(f"{source}: objects span multiple buckets {sorted(buckets)} — one mirror root per source")
    return (buckets.pop() if buckets else None), sorted({k for _, k in pairs})


def h5_header(vsi, tries=3):
    """3857 bounds + pixel dims from an HDF5 product's header, via a ``gdalinfo -json``
    *subprocess*. The isolation is the point: bathymetric-surface drivers have
    segfaulted on degenerate 1x1 stub products, and a crash must fail this one probe
    with a readable error instead of killing registration. Bounds come from gdalinfo's
    ``wgs84Extent`` (its bbox -> 3857) rather than re-parsing projected corners + WKT —
    one version-stable JSON field, and it matches the corner reprojection to ~mm
    (verified against live products). Transient fetch errors retry; a persistent
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
    """3857 bounds + pixel dims from a GeoTIFF header — in-process (the GeoTIFF
    driver has no crash history, so it keeps the cheap path; HDF5 goes through the
    subprocess isolation above)."""
    with rasterio.open(vsi) as src:
        if src.crs is None:
            raise RuntimeError(f"crs not defined on {vsi}")
        left, bottom, right, top = bounds_3857(src)
        return left, bottom, right, top, src.width, src.height


def read_header(bucket, key):
    """Header -> (3857 bounds, pixel dims) for one upstream object, read from the
    UPSTREAM URL, not the store: a new key was listed seconds ago and its bytes may
    not be mirrored yet. A failure here fails the run (see module docstring)."""
    vsi = to_vsicurl(f"https://{bucket}.s3.amazonaws.com/{key}")
    return h5_header(vsi) if key.lower().endswith(".h5") else tif_header(vsi)


def _upstream_key(filename):
    """A previous bounds.csv filename -> its upstream key, for the removed-key diff.
    Handles both this module's relative ``objects/<key>`` rows and the absolute
    ``/vsicurl/https://…`` rows the pre-mirror registration wrote, so the first run
    after cutover diffs honestly instead of reporting every key as removed."""
    if filename.startswith("objects/"):
        return filename[len("objects/"):]
    m = re.match(r"^/vsicurl/https://[^/]+/(.+)$", filename)
    return m.group(1) if m else filename


def register(source, keys, header):
    """bounds.csv + mirror.txt from the enumerated upstream keys.

    Carry-forward first: a key whose ``objects/<key>`` row is already registered
    keeps it verbatim — zero network reads, so a steady-state refresh touches ~tens
    of headers, not thousands. (Rows in the older absolute-URL shape don't qualify:
    their pixel dims came from an index approximation, so the one-time full header
    sweep at cutover re-measures every product for real.)

    Guards run BEFORE any header read or write: removed keys print loudly, and a
    shrink beyond SHRINK_TOLERANCE aborts with the previous registration intact
    (MIRROR_ALLOW_SHRINK=1 overrides). Only after every new header read succeeds do
    mirror.txt and bounds.csv get written — a half-read registration publishes
    nothing."""
    keys = sorted(set(keys))
    prev = _prev_bounds(source)
    removed = sorted({_upstream_key(f) for f in prev} - set(keys))
    for key in removed:
        print(f"REMOVED upstream: {key} (was registered, no longer listed)")
    shrink = 1 - len(keys) / len(prev) if prev else 0.0
    if shrink > SHRINK_TOLERANCE:
        msg = (f"{source}: enumeration shrank {len(prev)} -> {len(keys)} rows "
               f"({shrink:.1%} > {SHRINK_TOLERANCE:.0%})")
        if os.environ.get("MIRROR_ALLOW_SHRINK"):
            print(f"WARNING: {msg} — allowed by MIRROR_ALLOW_SHRINK")
        else:
            sys.exit(f"{msg} — upstream looks broken/half-published; refusing to publish "
                     f"over the previous registration (MIRROR_ALLOW_SHRINK=1 to override)")

    rows, todo = [], []
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
            row = (f"objects/{key}", *header(key))
            nonlocal done
            with lock:
                done += 1
                if done % 100 == 0:
                    print(f"  {done}/{len(todo)} headers")
            return i, row

        # Header reads are network-bound (one ranged GET each); a small pool turns
        # the cutover-sized sweep from hours into minutes. pool.map re-raises the
        # first failure — one bad header still fails the whole registration.
        with ThreadPoolExecutor(max_workers=16) as pool:
            for i, row in pool.map(read_one, todo):
                rows[i] = row

    os.makedirs(f"store/source/{source}", exist_ok=True)
    with open(f"store/source/{source}/mirror.txt", "w") as f:
        f.writelines(key + "\n" for key in keys)
    write_bounds(source, rows)
    print(f"{source}: {len(rows) - len(todo)} carried forward, {len(todo)} newly read, "
          f"{len(removed)} removed")


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: source_mirror.py <source-id>")
    source = sys.argv[1]
    bucket, keys = enumerate_keys(source)
    if not keys:
        sys.exit(f"{source}: nothing enumerated — refusing to publish an empty registration")
    print(f"{source}: {len(keys)} objects on {bucket}")
    register(source, keys, lambda key: read_header(bucket, key))
    # After register() so a guarded/failed run leaves nothing half-written; the
    # mirror process reads the bucket from here instead of re-deriving it from
    # file_list.txt with a second, weaker URL parser.
    with open(f"store/source/{source}/mirror-bucket.txt", "w") as f:
        f.write(bucket + "\n")


def _check():
    import shutil

    # cell split: the fixed-width issue field, exercised on the live population's
    # trip-wires — a letter-bearing issue code, a cell code ending in a digit, and
    # the one short cell code (all real ed3.0.0 products).
    assert cell_key("ed3.0.0/Southeast/Jacksonville/102US005JAXEF262297.h5") == "102US005JAXEF"
    assert cell_key("ed3.0.0/x/102US004LA1DO2622F7.h5") == "102US004LA1DO"   # issue has a letter
    assert cell_key("ed3.0.0/x/102US005FL7G1262227.h5") == "102US005FL7G1"   # cell ends in a digit
    assert cell_key("ed3.0.0/x/102US004OHLN262257.h5") == "102US004OHLN"     # short cell code

    # dedupe keeps newest-by-LastModified, NOT the lexically-greatest issue suffix
    # (the suffix encoding is undocumented — lexical order on it means nothing).
    items = [("p/102US005JAXEF262297.h5", "2026-01-01T00:00:00.000Z"),
             ("p/102US005JAXEF262227.h5", "2026-06-01T00:00:00.000Z"),  # lower suffix, newer object
             ("p/102US005JAXEG262247.h5", "2026-03-01T00:00:00.000Z")]
    assert dedupe_cells(items) == ["p/102US005JAXEF262227.h5", "p/102US005JAXEG262247.h5"], \
        dedupe_cells(items)

    # listing XML: Key/LastModified pair up — including in a pretty-printed variant —
    # and the continuation token is found
    xml = ('<ListBucketResult><IsTruncated>true</IsTruncated>'
           '<Contents><Key>a/x.h5</Key><LastModified>2026-01-02T03:04:05.000Z</LastModified>'
           '<ETag>"e"</ETag><Size>1</Size></Contents>'
           '<NextContinuationToken>tok==</NextContinuationToken></ListBucketResult>')
    assert _CONTENTS_RE.findall(xml) == [("a/x.h5", "2026-01-02T03:04:05.000Z")]
    pretty = xml.replace("><", ">\n  <")
    assert _CONTENTS_RE.findall(pretty) == [("a/x.h5", "2026-01-02T03:04:05.000Z")]
    # a complete listing has no token; a truncated one yields it (even pretty-printed);
    # truncated WITHOUT a parsable token refuses the partial enumeration
    assert _next_token(xml.replace("true", "false")) is None
    assert _next_token(xml) == "tok==" and _next_token(pretty) == "tok=="
    try:
        _next_token("<IsTruncated>true</IsTruncated>")
        assert False, "expected a truncated page without a token to exit"
    except SystemExit as e:
        assert "partial enumeration" in str(e), e

    # URL splitting + previous-filename normalization for the removed-key diff
    assert _split_bucket_key("https://noaa-s102-pds.s3.amazonaws.com/ed3.0.0/") == \
        ("noaa-s102-pds", "ed3.0.0/")
    assert _split_bucket_key("s3://b/k/t.tif") == ("b", "k/t.tif")
    assert _upstream_key("objects/dem/x.tif") == "dem/x.tif"
    assert _upstream_key("/vsicurl/https://noaa-s102-pds.s3.amazonaws.com/ed3.0.0/a.h5") == "ed3.0.0/a.h5"

    # register(): carried-forward rows do ZERO network reads (the fake header fn
    # records every call — same offline assertion pattern as source_remote), rows
    # come out relative, and mirror.txt carries the FULL key list, not just new keys.
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

    # shrink guard: dropping 1 of 2 rows (50% > 5%) refuses to publish and leaves
    # the previous registration untouched…
    try:
        register(src, ["dem/a.tif"], fake_header)
        assert False, "expected the shrink guard to exit"
    except SystemExit as e:
        assert "refusing to publish" in str(e), e
    with open(f"store/source/{src}/bounds.csv") as f:
        assert len(f.read().splitlines()) == 3, "guard must not overwrite bounds.csv"
    # …and MIRROR_ALLOW_SHRINK overrides it.
    os.environ["MIRROR_ALLOW_SHRINK"] = "1"
    try:
        register(src, ["dem/a.tif"], fake_header)
    finally:
        del os.environ["MIRROR_ALLOW_SHRINK"]
    with open(f"store/source/{src}/bounds.csv") as f:
        assert len(f.read().splitlines()) == 2
    shutil.rmtree(f"store/source/{src}")

    # a header-read failure raises (offline: gdalinfo on a path that cannot exist
    # exits nonzero instantly — the same subprocess path a segfaulting driver hits)
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
