"""Fetch a source from public S3 via GDAL /vsis3/, clipped to fetch_bbox.

For datasets exposed as a master VRT / COG on S3 (e.g. NOAA CUDEM): reading
through /vsis3/ and clipping pulls only the intersecting tiles — no bulk
download. Keeps the source's native CRS (the warp to Web Mercator happens later
in aggregation). file_list.txt holds the s3:// URL; fetch_bbox (W,S,E,N lon/lat)
is in metadata.json.
"""

import argparse
import os
import subprocess
import sys

import config


def main():
    p = argparse.ArgumentParser(description="Clip an s3:// source to a bbox via /vsis3/.")
    p.add_argument("source")
    p.add_argument("--bbox", required=True, help="W,S,E,N in lon/lat")
    a = p.parse_args()
    source = a.source
    w, s, e, n = (str(float(x)) for x in a.bbox.split(","))
    os.makedirs(f"store/source/{source}", exist_ok=True)
    env = {**os.environ, "AWS_NO_SIGN_REQUEST": "YES"}

    for i, url in enumerate(config.file_list(source)):
        if not url.startswith("s3://"):
            sys.exit(f"source_download_s3 expects s3:// urls, got {url}")
        out = f"store/source/{source}/{source}_{i}.tif"
        print(f"  [{i}] /vsis3/ clip {url} -> {out}")
        subprocess.run(
            ["gdalwarp", "-overwrite", "-te", w, s, e, n, "-te_srs", "EPSG:4326",
             "-of", "COG", "-co", "BLOCKSIZE=512", "-co", "OVERVIEWS=NONE",
             "-co", "BIGTIFF=IF_NEEDED", "-co", "COMPRESS=DEFLATE", "-co", "PREDICTOR=3",
             "/vsis3/" + url[len("s3://"):], out],
            check=True, env=env)


if __name__ == "__main__":
    main()
