"""Apply the bathymetry value transform: ``negate`` then ``datum_offset_m``.

Reads the knobs from ``metadata.json``:

  - ``negate``: flip positive-down depth sources (e.g. DDM, stored as +depth) to
    negative-down elevation.
  - ``datum_offset_m``: constant added to bring the source to ~MSL (a single
    scalar, not a full VDatum — a spatially-varying separation is tracked in #16).

Operates per file in ``store/source/<id>/``, in the source's native CRS (the
reprojection to Web Mercator happens later, in the aggregation stage), preserving
nodata/geotransform/CRS. Only valid pixels are transformed, so nodata never gets
negated into a spurious depth. Writes a tiled GeoTIFF; source_normalize makes the
final LERC COG.
"""

import argparse
import json
import os
import sys
from glob import glob

import numpy as np
import rasterio


def transform_file(filepath, negate, offset, clamp_positive=False):
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
    os.replace(tmp, filepath)


def write_sidecar(source, negate, offset, clamp_positive):
    """Record the applied transform in store/source/<id>/datum.json — the machine-readable
    provenance source_catalog folds into the catalog item (vertical-datum offset was invisible
    downstream when it lived only in this CLI arg). Written whenever the step runs, so a source
    whose recipe calls source_datum always leaves a sidecar."""
    os.makedirs(f"store/source/{source}", exist_ok=True)
    with open(f"store/source/{source}/datum.json", "w") as f:
        json.dump({"negate": bool(negate), "offset_m": float(offset),
                   "clamp_positive": bool(clamp_positive)}, f, indent=2)


def main():
    p = argparse.ArgumentParser(description="Apply negate + datum offset to a source's tifs.")
    p.add_argument("source")
    p.add_argument("--negate", action="store_true", help="flip positive-down depth to negative-down elevation")
    p.add_argument("--offset", type=float, default=0.0, help="metres added to reach ~MSL")
    p.add_argument("--clamp-positive", action="store_true",
                   help="after the offset, drop cells > 0 (above the water surface) to nodata — "
                        "removes a lake DEM's land fringe / a topobathy playa")
    a = p.parse_args()

    # Record what this invocation applies even when it's a no-op, so the sidecar exists for
    # every source whose recipe runs source_datum (source_catalog's invariant).
    write_sidecar(a.source, a.negate, a.offset, a.clamp_positive)

    if not a.negate and a.offset == 0 and not a.clamp_positive:
        print(f"{a.source}: no datum transform (negate=False, offset=0)")
        return
    filepaths = sorted(glob(f"store/source/{a.source}/*.tif"))
    print(f"{a.source}: negate={a.negate} offset={a.offset} clamp_positive={a.clamp_positive} "
          f"on {len(filepaths)} file(s)")
    for filepath in filepaths:
        transform_file(filepath, a.negate, a.offset, a.clamp_positive)


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
    print("source_datum.py self-check ok")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        _check()
    else:
        main()
