"""Fetch NOAA NOS Estuarine Bathymetric DEMs and convert each to a GeoTIFF.

The product ships as netCDF on the NGDC THREDDS server (one file per estuary).
Two reasons this is its own step rather than plain source_download + source_unzip:

  1. The shared steps glob ``store/source/<id>/*.tif`` — netCDF would be skipped —
     so each ``.nc`` is translated to ``.tif`` here (GDAL reads netCDF, writes GTiff).
  2. Each estuary embeds its own NAD83 / UTM zone (Long Island Sound is 18N, the
     Gulf coast 14N–16N, the Pacific 10N–11N). gdal_translate with NO ``-a_srs``
     preserves that per-file CRS, so the source is ``mixed_crs`` (like NOAA S-102):
     source_normalize is run WITHOUT --crs and aggregation reprojects each file
     from its own zone. A single project-wide --crs would corrupt every other zone.

Values are bed elevation on the local tidal datum (~MLLW): bed negative, intertidal
slightly positive — already GEBCO's convention, so no negate and no datum offset.

Reads the THREDDS fileServer URLs from file_list.txt; the output tif keeps the
estuary name (so bounds.csv / overlays stay legible). BBOX is not supported (these
are discrete estuaries, not a grid) — to build a subset, comment URLs in file_list.
"""

import os
import sys

import config
import utils


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: source_download_estuarine.py <source-id>")
    source = sys.argv[1]
    urls = config.file_list(source)
    if not urls:
        sys.exit(f"no URLs in {config.SOURCES_DIR}/{source}/file_list.txt")
    out_dir = f"store/source/{source}"
    os.makedirs(out_dir, exist_ok=True)
    print(f"downloading {source}: {len(urls)} estuary netCDF(s)")
    for i, url in enumerate(urls):
        stem = url.rsplit("/", 1)[-1].rsplit(".", 1)[0]  # e.g. long_island_sound_m040_30m
        nc = f"{out_dir}/{stem}.nc"
        tif = f"{out_dir}/{stem}.tif"
        if os.path.exists(tif):
            print(f"  [{i}] {stem}: tif exists, skipping")
            continue
        print(f"  [{i}] {url}")
        utils.http_download(url, nc)
        # Translate to GTiff preserving the embedded per-file CRS (no -a_srs). nodata
        # is already set in the netCDF (-9999); source_normalize makes the final COG.
        utils.run_command(
            f"gdal_translate -q -of GTiff -co TILED=YES -co COMPRESS=DEFLATE {nc} {tif}",
            silent=False,
        )
        os.remove(nc)


if __name__ == "__main__":
    main()
