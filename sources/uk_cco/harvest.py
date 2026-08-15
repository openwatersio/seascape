#!/usr/bin/env python3
"""Refresh the CCO R2 mirror — a one-off acquisition tool (NOT part of the build).

The Channel Coastal Observatory publishes the National Network of Regional Coastal Monitoring
Programmes' multibeam archive: ~13.5k ESRI ASCII grids on the OS 1 km grid, England and Wales.
Nothing about it is CI-fetchable — every file goes through a session-cookied metadata search, a
basket, and a download-request form that compiles the selection into a one-off tokenised zip. So
we harvest once, repack, and mirror to R2; `file_list.txt` then fetches that mirror over public
HTTPS. Re-run when CCO publishes new surveys:

  python harvest.py <work_dir>     # catalog → order → download → repack → manifest (resumable)
  rclone copy <work_dir>/mirror r2:data/bathymetry/mirror/uk_cco --include '*.zip'
  python harvest.py <work_dir> --file-list   # rewrite file_list.txt from the manifest
  python harvest.py <work_dir> --repack      # rebuild the mirror from the retained order zips
                                             # (a change to what repack keeps, no re-download)

CCO's catalog holds two separately licensed bodies of survey, so one harvest feeds two sources.
`--doris` selects the DORIS grids — Crown Copyright, no <licence> element, reuse granted in the
body of <metcom> — and writes ../uk_cco_doris/ instead. Everything else is identical:

  python harvest.py <work_dir> --doris --repack   # build sources/uk_cco_doris from the same zips
  rclone copy <work_dir>/mirror_doris r2:data/bathymetry/mirror/uk_cco_doris --include '*.zip'

The site's own AJAX contract, mirrored exactly (see its sendXMLHttpRequest / objectifyForm):

  session:  GET  /cco/                          → CCOMetadataSearch cookie + the live filter
                                                  schema in `var js_filter_results`
  POST /cco/test.php, body `data=<json>&request=<verb>&target=<t>`, the JSON concatenated RAW
  (form-encoding it double-escapes and the server 500s):
    add_area          data = a WKT polygon string, EPSG:27700; calls accumulate
    filter            data = the filter schema with the wanted format checked → results HTML
                      for the WHOLE selection (paginated client-side), each row carrying
                      data-itemnum, filename, data-size and a view_metadata id
    add_multi_basket  data = [itemnum, …]
    download          data = the download form as {name: encodeURIComponent(value)}
  The download reply carries the tokenised URL directly, so no email round-trip: poll it until
  the status offers a `Download Now` form, then echo that form back to get the zip.

Zips arrive as data/hydrographic/<name>.asc + metadata/hydrographic/<name>.asc.xml (FGDC, the
licensing authority per item). Grids are EPSG:27700, ODN, seafloor negative, nodata -9999.

POLITENESS: strictly serial, paced, and resumable — one catalog request covers the country, and
an order that is already downloaded is never re-requested. The site caps a basket at 10 GiB
of declared (uncompressed) size, so orders are batched well under it.

Stdlib only, no pipeline coupling.
"""

import collections
import html
import http.client
import http.cookiejar
import json
import os
import re
import shutil
import sys
import time
import typing
import urllib.error
import urllib.parse
import urllib.request
import zipfile

BASE = "https://coastalmonitoring.org/cco/"
TEST = BASE + "test.php"
UA = "seascape-harvest/1.0 (openwaters.io; brandon@opensoul.org)"

# The whole British National Grid — one polygon returns the entire catalog in one request,
# which is both simpler and politer than sweeping the coast in cells.
NATIONAL = (0, 0, 700000, 1300000)
SUBTYPE, FORMAT = "multibeam", "4"  # Bathymetric → Multibeam → ESRI ASCII grid

# Declared (uncompressed) bytes per order. The site refuses a basket over BASKET_CAP; staying
# well under it keeps each server-side compile short and each failure cheap to retry.
ORDER_BYTES = 4_000_000_000
BASKET_CAP = 10 * 1024 ** 3  # the site's own limit, in declared (uncompressed) bytes
PACE_S = 2       # between catalog/basket calls
POLL_S = 30      # between checks on a compiling order
POLL_MAX = 240   # give up on an order after ~2 h of compiling
TRANSFER_TRIES = 4   # a reset mid-body costs the transfer, not the order
RETRY_S = 60

FORM = {
    "download_email": "brandon@opensoul.org",
    "download_data_use": "8",   # Other
    "download_funding": "6",    # Other
    "download_comment": ("Open-source, non-navigational depth mapping (openwaters.io) — merging "
                         "openly licensed bathymetry with attribution; derived products marked "
                         "'Not to be used for navigational purposes'."),
    "agree_ogl": "on",
    "website": "",              # honeypot: must be present and empty
    "backto": "/cco/",
    "submit": "Send",
}

# <OS 1 km square><optional variant letter>_<survey date>[_add_]mb.asc
NAME = re.compile(r"([A-Z]{2}\d{4})([A-Z]?)_(\d{8})(?:_ADD_)?MB\.ASC$")
ROW = re.compile(
    r"<tr><td>(?P<date>[\d/]+)</td><td>[^<]*</td><td>(?P<detail>[^<]*)</td>"
    r'<td data-size="(?P<size>\d+)">[^<]*</td>'
    r'<td style="word-break: break-all;">(?P<name>[^<]*)</td>'
    r'<td><a href="view_metadata/\?id=(?P<meta>\d+)&type=hydrographic"[^>]*>View</a></td>'
    r"<td>(?P<fmt>[^<]*)</td>.*?data-itemnum=\"(?P<item>[^\"]+)\"", re.S)
TOKEN = re.compile(r"https?://[^\s\"'<>\\]*/download/index\.php\?dl=([0-9a-f]{32})", re.I)
ITEMNUM = re.compile(r'data-itemnum="([^"]+)"')
SIZE = re.compile(r'data-size="(\d+)"')
# The token URL is the requester's whole outstanding-order list, not one order, so the rows are
# read individually: each carries its id in the Delete form's clear_dl and, once compiled, a
# Download Now form. Those fields are visually hidden TEXT inputs, not type=hidden.
STATUS_TABLE = re.compile(r"<table[^>]*>(?:(?!</table>).)*Download Status(?:(?!</table>).)*</table>",
                          re.S)
STATUS_ROW = re.compile(r"<tr>(?:(?!</tr>).)*?<td(?:(?!</tr>).)*</tr>", re.S)
READY_FORM = re.compile(r"<form[^>]*>(?:(?!</form>).)*?Download Now(?:(?!</form>).)*</form>", re.S)
INPUT = re.compile(r"<input\b[^>]*>", re.I)
ATTR = re.compile(r"(\w+)\s*=\s*[\"']([^\"']*)[\"']")
DOWNLOAD_POST = "https://coastalmonitoring.org/download/index.php"
# Only a grant naming the National Archives licence lets us redistribute. Matching the words
# alone is not enough: some records say "Open Government Licence (CCO version)" with no URL and
# no version, which is CCO's own `conditions_nonOGL.txt` terms wearing the OGL's name.
OGL = re.compile(r"nationalarchives\.gov\.uk/doc/open-government-licence/version/(\d+)", re.I)
# DORIS (the Dorset Integrated Seabed survey) states no <licence> at all, and instead grants
# reuse of exactly what we mirror in the body of its <metcom>. Those grids redistribute under
# that sentence, not the OGL, so they are a source of their own.
DORIS_GRANT = re.compile(
    r"published on the website may be freely used for any application", re.I)
HEADER_BYTES = 400  # enough for the six AAIGrid header lines


class Blocked(RuntimeError):
    """The site returned server errors — back off and report rather than push through."""


def retrying(call, what):
    """Run one short request, riding out the resets and timeouts a long serial harvest meets.
    An HTTPError is the server answering and passes straight through — only a connection that
    never completed is worth repeating."""
    for attempt in range(1, TRANSFER_TRIES + 1):
        try:
            return call()
        except urllib.error.HTTPError:
            raise
        except (OSError, http.client.HTTPException) as ex:
            if attempt == TRANSFER_TRIES:
                raise Blocked(f"{what} failed {attempt}x: {ex!r}") from None
            print(f"  {what} failed ({ex!r}); retry {attempt}/{TRANSFER_TRIES - 1}")
            time.sleep(RETRY_S * attempt)


class Session:
    """One cookied CCO session. Every call is serial and paced."""

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.op.addheaders = [("User-Agent", UA)]
        page = self.get(BASE)
        m = re.search(r"var js_filter_results\s*=\s*new Object\((\{.*?\})\);", page, re.S)
        if not m:
            raise Blocked("no filter schema on /cco/ — the site's markup moved")
        self.schema = json.loads(m.group(1))

    def get(self, url):
        def call():
            with self.op.open(urllib.request.Request(url), timeout=600) as r:
                return r.read().decode("utf-8", "replace")

        return retrying(call, f"GET {url}")

    def post(self, url, fields):
        body = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/x-www-form-urlencoded", "Referer": BASE})
        with self.op.open(req, timeout=1800) as r:
            return r.read().decode("utf-8", "replace")

    def download(self, url, fields, dest):
        """Stream a POST response to `dest`. An order zip runs to hundreds of MB, and the
        server sends nothing at all until it has assembled the whole archive, so buffering it
        in memory would both waste it and hide whether the transfer is alive."""
        body = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/x-www-form-urlencoded", "Referer": BASE})
        tmp, n = dest + ".part", 0
        with self.op.open(req, timeout=3600) as r, open(tmp, "wb") as f:
            head = r.read(2)
            if head != b"PK":
                raise Blocked(f"download was not a zip ({head + r.read(120)!r})")
            f.write(head)
            n = 2
            while chunk := r.read(1 << 20):
                f.write(chunk)
                n += len(chunk)
        os.replace(tmp, dest)
        return n

    def ajax(self, data, verb, target="results"):
        # RAW concatenation, matching the site's own sendXMLHttpRequest — urlencode()ing the
        # JSON double-escapes it and test.php answers 500.
        body = f"data={json.dumps(data)}&request={verb}&target={target}".encode()
        req = urllib.request.Request(TEST, data=body, headers={
            "Content-Type": "application/x-www-form-urlencoded", "Referer": BASE,
            "X-Requested-With": "XMLHttpRequest"})
        time.sleep(PACE_S)

        def call():
            with self.op.open(req, timeout=900) as r:
                return json.loads(r.read().decode("utf-8", "replace"))

        try:
            return retrying(call, verb)
        except urllib.error.HTTPError as ex:
            raise Blocked(f"{verb} → HTTP {ex.code}") from None

    def filter_html(self):
        f = json.loads(json.dumps(self.schema))
        f["types"]["hydrographic"]["subtypes"][SUBTYPE]["formats"][FORMAT]["checked"] = True
        return self.ajax(f, "filter")

    def select_area(self, box=NATIONAL):
        w, s, e, n = box
        self.ajax(f"POLYGON(({w} {s},{e} {s},{e} {n},{w} {n},{w} {s}))", "add_area")


def parse_rows(page):
    rows = []
    for m in ROW.finditer(page):
        r = m.groupdict()
        g = NAME.match(r["name"].upper())
        if not g:
            sys.exit(f"unrecognised CCO filename {r['name']!r} — extend NAME before harvesting")
        rows.append({"name": r["name"], "item": r["item"], "meta": r["meta"],
                     "size": int(r["size"]), "date": r["date"], "detail": r["detail"],
                     "sq": g.group(1), "survey": g.group(3)})
    return rows


def select(rows):
    """One grid per OS 1 km square: the newest survey, and among same-date variants the
    largest. CCO registers many a filename under several survey records, and re-surveys
    squares over the years; either would stack overlapping rasters inside one mosaic."""
    unique = {}
    for r in sorted(rows, key=lambda r: int(r["meta"])):
        unique.setdefault(r["name"].upper(), r)
    best = {}
    for r in unique.values():
        rank = (r["survey"], r["size"], -int(r["meta"]))
        if r["sq"] not in best or rank > best[r["sq"]][0]:
            best[r["sq"]] = (rank, r)
    return sorted((v[1] for v in best.values()), key=lambda r: r["name"].upper())


def tile5k(sq):
    """The OS 5 km square containing a 1 km square — the mirror's repack unit."""
    return f"{sq[:2]}{int(sq[2:4]) // 5 * 5:02d}{int(sq[4:6]) // 5 * 5:02d}"


def plan(kept, limit=ORDER_BYTES):
    """Group by 5 km square, then batch whole groups into orders. A group is never split
    across orders, so every tile zip can be repacked as soon as its order lands."""
    groups = collections.defaultdict(list)
    for r in kept:
        groups[tile5k(r["sq"])].append(r)
    orders, cur, curb = [], [], 0
    for tile in sorted(groups):
        b = sum(r["size"] for r in groups[tile])
        if cur and curb + b > limit:
            orders.append(cur)
            cur, curb = [], 0
        cur.append(tile)
        curb += b
    if cur:
        orders.append(cur)
    return groups, orders


def place(session, items):
    """Basket the items and submit the download request → the requester's download-list URL.

    The basket lives in the session, so a fresh Session starts empty — but it is emptied anyway
    and then read back before submitting: what gets compiled is whatever the basket holds at
    submit time, and a basket carrying a previous order's items would silently double an order
    with nothing in the reply to show for it."""
    session.ajax("1", "empty_basket", target="basket")
    session.select_area()
    session.filter_html()
    session.ajax([r["item"] for r in items], "add_multi_basket", target="basket")
    if sum(r["size"] for r in items) > BASKET_CAP:
        raise Blocked(f"order exceeds the site's {BASKET_CAP} B basket cap")
    verify_basket(session.ajax(None, "display_basket", target="basket"), items)
    reply = session.ajax({k: urllib.parse.quote(v, safe="") for k, v in FORM.items()},
                         "download", target="download")
    m = TOKEN.search(reply if isinstance(reply, str) else json.dumps(reply))
    if not m:
        raise Blocked(f"no download token in the reply: {str(reply)[:400]}")
    return f"https://coastalmonitoring.org/download/index.php?dl={m.group(1)}"


def verify_basket(page, items):
    """Assert the basket holds this order and nothing else, by its own rendering: one row per
    item, and the declared bytes the site sums for its cap."""
    got = {i for i in ITEMNUM.findall(page)}
    want = {r["item"] for r in items}
    if got != want:
        raise Blocked(f"basket holds {len(got)} item(s), ordered {len(want)} "
                      f"({len(got - want)} extra, {len(want - got)} missing) — the basket did "
                      "not start empty, or the site dropped items")
    size = sum(int(x) for x in SIZE.findall(page))
    if size != sum(r["size"] for r in items):
        raise Blocked(f"basket declares {size} byte(s), ordered "
                      f"{sum(r['size'] for r in items)}")


def inputs(fragment):
    fields = {}
    for tag in INPUT.findall(fragment):
        a = dict(ATTR.findall(tag))
        if a.get("name"):
            fields[a["name"]] = a.get("value", "")
    return fields


def orders_pending(page):
    """One (id, Download Now fields) pair per outstanding order row. Both are absent while the
    server is mid-compile: that row shows only the word `Processing`, with neither the delete
    form that carries the id nor the download form that carries the fields."""
    table = STATUS_TABLE.search(page)
    rows = []
    for row in STATUS_ROW.findall(table.group(0) if table else ""):
        form = READY_FORM.search(row)
        fields = inputs(form.group(0)) if form else None
        rows.append((inputs(row).get("clear_dl") or (fields or {}).get("dl_id"), fields))
    return rows


def await_ready(session, token_url, before):
    """Block until THIS order's row offers its zip, and return that row's form fields. Keying
    on the id matters: an earlier order left unfetched would otherwise be downloaded in its
    place, silently repacking the wrong grids."""
    for _ in range(POLL_MAX):
        rows = orders_pending(session.get(token_url))
        ready = [f for oid, f in rows if f and oid not in before]
        if len(ready) > 1:
            raise Blocked(f"{len(ready)} unexpected orders ready at once — clear the queue")
        if ready:
            return ready[0]
        if not [r for r in rows if r[0] not in before]:
            raise Blocked(f"the order vanished from {token_url}")
        time.sleep(POLL_S)
    raise Blocked(f"order never compiled: {token_url}")


def collect(token_url, dest, before=()):
    """Wait for this order to compile, then pull its zip — retrying the transfer itself. A
    multi-hundred-MB download over a server that streams slowly gets its connection reset from
    time to time, and an order abandoned mid-transfer stays queued, so the retry re-reads the
    row and starts the body again rather than losing the whole order to one reset.

    The session is built here, not passed in: opening it is itself a request that can time out,
    and a caller's `Session()` argument would be evaluated outside this retry."""
    for attempt in range(1, TRANSFER_TRIES + 1):
        session = Session()
        fields = await_ready(session, token_url, before)
        try:
            return session.download(DOWNLOAD_POST, fields, dest)
        except (OSError, http.client.HTTPException) as ex:
            if attempt == TRANSFER_TRIES:
                raise Blocked(f"transfer failed {attempt}x: {ex!r}") from None
            print(f"  transfer failed ({ex!r}); retry {attempt}/{TRANSFER_TRIES - 1}")
            time.sleep(RETRY_S * attempt)


def field(xml, tag):
    """One FGDC element's text, whitespace-flattened. The CCO writer double-escapes markup
    (`andquot;`, `&#039;`), so unescape twice and strip any surviving tags."""
    m = re.search(f"<{tag}>(.*?)</{tag}>", xml, re.S | re.I)
    if not m:
        return ""
    text = html.unescape(html.unescape(m.group(1)))
    text = text.replace("andquot;", '"').replace("andamp;", "&")
    return " ".join(re.sub(r"<[^>]+>", " ", text).split())


def licensing(xml):
    """Per-item licensing as CCO states it: `<licence>` is the grant (OGL v3 for the national
    programme), `<metcom>` the navigational-use condition every derived product must carry, and
    `<ackn>` the attribution the licence requires by name. `<useconst>` is only a disclaimer URL."""
    return {k: field(xml, k) for k in ("licence", "metcom", "ackn", "copyrght", "depthdn")}


class Edition(typing.NamedTuple):
    """Which grids a mirror carries, and where it lands. CCO's catalog holds two separately
    licensed bodies of survey in one product, so each is mirrored as its own source: what a
    grid grants is a property of its FGDC record, not of the download it arrived in."""
    source: str
    keep: typing.Callable      # the record's grant, as manifest fields, or None to refuse
    refusal: str
    mirror: str
    manifest: str


def ogl_grant(entry):
    m = OGL.search(entry.get("licence", ""))
    return {"ogl_version": m.group(1)} if m else None


def doris_grant(entry):
    """A DORIS grid: no <licence> element, and the reuse grant stated in <metcom> instead. A
    record carrying BOTH is not DORIS-licensed — 33 say "Open Government Licence (CCO version)",
    which is CCO's bespoke terms, and they belong to neither mirror."""
    if entry.get("licence") or not DORIS_GRANT.search(entry.get("metcom") or ""):
        return None
    return {"grant": "metcom"}


OGL_EDITION = Edition("uk_cco", ogl_grant, "no Open Government Licence grant",
                      "mirror", "manifest.json")
DORIS_EDITION = Edition("uk_cco_doris", doris_grant, "not a DORIS-granted grid",
                        "mirror_doris", "manifest_doris.json")


def repack(order_zip, groups, tiles, mirror_dir, manifest, dropped, edition=OGL_EDITION):
    """Explode one order zip into the mirror's assets: one zip per (OS 5 km square, resolution)
    of .asc grids + their FGDC metadata. A zip is exactly what the pipeline's `asc-tile` unpack
    wants — it mosaics each asset's .asc members into one GeoTIFF — so an asset becomes a raster.

    Splitting a square's resolutions apart is what keeps the mosaic honest. One VRT has ONE
    resolution, so 1 m and 2 m grids mosaicked together would be resampled to the average of the
    two, and no pixel would sit where it was measured. Split, each raster keeps its native grid;
    they overlap only where two surveys of different resolution cover the same seabed, and the
    aggregation builds at the finer one either way.

    A grid whose FGDC record carries no Open Government Licence grant never reaches the mirror
    at all — an absent grant is not a grant, so it is not ours to redistribute."""
    want = {r["name"].upper(): (tile, r) for tile in tiles for r in groups[tile]}
    found, seen = collections.defaultdict(list), set()
    with zipfile.ZipFile(order_zip) as z:
        names = z.namelist()
        meta = {os.path.basename(n).upper(): n for n in names
                if "/metadata/" in n.lower() and n.lower().endswith(".xml")}
        for n in names:
            base = os.path.basename(n).upper()
            if not base.endswith(".ASC") or base not in want or base in seen:
                continue
            seen.add(base)
            tile, r = want[base]
            found[tile].append((n, r, meta.get(base + ".XML")))
        os.makedirs(mirror_dir, exist_ok=True)
        written = []
        for tile, entries in sorted(found.items()):
            assets = collections.defaultdict(list)
            for member, r, mx in sorted(entries):
                xml = z.read(mx).decode("utf-8", "replace") if mx else ""
                with z.open(member) as f:  # header only — the grid itself can be hundreds of MB
                    cs = cellsize(f.read(HEADER_BYTES))
                e = {"tile": tile, "name": os.path.basename(member), "itemnum": r["item"],
                     "metadata_id": r["meta"], "size": r["size"], "survey": r["survey"],
                     "cellsize": cs, "_member": member, "_xml": mx, **licensing(xml)}
                granted = edition.keep(e)
                if granted is None:
                    dropped.append({**public(e), "reason": edition.refusal})
                elif cs is None:
                    dropped.append({**public(e), "reason": "no cellsize in the AAIGrid header"})
                else:
                    e.update(granted)
                    assets[res_class(cs)].append(e)
            for res, keep in sorted(assets.items()):
                asset = f"{tile}_{res:g}m"
                out = f"{mirror_dir}/{asset}.zip"
                tmp = out + ".part"
                with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as w:
                    for e in keep:
                        w.writestr(e["name"], z.read(e["_member"]))
                        if e["_xml"]:
                            w.writestr("metadata/" + os.path.basename(e["_xml"]),
                                       z.read(e["_xml"]))
                        manifest.append({**public(e), "asset": asset, "resolution": res})
                os.replace(tmp, out)
                written.append(asset)
    return written, sorted(set(want) - seen)


def public(entry):
    """A manifest row — the entry without the archive-member bookkeeping."""
    return {k: v for k, v in entry.items() if not k.startswith("_")}


def cellsize(head):
    """The AAIGrid header's cell size — CCO mixes 0.5/1/2 m grids across surveys."""
    m = re.search(rb"cellsize\s+([\d.]+)", head, re.I)
    return float(m.group(1)) if m else None


def res_class(cs):
    """The resolution an asset is named for. CCO's headers carry the cell size a survey's
    gridding actually landed on (1.9866724138, 1.999936, 2.0 …), so an exact comparison would
    split one 2 m survey into several assets and rank the rounding noise as `finer`. Two
    significant figures is the coarsest rounding that still separates 0.5 from 1 from 2."""
    return float(f"{cs:.2g}")


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save(path, obj):
    with open(path + ".part", "w") as f:
        json.dump(obj, f)
    os.replace(path + ".part", path)


FILE_LIST_HEADS = {
    "uk_cco": (
        "# CCO Multibeam Bathymetry (Channel Coastal Observatory / National Network of Regional\n"
        "# Coastal Monitoring Programmes, England & Wales) — OGL v2.0/v3.0, NOT for navigation.\n"),
    "uk_cco_doris": (
        "# DORIS — DORset Integrated Seabed survey (Channel Coastal Observatory), Crown Copyright.\n"
        "# Its FGDC records state no licence and grant reuse in the body of <metcom> instead, so it\n"
        "# mirrors as its own source rather than under uk_cco's OGL. NOT for navigation.\n"
        "# Regenerated by `sources/uk_cco/harvest.py <work_dir> --doris` — one harvest, two sources.\n"),
}


def write_file_list(assets, path, edition=OGL_EDITION):
    header = FILE_LIST_HEADS[edition.source] + (
        f"# {len(assets)} zips, one per OS 5 km square per resolution (`<square>_<cellsize>m.zip`),\n"
        f"# each holding the newest surveyed ESRI ASCII grid of every OS 1 km square it covers.\n"
        f"# These are our R2 MIRROR: CCO ships data only through a session-cookied basket and a\n"
        f"# tokenised, server-compiled download, so sources/uk_cco/harvest.py harvests and repacks\n"
        f"# once and this file_list then fetches the mirror over public HTTPS, no session, fully\n"
        f"# CI-reproducible. Refresh = re-harvest + re-sync when CCO publishes new surveys.\n")
    with open(path, "w") as f:
        f.write(header)
        f.writelines(
            f"https://data.openwaters.io/bathymetry/mirror/{edition.source}/{a}.zip\n"
            for a in assets)


def harvest(work, redo_repack=False, edition=OGL_EDITION):
    os.makedirs(work, exist_ok=True)
    cat_path, state_path = f"{work}/catalog.json", f"{work}/state.json"
    man_path = f"{work}/{edition.manifest}"
    mirror_dir = f"{work}/{edition.mirror}"
    drop_path = f"{work}/dropped_{edition.source}.json"
    rows = load(cat_path, None)
    if rows is None:
        s = Session()
        s.select_area()
        rows = parse_rows(s.filter_html())
        if not rows:
            sys.exit("catalog came back empty — the filter ids or markup moved; check the site.")
        save(cat_path, rows)
    print(f"catalog: {len(rows)} items, {sum(r['size'] for r in rows) / 1e9:.1f} GB declared")
    kept = select(rows)
    groups, orders = plan(kept)
    print(f"selected {len(kept)} grids over {len(groups)} OS 5 km tiles, "
          f"{sum(r['size'] for r in kept) / 1e9:.1f} GB declared → {len(orders)} order(s)")

    state = load(state_path, {"token": None, "orders": {}})
    manifest, dropped = load(man_path, []), load(drop_path, [])
    if redo_repack:  # a repack-policy change costs nothing: the order zips are still here
        manifest, dropped = [], []
        shutil.rmtree(mirror_dir, ignore_errors=True)
        for i, st in state["orders"].items():
            # Only an order whose zip was kept can be repacked; one still to be fetched stays
            # exactly as it was, so a repack never turns into a re-download.
            if os.path.exists(f"{work}/orders/order_{int(i):03d}.zip"):
                st.pop("repacked", None)
    done_assets = {e["asset"] for e in manifest}
    for i, tiles in enumerate(orders):
        st = state["orders"].setdefault(str(i), {})
        zip_path = f"{work}/orders/order_{i:03d}.zip"
        os.makedirs(f"{work}/orders", exist_ok=True)
        items = [r for t in tiles for r in groups[t]]
        decl = sum(r["size"] for r in items)
        if st.get("repacked"):
            continue
        print(f"order {i}: {len(tiles)} tile(s), {len(items)} grid(s), {decl / 1e9:.2f} GB declared")
        if not os.path.exists(zip_path):
            if not st.get("token"):
                s = Session()
                # Everything already queued under this email belongs to an earlier order.
                st["before"] = (sorted(o for o, _ in orders_pending(s.get(state["token"])) if o)
                                if state.get("token") else [])
                st["token"] = state["token"] = place(s, items)
                save(state_path, state)
                print(f"  requested → {st['token']}")
            n = collect(st["token"], zip_path, st.get("before", []))
            st["zip_bytes"] = n
            save(state_path, state)
            print(f"  downloaded {n / 1e6:.0f} MB")
        got, missing = repack(zip_path, groups, tiles, mirror_dir, manifest, dropped,
                              edition)
        st["repacked"] = True
        st["missing"] = missing
        save(man_path, manifest)
        save(drop_path, dropped)
        save(state_path, state)
        done_assets.update(got)
        if missing:
            print(f"  WARNING: {len(missing)} requested grid(s) absent from the zip")
        # The order zip stays: it is the only copy of the raw download, and a change to what
        # repack keeps would otherwise mean re-requesting every order from the server.
    assets = sorted(done_assets)
    print(f"{edition.source}: {len(assets)} asset(s), {len(manifest)} grid(s) in {mirror_dir}")
    for text, n in collections.Counter(
            e.get("licence") or "(none declared)" for e in manifest).most_common():
        print(f"  licence {n:6d}  {text[:110]}")
    for text, n in collections.Counter(e["reason"] for e in dropped).most_common():
        print(f"  dropped {n:6d}  {text}")
    for res, n in sorted(collections.Counter(e["resolution"] for e in manifest).items()):
        print(f"  {res:g} m {n:6d} grid(s)")
    mixed = collections.defaultdict(set)
    for e in manifest:
        mixed[e["tile"]].add(e["resolution"])
    multi = {t: sorted(r) for t, r in mixed.items() if len(r) > 1}
    print(f"  {len(multi)} square(s) carry more than one resolution")
    return assets


def _check():
    rows = [
        # the same filename registered under three survey records, plus older surveys and a
        # same-date variant grid of the same square
        {"name": "TG5016_20250912mb.asc", "item": "1-4", "meta": "3", "size": 10,
         "date": "12/09/2025", "detail": "Multibeam", "sq": "TG5016", "survey": "20250912"},
        {"name": "tg5016_20250912mb.asc", "item": "2-4", "meta": "1", "size": 10,
         "date": "12/09/2025", "detail": "Multibeam", "sq": "TG5016", "survey": "20250912"},
        {"name": "TG5016_20200101mb.asc", "item": "3-4", "meta": "2", "size": 99,
         "date": "01/01/2020", "detail": "Multibeam", "sq": "TG5016", "survey": "20200101"},
        {"name": "SM8941d_20200920mb.asc", "item": "4-4", "meta": "4", "size": 5,
         "date": "20/09/2020", "detail": "Swath bathymetry", "sq": "SM8941", "survey": "20200920"},
        {"name": "SM8941_20200920mb.asc", "item": "5-4", "meta": "5", "size": 7,
         "date": "20/09/2020", "detail": "Swath bathymetry", "sq": "SM8941", "survey": "20200920"},
    ]
    k = select(rows)
    assert [r["item"] for r in k] == ["5-4", "2-4"], k  # newest wins; ties go to the larger grid
    assert tile5k("SZ0479") == "SZ0075" and tile5k("TF4590") == "TF4590"
    assert tile5k("NZ5934") == "NZ5530"

    # every filename shape the live catalog carries must parse
    for n, sq, dt in (("SZ0479_20250731mb.asc", "SZ0479", "20250731"),
                      ("tm4974_20250929mb.asc", "TM4974", "20250929"),
                      ("SM8941d_20200920mb.asc", "SM8941", "20200920"),
                      ("tf5086_20170828_add_mb.asc", "TF5086", "20170828")):
        g = NAME.match(n.upper())
        assert g and g.group(1) == sq and g.group(3) == dt, n

    html = ('<tr><td>31/07/2025</td><td>Bathymetric</td><td>Multibeam</td>'
            '<td data-size="18021920">17&nbsp;MB</td>'
            '<td style="word-break: break-all;">SZ0479_20250731mb.asc</td>'
            '<td><a href="view_metadata/?id=694346&type=hydrographic" target="_blank">View</a>'
            '</td><td>ESRI ASCII grid raster format</td><td class="center">'
            '<input id="694346-4_chk_results" type="checkbox" data-itemnum="694346-4" '
            'class="chk 694346-4" data-type="hydrographic" value=""></td></tr>')
    r = parse_rows(html)
    assert len(r) == 1 and r[0]["item"] == "694346-4" and r[0]["meta"] == "694346", r
    assert r[0]["size"] == 18021920 and r[0]["sq"] == "SZ0479"

    # whole groups only, never split across orders
    kept = [{"sq": "AA0000", "size": 6, "name": "a"}, {"sq": "AA0001", "size": 6, "name": "b"},
            {"sq": "AA1000", "size": 5, "name": "c"}]
    g, o = plan(kept, limit=10)
    assert sorted(g) == ["AA0000", "AA1000"] and o == [["AA0000"], ["AA1000"]], (g, o)
    assert plan(kept, limit=100)[1] == [["AA0000", "AA1000"]]

    # The page carries unrelated login/register forms whose inputs must not leak in, and the
    # ready form's own fields are display:none TEXT inputs, not type=hidden.
    login = ('<form action="/x"><input type="text" name="login_email" value="a"></form>')
    head = ('<table class="table table-striped"><tr><th>Download Requested</th>'
            '<th>Downloaded</th><th>Total Size</th><th>Download Status</th><th></th></tr>')
    queued = ('<tr><td>2026-08-15 03:10</td><td></td><td>272 MB</td><td>Queued</td><td>'
              '<form><input type="hidden" name="clear_dl" value="23179"></form></td></tr>')
    # mid-compile: neither the delete form's id nor a download form
    processing = '<tr><td>2026-08-15 03:20</td><td></td><td>9 MB</td><td>Processing</td><td></td></tr>'
    done = ('<tr><td>2026-08-15 03:05</td><td></td><td>46 KB</td><td>'
            '<form action="index.php" method="post">'
            '<input type="submit" name="submit" value="Download Now">'
            '<input name="dl_id" style="display:none" type="text" value="23178">'
            '<input name="dl_name" style="display:none" type="text" value="cco_data-1"></form>'
            '</td><td><form><input type="hidden" name="clear_dl" value="23178">'
            '</form></td></tr>')
    p = orders_pending(login + head + done + queued + processing + "</table>")
    assert [r[0] for r in p] == ["23178", "23179", None], p  # the header row is not an order
    assert p[0][1] == {"submit": "Download Now", "dl_id": "23178",
                       "dl_name": "cco_data-1"}, p
    assert p[1][1] is None and p[2][1] is None, p
    assert orders_pending(login) == []
    assert TOKEN.search("go to http://coastalmonitoring.org/download/index.php?dl="
                        + "a" * 32).group(1) == "a" * 32
    # display_basket renders one row per basketed item, carrying its itemnum and declared size;
    # the total the site checks against its cap is the sum of those.
    def basket(*rows):
        return "".join(f'<tr><td data-size="{s}">x</td>'
                       f'<td><input data-itemnum="{i}"></td></tr>' for i, s in rows)

    order = [{"item": "1-4", "size": 10}, {"item": "2-4", "size": 7}]
    verify_basket(basket(("1-4", 10), ("2-4", 7)), order)
    for bad, why in ((basket(("1-4", 10), ("2-4", 7), ("9-4", 3)), "a leftover item"),
                     (basket(("1-4", 10)), "a dropped item"),
                     (basket(("1-4", 10), ("2-4", 8)), "a size that disagrees")):
        try:
            verify_basket(bad, order)
            assert False, f"{why} must not pass"
        except Blocked:
            pass

    # "Open Government Licence (CCO version)" names the OGL but grants CCO's own terms
    assert not OGL.search("Open Government Licence (CCO version)")
    assert OGL.search("supplied under the Open Government Licence. The terms of the Licence can "
                      "be found at: https://www.nationalarchives.gov.uk/doc/"
                      "open-government-licence/version/2/.").group(1) == "2"
    # the licence lives in <licence>, not <useconst> (a bare disclaimer URL), and the CCO
    # writer double-escapes its own markup
    lic = licensing("<useconst>http://x/disclaimer.html</useconst>"
                    "<licence>Supplied under the Open\n Government Licence.</licence>"
                    "<metcom>marked with andquot;Not to be used for navigational "
                    "purposesandquot;</metcom><ackn>Courtesy of St Mary&amp;#039;s</ackn>"
                    "<depthdn>Ordnance Datum Newlyn</depthdn>")
    assert lic["licence"] == "Supplied under the Open Government Licence.", lic
    assert lic["metcom"].endswith('"Not to be used for navigational purposes"'), lic
    assert lic["ackn"] == "Courtesy of St Mary's" and lic["depthdn"] == "Ordnance Datum Newlyn"
    assert cellsize(b"ncols 32\nnrows 500\nxllcenter 320000.0\nyllcenter 1.0\n"
                    b"cellsize 2.0000000000\nnodata_value -9999.0\n") == 2.0
    assert cellsize(b"no header here") is None

    # A survey's gridding lands on a cell size near, not on, its nominal resolution, so the
    # rounding noise must not read as a separate (or finer) resolution.
    assert res_class(1.9866724138) == res_class(1.999936) == res_class(2.0) == 2.0
    assert res_class(0.5) == 0.5 and res_class(1.0) == 1.0 and res_class(0.25) == 0.25

    # repack against a synthetic order zip: a square surveyed at both 1 m and 2 m splits into
    # one asset per resolution, and a grid with no OGL grant never reaches the mirror
    import tempfile

    def grid(res):
        return (f"ncols 4\nnrows 4\nxllcenter 0\nyllcenter 0\ncellsize {res}\n"
                "nodata_value -9999\n" + "-1 -2 -3 -4\n" * 4).encode()

    def fgdc(licence):
        return f"<metadata><licence>{licence}</licence></metadata>".encode()

    ogl = ("Supplied under the Open Government Licence, https://www.nationalarchives.gov.uk"
           "/doc/open-government-licence/version/3/.")
    names = {"SZ0075_20200101mb.asc": (1.0, ogl),
             "SZ0176_20200101mb.asc": (0.9999812, ogl),  # the same 1 m survey, gridded
             "SZ0277_20100101mb.asc": (2.0, ogl),
             "SZ0378_20200101mb.asc": (1.0, "Open Government Licence (CCO version)")}
    with tempfile.TemporaryDirectory() as tmp:
        order = f"{tmp}/order.zip"
        with zipfile.ZipFile(order, "w") as w:
            for n, (res, lic) in names.items():
                w.writestr(f"cco_data-1/data/hydrographic/{n}", grid(res))
                w.writestr(f"cco_data-1/metadata/hydrographic/{n}.xml", fgdc(lic))
        rs = [{"name": n, "item": f"{i}-4", "meta": str(i), "size": 1,
               "survey": n[7:15], "sq": n[:6].upper()} for i, n in enumerate(names)]
        man, drop = [], []
        got, missing = repack(order, {"SZ0075": rs}, ["SZ0075"], f"{tmp}/mirror", man, drop)
        assert got == ["SZ0075_1m", "SZ0075_2m"] and missing == [], (got, missing)
        assert {e["name"]: e["asset"] for e in man} == {
            "SZ0075_20200101mb.asc": "SZ0075_1m",
            "SZ0176_20200101mb.asc": "SZ0075_1m",   # rounding noise stays in the 1 m asset
            "SZ0277_20100101mb.asc": "SZ0075_2m"}, man
        assert [(e["name"], e["reason"]) for e in drop] == [
            ("SZ0378_20200101mb.asc", "no Open Government Licence grant")], drop
        assert {e["ogl_version"] for e in man} == {"3"}, man
        with zipfile.ZipFile(f"{tmp}/mirror/SZ0075_1m.zip") as z:
            assert sorted(z.namelist()) == [
                "SZ0075_20200101mb.asc", "SZ0176_20200101mb.asc",
                "metadata/SZ0075_20200101mb.asc.xml",
                "metadata/SZ0176_20200101mb.asc.xml"], z.namelist()
        # a square with nothing redistributable publishes no zip at all
        man, drop = [], []
        only_bad = [r for r in rs if r["name"] == "SZ0378_20200101mb.asc"]
        got, _ = repack(order, {"SZ0075": only_bad}, ["SZ0075"], f"{tmp}/mirror2", man, drop)
        assert got == [] and man == [] and len(drop) == 1, (got, man, drop)

    # The DORIS edition takes the grids the OGL one refuses, and only those: the grant lives in
    # <metcom>, and a record that also names a licence is not DORIS-licensed.
    grant = ("Hydrographic data must not be used for navigational purposes. Extracted grid "
             "survey data sets (e.g. 1m spacing) published on the website may be freely used "
             "for any application (except for products and services used in support of "
             "navigation)")
    assert doris_grant({"metcom": grant}) == {"grant": "metcom"}
    assert doris_grant({"licence": "Open Government Licence (CCO version)",
                        "metcom": grant}) is None, "a licensed record is not DORIS"
    assert doris_grant({"metcom": "must not be used for navigational purposes"}) is None
    assert ogl_grant({"metcom": grant, "licence": ""}) is None
    with tempfile.TemporaryDirectory() as tmp:
        order = f"{tmp}/order.zip"
        with zipfile.ZipFile(order, "w") as w:
            w.writestr("d/data/hydrographic/SY6080_20090101mb.asc", grid(1.0))
            w.writestr("d/metadata/hydrographic/SY6080_20090101mb.asc.xml",
                       f"<metadata><metcom>{grant}</metcom>"
                       f"<copyrght>Crown Copyright</copyrght></metadata>".encode())
            w.writestr("d/data/hydrographic/SY6081_20090101mb.asc", grid(1.0))
            w.writestr("d/metadata/hydrographic/SY6081_20090101mb.asc.xml", fgdc(ogl))
        rs = [{"name": n, "item": "1-4", "meta": "1", "size": 1, "survey": "20090101",
               "sq": n[:6]} for n in ("SY6080_20090101mb.asc", "SY6081_20090101mb.asc")]
        man, drop = [], []
        got, _ = repack(order, {"SY6080": rs}, ["SY6080"], f"{tmp}/m", man, drop, DORIS_EDITION)
        assert got == ["SY6080_1m"] and [e["name"] for e in man] == \
            ["SY6080_20090101mb.asc"], (got, man)
        assert [e["reason"] for e in drop] == ["not a DORIS-granted grid"], drop
        # the same order, read as the OGL edition, keeps exactly the other grid
        man, drop = [], []
        repack(order, {"SY6080": rs}, ["SY6080"], f"{tmp}/m2", man, drop, OGL_EDITION)
        assert [e["name"] for e in man] == ["SY6081_20090101mb.asc"], man
    print("harvest.py self-check ok")


def main():
    args = sys.argv[1:]
    if args[:1] == ["--check"]:
        return _check()
    if not args:
        sys.exit(__doc__.strip().splitlines()[0]
                 + "\n\nusage: harvest.py <work_dir> [--doris] [--file-list | --repack]")
    work = args[0]
    edition = DORIS_EDITION if "--doris" in args else OGL_EDITION
    if "--file-list" in args:
        manifest = load(f"{work}/{edition.manifest}", [])
        if not manifest:
            sys.exit(f"no {edition.manifest} in {work} — run the harvest first")
        assets = sorted({e["asset"] for e in manifest})
    else:
        assets = harvest(work, redo_repack="--repack" in args, edition=edition)
    # Each edition writes into its own source directory; they are siblings under sources/.
    path = f"{os.path.dirname(sys.path[0])}/{edition.source}/file_list.txt"
    write_file_list(assets, path, edition)
    print(f"wrote {len(assets)} asset URLs to {path}")


if __name__ == "__main__":
    main()
