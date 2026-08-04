"""Apply the bathymetry value transform: ``negate``, ``datum_offset_m``, ``offset_surface``.

Reads the knobs from ``metadata.json``:

  - ``negate``: flip positive-down depth sources (e.g. DDM, stored as +depth) to
    negative-down elevation.
  - ``datum_offset_m``: constant added to bring the source to its target datum (a
    single scalar — right for a lake surface, useless for a tidal separation).
  - ``offset_surface``: a reference raster subtracted per pixel, for the tidal case a
    scalar can't express. The reference is the target datum's height in the SOURCE's own
    vertical frame (``store/datum/navd88_mllw.tif``, built by ``datum_grid.py``, is MLLW
    expressed in NAVD88), so::

        elevation_re_chart_datum = bed - reference

    ``reference`` is negative almost everywhere the US coast is covered, so the bed rises
    and depths get shallower — the bias-shallow-safe direction.

Applied in that order, then ``clamp_positive``. Operates per file in
``store/source/<id>/``, in the source's native CRS (the reprojection to Web Mercator
happens later, in the aggregation stage), preserving nodata/geotransform. Only valid
pixels are transformed, so nodata never gets negated into a spurious depth. Writes a
tiled GeoTIFF; source_normalize makes the final LERC COG.
"""

import argparse
import json
import os
import sys
from glob import glob

import numpy as np
import rasterio
from pyproj import CRS as ProjCRS
from rasterio.warp import Resampling, reproject
from rasterio.windows import Window

DATUM_STORE = "store/datum"

# A single file can be tens of gigapixels (CUDEM ships 8112x8112 tiles, and the reference
# resampled onto one is a second array that size), so the transform runs in row stripes.
STRIPE_ROWS = 512


def surface_path(name):
    """An ``--offset-surface`` argument -> a readable raster path. A bare name resolves in
    the datum store — the shape ``metadata.json`` declares; anything carrying a separator or
    an extension is used verbatim."""
    if "/" in name or name.endswith((".tif", ".tiff")):
        return name
    return f"{DATUM_STORE}/{name}.tif"


def horizontal_crs(crs):
    """The horizontal half of a compound source CRS. CUDEM tiles declare NAD83 + NAVD88
    height (EPSG:5498); once the reference is subtracted they are no longer on NAVD88 and EPSG
    has no compound code for MLLW, so the vertical component is dropped rather than left lying."""
    proj = ProjCRS.from_wkt(crs.to_wkt())
    if not proj.is_compound:
        return crs
    return rasterio.crs.CRS.from_wkt(proj.sub_crs_list[0].to_wkt())


def reference_on(ref, transform, shape, crs):
    """The reference resampled bilinearly onto a window of the file's own grid, NaN outside
    its coverage. Bilinear is a local operation on the REFERENCE grid, which is far coarser
    (0.001 deg) than any file it corrects, so striping the destination changes nothing.
    The ~1 m NAD83/WGS 84 horizontal difference is nothing against a separation field that
    changes by metres per hundred kilometres."""
    out = np.full(shape, np.nan, dtype="float32")
    reproject(source=rasterio.band(ref, 1), destination=out,
              src_transform=ref.transform, src_crs=ref.crs, src_nodata=ref.nodata,
              dst_transform=transform, dst_crs=crs, dst_nodata=np.nan,
              resampling=Resampling.bilinear)
    return out


def _stripes(height, width):
    for top in range(0, height, STRIPE_ROWS):
        yield Window(0, top, width, min(STRIPE_ROWS, height - top))


def transform_file(filepath, negate, offset, clamp_positive=False, surface=None):
    """Rewrite one file with the value transform applied. Returns the number of pixels the
    reference actually corrected (0 when no ``surface`` is given)."""
    ref = rasterio.open(surface) if surface else None
    corrected = 0
    try:
        with rasterio.open(filepath) as src:
            profile = src.profile
            if surface and src.crs is None:
                raise ValueError(f"{filepath}: --offset-surface needs the file's own CRS "
                                 "(the reference is resampled onto its grid)")
            crs = horizontal_crs(src.crs) if surface else src.crs
            nodata = profile.get("nodata")
            if clamp_positive and nodata is None:
                raise ValueError(f"{filepath}: --clamp-positive needs a nodata value set")
            profile.update(driver="GTiff", dtype="float32", tiled=True, blockxsize=512,
                           blockysize=512, compress="deflate", crs=crs)
            tmp = filepath + ".datum.tif"
            with rasterio.open(tmp, "w", **profile) as dst:
                for window in _stripes(src.height, src.width):
                    data = src.read(1, window=window).astype("float32")
                    mask = src.read_masks(1, window=window) != 0  # True where valid
                    valid = data[mask]
                    if negate:
                        valid = -valid
                    if offset:
                        valid = valid + np.float32(offset)
                    data[mask] = valid
                    if ref is not None:
                        reference = reference_on(ref, src.window_transform(window),
                                                 data.shape, crs)
                        # A pixel the reference does not cover is passed through at its
                        # uncorrected value: outside VDatum coverage (Hawaii, the Pacific
                        # territories, Alaska off the southeast panhandle) that keeps a
                        # bounded, documented datum bias instead of a hole in shipped data.
                        take = mask & np.isfinite(reference)
                        data[take] -= reference[take]
                        corrected += int(take.sum())
                    if clamp_positive:
                        # After the offset, 0 = water surface; anything > 0 is the surrounding
                        # terrain (a lake DEM's land fringe, or a topobathy playa) — drop it to
                        # nodata so it can't bleed into the water layer as false land.
                        data[mask & (data > 0)] = np.float32(nodata)
                    dst.write(data, 1, window=window)
        os.replace(tmp, filepath)
    finally:
        if ref is not None:
            ref.close()
    return corrected


def write_sidecar(source, negate, offset, clamp_positive, surface=None):
    """Record the applied transform in store/source/<id>/datum.json — the machine-readable
    provenance source_catalog folds into the catalog item (vertical-datum offset was invisible
    downstream when it lived only in this CLI arg). Written whenever the step runs, so a source
    whose recipe calls source_datum always leaves a sidecar."""
    os.makedirs(f"store/source/{source}", exist_ok=True)
    with open(f"store/source/{source}/datum.json", "w") as f:
        json.dump({"negate": bool(negate), "offset_m": float(offset),
                   "clamp_positive": bool(clamp_positive),
                   "offset_surface": surface or None}, f, indent=2)


def main():
    p = argparse.ArgumentParser(description="Apply negate + datum offset to a source's tifs.")
    p.add_argument("source")
    p.add_argument("--negate", action="store_true", help="flip positive-down depth to negative-down elevation")
    p.add_argument("--offset", type=float, default=0.0, help="metres added to reach the target datum")
    p.add_argument("--offset-surface",
                   help="reference raster subtracted per pixel — the target datum's height in "
                        "the source's own vertical frame. A bare name resolves to "
                        f"{DATUM_STORE}/<name>.tif")
    p.add_argument("--clamp-positive", action="store_true",
                   help="after the offset, drop cells > 0 (above the water surface) to nodata — "
                        "removes a lake DEM's land fringe / a topobathy playa")
    a = p.parse_args()

    # Record what this invocation applies even when it's a no-op, so the sidecar exists for
    # every source whose recipe runs source_datum (source_catalog's invariant).
    write_sidecar(a.source, a.negate, a.offset, a.clamp_positive, a.offset_surface)

    if not a.negate and a.offset == 0 and not a.clamp_positive and not a.offset_surface:
        print(f"{a.source}: no datum transform (negate=False, offset=0)")
        return
    surface = surface_path(a.offset_surface) if a.offset_surface else None
    if surface and not os.path.isfile(surface):
        sys.exit(f"{a.source}: offset surface {surface} is not in the store — "
                 "build it with datum_grid.py")
    filepaths = sorted(glob(f"store/source/{a.source}/*.tif"))
    print(f"{a.source}: negate={a.negate} offset={a.offset} surface={a.offset_surface} "
          f"clamp_positive={a.clamp_positive} on {len(filepaths)} file(s)")
    uncorrected = 0
    for filepath in filepaths:
        corrected = transform_file(filepath, a.negate, a.offset, a.clamp_positive, surface)
        if surface and not corrected:
            uncorrected += 1
    if uncorrected:
        print(f"{a.source}: {uncorrected} file(s) outside the reference's coverage — "
              "passed through uncorrected")


def _check():
    """Self-check the value transform on synthetic rasters (no GDAL CLI): negate + offset,
    clamp_positive, and the offset-surface subtract — including the reference-nodata
    pass-through, the source's own nodata staying untouched, and a compound source CRS losing
    its (now wrong) vertical component."""
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

    # A bare name resolves in the datum store; a path is taken as given.
    assert surface_path("navd88_mllw") == f"{DATUM_STORE}/navd88_mllw.tif"
    assert surface_path("/tmp/ref.tif") == "/tmp/ref.tif"

    # offset_surface: a reference covering the west half only, at -1 m (chart datum 1 m below
    # the source's zero). Covered water rises by 1 m, uncovered water is passed through, and
    # the file's own nodata is never touched.
    ref_path = os.path.join(d, "ref.tif")
    ref = np.array([[-1.0, -1.0, -9999.0, -9999.0]] * 4, dtype="float32")
    with rasterio.open(ref_path, "w", driver="GTiff", height=4, width=4, count=1,
                       dtype="float32", crs="EPSG:4326", nodata=-9999.0,
                       transform=from_origin(0.0, 4.0, 1.0, 1.0)) as dst:
        dst.write(ref, 1)
    path3 = os.path.join(d, "t3.tif")
    bed = np.array([[-10.0, -20.0, -30.0, nodata]] * 4, dtype="float32")
    with rasterio.open(path3, "w", driver="GTiff", height=4, width=4, count=1,
                       dtype="float32", crs="EPSG:4326", nodata=nodata,
                       transform=from_origin(0.0, 4.0, 1.0, 1.0)) as dst:
        dst.write(bed, 1)
    corrected = transform_file(path3, negate=False, offset=0.0, surface=ref_path)
    with rasterio.open(path3) as src:
        o3 = src.read(1)
    assert corrected == 8, corrected  # the two western columns of four rows
    assert o3[0, 0] == -9.0 and o3[0, 1] == -19.0, o3  # bed - (-1): shallower
    assert o3[0, 2] == -30.0, o3                       # no reference coverage: unchanged
    assert o3[0, 3] == nodata, o3                      # the file's own nodata: untouched

    # A source whose reference does not reach it at all is reported, not corrupted.
    path4 = os.path.join(d, "t4.tif")
    with rasterio.open(path4, "w", driver="GTiff", height=2, width=2, count=1,
                       dtype="float32", crs="EPSG:4326", nodata=nodata,
                       transform=from_origin(50.0, 4.0, 1.0, 1.0)) as dst:
        dst.write(np.full((2, 2), -5.0, dtype="float32"), 1)
    assert transform_file(path4, False, 0.0, surface=ref_path) == 0
    with rasterio.open(path4) as src:
        assert (src.read(1) == -5.0).all(), src.read(1)

    # A compound source CRS (CUDEM's NAD83 + NAVD88 height) keeps its horizontal half only —
    # the vertical component is a lie once the reference has been subtracted.
    assert horizontal_crs(rasterio.crs.CRS.from_epsg(5498)).to_epsg() == 4269
    assert horizontal_crs(rasterio.crs.CRS.from_epsg(4269)).to_epsg() == 4269

    # Striping is an implementation detail, not a value change: a file taller than one stripe
    # transforms identically to a short one.
    tall = os.path.join(d, "tall.tif")
    rows = STRIPE_ROWS + 3
    with rasterio.open(tall, "w", driver="GTiff", height=rows, width=2, count=1,
                       dtype="float32", crs="EPSG:4326", nodata=nodata,
                       transform=from_origin(0.0, 4.0, 0.001, 0.001)) as dst:
        dst.write(np.full((rows, 2), -10.0, dtype="float32"), 1)
    transform_file(tall, negate=False, offset=-2.0)
    with rasterio.open(tall) as src:
        assert (src.read(1) == -12.0).all(), src.read(1)
    print("source_datum.py self-check ok")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        _check()
    else:
        main()
