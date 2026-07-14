"""Apply the bathymetry value transform: ``negate``, ``datum_offset_m``, then an
optional ``offset_surface`` (a spatially-varying reference subtraction).

Reads the knobs from ``metadata.json`` / recipe args:

  - ``negate``: flip positive-down depth sources (e.g. DDM, stored as +depth) to
    negative-down elevation.
  - ``datum_offset_m``: constant added to bring the source to ~MSL (a single
    offset — the flat-datum case, e.g. a lake surface level).
  - ``offset_surface``: a reference raster (height in the source's vertical frame)
    subtracted per pixel, for sources whose datum separation varies in space rather
    than being one constant. Turns ``bed`` into ``bed - reference`` — e.g. an NHN
    riverbed minus the BSH SKN-Fläche (chart datum in NHN) → depth below chart datum.
    Where the reference does not cover a pixel, that pixel becomes nodata (it cannot
    be referenced), so partial-coverage references only touch what they cover.

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
from rasterio.warp import Resampling, reproject


def _surface_on_grid(surface_path, profile):
    """Resample a reference raster onto the tile grid (profile's crs/transform/shape),
    bilinear (it's a smooth continuous surface). Returns (values float32, valid bool);
    valid is False where the reference has no data over the tile."""
    dst = np.full((profile["height"], profile["width"]), np.nan, dtype="float32")
    with rasterio.open(surface_path) as ref:
        reproject(
            source=rasterio.band(ref, 1),
            destination=dst,
            src_transform=ref.transform, src_crs=ref.crs,
            dst_transform=profile["transform"], dst_crs=profile["crs"],
            src_nodata=ref.nodata, dst_nodata=np.nan,
            resampling=Resampling.bilinear,
        )
    return dst, np.isfinite(dst)


def transform_file(filepath, negate, offset, clamp_positive=False, offset_surface=None):
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

    if offset_surface:
        nodata = profile.get("nodata")
        if nodata is None:
            raise ValueError(f"{filepath}: --offset-surface needs a nodata value set")
        ref, ref_valid = _surface_on_grid(offset_surface, profile)
        # bed - reference where both cover; a bed pixel with no reference cannot be
        # referenced to the datum, so drop it to nodata rather than leave it at NHN.
        both = mask & ref_valid
        data[mask & ~ref_valid] = np.float32(nodata)
        data[both] = data[both] - ref[both]

    if clamp_positive:
        # After the transform, 0 = the reference (water surface / chart datum); anything
        # > 0 is above it (surrounding terrain, a lake DEM's land fringe, a topobathy
        # playa) — drop it to nodata so it can't bleed into the water layer as false land.
        nodata = profile.get("nodata")
        if nodata is None:
            raise ValueError(f"{filepath}: --clamp-positive needs a nodata value set")
        cur = data != np.float32(nodata) if nodata is not None else mask
        data[mask & cur & (data > 0)] = np.float32(nodata)

    profile.update(driver="GTiff", dtype="float32", tiled=True,
                   blockxsize=512, blockysize=512, compress="deflate")
    tmp = filepath + ".datum.tif"
    with rasterio.open(tmp, "w", **profile) as dst:
        dst.write(data, 1)
    os.replace(tmp, filepath)


def write_sidecar(source, negate, offset, clamp_positive, offset_surface=None):
    """Record the applied transform in store/source/<id>/datum.json — the machine-readable
    provenance source_catalog folds into the catalog item (vertical-datum offset was invisible
    downstream when it lived only in this CLI arg). Written whenever the step runs, so a source
    whose recipe calls source_datum always leaves a sidecar."""
    os.makedirs(f"store/source/{source}", exist_ok=True)
    with open(f"store/source/{source}/datum.json", "w") as f:
        json.dump({"negate": bool(negate), "offset_m": float(offset),
                   "clamp_positive": bool(clamp_positive),
                   "offset_surface": os.path.basename(offset_surface) if offset_surface else None}, f, indent=2)


def _resolve_surface(source, arg):
    """Accept a path or a filename cached under store/source/<id>/."""
    if os.path.exists(arg):
        return arg
    candidate = f"store/source/{source}/{arg}"
    if os.path.exists(candidate):
        return candidate
    sys.exit(f"--offset-surface: {arg} not found (also tried {candidate})")


def main():
    p = argparse.ArgumentParser(description="Apply negate + datum offset (scalar and/or surface) to a source's tifs.")
    p.add_argument("source")
    p.add_argument("--negate", action="store_true", help="flip positive-down depth to negative-down elevation")
    p.add_argument("--offset", type=float, default=0.0, help="metres added to reach ~MSL (flat datum)")
    p.add_argument("--offset-surface", dest="offset_surface", default=None,
                   help="reference raster (height in the source frame) subtracted per pixel — "
                        "the spatially-varying datum case (e.g. BSH SKN-Fläche for chart datum). "
                        "A path, or a filename under store/source/<id>/.")
    p.add_argument("--clamp-positive", action="store_true",
                   help="after the transform, drop cells > 0 (above the reference) to nodata — "
                        "removes a lake DEM's land fringe / a topobathy playa")
    a = p.parse_args()

    surface = _resolve_surface(a.source, a.offset_surface) if a.offset_surface else None

    # Record what this invocation applies even when it's a no-op, so the sidecar exists for
    # every source whose recipe runs source_datum (source_catalog's invariant).
    write_sidecar(a.source, a.negate, a.offset, a.clamp_positive, surface)

    if not a.negate and a.offset == 0 and not a.clamp_positive and not surface:
        print(f"{a.source}: no datum transform (negate=False, offset=0, no surface)")
        return
    filepaths = sorted(glob(f"store/source/{a.source}/*.tif"))
    print(f"{a.source}: negate={a.negate} offset={a.offset} surface={os.path.basename(surface) if surface else None} "
          f"clamp_positive={a.clamp_positive} on {len(filepaths)} file(s)")
    for filepath in filepaths:
        transform_file(filepath, a.negate, a.offset, a.clamp_positive, surface)


def _check():
    """Self-check the value transform on synthetic rasters (no GDAL CLI)."""
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

    # offset_surface: bed (NHN) minus a chart-datum surface (NHN), partial coverage.
    # bed grid: 2x2 at NHN; nodata one cell. Reference covers only the left column.
    bedp = os.path.join(d, "bed.tif")
    bed = np.array([[-10.0, -8.0], [-6.0, nodata]], dtype="float32")
    with rasterio.open(bedp, "w", driver="GTiff", height=2, width=2, count=1,
                       dtype="float32", nodata=nodata, crs="EPSG:4326",
                       transform=from_origin(0, 2, 1, 1)) as dst:
        dst.write(bed, 1)
    # reference: SKN in NHN ~ -1.5 over the left half, nodata over the right half
    refp = os.path.join(d, "ref.tif")
    refnd = 99999.0
    ref = np.array([[-1.5, refnd], [-1.5, refnd]], dtype="float32")
    with rasterio.open(refp, "w", driver="GTiff", height=2, width=2, count=1,
                       dtype="float32", nodata=refnd, crs="EPSG:4326",
                       transform=from_origin(0, 2, 1, 1)) as dst:
        dst.write(ref, 1)
    transform_file(bedp, negate=False, offset=0.0, offset_surface=refp)
    with rasterio.open(bedp) as src:
        ob = src.read(1)
    # left column referenced: bed - (-1.5) = bed + 1.5 ; right column no reference -> nodata
    assert abs(ob[0, 0] - (-8.5)) < 1e-4, ob        # -10 - (-1.5)
    assert abs(ob[1, 0] - (-4.5)) < 1e-4, ob        # -6  - (-1.5)
    assert ob[0, 1] == nodata, ob                    # covered bed but no reference -> dropped
    assert ob[1, 1] == nodata, ob                    # bed nodata stays nodata
    print("source_datum.py self-check ok")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        _check()
    else:
        main()
