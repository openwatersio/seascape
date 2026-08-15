#!/usr/bin/env python3
"""Regenerate file_list.txt for the EA Multibeam Bathymetry source (NOT part of the build).

The build itself just fetches the URLs in file_list.txt — no API calls, no key minting.
Re-run this only when the public subscription key rotates or the EA ships new survey years:

  python harvest.py            # sweeps the Defra survey tile API, rewrites file_list.txt

Acquisition is the same two unauthenticated HTTPS calls sources/uk_surfzone documents, against
a different product:

  index:    POST .../backend/catalog/api/tiles/collections/survey/search   (GeoJSON Polygon, 4326)
            → results[] each with product.id, year.id, tile.id (OS 5km, e.g. "SY9585")
  download: GET  {TILE_BASE}/<product>/<year>/<res>/<tile.id>?subscription-key=<KEY>
            → application/zip of ESRI ASCII grids <ostile><quadrant>_<surveydate>mb.asc,
              one per surveyed 500 m quadrant of the 5 km tile (1000 x 1000 px at 0.5 m)

KEY is a public client-side key baked into the site's JS bundle (currently "dspui"); it's
unauthenticated by design but not contractually stable, so pin it here — rotation is a one-liner.
We sweep England's coast in coarse cells (one search caps its result set, so a single national
polygon would truncate) and keep one year per tile: the archive re-surveys the same 5 km tiles,
and two vintages of one tile would stack overlapping rasters inside one source, where the merge
has no priority to order them.

Each tile's years are then probed newest-first, because the index and the tile store disagree:
some (tile, year) pairs the search returns 404 at download, and a 404 in file_list.txt fails
the source's fetch. Three constraints shape the probe — the API answers HEAD with 405, so it
has to be a GET abandoned after the first byte; it is rate-sensitive enough that concurrency
draws spurious 403/504, hence serial; and a 504 means "still buffering", not "absent", so only
a 404 is allowed to demote a tile to an older survey. Budget ~2 h: the server buffers each zip
whole before it answers (up to ~45 s for a large tile), and PACE_S spaces the probes out so the
sweep doesn't drive the endpoint into blanket 502/504s — unpaced, it does, within a few hundred
requests, and then nothing resolves until it recovers.

Stdlib only, no pipeline coupling.
"""

import io
import json
import sys
import time
import urllib.error
import urllib.request

SEARCH = "https://environment.data.gov.uk/backend/catalog/api/tiles/collections/survey/search"
TILE_BASE = "https://environment.data.gov.uk/tiles/collections/survey"
PRODUCT, RES = "bathymetry_coastal_multibeam", "0.5"
KEY = "dspui"
BBOX = (-5.9, 49.8, 2.0, 56.0)  # England coast (incl. Scilly); the EA archive is England-only
STEP = 0.8                      # cell size in degrees — small enough that no cell truncates
# Every probe makes the server buffer a whole ~25 MB zip, so back-to-back probes degrade the
# endpoint into 502/504s for everyone. Pace them; the sweep is slow either way.
PACE_S = 5
HEADERS = {"Content-Type": "application/geo+json", "Accept": "*/*",
           "Origin": "https://environment.data.gov.uk",
           "Referer": "https://environment.data.gov.uk/survey"}


def tile_url(tile_id, year):
    return f"{TILE_BASE}/{PRODUCT}/{year}/{RES}/{tile_id}?subscription-key={KEY}"


def cells(bbox, step):
    w, s, e, n = bbox
    la = s
    while la < n:
        lo = w
        while lo < e:
            yield (lo, la, min(lo + step, e), min(la + step, n))
            lo += step
        la += step


class Unresolved(Exception):
    """Retries exhausted with no definitive answer from the API."""


def _retrying(call, attempts=4):
    """Run an API call, backing off through the 502/504s the Defra endpoint returns under any
    sustained load. Only a 404 is definitive and passes straight through to the caller."""
    for attempt in range(attempts):
        try:
            return call()
        except urllib.error.HTTPError as ex:
            if ex.code == 404:
                raise
            last = ex.code
        except Exception as ex:
            last = ex
        if attempt < attempts - 1:
            time.sleep(5 * 2 ** attempt)
    raise Unresolved(f"{last} after {attempts} attempts")


def search(w, s, e, n):
    body = json.dumps({"type": "Polygon",
                       "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}).encode()

    def call():
        req = urllib.request.Request(SEARCH, data=body, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r).get("results", [])

    return _retrying(call)


def harvest(searcher=search):
    """Sweep the index → {tile: {year, …}}. Cells that fail are re-swept in a second pass:
    the endpoint's 502s outlast one cell's backoff but clear over the minutes a full sweep
    takes, and a silently dropped cell would shrink coverage with no failure to notice."""
    years, grid = {}, list(cells(BBOX, STEP))
    pending = list(enumerate(grid, 1))
    for attempt in range(2):
        failed = []
        for i, c in pending:
            try:
                for res in searcher(*c):
                    if res["product"]["id"] != PRODUCT or res["resolution"]["id"] != RES:
                        continue
                    years.setdefault(res["tile"]["id"], set()).add(res["year"]["id"])
            except Exception as ex:
                failed.append((i, c))
                print(f"  cell {i}/{len(grid)} failed: {ex}")
            if not attempt and i % 10 == 0:
                print(f"  {i}/{len(grid)} cells, {len(years)} tiles")
        pending = failed
        if not pending:
            return years
        print(f"  re-sweeping {len(pending)} failed cell(s)")
    # Never probe a partial sweep: the missing tiles would look like real absence.
    sys.exit(f"{len(pending)} cell(s) failed twice — coverage would be incomplete; re-run.")


def serves(url):
    """Whether the tile store actually has this (tile, year) — a GET abandoned after the first
    byte, since the API answers HEAD with 405. Only a 404 proves absence: the API 504s while
    buffering a slow tile, and reading that as absent silently demotes the tile to an older
    survey, so an unresolvable probe raises rather than lying."""
    def call():
        time.sleep(PACE_S)
        with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS),
                                    timeout=300) as r:
            return bool(r.read(1))

    try:
        return _retrying(call)
    except urllib.error.HTTPError:  # only the definitive 404 escapes _retrying
        return False
    except Unresolved as ex:
        raise Unresolved(f"{ex} on {url}") from None


def resolve(years, probe=serves):
    """Newest served year per tile. Probed serially: concurrency draws spurious 403/504.
    Only a confirmed year is written, so an unresolvable one falls through to an older
    survey rather than putting a URL the build can't fetch in file_list.txt."""
    resolved, dropped, flaky = [], [], []
    for i, (tile, ys) in enumerate(sorted(years.items()), 1):
        newest, skipped = max(ys), []
        for year in sorted(ys, reverse=True):
            try:
                ok = probe(tile_url(tile, year))
            except Unresolved as ex:
                flaky.append(f"{tile}/{year}")
                skipped.append(f"{year} unconfirmed")
                print(f"  {ex}")
                continue
            if ok:
                resolved.append((tile, year))
                if year != newest:
                    print(f"  {tile}: fell back to {year} ({', '.join(skipped)})")
                break
            skipped.append(f"{year} 404")
        else:
            dropped.append(tile)
        if i % 10 == 0:
            print(f"  probed {i}/{len(years)} tiles, {len(resolved)} served")
    if flaky:
        print(f"WARNING: {len(flaky)} (tile, year) never answered definitively (a newer survey "
              f"may exist): {' '.join(flaky)}")
    if dropped:
        print(f"WARNING: {len(dropped)} tile(s) served no year at all: {' '.join(dropped)}")
    return resolved


def write_file_list(tiles, path):
    header = (
        f"# EA Multibeam Bathymetry (Environment Agency, England nearshore, 0.5 m) — OGL v3.0.\n"
        f"# {len(tiles)} OS 5km tiles, newest served survey year each, fetched as per-tile ZIPs of\n"
        f"# 0.5 m ESRI ASCII grids from the Defra survey tile API over public HTTPS (the\n"
        f"# ?subscription-key={KEY} below is a public client-side key, no login). Generated by\n"
        f"# harvest.py — re-run it if the key rotates or the EA ships new survey years.\n")
    with open(path, "w") as f:
        f.write(header)
        f.writelines(tile_url(t, y) + "\n" for t, y in tiles)


def _http(code):
    return urllib.error.HTTPError("u", code, "", {}, io.BytesIO(b""))


def _check():
    def raises(code):
        def call():
            raise _http(code)
        return call

    try:  # a 404 is definitive and must not be retried into Unresolved
        _retrying(raises(404), attempts=1)
        assert False, "404 must propagate"
    except urllib.error.HTTPError as ex:
        assert ex.code == 404
    try:
        _retrying(raises(504), attempts=1)
        assert False, "an exhausted retry must raise Unresolved, not return"
    except Unresolved as ex:
        assert "504" in str(ex), ex
    tries = []

    def recovers():
        tries.append(1)
        if len(tries) < 2:
            raise _http(502)
        return "ok"

    assert _retrying(recovers, attempts=2) == "ok"

    assert tile_url("SY9585", "2022") == (
        "https://environment.data.gov.uk/tiles/collections/survey/"
        "bathymetry_coastal_multibeam/2022/0.5/SY9585?subscription-key=dspui")
    g = list(cells((0.0, 0.0, 1.6, 0.8), 0.8))  # 2 cells wide, 1 tall; clamps the last edge
    assert len(g) == 2 and g[0] == (0.0, 0.0, 0.8, 0.8) and g[1][2] == 1.6
    # A cell that fails once is re-swept, and its tiles still land in the result.
    hit = {"product": {"id": PRODUCT}, "resolution": {"id": RES},
           "tile": {"id": "TQ0000"}, "year": {"id": "2020"}}
    seen_cells = []

    def once_flaky(w, s, e, n):
        seen_cells.append((w, s))
        if len(seen_cells) == 1:
            raise Unresolved("502 after 4 attempts")
        return [hit] if (w, s) == seen_cells[0] else []

    assert harvest(searcher=once_flaky) == {"TQ0000": {"2020"}}, "a re-swept cell must count"

    served = {tile_url("TF4590", "2011"), tile_url("TQ0000", "2020")}
    r = resolve({"TF4590": {"2011", "2017"},  # newest 404s → falls back
                 "TQ0000": {"2020"},
                 "SX0000": {"1999"}},         # nothing served → dropped entirely
                probe=served.__contains__)
    assert r == [("TF4590", "2011"), ("TQ0000", "2020")], r

    def flaky(url):  # a 504 must never be read as absence
        raise Unresolved(f"504 on {url}")

    assert resolve({"TA0000": {"2020"}}, probe=flaky) == []
    calls = []

    def once(url):  # 504 on the newest, 404 on the next, served on the oldest
        calls.append(url)
        if url.endswith("2020/0.5/TA0000?subscription-key=dspui"):
            raise Unresolved("504")
        return url.endswith("2011/0.5/TA0000?subscription-key=dspui")

    assert resolve({"TA0000": {"2011", "2015", "2020"}}, probe=once) == [("TA0000", "2011")]
    assert len(calls) == 3, calls
    print("harvest.py self-check ok")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        _check()
    else:
        path = f"{sys.path[0]}/file_list.txt"
        tiles = resolve(harvest())
        if not tiles:
            sys.exit("no tiles found — the key may have rotated or the API moved; check the site.")
        write_file_list(tiles, path)
        print(f"wrote {len(tiles)} tile URLs to {path}")
