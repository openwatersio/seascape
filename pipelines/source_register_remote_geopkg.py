"""Register a streaming source from a tile-scheme *GeoPackage* index (e.g. NOAA S-102's
navigation tile scheme): a per-tile URL column — named by ``link_column`` in metadata
(``S102V30`` for S-102; defaults to ``GeoTIFF_Link``) — gives each tile's COG/HDF5;
features with a null link are dropped.

``file_list.txt`` holds either the tile-scheme prefix (``…/_CATALOG/`` for S-102 — the newest
dated ``.gpkg`` under it is resolved via one public S3 list) or a direct ``.gpkg`` URL. ``BBOX``
(W,S,E,N lon/lat) pushes an OGR spatial filter on the tile geometry — S-102 tile names aren't
lat/lon-encoded, so geometry is the only prefilter.

The gpkg already *is* the index, so ``bounds.csv`` is built straight from it — footprint
geometry → 3857 bounds, ``Resolution`` → pixel size — with **no per-tile GDAL header reads**
(7.4k ``/vsicurl`` round-trips would take ~40 min). The index can't see what's actually in
the bucket, though, so one parallel HTTP HEAD sweep (~30 s) then drops products that are
missing or GDAL-unreadable before they can crash an aggregate shard (see ``validate_objects``).
See ``source_remote`` for the streaming model; ``source_register_remote_urllist`` is the
header-read flat-urllist variant (CUDEM).

Run from pipelines/:  uv run python source_register_remote_geopkg.py <source-id>
"""

import math
import os
import re
import sys

import geopandas as gpd
import requests

import config
import utils
from source_remote import to_vsicurl, write_bounds


def _newest_key(list_xml):
    """The lexically-greatest ``.gpkg`` key in an S3 ListBucketResult XML, else None. BlueTopo's
    tile-scheme filenames are date-stamped, so lexical max == newest. Plain regex over <Key> (not
    an XML parser) — keys are simple and this sidesteps XXE/entity-expansion on the response."""
    gpkgs = sorted(k for k in re.findall(r"<Key>([^<]+)</Key>", list_xml) if k.endswith(".gpkg"))
    return gpkgs[-1] if gpkgs else None


def newest_gpkg(prefix_url):
    """Resolve a public-bucket prefix URL to its newest ``.gpkg`` (one S3 list, no creds). NOAA
    re-publishes BlueTopo's tile scheme under a stable prefix, so this tracks the current catalog
    without vendoring the dated filename."""
    host, _, prefix = prefix_url.partition(".s3.amazonaws.com/")
    host += ".s3.amazonaws.com"
    r = requests.get(host, params={"list-type": "2", "prefix": prefix}, timeout=60)
    r.raise_for_status()
    key = _newest_key(r.text)
    if not key:
        sys.exit(f"no .gpkg under {prefix_url}")
    return f"{host}/{key}"


def _populated_mask(links):
    """Boolean mask of populated tiles. pandas reads a NULL GeoTIFF_Link as float NaN — and
    ``bool(nan)`` is True — so test for a non-empty *string*, not bare truthiness. (~5k of
    BlueTopo's ~12.7k tiles are unpopulated.)"""
    return [isinstance(u, str) and bool(u) for u in links]


def _dims(ext_x_3857, ext_y_3857, lat, res_m):
    """Pixel width/height from a tile's 3857 extent + native metre resolution. 3857 metres are
    stretched ~1/cos(lat) vs ground metres, so the cos-corrected extent / resolution reproduces
    the pixel count a COG header would report — keeping the covering's maxzoom inference identical
    to the header-read path, without opening the tile."""
    cos = math.cos(math.radians(lat))
    return max(1, round(ext_x_3857 * cos / res_m)), max(1, round(ext_y_3857 * cos / res_m))


def _x_extent(left, right):
    """3857 (left, right) -> (left, right, x_extent), antimeridian-aware. A tile whose
    reprojected span is ~the whole world has crossed 180 (its vertices land on both ±X_MAX);
    return the wrapped convention left>right — what the covering's split_at_antimeridian
    expects — plus the true narrow width, not the full-globe span."""
    ext = right - left
    if ext > 0.9 * 2 * utils.X_MAX_3857:
        return right, left, 2 * utils.X_MAX_3857 - ext
    return left, right, ext


def gpkg_bounds(gpkg_url, bbox, link_col="GeoTIFF_Link"):
    """Build bounds.csv rows straight from the tile-scheme GeoPackage — no per-tile header reads.
    The gpkg indexes every tile (footprint geometry + Resolution + a per-tile URL), so 3857 bounds
    come from reprojecting the footprint and pixel size from ``_dims``. ``bbox`` (W,S,E,N lon/lat)
    pushes an OGR spatial filter (gpkg geometry is WGS84) so a regional build reads only nearby rows.
    ``link_col`` names the per-tile URL column — ``S102V30`` (S-102), else the ``GeoTIFF_Link`` default."""
    gdf = gpd.read_file("/vsicurl/" + gpkg_url, bbox=tuple(bbox) if bbox else None)
    gdf = gdf[_populated_mask(gdf[link_col])]
    if gdf.empty:
        return []
    lat = gdf.geometry.representative_point().y          # tile center latitude (WGS84)
    b = gdf.geometry.to_crs(3857).bounds                 # vectorized reproject -> minx,miny,maxx,maxy
    rows = []
    for url, la, l, bot, r, t, resstr in zip(
            gdf[link_col], lat, b["minx"], b["miny"], b["maxx"], b["maxy"], gdf["Resolution"]):
        m = re.search(r"\d+(?:\.\d+)?", str(resstr))     # "16m" -> 16; default coarse if absent
        res_m = float(m.group()) if m else 16.0
        l, r, ext_x = _x_extent(l, r)                    # wrap dateline tiles (Aleutians)
        w, h = _dims(ext_x, t - bot, la, res_m)
        rows.append((to_vsicurl(url), l, bot, r, t, w, h))
    return rows


# Objects smaller than this get PROBED before registering (not dropped on size alone —
# real slivers of coastline compress under 50 KB; ~98 of S-102's 4.3k products are
# sub-100 KB and most are fine). The probe exists because NOAA has published stub
# products (a ~25 KB file whose BathymetryCoverage grid is 1x1 cells) that the
# tile-scheme index still advertises at the nominal cell footprint, and older S102
# drivers (container GDAL 3.8) crash on them mid-aggregate. Report stubs to NOAA OCS:
# https://www.nauticalcharts.noaa.gov/customer-service/assist/
PROBE_UNDER_BYTES = 100_000
# More drops than this means the catalog itself is broken (mass re-publish, auth
# change) — fail the source rather than quietly registering a gutted coastline (the
# previous bounds.csv in R2 keeps serving until the catalog recovers).
MAX_DROPS = 50


def _head_size(url, tries=3):
    """Content-Length via HEAD, or None if the object is missing (404/403). Transient
    errors (timeouts, 5xx) retry, then raise — a network blip must not read as 'missing'
    and silently drop a good product."""
    import time
    for attempt in range(1, tries + 1):
        try:
            r = requests.head(url, timeout=30)
            if r.status_code in (403, 404):
                return None
            r.raise_for_status()
            return int(r.headers.get("Content-Length", 0))
        except requests.exceptions.RequestException:
            if attempt == tries:
                raise
            time.sleep(2 ** attempt)


def _gdal_openable(vsicurl_path, tries=3):
    """Can the SAME GDAL the aggregate stage uses open this object? gdalinfo in a
    subprocess, so a driver crash (older S102 drivers segfault on degenerate 1x1
    products) is contained instead of killing registration — and the verdict tracks
    whatever GDAL the toolchain image ships, so a GDAL upgrade that fixes the driver
    automatically re-admits the products. Retries so a transient /vsicurl blip can't
    misclassify a good product as unreadable."""
    import subprocess
    import time
    for attempt in range(1, tries + 1):
        if subprocess.run(["gdalinfo", vsicurl_path], capture_output=True).returncode == 0:
            return True
        if attempt < tries:
            time.sleep(2 ** attempt)
    return False


def validate_objects(rows, head=_head_size, openable=_gdal_openable):
    """Drop rows whose object is missing from the bucket or unreadable by GDAL, loudly.
    The tile-scheme gpkg advertises every product at its nominal cell footprint, so the
    index can't see either case. One parallel HEAD sweep (~30 s for the full catalog)
    finds missing objects and flags suspiciously small ones; only those few candidates
    get the heavier gdalinfo probe — small-but-real products stay registered."""
    from concurrent.futures import ThreadPoolExecutor

    urls = [row[0].replace("/vsicurl/", "", 1) for row in rows]
    with ThreadPoolExecutor(max_workers=16) as pool:
        sizes = list(pool.map(head, urls))
        candidates = [row[0] for row, size in zip(rows, sizes)
                      if size is not None and size < PROBE_UNDER_BYTES]
        verdicts = dict(zip(candidates, pool.map(openable, candidates)))
    kept, dropped = [], 0
    for row, url, size in zip(rows, urls, sizes):
        if size is None:
            print(f"DROPPING {url}: object missing (in the catalog, not in the bucket)")
        elif size < PROBE_UNDER_BYTES and not verdicts[row[0]]:
            print(f"DROPPING {url}: {size} bytes and GDAL cannot open it — stub/corrupt product "
                  f"(report via https://www.nauticalcharts.noaa.gov/customer-service/assist/)")
        else:
            kept.append(row)
            continue
        dropped += 1
    if dropped:
        print(f"WARNING: dropped {dropped} of {len(rows)} catalog products")
    if dropped > MAX_DROPS:
        sys.exit(f"{dropped} drops exceeds MAX_DROPS={MAX_DROPS} — catalog looks broken, refusing to register")
    return kept


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: source_register_remote_geopkg.py <source-id>")
    source = sys.argv[1]
    link_col = config.load_metadata(source).get("link_column", "GeoTIFF_Link")
    bbox = os.environ.get("BBOX", "").strip()
    bbox = [float(x) for x in bbox.split(",")] if bbox else None

    rows = []
    for manifest in config.file_list(source):
        gpkg = newest_gpkg(manifest) if manifest.endswith("/") else manifest
        print(f"reading tile-scheme gpkg {gpkg}")
        rows += gpkg_bounds(gpkg, bbox, link_col)
    write_bounds(source, validate_objects(rows))


def _check():
    sample = (
        '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        "<Contents><Key>BlueTopo/_BlueTopo_Tile_Scheme/BlueTopo_Tile_Scheme_20250101_000000.gpkg</Key></Contents>"
        "<Contents><Key>BlueTopo/_BlueTopo_Tile_Scheme/BlueTopo_Tile_Scheme_20260616_191529.gpkg</Key></Contents>"
        "<Contents><Key>BlueTopo/_BlueTopo_Tile_Scheme/index.html</Key></Contents>"
        "</ListBucketResult>"
    )
    assert _newest_key(sample).endswith("20260616_191529.gpkg"), _newest_key(sample)
    assert _newest_key("<ListBucketResult/>") is None
    # unpopulated tiles read as float NaN (truthy!) — must be masked out, not .lower()'d
    assert _populated_mask(["a.tif", "", float("nan"), "b.tiff"]) == [True, False, False, True]
    # pixel size: at the equator 3857 == ground; at 60°N (cos ½) a tile is half the px per 3857 m
    assert _dims(1000, 1000, 0.0, 10.0) == (100, 100)
    assert _dims(1000, 1000, 60.0, 10.0) == (50, 50)
    # antimeridian: a normal box passes through; a ~full-width span becomes the wrapped
    # convention (left>right) with the true narrow width (what split_at_antimeridian splits).
    XM = utils.X_MAX_3857
    assert _x_extent(-100.0, 100.0) == (-100.0, 100.0, 200.0)
    wl, wr, wext = _x_extent(-0.9999 * XM, 0.9999 * XM)
    assert wl > wr and 0 < wext < 0.01 * XM, (wl, wr, wext)

    # validate_objects: big products register unprobed; small ones are PROBED, and only
    # the unreadable drop — a 46 KB sliver-of-coastline product is real and must stay
    # (the size-only threshold dropped 90+ real products). Missing objects drop; a
    # systemic wipe (> MAX_DROPS) hard-fails instead of registering a gutted coastline.
    row = lambda u: (f"/vsicurl/https://x/{u}", 0, 0, 1, 1, 10, 10)
    sizes = {"https://x/real.h5": 5_000_000, "https://x/sliver.h5": 46_435,
             "https://x/stub.h5": 25_048, "https://x/gone.h5": None}
    probed = []
    def fake_probe(path):
        probed.append(path)
        return "stub" not in path
    kept = validate_objects([row("real.h5"), row("sliver.h5"), row("stub.h5"), row("gone.h5")],
                            head=sizes.get, openable=fake_probe)
    assert [r[0] for r in kept] == ["/vsicurl/https://x/real.h5", "/vsicurl/https://x/sliver.h5"], kept
    assert sorted(probed) == ["/vsicurl/https://x/sliver.h5", "/vsicurl/https://x/stub.h5"], \
        f"only small candidates get probed: {probed}"
    try:
        validate_objects([row(f"gone{i}.h5") for i in range(MAX_DROPS + 1)],
                         head=lambda u: None, openable=lambda p: True)
        assert False, "expected a systemic drop count to exit"
    except SystemExit as e:
        assert "catalog looks broken" in str(e), e
    print("source_register_remote_geopkg.py self-check ok")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        _check()
    else:
        main()
