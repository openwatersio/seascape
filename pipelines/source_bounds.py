"""Write store/source/<id>/bounds.csv (one row per file, EPSG:3857 bounds).

Vendored from mapterhorn (BSD-3). Drives the aggregation covering: maxzoom per
file is derived from these bounds + the pixel dimensions.
"""

from glob import glob
import sys
import math

import rasterio
from rasterio.warp import transform_bounds

import utils


def main():
    if len(sys.argv) <= 1:
        sys.exit("usage: source_bounds.py <source-id>")
    source = sys.argv[1]
    print(f"creating bounds for {source}...")

    filepaths = sorted(glob(f"store/source/{source}/*.tif"))
    lines = ["filename,left,bottom,right,top,width,height\n"]

    for j, filepath in enumerate(filepaths):
        with rasterio.open(filepath) as src:
            if src.crs is None:
                raise ValueError(f"crs not defined on {filepath}")
            left, bottom, right, top = transform_bounds(src.crs, "EPSG:3857", *src.bounds)
            if right - left > 0.9 * 2 * utils.X_MAX_3857:
                # probably crosses the antimeridian — transform_bounds flips l/r
                left, right = right, left
            for num in [left, bottom, right, top]:
                if not math.isfinite(num):
                    raise ValueError(f"non-finite bound: src.bounds={src.bounds} crs={src.crs}")
            filename = filepath.split("/")[-1]
            lines.append(f"{filename},{left},{bottom},{right},{top},{src.width},{src.height}\n")
        if j % 100 == 0:
            print(f"{j} / {len(filepaths)}")

    # Write-if-changed: a re-prep that reproduces identical bounds leaves the mtime
    # alone, so nothing downstream (polygon/cover/coverage) re-runs off a no-op.
    utils.write_if_changed(f"store/source/{source}/bounds.csv", "".join(lines))


if __name__ == "__main__":
    main()
