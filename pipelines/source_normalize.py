"""Normalize source rasters to internally-tiled LERC COGs.

Assigns the horizontal CRS and nodata from metadata.json (via ``-a_srs``/
``-a_nodata`` — NOT reprojection, so no vertical/geoid shift; the warp to Web
Mercator happens later in aggregation) and writes the COG, all in one gdal pass.
"""

import argparse
import os
import subprocess
from glob import glob


# Lossless float compression that's available in any GDAL build (DEFLATE +
# float predictor). Avoids a hard dependency on the LERC codec, which the stock
# GDAL CLI may lack (plan risk #6).
COG_OPTS = ["-co", "BLOCKSIZE=512", "-co", "OVERVIEWS=NONE", "-co", "SPARSE_OK=YES",
            "-co", "BIGTIFF=IF_NEEDED", "-co", "COMPRESS=DEFLATE", "-co", "PREDICTOR=3"]


def normalize_file(filepath, crs, nodata):
    tmp = filepath + ".norm.tif"
    cmd = ["gdal_translate", "-of", "COG", *COG_OPTS]
    if crs:
        cmd += ["-a_srs", crs]
    if nodata is not None:
        cmd += ["-a_nodata", str(nodata)]
    cmd += [filepath, tmp]
    subprocess.run(cmd, check=True)
    os.replace(tmp, filepath)


def main():
    p = argparse.ArgumentParser(description="Assign CRS/nodata and rewrite as a LERC/DEFLATE COG.")
    p.add_argument("source")
    p.add_argument("--crs", help="horizontal CRS to assign (e.g. EPSG:4269)")
    p.add_argument("--nodata", help="nodata value to assign")
    a = p.parse_args()
    filepaths = sorted(glob(f"store/source/{a.source}/*.tif"))
    print(f"{a.source}: normalize {len(filepaths)} file(s) (crs={a.crs} nodata={a.nodata})")
    for filepath in filepaths:
        normalize_file(filepath, a.crs, a.nodata)


if __name__ == "__main__":
    main()
