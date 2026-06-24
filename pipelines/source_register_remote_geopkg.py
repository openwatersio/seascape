"""Register a streaming source from a tile-scheme *GeoPackage* index (e.g. NOAA S-102's
navigation tile scheme): a per-tile URL column — named by ``link_column`` in metadata
(``S102V30`` for S-102, ``GeoTIFF_Link`` for a BlueTopo-style index) — gives each tile's
COG/HDF5; features with a null link are dropped.

``file_list.txt`` holds either the tile-scheme prefix (``…/_CATALOG/`` for S-102 — the newest
dated ``.gpkg`` under it is resolved via one public S3 list) or a direct ``.gpkg`` URL. ``BBOX``
(W,S,E,N lon/lat) pushes an OGR spatial filter on the tile geometry — S-102 tile names aren't
lat/lon-encoded, so geometry is the only prefilter.

The gpkg already *is* the index, so ``bounds.csv`` is built straight from it — footprint
geometry → 3857 bounds, ``Resolution`` → pixel size — with **no per-tile header reads** (7.4k
``/vsicurl`` round-trips would take ~40 min). See ``source_remote`` for the streaming model;
``source_register_remote_urllist`` is the header-read flat-urllist variant (CUDEM).

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
    ``link_col`` names the per-tile URL column — ``GeoTIFF_Link`` (BlueTopo), ``S102V30`` (NOAA S-102)."""
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
    write_bounds(source, rows)


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
    print("source_register_remote_geopkg.py self-check ok")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        _check()
    else:
        main()
