"""Apply the bathymetry value transform: ``negate`` then ``datum_offset_m``.

Reads the knobs from ``metadata.json``:

  - ``negate``: flip positive-down depth sources (e.g. DDM, stored as +depth) to
    negative-down elevation.
  - ``datum_offset_m``: constant added to bring the source to ~MSL (a single
    offset, not full VDatum — the WS3 seam for a spatially-varying separation).

Operates per file in ``store/source/<id>/``, in the source's native CRS (the
reprojection to Web Mercator happens later, in the aggregation stage), preserving
nodata/geotransform/CRS. Only valid pixels are transformed, so nodata never gets
negated into a spurious depth. Writes a tiled GeoTIFF; source_normalize makes the
final LERC COG.
"""

import argparse
import sys
from glob import glob

import numpy as np
import rasterio


def transform_file(filepath, negate, offset):
    with rasterio.open(filepath) as src:
        profile = src.profile
        data = src.read(1)
        mask = src.read_masks(1) != 0  # True where valid

    data = data.astype("float32")
    valid = data[mask]
    if negate:
        valid = -valid
    if offset:
        valid = valid + np.float32(offset)
    data[mask] = valid

    profile.update(driver="GTiff", dtype="float32", tiled=True,
                   blockxsize=512, blockysize=512, compress="deflate")
    tmp = filepath + ".datum.tif"
    with rasterio.open(tmp, "w", **profile) as dst:
        dst.write(data, 1)
    import os
    os.replace(tmp, filepath)


def main():
    p = argparse.ArgumentParser(description="Apply negate + datum offset to a source's tifs.")
    p.add_argument("source")
    p.add_argument("--negate", action="store_true", help="flip positive-down depth to negative-down elevation")
    p.add_argument("--offset", type=float, default=0.0, help="metres added to reach ~MSL")
    a = p.parse_args()

    if not a.negate and a.offset == 0:
        print(f"{a.source}: no datum transform (negate=False, offset=0)")
        return
    filepaths = sorted(glob(f"store/source/{a.source}/*.tif"))
    print(f"{a.source}: negate={a.negate} offset={a.offset} on {len(filepaths)} file(s)")
    for filepath in filepaths:
        transform_file(filepath, a.negate, a.offset)


def _check():
    """Self-check the value transform on a synthetic raster (no GDAL CLI)."""
    import os
    import tempfile
    from rasterio.transform import from_origin

    d = tempfile.mkdtemp()
    path = os.path.join(d, "t.tif")
    nodata = -9999.0
    arr = np.array([[5.0, 10.0, nodata], [0.0, 2.5, 100.0]], dtype="float32")  # +depth
    with rasterio.open(path, "w", driver="GTiff", height=2, width=3, count=1,
                       dtype="float32", nodata=nodata, crs="EPSG:4326",
                       transform=from_origin(0, 2, 1, 1)) as dst:
        dst.write(arr, 1)

    transform_file(path, negate=True, offset=-1.0)  # depth->elev, then -1 m datum
    with rasterio.open(path) as src:
        out = src.read(1)
    # valid pixels: -(v) - 1 ; nodata untouched
    assert out[0, 0] == -6.0 and out[0, 1] == -11.0, out
    assert out[1, 0] == -1.0 and out[1, 1] == -3.5 and out[1, 2] == -101.0, out
    assert out[0, 2] == nodata, out[0, 2]  # nodata not negated into +9999
    print("source_datum.py self-check ok")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        _check()
    else:
        main()
