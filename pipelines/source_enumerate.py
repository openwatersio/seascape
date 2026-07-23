"""Enumerate one source's fetchable items -> store/source/<id>/items.txt.

The single enumeration entry point for EVERY source — one item (a fetchable URL) per
line. Two modes, chosen by whether ``metadata.json`` declares a ``filter``:

  static (no filter)  — echo ``file_list.txt`` verbatim (comments already stripped): the
                        committed list IS the enumeration, so the job is trivial + instant.
  listed (filter set) — each ``file_list.txt`` line is a bucket PREFIX (trailing ``/`` —
                        anonymous ListObjectsV2) or a urllist MANIFEST (fetched, one URL
                        per line). The resulting keys/URLs are kept by ``filter`` (fnmatch
                        over the key path, ``|``-separated alternatives) and, when the
                        source declares a ``dedupe`` strategy, reduced by it.

A listed enumeration that shrinks past SHRINK_TOLERANCE vs the previous items.txt refuses
to overwrite it (MIRROR_ALLOW_SHRINK=1 overrides) — a mass disappearance is an upstream
break (repartition, auth change, half-published edition), not a real edition. A static
list shrinks only when its committed file_list.txt does, which is intentional by
definition, so static enumerations are never guarded. items.txt is written only when it
changes, so an unchanged enumeration doesn't cascade downstream.

Run from pipelines/:  uv run python source_enumerate.py <source-id>
"""

import fnmatch
import os
import re
import sys
from urllib.parse import urlsplit

import requests

import config
import utils

# Refuse a listed enumeration that shrank by more than this fraction of the previous one — a
# small prune of superseded tiles passes, a mass disappearance trips the guard (see docstring).
SHRINK_TOLERANCE = 0.05

# S-102 filenames end in a fixed-width 6-character issue field before ``.h5``
# (``102US005JAXEF262297.h5`` -> cell ``102US005JAXEF``, issue ``262297``). Verified against
# the full ed3.0.0 population: the field is alphanumeric (NOT digits-only) and cell codes
# themselves can end in digits, so a fixed-width strip is the only split that groups every
# product correctly.
ISSUE_FIELD_LEN = 6

# ListObjectsV2 response fields, matched by regex rather than an XML parser — skipping the
# parser sidesteps XXE/entity expansion on an untrusted payload. The live response keeps
# Key/LastModified adjacent per <Contents>; the \s* tolerates a pretty-printed variant.
_CONTENTS_RE = re.compile(r"<Key>([^<]+)</Key>\s*<LastModified>([^<]+)</LastModified>")
_TOKEN_RE = re.compile(r"<NextContinuationToken>\s*([^<\s]+)\s*</NextContinuationToken>")


def _split_bucket_key(url):
    """A public object/prefix URL -> (bucket, key). Virtual-host https form
    (``https://<bucket>.s3[.region].amazonaws.com/<key>``) or ``s3://<bucket>/<key>``. The
    key may be empty — a bare bucket root is a valid list prefix (nz_coastal lists the whole
    bucket, then filters)."""
    m = re.match(r"^https://([^./]+)\.s3[^/]*\.amazonaws\.com/(.*)$", url)
    if m:
        return m.group(1), m.group(2)
    m = re.match(r"^s3://([^/]+)/(.*)$", url)
    if m:
        return m.group(1), m.group(2)
    sys.exit(f"cannot split bucket/key from {url} — listed sources need public-bucket URLs")


def _next_token(xml):
    """Continuation token of a truncated listing page, or None when complete. A truncated
    page whose token can't be found is a hard error: returning None there would silently
    enumerate a partial bucket."""
    if "<IsTruncated>true</IsTruncated>" not in xml:
        return None
    m = _TOKEN_RE.search(xml)
    if not m:
        sys.exit("truncated listing without a parsable NextContinuationToken — "
                 "refusing a partial enumeration")
    return m.group(1)


def list_prefix(bucket, prefix):
    """Every (key, LastModified) under a public bucket prefix — paginated ListObjectsV2, no
    creds. LastModified rides along because it is the only trustworthy recency signal for a
    dedupe strategy."""
    host = f"https://{bucket}.s3.amazonaws.com"
    items, token = [], None
    while True:
        params = {"list-type": "2", "prefix": prefix}
        if token:
            params["continuation-token"] = token
        r = requests.get(host, params=params, timeout=60,
                         headers={"User-Agent": utils.USER_AGENT})
        r.raise_for_status()
        items += _CONTENTS_RE.findall(r.text)
        token = _next_token(r.text)
        if not token:
            return items


def filelist_urls(text):
    """File URLs in a downloaded urllist manifest: non-blank, non-comment lines."""
    return [l.strip() for l in text.splitlines()
            if l.strip() and not l.lstrip().startswith("#")]


def _fetch_urllist(url):
    r = requests.get(url, timeout=60, headers={"User-Agent": utils.USER_AGENT})
    r.raise_for_status()
    return filelist_urls(r.text)


def _key_path(url):
    """The key path a `filter` matches against: everything after the host, no leading slash."""
    return urlsplit(url).path.lstrip("/")


def _matches(path, pattern):
    """fnmatch `path` against a `|`-separated alternation of globs (case-insensitive)."""
    lp = path.lower()
    return any(fnmatch.fnmatch(lp, alt.strip().lower()) for alt in pattern.split("|"))


def cell_key(key):
    """The re-issue-stable cell id of an S-102 product key/URL: basename minus ``.h5`` minus
    the fixed-width issue field. A degenerate short name keys as itself."""
    stem = key.rsplit("/", 1)[-1].removesuffix(".h5")
    return stem[:-ISSUE_FIELD_LEN] if len(stem) > ISSUE_FIELD_LEN else stem


def _dedupe_s102_issue(pairs):
    """[(url, LastModified)] -> one URL per S-102 cell, keeping the NEWEST LastModified. The
    ``_CATALOG/`` sidecar exclusion is S-102-layout knowledge, so it lives in the strategy,
    not in a filter. Issue-field encoding is undocumented, so lexical order on it means
    nothing; the listing's ISO-8601 timestamps are the only trustworthy order (string max ==
    newest). Steady-state the upstream deletes the superseded issue; the dedupe exists for the
    mid-republish window where both issues list at once."""
    kept = [(u, m) for u, m in pairs if "/_CATALOG/" not in u]
    best = {}
    for url, modified in kept:
        cell = cell_key(url)
        if cell not in best or modified > best[cell][1]:
            best[cell] = (url, modified)
    if len(best) < len(kept):
        print(f"  deduped {len(kept) - len(best)} re-issued cell(s)")
    return sorted(u for u, _ in best.values())


DEDUPERS = {"s102-issue": _dedupe_s102_issue}


def enumerate_source(source):
    """-> (items, listed): the fetchable URLs plus whether this was a listed enumeration."""
    meta = config.load_metadata(source)
    filter_pat = meta.get("filter")
    dedupe = meta.get("dedupe")
    entries = config.file_list(source)

    if filter_pat is None:
        if dedupe is not None:
            sys.exit(f"{source}: `dedupe` needs a `filter` — it applies to listed enumerations only")
        for e in entries:
            if e.endswith("/"):
                sys.exit(f"{source}: bucket-prefix entry {e!r} needs a `filter` "
                         "(a static source echoes plain URLs)")
        return entries, False  # static: the committed list IS the enumeration

    if dedupe and dedupe not in DEDUPERS:
        sys.exit(f"{source}: unknown dedupe strategy {dedupe!r} (known: {sorted(DEDUPERS)})")
    candidates = []  # (url, LastModified|None)
    for entry in entries:
        if entry.endswith("/"):
            bucket, prefix = _split_bucket_key(entry)
            listed = list_prefix(bucket, prefix)
            print(f"{source}: {len(listed)} keys under {entry}")
            candidates += [(f"https://{bucket}.s3.amazonaws.com/{k}", m) for k, m in listed]
        else:
            print(f"{source}: reading urllist {entry}")
            candidates += [(u, None) for u in _fetch_urllist(entry)]
    kept = [(u, m) for u, m in candidates if _matches(_key_path(u), filter_pat)]
    print(f"{source}: {len(kept)}/{len(candidates)} listed keys pass filter {filter_pat!r}")
    if not kept:
        sys.exit(f"{source}: filter {filter_pat!r} matched none of {len(candidates)} listed "
                 "keys — the upstream layout may have moved")
    items = DEDUPERS[dedupe](kept) if dedupe else sorted(u for u, _ in kept)
    return items, True


def write_items(source, items, listed):
    """Write items.txt (write-if-changed). A listed enumeration guards against a shrink past
    SHRINK_TOLERANCE vs the previous items.txt; a static one never does."""
    path = f"store/source/{source}/items.txt"
    # raws are keyed by URL hash and staged names by list position, so a duplicate URL
    # would silently collapse to one raw yet stage twice — fail at the source of truth
    if len(set(items)) != len(items):
        from collections import Counter
        dupes = sorted(u for u, n in Counter(items).items() if n > 1)
        sys.exit(f"{source}: duplicate item URL(s) in the enumeration: {dupes[:3]}"
                 f"{' …' if len(dupes) > 3 else ''}")
    if listed:
        prev = config.items(source)
        shrink = 1 - len(items) / len(prev) if prev else 0.0
        if shrink > SHRINK_TOLERANCE:
            msg = (f"{source}: enumeration shrank {len(prev)} -> {len(items)} items "
                   f"({shrink:.1%} > {SHRINK_TOLERANCE:.0%})")
            if os.environ.get("MIRROR_ALLOW_SHRINK"):
                print(f"WARNING: {msg} — allowed by MIRROR_ALLOW_SHRINK")
            else:
                sys.exit(f"{msg} — upstream looks broken/half-published; refusing to overwrite "
                         "the previous items.txt (MIRROR_ALLOW_SHRINK=1 to override)")
    os.makedirs(f"store/source/{source}", exist_ok=True)
    changed = utils.write_if_changed(path, "".join(u + "\n" for u in items))
    print(f"{source}: {len(items)} items ({'wrote' if changed else 'unchanged'}) -> {path}")


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: source_enumerate.py <source-id>")
    source = sys.argv[1]
    items, listed = enumerate_source(source)
    write_items(source, items, listed)


def _check():
    import json
    import shutil
    import tempfile

    # URL splitting: virtual-host https + s3:// forms.
    assert _split_bucket_key("https://noaa-s102-pds.s3.amazonaws.com/ed3.0.0/") == \
        ("noaa-s102-pds", "ed3.0.0/")
    assert _split_bucket_key("https://b.s3.ap-southeast-2.amazonaws.com/a/x.tiff") == ("b", "a/x.tiff")
    assert _split_bucket_key("s3://b/k/t.tif") == ("b", "k/t.tif")

    # listing XML: Key/LastModified pair up (incl. pretty-printed) and the token is found.
    xml = ('<ListBucketResult><IsTruncated>true</IsTruncated>'
           '<Contents><Key>a/x.h5</Key><LastModified>2026-01-02T03:04:05.000Z</LastModified>'
           '<ETag>"e"</ETag><Size>1</Size></Contents>'
           '<NextContinuationToken>tok==</NextContinuationToken></ListBucketResult>')
    assert _CONTENTS_RE.findall(xml) == [("a/x.h5", "2026-01-02T03:04:05.000Z")]
    pretty = xml.replace("><", ">\n  <")
    assert _CONTENTS_RE.findall(pretty) == [("a/x.h5", "2026-01-02T03:04:05.000Z")]
    assert _next_token(xml.replace("true", "false")) is None
    assert _next_token(xml) == "tok==" and _next_token(pretty) == "tok=="
    try:
        _next_token("<IsTruncated>true</IsTruncated>")
        assert False, "expected a truncated page without a token to exit"
    except SystemExit as e:
        assert "partial enumeration" in str(e), e

    # filter: |-alternation, key-path match (nz_coastal's nested glob), case-insensitive.
    assert _matches("dem/x.tif", "*.tif|*.tiff") and _matches("dem/x.tiff", "*.tif|*.tiff")
    assert not _matches("dem/x.xml", "*.tif|*.tiff")
    assert _matches("auckland/s_2025/dem_1m/2193/AY31.tiff", "*/dem_1m/*/*.tiff")
    assert not _matches("auckland/s_2025/dsm_1m/2193/AY31.tiff", "*/dem_1m/*/*.tiff")
    assert not _matches("auckland/s_2025/dem_10m/2193/AY31.tiff", "*/dem_1m/*/*.tiff")

    # cell split + s102-issue dedupe: newest-by-LastModified, drop _CATALOG, letter-bearing
    # issue codes and cell codes ending in a digit both split correctly.
    assert cell_key("ed3.0.0/x/102US005JAXEF262297.h5") == "102US005JAXEF"
    assert cell_key("ed3.0.0/x/102US004LA1DO2622F7.h5") == "102US004LA1DO"
    assert cell_key("ed3.0.0/x/102US005FL7G1262227.h5") == "102US005FL7G1"
    pairs = [("h/p/102US005JAXEF262297.h5", "2026-01-01T00:00:00.000Z"),
             ("h/p/102US005JAXEF262227.h5", "2026-06-01T00:00:00.000Z"),  # lower suffix, newer
             ("h/p/102US005JAXEG262247.h5", "2026-03-01T00:00:00.000Z"),
             ("h/_CATALOG/102US005JAXEF262297.h5", "2026-09-01T00:00:00.000Z")]  # sidecar dropped
    assert _dedupe_s102_issue(pairs) == ["h/p/102US005JAXEF262227.h5", "h/p/102US005JAXEG262247.h5"], \
        _dedupe_s102_issue(pairs)

    # enumerate + write_items against a tmp store: static echoes verbatim + never guards a
    # shrink; a static prefix line without a filter errors; a listed shrink refuses to write.
    d = tempfile.mkdtemp()
    cwd, saved = os.getcwd(), config.SOURCES_DIR
    try:
        os.chdir(d)
        config.SOURCES_DIR = "sources"

        sid = "_enum_static"
        os.makedirs(f"sources/{sid}")
        with open(f"sources/{sid}/file_list.txt", "w") as f:
            f.write("# a comment\nhttps://x/a.tif\nhttps://x/b.tif\n")
        with open(f"sources/{sid}/metadata.json", "w") as f:
            json.dump({"name": "S"}, f)
        items, listed = enumerate_source(sid)
        assert items == ["https://x/a.tif", "https://x/b.tif"] and not listed, (items, listed)
        write_items(sid, items, listed)
        assert config.items(sid) == items
        write_items(sid, ["https://x/a.tif"], listed=False)  # static shrink is fine — no guard
        assert config.items(sid) == ["https://x/a.tif"]
        # duplicate URLs fail fast — they'd collapse to one raw hash but stage twice
        try:
            write_items(sid, ["https://x/a.tif", "https://x/a.tif"], listed=False)
            assert False, "expected duplicate items to exit"
        except SystemExit as e:
            assert "duplicate item URL" in str(e) and "a.tif" in str(e), e

        with open(f"sources/{sid}/file_list.txt", "w") as f:
            f.write("https://x/prefix/\n")
        try:
            enumerate_source(sid)
            assert False, "expected a prefix line without a filter to exit"
        except SystemExit as e:
            assert "needs a `filter`" in str(e), e

        lid = "_enum_listed"
        os.makedirs(f"sources/{lid}")
        with open(f"sources/{lid}/file_list.txt", "w") as f:
            f.write("https://x/\n")
        with open(f"sources/{lid}/metadata.json", "w") as f:
            json.dump({"name": "L", "filter": "*.tif"}, f)
        os.makedirs(f"store/source/{lid}")
        write_items(lid, [f"https://x/t{i}.tif" for i in range(10)], listed=True)
        try:
            write_items(lid, ["https://x/t0.tif"], listed=True)  # 10 -> 1 = 90% shrink
            assert False, "expected the shrink guard to exit"
        except SystemExit as e:
            assert "refusing to overwrite" in str(e), e
        assert len(config.items(lid)) == 10, "guard must leave items.txt intact"
        os.environ["MIRROR_ALLOW_SHRINK"] = "1"
        try:
            write_items(lid, ["https://x/t0.tif"], listed=True)
        finally:
            del os.environ["MIRROR_ALLOW_SHRINK"]
        assert config.items(lid) == ["https://x/t0.tif"]
    finally:
        os.chdir(cwd)
        config.SOURCES_DIR = saved
        shutil.rmtree(d, ignore_errors=True)
    print("source_enumerate.py self-check ok")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        _check()
    else:
        main()
