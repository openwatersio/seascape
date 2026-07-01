"""Apply the bathymetry value transform: ``scale`` then ``negate`` then ``datum_offset_m``.

Reads the knobs from ``metadata.json``:

  - ``scale``: multiply valid pixels for a unit conversion, applied first — e.g.
    ``0.01`` for a source stored in centimetres (Allen Coral Atlas SDB) to reach metres.
  - ``negate``: flip positive-down depth sources (e.g. DDM, stored as +depth) to
    negative-down elevation.
  - ``datum_offset_m``: constant added to bring the source to ~MSL (a single
    offset, not full VDatum — the Milestone 3 seam for a spatially-varying separation).

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


def transform_file(filepath, negate, offset, clamp_positive=False, scale=1.0):
    with rasterio.open(filepath) as src:
        profile = src.profile
        data = src.read(1)
        mask = src.read_masks(1) != 0  # True where valid

    data = data.astype("float32")
    valid = data[mask]
    if scale != 1.0:
        valid = valid * np.float32(scale)
    if negate:
        valid = -valid
    if offset:
        valid = valid + np.float32(offset)
    data[mask] = valid
    if clamp_positive:
        # After the offset, 0 = water surface; anything > 0 is the surrounding terrain
        # (a lake DEM's land fringe, or a topobathy playa) — drop it to nodata so it
        # can't bleed into the water layer as false land.
        nodata = profile.get("nodata")
        if nodata is None:
            raise ValueError(f"{filepath}: --clamp-positive needs a nodata value set")
        data[mask & (data > 0)] = np.float32(nodata)

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
    p.add_argument("--scale", type=float, default=1.0,
                   help="multiply valid pixels (unit conversion, applied before negate) — "
                        "e.g. 0.01 for centimetre depths to metres")
    p.add_argument("--clamp-positive", action="store_true",
                   help="after the offset, drop cells > 0 (above the water surface) to nodata — "
                        "removes a lake DEM's land fringe / a topobathy playa")
    a = p.parse_args()

    if not a.negate and a.offset == 0 and a.scale == 1.0 and not a.clamp_positive:
        print(f"{a.source}: no datum transform (negate=False, offset=0, scale=1)")
        return
    filepaths = sorted(glob(f"store/source/{a.source}/*.tif"))
    print(f"{a.source}: scale={a.scale} negate={a.negate} offset={a.offset} "
          f"clamp_positive={a.clamp_positive} on {len(filepaths)} file(s)")
    for filepath in filepaths:
        transform_file(filepath, a.negate, a.offset, a.clamp_positive, a.scale)


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

    # clamp_positive: topobathy (negative bed, positive land) -> land dropped to nodata
    path2 = os.path.join(d, "t2.tif")
    arr2 = np.array([[-50.0, -10.0], [5.0, nodata]], dtype="float32")
    with rasterio.open(path2, "w", driver="GTiff", height=2, width=2, count=1,
                       dtype="float32", nodata=nodata, crs="EPSG:4326",
                       transform=from_origin(0, 2, 1, 1)) as dst:
        dst.write(arr2, 1)
    transform_file(path2, negate=False, offset=0.0, clamp_positive=True)
    with rasterio.open(path2) as src:
        o2 = src.read(1)
    assert o2[0, 0] == -50.0 and o2[0, 1] == -10.0, o2  # bed kept
    assert o2[1, 0] == nodata and o2[1, 1] == nodata, o2  # +5 land clamped; nodata untouched

    # scale + negate: centimetre depths (ACA SDB) -> metres, flipped to elevation
    path3 = os.path.join(d, "t3.tif")
    arr3 = np.array([[100.0, 313.0, nodata]], dtype="float32")  # +depth in cm
    with rasterio.open(path3, "w", driver="GTiff", height=1, width=3, count=1,
                       dtype="float32", nodata=nodata, crs="EPSG:4326",
                       transform=from_origin(0, 1, 1, 1)) as dst:
        dst.write(arr3, 1)
    transform_file(path3, negate=True, offset=0.0, scale=0.01)  # cm depth -> m elevation
    with rasterio.open(path3) as src:
        o3 = src.read(1)
    assert abs(o3[0, 0] - (-1.0)) < 1e-6 and abs(o3[0, 1] - (-3.13)) < 1e-6, o3
    assert o3[0, 2] == nodata, o3  # nodata untouched by scale+negate
    print("source_datum.py self-check ok")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        _check()
    else:
        main()
