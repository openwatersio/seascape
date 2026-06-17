"""Register a streaming source from a tile-scheme *GeoPackage* — NOAA BlueTopo's tile index,
where each feature's ``GeoTIFF_Link`` column is the per-tile COG URL (many features are
unpopulated → null link, dropped).

``file_list.txt`` holds either the tile-scheme prefix (``…/_BlueTopo_Tile_Scheme/`` — the
newest dated ``.gpkg`` under it is resolved via one public S3 list) or a direct ``.gpkg`` URL.
``BBOX`` (W,S,E,N lon/lat) pushes an OGR spatial filter on the tile geometry — BlueTopo tile
names aren't lat/lon-encoded, so geometry is the only prefilter. See ``source_remote`` for the
streaming model; ``source_register_remote_urllist`` is the flat-urllist variant.

Run from pipelines/:  uv run python source_register_remote_geopkg.py <source-id>
"""

import os
import re
import sys

import geopandas as gpd
import requests

import config
from source_remote import register_tiles


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


def gpkg_tile_urls(gpkg_url, bbox):
    """Tile COG URLs from a tile-scheme GeoPackage: the GeoTIFF_Link of each *populated* feature.
    ``bbox`` (W,S,E,N lon/lat) pushes an OGR spatial filter (gpkg geometry is WGS84) so a regional
    build reads only nearby rows."""
    gdf = gpd.read_file("/vsicurl/" + gpkg_url, bbox=tuple(bbox) if bbox else None)
    return _populated_links(gdf["GeoTIFF_Link"])


def _populated_links(links):
    """Drop unpopulated tiles. pandas reads a NULL GeoTIFF_Link as float NaN — and
    ``bool(nan)`` is True — so a bare ``if u`` lets NaN through and ``u.lower()`` later
    blows up. Keep only non-empty strings. (~5k of BlueTopo's ~12.7k tiles are unpopulated.)"""
    return [u for u in links if isinstance(u, str) and u]


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: source_register_remote_geopkg.py <source-id>")
    source = sys.argv[1]
    bbox = os.environ.get("BBOX", "").strip()
    bbox = [float(x) for x in bbox.split(",")] if bbox else None

    urls = []
    for manifest in config.file_list(source):
        gpkg = newest_gpkg(manifest) if manifest.endswith("/") else manifest
        print(f"reading tile-scheme gpkg {gpkg}")
        urls += gpkg_tile_urls(gpkg, bbox)
    urls = [u for u in urls if u.lower().endswith((".tif", ".tiff"))]
    register_tiles(source, urls)


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
    # unpopulated tiles read as float NaN (truthy!) — must be dropped, not .lower()'d
    assert _populated_links(["a.tif", "", float("nan"), "b.tiff"]) == ["a.tif", "b.tiff"]
    print("source_register_remote_geopkg.py self-check ok")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        _check()
    else:
        main()
