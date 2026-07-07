#!/usr/bin/env python3
"""Regenerate file_list.txt for the AusSeabed source (NOT part of the build).

The build itself just fetches the URLs in file_list.txt. Re-run this when
Geoscience Australia publishes new surveys:

  python harvest.py            # queries the register, verifies zips, rewrites file_list.txt
  python harvest.py --check    # offline self-check

The AusSeabed Marine Data Register is a public GeoServer WFS
(warehouse.ausseabed.gov.au). Its ACQUISITIONS_INDEX layer catalogs every known
Australian survey (~3,300), most of which are register-only metadata: restricted
licences, third-party portals, or no download at all. The buildable subset is
PRODUCT_STATUS=PUBLISHED + CC-BY 4.0 + no embargo, whose DATA_URLs are zip
archives of Float32 COGs on files.ausseabed.gov.au (anonymous CloudFront/S3).
Some records instead emit s3://seabed-producthouse-open URLs, which deny
anonymous access — the same basenames resolve on the files host, so we map them.
The COMPILATIONS_INDEX layer (SDB and regional compilations) is deliberately
excluded: SDB is too noisy for a chart (see the tabled Allen Coral Atlas), and
the useful compilations (gbr30, AusBathyTopo) are already their own sources.

Every kept zip's central directory is range-read to confirm it's fetchable and
to catch tif basename collisions — source_unzip flattens members by basename,
so a collision across zips would silently overwrite one survey with another.
Stdlib only, no pipeline coupling.
"""

import csv
import io
import json
import struct
import sys
import urllib.parse
import urllib.request

WFS = "https://warehouse.ausseabed.gov.au/geoserver/ows"
LAYER = "ausseabed:MARINEDATAREGISTER_ACQUISITIONS_INDEX"
FIELDS = ["NAME", "NEWGAID", "BATHY_TYPES", "DATA_TYPES", "DATA_URL", "META_URL",
          "LEGAL_CONSTRAINTS", "EMBARGO", "PRODUCT_STATUS", "AREA_KM2"]
FILES_HOST = "files.ausseabed.gov.au"
CC_BY = "Creative Commons - Attribution 4.0 International"


def fetch_index():
    q = urllib.parse.urlencode({
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typeNames": LAYER, "outputFormat": "csv",
        "propertyName": ",".join(FIELDS),  # skip GEOM — footprints are ~100 MB
    })
    with urllib.request.urlopen(f"{WFS}?{q}", timeout=120) as r:
        return list(csv.DictReader(io.TextIOWrapper(r, encoding="utf-8")))


def map_url(url):
    """A record's DATA_URL resolved to the anonymous files host, or None if the
    data isn't GA-hosted (state portals, Azure blobs, 'N/A', …)."""
    host = urllib.parse.urlparse(url).netloc
    if host == FILES_HOST:
        return url
    if "seabed-producthouse-open" in host:  # 403 anonymous; same basename on the files host
        return f"https://{FILES_HOST}/survey/{url.rsplit('/', 1)[-1]}"
    return None


def select(rows):
    kept, dropped = {}, {}
    for r in rows:
        reason = None
        if r["PRODUCT_STATUS"] != "PUBLISHED":
            reason = f"status {r['PRODUCT_STATUS'] or '?'}"
        elif r["LEGAL_CONSTRAINTS"] != CC_BY:
            reason = f"licence {r['LEGAL_CONSTRAINTS'] or '?'}"
        elif r["EMBARGO"] != "No":
            reason = "embargoed"
        elif "satellite" in r["BATHY_TYPES"].lower():  # SDB — see the tabled ACA source
            reason = "satellite-derived"
        elif "bathymetry" not in r["DATA_TYPES"].lower() or \
                "backscatter" in r["DATA_URL"].rsplit("/", 1)[-1].lower():
            # backscatter/sidescan products; some re-bundle their sibling's bathy tif,
            # which would collide with the real bathymetry zip at unzip time
            reason = f"not a bathymetry product ({r['DATA_TYPES'] or '?'})"
        elif not map_url(r["DATA_URL"]):
            reason = f"not GA-hosted ({urllib.parse.urlparse(r['DATA_URL']).netloc or 'no url'})"
        if reason:
            dropped[reason] = dropped.get(reason, 0) + 1
        else:
            kept[map_url(r["DATA_URL"])] = r["NAME"]  # dedupe: some zips appear twice
    return kept, dropped


def zip_tif_members(url):
    """Data-tif member basenames from a remote zip's central directory (two range
    reads, no download). Raises urllib.error.HTTPError on 403/404."""
    def ranged(spec):
        req = urllib.request.Request(url, headers={"Range": f"bytes={spec}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()

    tail = ranged("-66000")  # EOCD + max comment
    eocd = tail.rfind(b"PK\x05\x06")
    cd_size, cd_off = struct.unpack("<II", tail[eocd + 12:eocd + 20])
    if 0xFFFFFFFF in (cd_size, cd_off):  # none today — zips top out ~2.8 GB
        sys.exit(f"{url} is ZIP64 — extend zip_tif_members before rebuilding the list")
    cd, names, p = ranged(f"{cd_off}-{cd_off + cd_size - 1}"), [], 0
    while cd[p:p + 4] == b"PK\x01\x02":
        nlen, elen, clen = struct.unpack("<HHH", cd[p + 28:p + 34])
        name = cd[p + 46:p + 46 + nlen].decode("utf-8", "replace")
        base = name.rsplit("/", 1)[-1]
        if base.lower().endswith((".tif", ".tiff")) and "_hs.tif" not in base.lower():
            # mirror source_unzip's dest_name: extracted as lowercase .tif
            names.append(base.rsplit(".", 1)[0] + ".tif")
        p += 46 + nlen + elen + clen
    # mirror the recipe's `source_unzip --prefer _cog.tif`: 21 zips ship each grid
    # twice (raw tif beside its _cog twin) — only the cogs get extracted
    cogs = [n for n in names if "_cog.tif" in n.lower()]
    return cogs or names


def verify(kept):
    ok, seen = [], {}
    for i, (url, name) in enumerate(sorted(kept.items()), 1):
        try:
            members = zip_tif_members(url)
        except Exception as ex:
            print(f"  DROP {url.rsplit('/', 1)[-1]}: {ex}")
            continue
        if not members:
            print(f"  DROP {url.rsplit('/', 1)[-1]}: no data tifs in zip")
            continue
        for m in members:
            if m in seen:
                sys.exit(f"tif basename collision: {m} in both {seen[m]} and {url} — "
                         "source_unzip would overwrite one; fix before building")
            seen[m] = url
        ok.append((url, name))
        if i % 25 == 0:
            print(f"  {i}/{len(kept)} zips verified")
    return ok, len(seen)


def write_file_list(entries, ntifs, path):
    with open(path, "w") as f:
        f.write(
            f"# AusSeabed per-survey L3 bathymetry (Geoscience Australia) — CC-BY 4.0.\n"
            f"# {len(entries)} survey zips ({ntifs} data tifs) on {FILES_HOST}, selected from the\n"
            f"# Marine Data Register WFS: PUBLISHED + CC-BY + no embargo + GA-hosted, SDB excluded.\n"
            f"# Generated by harvest.py — re-run it when GA publishes new surveys.\n")
        f.writelines(url + "\n" for url, _ in entries)


def _check():
    assert map_url("https://files.ausseabed.gov.au/survey/A-1m-2020.zip").endswith("A-1m-2020.zip")
    assert map_url("https://seabed-producthouse-open.s3.ap-southeast-2.amazonaws.com/x/L3/"
                   "bathymetry/downloads/B-2m-2020.zip") == \
        "https://files.ausseabed.gov.au/survey/B-2m-2020.zip"
    assert map_url("https://maps.slip.wa.gov.au/something") is None
    row = {"PRODUCT_STATUS": "PUBLISHED", "LEGAL_CONSTRAINTS": CC_BY, "EMBARGO": "No",
           "BATHY_TYPES": "Multibeam", "DATA_TYPES": "Bathymetry",
           "DATA_URL": "https://files.ausseabed.gov.au/survey/A.zip", "NAME": "A"}
    kept, dropped = select([row, {**row, "LEGAL_CONSTRAINTS": "Restricted"},
                            {**row, "BATHY_TYPES": "Satellite-derived Bathymetry (SDB)"},
                            {**row, "EMBARGO": "Yes"},
                            {**row, "DATA_TYPES": "Backscatter"},
                            {**row, "DATA_URL": "https://files.ausseabed.gov.au/survey/"
                                                "A-Backscatter-5m.zip"},
                            row])  # last row = duplicate URL
    assert len(kept) == 1 and sum(dropped.values()) == 5, (kept, dropped)
    # zip central-directory parse on a minimal in-memory zip served via data buffering
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("32Bit_Geotiff/a_cog.tif", b"x")
        z.writestr("32Bit_Geotiff/a_hs.tif", b"x")
        z.writestr("metadata/metadata.txt", b"x")
    raw = buf.getvalue()
    eocd = raw.rfind(b"PK\x05\x06")
    cd_size, cd_off = struct.unpack("<II", raw[eocd + 12:eocd + 20])
    assert raw[cd_off:cd_off + 4] == b"PK\x01\x02"  # same layout zip_tif_members walks
    print("harvest.py self-check ok")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        _check()
        sys.exit()
    rows = fetch_index()
    print(f"register: {len(rows)} records")
    kept, dropped = select(rows)
    for reason, n in sorted(dropped.items(), key=lambda kv: -kv[1]):
        print(f"  dropped {n}: {reason}")
    print(f"selected {len(kept)} unique zips; verifying central directories...")
    entries, ntifs = verify(kept)
    if not entries:
        sys.exit("nothing survived verification — has the register or files host moved?")
    path = f"{sys.path[0]}/file_list.txt"
    write_file_list(entries, ntifs, path)
    print(f"wrote {len(entries)} zip URLs ({ntifs} data tifs) to {path}")
