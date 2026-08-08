"""Compose ``store/datum/navd88_chart.tif`` — local chart datum as a height in the NAVD88 frame.

A support artifact like the landmask, NOT a ``sources/`` entry (everything under ``sources/``
enters the merge). It is the reference a NAVD88 source is corrected against on the subtract
convention::

    reference = -S       where S = NAVD88 - chart datum  (Mayport 8720211: S = +0.947, ref = -0.947)
    bed_chart = bed_NAVD88 - reference

Chart datum is MLLW over the US coast and CRD (Columbia River Datum) in the Columbia reach,
because that is what NOAA charts each against. ``S > 0`` almost everywhere, so the corrected
bed rises and depths get shallower — the bias-shallow-safe direction; it goes the other way up
a river, where the low-water surface climbs above the geoid with the channel.

Input is NOAA's VDatum grid bundle (public domain), pinned by its dated URL. Per tidal region
the bundle ships ``<REGION>_{tss,mllw}.gtx`` (Float32, nodata -88.8888, longitudes on a 0-360
axis) plus a ``.met`` sidecar holding the authoritative bbox and — load-bearing — the region's
horizontal frame, which selects the composition formula. The GTX driver already converts NOAA's
node registration to GDAL's pixel-is-area corner convention, so no half-pixel correction applies.

Stages: compose S per region on its own mllw grid -> mosaic smallest-bbox-last so the finest
region wins where valid -> bounded nearest-fill of the interior gaps between adjacent regions
-> repaint the Columbia onto CRD -> one 4326 COG. Coverage is US coastal only: Hawaii, Guam,
CNMI, American Samoa and Alaska outside the southeast panhandle have no VDatum grid at all, and
outside coverage the reference is nodata, which ``source_datum --offset-surface`` treats as
"leave the pixel alone".

Publication is gated on two independent checks, because either alone ships a broken surface:
the benchmark stations validate the formula and its sign at 16 points, and ``check_coverage``
validates that all 52 regions are actually in the mosaic and that it is the size it was. The
per-region pixel counts land in ``store/datum/navd88_chart.json`` beside the COG.

Run from pipelines/:
    uv run python datum_grid.py --bundle <extracted-vdatum-dir>   # or omit to fetch the zip
    uv run python datum_grid.py --check
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from glob import glob

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.features import rasterize
from rasterio.transform import Affine
from rasterio.warp import Resampling, reproject
from rasterio.windows import Window, from_bounds

from source_normalize import COG_OPTS

BUNDLE_URL = "https://vdatum.noaa.gov/download/data/vdatum_all_20250917.zip"
STORE = "store/datum"
OUT = f"{STORE}/navd88_chart.tif"

GTX_NODATA = -88.8888  # every VDatum .gtx, all 52 regions
NODATA = -9999.0

# 0.001 deg (~110 m) is the modal native resolution of the 52 regions, so nothing is
# downsampled by more than 2:1; measured p99.9 error of that step is <=1.6 cm against a
# product whose own stated uncertainty is 9 cm.
RES = 0.001

# NAD83(2011) coordinates are referenced to epoch 2010.0 — the epoch the NAD83->ITRF2014
# Helmert must be evaluated at. Off by a decade costs ~1 cm of separation.
NAD83_EPOCH = 2010.0

CRD_DIR = "CRD"  # the bundle's Columbia River Datum override, outside the 52 tidal regions

# Adjacent regions' validity polygons leave interior holes 5-30 km wide (Panama City 5 km,
# Neah Bay 5 km, Sitka 20-30 km). Beyond this the reference stays nodata rather than
# extrapolating a tidal surface into water no region models.
FILL_KM = 20.0
KM_PER_DEG = 111.32  # a degree of latitude; a degree of longitude is shorter, so a fill
                     # radius measured in degrees never reaches further than FILL_KM

# Published (ortho_datum - chart datum) from the CO-OPS metadata API, epoch 1983-2001, converted
# from station-datum feet. Spans all three formula branches, both datums and both signs;
# Boston/San Diego bracket the 1.5 m span that rules out any single scalar.
#     https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations/{id}/datums.json
# CRD stations publish no CRD row: the datum sits at the station's `CRD_OFFSET` value on its own
# station datum, so S = (NAVD88 - CRD_OFFSET) ft x 0.3048. Kalama's 5.95 ft offset is what pins
# that reading — the alternatives it rules out miss there by metres, not centimetres.
BENCHMARKS = [
    # (id, name, lon, lat, published S in metres, chart datum)
    ("8720211", "Mayport FL", -81.4133, 30.4000, +0.948, "MLLW"),
    ("8729108", "Panama City FL", -85.6644, 30.1497, +0.171, "MLLW"),
    ("8779770", "Port Isabel TX", -97.2155, 26.0612, +0.259, "MLLW"),
    ("8443970", "Boston MA", -71.0503, 42.3539, +1.676, "MLLW"),
    ("8665530", "Charleston SC", -79.9236, 32.7808, +0.957, "MLLW"),
    ("9410170", "San Diego CA", -117.1767, 32.7156, +0.131, "MLLW"),
    ("9414290", "San Francisco CA", -122.4659, 37.8063, -0.018, "MLLW"),
    # Astoria is 4 km seaward of the CRD envelope, so it still validates the MLLW composition —
    # and it guards the transition, CRD being defined to merge into MLLW at the river entrance.
    ("9439040", "Astoria OR", -123.7683, 46.2073, -0.064, "MLLW"),
    ("9440569", "Skamokawa WA", -123.4565, 46.2703, -0.381, "CRD"),
    ("9440357", "Kalama WA", -122.8367, 45.9867, -1.088, "CRD"),
    ("9439201", "St Helens OR", -122.7970, 45.8650, -1.305, "CRD"),
    ("9447130", "Seattle WA", -122.3393, 47.6026, +0.713, "MLLW"),
    ("9453220", "Yakutat AK", -139.7334, 59.5485, +0.180, "MLLW"),
    ("9451054", "Port Alexander AK", -134.6470, 56.2467, +0.360, "MLLW"),
    ("9755371", "San Juan PR", -66.1164, 18.4589, +0.232, "MLLW"),
    ("9751639", "Charlotte Amalie VI", -64.9258, 18.3306, +0.116, "MLLW"),
]
# VDatum's own stated uncertainty for NAVD88->MLLW is 9 cm; the tolerance sits inside that.
BENCHMARK_TOL = 0.05

# The 52 tidal regions the pinned bundle ships. `regions()` skips a directory whose .met or
# mllw grid it cannot see, so a renamed member or a half-extracted bundle drops a whole coast
# out of the mosaic — 16 point samples never notice, this does. Re-seed with the bundle URL.
EXPECTED_REGIONS = frozenset({
    "AKglacier00_8301", "AKwhale00_8301", "AKyakutat00_8301", "ALFLgom02_8301",
    "ALmobile02_8301", "CAmontby13_8301", "CAoregon00_8301", "CAsfbay13_8301",
    "CAsfdel00_8301", "CAsouin00_8301", "CAsouthn00_8301", "DEVAemb23_8301",
    "DEdelbay33_8301", "FLGAeastbays31_8301", "FLGAeastshelf41_8301", "FLandrew02_8301",
    "FLapalach01_8301", "FLjoseph03_8301", "FLpensac02_8301", "FLsoicw01_8301",
    "FLsouth12_8301", "FLwest01_8301", "GASCNCsab31_8301", "LAatchaf00_8301",
    "LAmobile02_8301", "LATXintra00_8301", "LATXoffshr00_8301", "MDVAechb11_8301",
    "MDnwchb11_8301", "MENHMAgome23_8301", "NCcoast11_8301", "NCinner11_8301",
    "NJVAmab33_8301", "NJncstemb12_8301", "NJscstemb32_8301", "NYNJhbr34_8301",
    "NYgr8bay34_8301", "ORcoain00_8301", "ORcoast00_8301", "PRVIis00_8301",
    "PRVIof00_8301", "RICTbis44_8301", "TXLAgulf00_8301", "TXcentr00_8301",
    "TXintra00_8301", "TXoffshr00_8301", "VAswchb11_8301", "WAcoast00_8301",
    "WAjdfin00_8301", "WAjdfuca14_8301", "WApugets13_8301", "WCoffsh00_8301",
})

# Valid pixels in the published surface, counted over store/datum/navd88_chart.tif. Composition
# is deterministic under the pinned bundle, so the band absorbs only resampler edge changes
# across GDAL versions; anything that truncates or drops coverage moves it by far more.
EXPECTED_TOTAL_PX = 351_067_891
COVERAGE_TOL = 0.02


# ── bundle ───────────────────────────────────────────────────────────────────────────

def wanted_member(name):
    """Is this zip member one of the surfaces the composition needs? Everything else — the
    other tidal datums, the uncertainty twins, the horizontal shift grids, the bundled JRE
    under ``lib/`` — is 20 of the bundle's 21 GB. Matched case-insensitively against the
    parent directory's name: one shipped file is lowercase-prefixed against its own directory
    (``TXintra00_8301/txintra00_8301_svu_mtl.gtx``), and the same defect could recur."""
    parts = name.split("/")
    if len(parts) < 3 or parts[1] == "lib":
        return False
    if parts[1] == "core":
        return parts[2] in ("geoid18", "geoid12b", "xgeoid20b") and len(parts) > 3
    region, leaf = parts[1].lower(), parts[-1].lower()
    if region == CRD_DIR.lower():
        return leaf in ("crd.gtx", "crd.bnd")
    return leaf in (f"{region}.met", f"{region}_tss.gtx", f"{region}_mllw.gtx")


def harvest(dest):
    """Download the pinned bundle and extract only the wanted members, returning the extracted
    ``vdatum/`` root. The URL's date IS the version pin."""
    os.makedirs(dest, exist_ok=True)
    zip_path = os.path.join(dest, os.path.basename(BUNDLE_URL))
    if not os.path.isfile(zip_path):
        print(f"fetching {BUNDLE_URL}")
        tmp = zip_path + ".part"
        urllib.request.urlretrieve(BUNDLE_URL, tmp)
        os.replace(tmp, zip_path)
    root = os.path.join(dest, "vdatum")
    # A fresh tree per extraction: extracting a new bundle edition over an old tree would
    # silently mix regions from both editions into the composed surface.
    shutil.rmtree(root, ignore_errors=True)
    with zipfile.ZipFile(zip_path) as z:
        members = [m for m in z.namelist() if wanted_member(m)]
        print(f"extracting {len(members)} of {len(z.namelist())} bundle members")
        z.extractall(dest, members=members)
    return root


def read_met(path):
    """A ``.met`` sidecar as a dict. Authoritative for the region's bbox (on the 0-360
    longitude axis) and for ``horz``, the frame that selects the composition formula."""
    out = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                out[key.strip()] = value.strip()
    return out


def surface(region_dir, name):
    """``<REGION>_<name>.gtx`` in a region directory, matched on the exact basename,
    case-insensitively. Exactness matters: ``*_svu_mllw.gtx`` and ``*_mllw_unc.gtx`` are
    uncertainty twins that a loose glob would happily compose into the reference."""
    region = os.path.basename(region_dir.rstrip("/")).lower()
    want = f"{region}_{name}.gtx"
    for path in glob(os.path.join(region_dir, "*.gtx")):
        if os.path.basename(path).lower() == want:
            return path
    return None


def regions(bundle):
    """Every tidal region in the bundle, largest bbox first — the mosaic order that lets the
    smallest (finest) region land last and win. Directories with no ``.met`` (``IGLD85``,
    ``core``, and ``CRD``, which ``crd_override`` handles on its own terms) drop out here."""
    out = []
    for met_path in sorted(glob(os.path.join(bundle, "*", "*.met"))):
        region_dir = os.path.dirname(met_path)
        region_id = os.path.basename(region_dir)
        if not surface(region_dir, "mllw"):
            continue
        met = read_met(met_path)
        west, east = float(met["minlon"]) - 360.0, float(met["maxlon"]) - 360.0
        south, north = float(met["minlat"]), float(met["maxlat"])
        out.append({"id": region_id, "dir": region_dir, "horz": met["horz"],
                    "bbox": (west, south, east, north),
                    "area": (east - west) * (north - south)})
    out.sort(key=lambda r: -r["area"])
    return out


# ── composition ──────────────────────────────────────────────────────────────────────

def separation(horz, prvi, tss, mllw, hybrid_geoid=None, xgeoid=None, delta=None):
    """``S = NAVD88 - MLLW`` in metres, positive up, for one region's samples.

    Every tidal ``.gtx`` is the height of its datum's surface above Local MSL, but *what tss is
    referenced to* differs by frame, and the branch must be read from the region's ``.met``
    ``horz`` field — the two conventions interleave geographically, and guessing costs 0.4-1.2 m,
    part of it toward deeper water.

    - ``NAD83`` frames: tss sits on the hybrid geoid, which by construction IS the NAVD88
      surface, so no geoid term appears.
    - ``IGS14``/``IGS08`` frames: tss sits on the gravimetric xGEOID20B surface in the ITRF
      frame instead, so the chain crosses back through the hybrid geoid and the NAD83->ITRF2014
      ellipsoid-height change. Skipping this over-shallows by up to 1.2 m.
    - PRVI: PRVD02 and VIVD09 are LMSL realizations, so the local-datum-to-LMSL step is ~0 and
      tss is not in the chain at all; NAVD88 is not even defined there.
    """
    if prvi:
        return -mllw
    if horz == "NAD83":
        return tss - mllw
    return (hybrid_geoid + delta - xgeoid) + tss - mllw


def hybrid_geoid_path(bundle, region_id):
    """The NAVD88 hybrid geoid for a region: GEOID18, except in Alaska where GEOID18 has no
    coverage and GEOID12B is what VDatum itself uses."""
    if region_id.startswith("AK"):
        return f"{bundle}/core/geoid12b/g2012ba0.gtx"
    return f"{bundle}/core/geoid18/g2018u0.gtx"


def sample_onto(path, transform, shape):
    """A .gtx resampled bilinearly onto another grid, NaN outside its valid area. Both grids
    are plain lon/lat on the bundle's 0-360 axis, so this is interpolation, not reprojection."""
    dst = np.full(shape, np.nan, dtype="float32")
    with rasterio.open(path) as src:
        data = src.read(1)
        # Masked array, not rasterio.band(): reproject ignores src_nodata on a band source
        # (rasterio 1.4.4 / GDAL 3.10.3), and -88.8888 then interpolates through as a height.
        source = np.ma.masked_where(np.abs(data - GTX_NODATA) <= 1e-3, data)
        reproject(source=source, destination=dst,
                  src_transform=src.transform, src_crs="EPSG:4326",
                  dst_transform=transform, dst_crs="EPSG:4326", dst_nodata=np.nan,
                  resampling=Resampling.bilinear)
    return dst


def ellipsoid_shift(transform, shape, rows_per_chunk=256):
    """NAD83(2011) -> ITRF2014 ellipsoid-height change at h = 0, metres, per pixel: a
    time-dependent Helmert (EPSG:6319 -> EPSG:7912), no grids involved."""
    height, width = shape
    lons = transform.c + (np.arange(width) + 0.5) * transform.a
    lons = np.where(lons > 180.0, lons - 360.0, lons)
    transformer = Transformer.from_crs("EPSG:6319", "EPSG:7912", always_xy=True)
    out = np.empty(shape, dtype="float32")
    for start in range(0, height, rows_per_chunk):
        stop = min(start + rows_per_chunk, height)
        lats = transform.f + (np.arange(start, stop) + 0.5) * transform.e
        flat_lon = np.tile(lons, stop - start)
        flat_lat = np.repeat(lats, width)
        _, _, dz, _ = transformer.transform(flat_lon, flat_lat, np.zeros(flat_lon.size),
                                            np.full(flat_lon.size, NAD83_EPOCH))
        out[start:stop] = np.asarray(dz, dtype="float32").reshape(stop - start, width)
    return out


def compose(bundle, region):
    """One region's reference surface (``-S``) on its own mllw grid, plus that grid's
    transform with longitudes moved off the bundle's 0-360 axis. The mllw grid is the frame of
    record: several regions ship tss on a larger shared grid than their own mllw extent."""
    with rasterio.open(surface(region["dir"], "mllw")) as src:
        mllw = src.read(1).astype("float32")
        transform, shape = src.transform, src.shape
    valid = np.abs(mllw - GTX_NODATA) > 1e-3
    prvi = region["id"].startswith("PRVI")

    tss = hybrid = xgeoid = delta = None
    if not prvi:
        tss = sample_onto(surface(region["dir"], "tss"), transform, shape)
        valid &= np.isfinite(tss)
        if region["horz"] != "NAD83":
            hybrid = sample_onto(hybrid_geoid_path(bundle, region["id"]), transform, shape)
            xgeoid = sample_onto(f"{bundle}/core/xgeoid20b/conuspac.gtx", transform, shape)
            delta = ellipsoid_shift(transform, shape)
            valid &= np.isfinite(hybrid) & np.isfinite(xgeoid)

    with np.errstate(invalid="ignore"):
        S = separation(region["horz"], prvi, tss, mllw, hybrid, xgeoid, delta)
    reference = np.where(valid, -S, NODATA).astype("float32")
    if transform.c > 180.0:  # no region straddles the meridian, so shifting the origin is all
        transform = Affine(transform.a, 0.0, transform.c - 360.0,
                           0.0, transform.e, transform.f)
    return reference, transform


def write_region(reference, transform, path):
    height, width = reference.shape
    profile = dict(driver="GTiff", height=height, width=width, count=1, dtype="float32",
                   crs="EPSG:4326", nodata=NODATA, transform=transform,
                   tiled=True, blockxsize=512, blockysize=512, compress="deflate",
                   predictor=3, BIGTIFF="IF_SAFER")
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(reference, 1)


# ── mosaic + fill ────────────────────────────────────────────────────────────────────

def snap_bounds(boxes, res):
    """The union of region bboxes, snapped outward onto the output lattice."""
    west = np.floor(min(b[0] for b in boxes) / res) * res
    south = np.floor(min(b[1] for b in boxes) / res) * res
    east = np.ceil(max(b[2] for b in boxes) / res) * res
    north = np.ceil(max(b[3] for b in boxes) / res) * res
    return west, south, east, north


def mosaic(region_tifs, bounds, out_path, res=RES):
    """Warp each region into one sparse grid, in the order given. gdalwarp into an existing
    destination writes only where the source has data, so a later region wins in the overlap
    and earlier coverage falls through the holes — pass regions largest-bbox first and the
    finest region lands on top. Region bboxes overlap by hundreds of km, so this ordering is
    doing real work.

    ``bilinear`` throughout: no region is downsampled by more than 2:1 (where bilinear on an
    aligned grid IS the 2x2 average), and unlike gdalwarp's ``average`` it does not dilate a
    region's footprint by a pixel past its own validity edge."""
    west, south, east, north = bounds
    width = int(round((east - west) / res))
    height = int(round((north - south) / res))
    profile = dict(driver="GTiff", height=height, width=width, count=1, dtype="float32",
                   crs="EPSG:4326", nodata=NODATA,
                   transform=Affine(res, 0.0, west, 0.0, -res, north),
                   tiled=True, blockxsize=512, blockysize=512, BIGTIFF="YES",
                   SPARSE_OK="YES")
    with rasterio.open(out_path, "w", **profile):
        pass  # SPARSE_OK: blocks materialize only where a region writes into them
    for i, path in enumerate(region_tifs, 1):
        # No -dstnodata: on an existing destination it re-initializes the target window and
        # would erase the regions already mosaicked underneath.
        subprocess.run(["gdalwarp", "-q", "-r", "bilinear", path, out_path], check=True)
        print(f"  mosaic {i}/{len(region_tifs)} {os.path.basename(path)}")
    return width, height


def nearest_fill(values, valid, max_px):
    """Bounded nearest-neighbour fill: an invalid cell within ``max_px`` of a valid one takes
    that neighbour's value, everything further stays invalid. Returns (filled, filled_valid).
    Nearest is adequate — the separation field changes by metres per hundred kilometres."""
    from scipy.ndimage import distance_transform_edt
    distance, indices = distance_transform_edt(~valid, return_indices=True)
    filled = values[tuple(indices)]
    ok = valid | (distance <= max_px)
    return np.where(ok, filled, NODATA).astype("float32"), ok


def fill_holes(path, res=RES, fill_km=FILL_KM):
    """Close the interior gaps between adjacent regions' validity polygons, in place. Returns
    (mosaicked valid px, px the fill added).

    Two scales, because the mosaic is billions of pixels and a distance transform over it is
    not affordable: solve the fill on a ~1 km grid (where a smooth metre-scale field loses
    nothing) and paste it back only into the fine grid's holes."""
    from scipy.ndimage import map_coordinates

    coarse_res = res * 10
    coarse_path = path + ".coarse.tif"
    subprocess.run(["gdalwarp", "-q", "-overwrite", "-tr", str(coarse_res), str(coarse_res),
                    "-r", "average", path, coarse_path], check=True)
    with rasterio.open(coarse_path) as src:
        coarse = src.read(1)
        coarse_transform = src.transform
    coarse_valid = coarse != NODATA
    # A radius in degrees of latitude is never longer than the same number of degrees of
    # longitude on the ground, so measuring the bound this way can only under-reach.
    max_px = fill_km / KM_PER_DEG / coarse_res
    filled, filled_valid = nearest_fill(coarse.astype("float32"), coarse_valid, max_px)
    print(f"  fill: {int(coarse_valid.sum())} -> {int(filled_valid.sum())} coarse cells "
          f"(<= {fill_km:g} km)")

    patched = 0
    valid = 0
    inverse = ~coarse_transform
    with rasterio.open(path, "r+") as dst:
        for _, window in dst.block_windows(1):
            block = dst.read(1, window=window)
            holes = block == NODATA
            valid += block.size - int(holes.sum())
            if not holes.any():
                continue
            rows, cols = np.mgrid[window.row_off:window.row_off + window.height,
                                  window.col_off:window.col_off + window.width]
            xs, ys = dst.transform * (cols + 0.5, rows + 0.5)
            cx, cy = inverse * (xs, ys)
            coords = np.stack([cy - 0.5, cx - 0.5])
            values = map_coordinates(filled, coords, order=1, mode="nearest")
            ok = map_coordinates(filled_valid.astype("float32"), coords, order=1,
                                 mode="nearest") > 0.999
            take = holes & ok
            if not take.any():
                continue
            block[take] = values[take]
            dst.write(block, 1, window=window)
            patched += int(take.sum())
    os.remove(coarse_path)
    print(f"  fill: patched {patched} fine pixels")
    # Counted while the fill already had every block decompressed — a rescan of the
    # billions-of-pixel COG just for this number is not affordable.
    return valid, patched


# ── Columbia River Datum override ────────────────────────────────────────────────────

def read_bnd(path):
    """A VDatum ``.bnd`` validity polygon as a lon/lat ring. Two leading columns per line;
    trailing flag columns and the blank lines that pad the file are ignored."""
    ring = []
    with open(path) as f:
        for line in f:
            fields = line.split()
            if len(fields) >= 2:
                ring.append((float(fields[0]), float(fields[1])))
    return ring


def crd_override(bnd_path, gtx_path, path, res=RES, fill_km=FILL_KM):
    """Repaint the Columbia River reach onto CRD (Columbia River Datum), in place.

    NOAA charts the Columbia against CRD, not MLLW. The two are equal by construction at the
    river entrance and diverge upstream to 0.67 m by Longview, with MLLW the deeper — so
    correcting the river to MLLW charts it deeper than its own datum, the unsafe direction.

    ``crd.gtx`` needs no composition: its values ARE the CRD surface's height above NAVD88,
    which is already this file's reference convention. Verified against the three CO-OPS
    stations publishing both NAVD88 and ``CRD_OFFSET`` (residuals <= 3.2 cm) and, at the mouth,
    against Astoria's MLLW (0.8 mm) — see the ``BENCHMARKS`` table.

    Returns (px now on CRD, px the repaint newly covered).

    ``CRD.bnd`` is the datum boundary, NOT the grid's nodata footprint: ``crd.gtx`` carries
    values ~50 km seaward of the envelope out over the open Pacific, where MLLW is what charts
    use. Inside the envelope CRD wins outright — no MLLW value survives there — and the grid's
    upland holes are nearest-filled from CRD's own channel values rather than left on MLLW,
    which would step by up to 0.67 m at the water's edge. Run after ``fill_holes`` so neither
    datum can be smeared across the boundary by a later fill.
    """
    ring = read_bnd(bnd_path)
    west, east = min(x for x, _ in ring), max(x for x, _ in ring)
    south, north = min(y for _, y in ring), max(y for _, y in ring)
    with rasterio.open(path, "r+") as dst:
        box = from_bounds(west, south, east, north, dst.transform)
        # A pixel of margin so rasterizing the ring can't clip against the window edge, then
        # clamped: an envelope reaching the mosaic's own edge would otherwise index off it.
        col = max(0, int(np.floor(box.col_off)) - 1)
        row = max(0, int(np.floor(box.row_off)) - 1)
        window = Window(col, row,
                        min(dst.width - col, int(np.ceil(box.col_off + box.width)) + 1 - col),
                        min(dst.height - row, int(np.ceil(box.row_off + box.height)) + 1 - row))
        transform = dst.window_transform(window)
        shape = (int(window.height), int(window.width))
        inside = rasterize([({"type": "Polygon", "coordinates": [ring]}, 1)], out_shape=shape,
                           transform=transform, fill=0, dtype="uint8").astype(bool)
        # crd.gtx is on the bundle's 0-360 longitude axis; meet it there. Downsampling its
        # 0.0002 deg to the output grid is safe undersampling: a river datum's slope is ~1 cm/km,
        # so the field moves ~1 mm across an output pixel.
        crd = sample_onto(gtx_path, Affine(transform.a, 0.0, transform.c + 360.0,
                                           0.0, transform.e, transform.f), shape)
        valid = np.isfinite(crd)
        filled, ok = nearest_fill(np.where(valid, crd, NODATA).astype("float32"), valid,
                                  fill_km / KM_PER_DEG / res)
        block = dst.read(1, window=window)
        take = inside & ok
        added = int((take & (block == NODATA)).sum())
        block[take] = filled[take]
        dst.write(block, 1, window=window)
    print(f"  CRD: {int(take.sum())} px on Columbia River Datum ({added} newly covered)")
    return int(take.sum()), added


def build(bundle, out=OUT):
    # Keyed to the output so concurrent surface builds (Phase 2 adds more grids) can't
    # share — and race on — one scratch tree.
    work = out + ".work"
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

    found = regions(bundle)
    if not found:
        sys.exit(f"no VDatum tidal regions under {bundle}")
    print(f"composing {len(found)} regions")
    tifs, boxes, region_px = [], [], {}
    for i, region in enumerate(found, 1):
        reference, transform = compose(bundle, region)
        path = os.path.join(work, f"{region['id']}.tif")
        write_region(reference, transform, path)
        tifs.append(path)
        boxes.append(region["bbox"])
        region_px[region["id"]] = int((reference != NODATA).sum())
        print(f"  {i}/{len(found)} {region['id']} ({region['horz']}) "
              f"{region_px[region['id']]} valid px")

    bounds = snap_bounds(boxes, RES)
    merged = os.path.join(work, "mosaic.tif")
    width, height = mosaic(tifs, bounds, merged)
    print(f"mosaic {width}x{height} at {RES} deg, bounds {bounds}")
    mosaic_px, filled_px = fill_holes(merged)
    crd = os.path.join(bundle, CRD_DIR)
    crd_px, crd_added = crd_override(os.path.join(crd, "CRD.bnd"),
                                     os.path.join(crd, "crd.gtx"), merged)
    valid = mosaic_px + filled_px + crd_added
    # Per-region counts are on each region's own grid, so they overlap and out-total the
    # mosaic; what they pin is that every region reached the mosaic with data.
    report = {"regions": region_px, "mosaic_px": mosaic_px, "filled_px": filled_px,
              "crd_px": crd_px, "total_px": valid, "res": RES, "fill_km": FILL_KM,
              "bounds": list(bounds), "size": [width, height], "bundle": BUNDLE_URL}

    tmp = out + ".tmp.tif"
    # OVERVIEWS=NONE: nothing reads a pyramid of the reference (reference_on and the checks
    # read full res), and COG_OPTS no longer suppresses overviews itself.
    subprocess.run(["gdal_translate", "-of", "COG", *COG_OPTS, "-co", "OVERVIEWS=NONE",
                    "-co", "PREDICTOR=3",
                    merged, tmp], check=True)
    # Gate publication on the benchmark stations AND on coverage: a formula/mosaic/input
    # regression must fail the build here, never ship a surface that silently mis-corrects
    # (or silently fails to correct) every CUDEM depth.
    check_benchmarks(tmp)
    check_coverage(report)
    os.replace(tmp, out)
    with open(report_path(out), "w") as f:
        json.dump(report, f, indent=2)
    shutil.rmtree(work, ignore_errors=True)
    print(f"{out}: {width}x{height}, {valid} valid px, "
          f"{os.path.getsize(out) / 1e6:.1f} MB")


# ── verification ─────────────────────────────────────────────────────────────────────

def sample_bilinear(path, lon, lat):
    """One point out of a 4326 raster, bilinear on pixel centres. None if any contributing
    pixel is nodata."""
    with rasterio.open(path) as src:
        col, row = ~src.transform * (lon, lat)
        col, row = col - 0.5, row - 0.5
        col0, row0 = int(np.floor(col)), int(np.floor(row))
        fx, fy = col - col0, row - row0
        cell = src.read(1, window=Window(col0, row0, 2, 2), boundless=True,
                        fill_value=src.nodata).astype("float64")
        if np.any(cell == src.nodata):
            return None
    top = cell[0, 0] * (1 - fx) + cell[0, 1] * fx
    bottom = cell[1, 0] * (1 - fx) + cell[1, 1] * fx
    return top * (1 - fy) + bottom * fy


def report_path(out=OUT):
    """The coverage sidecar beside a composed surface."""
    return os.path.splitext(out)[0] + ".json"


def valid_px(path):
    """Valid pixels in a composed surface. The COG is sparse — the ~90% of blocks no region
    wrote are never materialized — so counting the published grid costs a few seconds."""
    with rasterio.open(path) as src:
        return sum(int(np.count_nonzero(src.read(1, window=window) != src.nodata))
                   for _, window in src.block_windows(1))


def check_total_px(total, tol=COVERAGE_TOL):
    """The surface is the size it was. Truncated bounds, a fill that stopped filling, or a
    region composing to a fraction of itself all land here and nowhere else."""
    if not EXPECTED_TOTAL_PX * (1 - tol) <= total <= EXPECTED_TOTAL_PX * (1 + tol):
        raise AssertionError(f"surface holds {total} valid px, expected {EXPECTED_TOTAL_PX} "
                             f"+/-{tol:.0%} — coverage changed, not just its values")
    return total


def check_coverage(report, tol=COVERAGE_TOL):
    """Every bundle region reached the mosaic, and the mosaic is the size it was.

    The station checks sample 16 points: a region that fell out of the bundle glob, a region
    that composed to nothing, or a rebuild that truncated the mosaic moves none of them, and
    the resulting hole is a coast charted on the wrong datum. Names what is missing, because
    the fix (which region, which grid) is otherwise a bundle-wide hunt."""
    composed = report["regions"]
    missing = sorted(EXPECTED_REGIONS - set(composed))
    unknown = sorted(set(composed) - EXPECTED_REGIONS)
    empty = sorted(r for r, px in composed.items() if not px)
    if missing:
        raise AssertionError(f"{len(missing)} VDatum region(s) absent from the composition: "
                             f"{', '.join(missing)}")
    if unknown:
        raise AssertionError(f"region(s) EXPECTED_REGIONS does not list: {', '.join(unknown)} "
                             "— reseed it and EXPECTED_TOTAL_PX against the new bundle")
    if empty:
        raise AssertionError(f"region(s) composed 0 valid px: {', '.join(empty)}")
    total = check_total_px(report["total_px"], tol)
    print(f"coverage ok ({len(composed)} regions, {total} valid px, "
          f"{total / EXPECTED_TOTAL_PX - 1:+.2%} vs expected)")


def check_benchmarks(path=OUT, tol=BENCHMARK_TOL):
    """The composed reference against published CO-OPS separations. The sign is the point: the
    file holds -S, so a station's separation reads back as the negated sample. Each station is
    checked against the datum its own chart uses, which is also what fixes the CRD envelope's
    edge in place — Astoria must keep reading MLLW, Skamokawa must have crossed to CRD."""
    worst = 0.0
    for station, name, lon, lat, published, datum in BENCHMARKS:
        value = sample_bilinear(path, lon, lat)
        if value is None:
            raise AssertionError(f"{name} ({station}): no reference coverage at {lon},{lat}")
        got = -value
        residual = got - published
        worst = max(worst, abs(residual))
        print(f"  {name:22s} {station}  {datum:4s}  S={got:+.4f}  published={published:+.3f}  "
              f"residual={residual:+.4f}")
        assert abs(residual) <= tol, f"{name} ({datum}): |{residual:.4f}| > {tol} m"
    print(f"benchmark stations ok (worst |residual| = {worst:.4f} m)")


def _check():
    """Offline tier: the branch formula, the coverage gate, the mosaic's priority +
    fallthrough, and the bounded fill, on synthetic rasters. The grid-dependent tier runs on
    top whenever a composed surface is present: its counted size, its per-region sidecar if
    one sits beside it, and the benchmark stations."""
    import tempfile
    from rasterio.transform import from_origin

    # Each branch, against the published separation at a station that exercises it. The IGS
    # numbers are the grid samples at Seattle 9447130 (published +0.713).
    assert abs(separation("NAD83", False, 0.1730, -0.7734) - 0.9464) < 1e-4  # Mayport
    assert abs(separation("NAD83", True, None, -0.2290) - 0.2290) < 1e-4     # San Juan (PRVI)
    igs = separation("IGS14", False, -0.1200, -2.02341, hybrid_geoid=-23.6732,
                     xgeoid=-22.8223, delta=-0.32982)
    assert abs(igs - 0.713) < 0.02, igs                                     # Seattle
    # The geoid chain is not optional: dropping it over-shallows by more than a metre.
    assert separation("NAD83", False, -0.1200, -2.02341) - igs > 1.0

    # wanted_member: surfaces in, uncertainty twins and the bundled JRE out, case-robust.
    assert wanted_member("vdatum/FLGAeastbays31_8301/FLGAeastbays31_8301_mllw.gtx")
    assert wanted_member("vdatum/FLGAeastbays31_8301/FLGAeastbays31_8301.met")
    assert wanted_member("vdatum/core/geoid18/g2018u0.gtx")
    assert not wanted_member("vdatum/TXcentr00_8301/TXcentr00_8301_svu_mllw.gtx")
    assert not wanted_member("vdatum/CAsfdel00_8301/CAsfdel00_8301_mllw_unc.gtx")
    assert not wanted_member("vdatum/lib/jre/bin/java")
    assert not wanted_member("vdatum/core/ncla/ncla.gtx")
    assert wanted_member("vdatum/CRD/crd.gtx")
    assert wanted_member("vdatum/CRD/CRD.bnd")
    assert not wanted_member("vdatum/CRD/CRD.kml")

    # check_coverage: a full composition passes; a dropped region, a region that composed to
    # nothing, and a truncated mosaic each fail naming what went wrong.
    full = {"regions": {r: 1_000_000 for r in EXPECTED_REGIONS}, "total_px": EXPECTED_TOTAL_PX}
    check_coverage(full)

    def refuse(report, expect):
        try:
            check_coverage(report)
            assert False, f"expected check_coverage to fail on {expect}"
        except AssertionError as e:
            assert expect in str(e), (expect, e)

    dropped = {**full, "regions": {r: p for r, p in full["regions"].items()
                                   if r != "WApugets13_8301"}}
    refuse(dropped, "WApugets13_8301")
    refuse({**full, "regions": {**full["regions"], "PRVIis00_8301": 0}}, "PRVIis00_8301")
    refuse({**full, "regions": {**full["regions"], "XXnew00_8301": 5}}, "XXnew00_8301")
    refuse({**full, "total_px": int(EXPECTED_TOTAL_PX * 0.8)}, "coverage changed")
    check_coverage({**full, "total_px": int(EXPECTED_TOTAL_PX * 1.01)})  # inside the band

    # nearest_fill: a hole inside the radius takes its neighbour, one outside stays nodata.
    values = np.array([[1.0, 0.0, 0.0, 0.0, 2.0]], dtype="float32")
    valid = np.array([[True, False, False, False, True]])
    filled, ok = nearest_fill(values, valid, max_px=1.0)
    assert ok.tolist() == [[True, True, False, True, True]], ok
    assert filled[0, 1] == 1.0 and filled[0, 3] == 2.0 and filled[0, 2] == NODATA, filled

    tmp = tempfile.mkdtemp()
    cwd = os.getcwd()
    try:
        os.chdir(tmp)

        def synth(name, array, west, north, res):
            height, width = array.shape
            with rasterio.open(name, "w", driver="GTiff", height=height, width=width, count=1,
                               dtype="float32", crs="EPSG:4326", nodata=NODATA,
                               transform=from_origin(west, north, res, res)) as dst:
                dst.write(array.astype("float32"), 1)

        # Priority + fallthrough: a big coarse region with a hole, and a small fine region on
        # top of part of it. The fine values must win where they exist, the coarse must show
        # through everywhere else, and the coarse hole must stay a hole through the mosaic.
        big = np.full((8, 8), -1.0)
        big[7, 7] = NODATA
        synth("big.tif", big, 0.0, 4.0, 0.5)
        small = np.full((8, 8), -2.0)
        synth("small.tif", small, 0.0, 4.0, 0.25)
        bounds = snap_bounds([(0.0, 0.0, 4.0, 4.0)], 0.05)
        mosaic(["big.tif", "small.tif"], bounds, "m.tif", res=0.05)
        with rasterio.open("m.tif") as src:
            merged = src.read(1)
        assert merged.shape == (80, 80), merged.shape
        assert (merged[:40, :40] == -2.0).all(), merged     # smallest bbox landed last, wins
        assert (merged[:10, 50:] == -1.0).all(), merged     # coarse shows through elsewhere
        assert (merged[70:, 70:] == NODATA).all(), merged   # the coarse hole stays a hole

        # The fill closes that hole from its neighbours once the radius reaches it, and
        # leaves the reference untouched where it was already valid.
        fill_holes("m.tif", res=0.05, fill_km=200.0)
        with rasterio.open("m.tif") as src:
            filled_grid = src.read(1)
        assert (filled_grid[70:, 70:] == -1.0).all(), filled_grid[70:, 70:]
        assert (filled_grid[:40, :40] == -2.0).all(), filled_grid[:40, :40]

        # The CRD override, on an envelope covering the mosaic's top-left quadrant only. The
        # river grid spans the WHOLE mosaic, as crd.gtx does out over the open Pacific: the
        # envelope, not the grid's footprint, has to be what stops the repaint.
        with open("crd.bnd", "w") as f:  # lon 0..2, lat 3..4 — rows 0:20, cols 0:40
            f.write("0.0 4.0 1 0\n2.0 4.0 0 0\n\n2.0 3.0 0 0\n0.0 3.0 0 0\n0.0 4.0 1 0\n")
        assert read_bnd("crd.bnd")[:2] == [(0.0, 4.0), (2.0, 4.0)]
        synth("crd.tif", np.full((8, 8), -3.0), 0.0, 4.0, 0.5)
        crd_override("crd.bnd", "crd.tif", "m.tif", res=0.05, fill_km=200.0)
        with rasterio.open("m.tif") as src:
            out = src.read(1)
        assert (out[:20, :40] == -3.0).all(), out[:20, :40]    # envelope: CRD wins outright
        assert (out[:20, 40:] == -1.0).all(), out[:20, 40:]    # beyond it: CRD values ignored
        assert (out[20:40, :40] == -2.0).all(), out[20:40, :40]
        assert (out[70:, 70:] == -1.0).all(), out[70:, 70:]    # the filled hole, untouched

        # An envelope the river grid only partly covers: its holes fill from CRD's own values,
        # never from the MLLW underneath — a datum step at the water's edge, otherwise.
        river = np.full((8, 8), GTX_NODATA)
        river[:, :2] = -4.0                                    # CRD data over lon 0..1 only
        synth("crd2.tif", river, 0.0, 4.0, 0.5)
        crd_override("crd.bnd", "crd2.tif", "m.tif", res=0.05, fill_km=200.0)
        with rasterio.open("m.tif") as src:
            out = src.read(1)
        assert (out[:20, :40] == -4.0).all(), out[:20, :40]
        assert (out[:20, 40:] == -1.0).all(), out[:20, 40:]
        assert (out[20:40, :40] == -2.0).all(), out[20:40, :40]
    finally:
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)

    print("datum_grid.py self-check ok (offline tier)")
    if os.path.isfile(OUT):
        # Count the published grid rather than trusting the build's own bookkeeping: a
        # sidecar written beside a truncated surface would agree with itself.
        counted = check_total_px(valid_px(OUT))
        print(f"{OUT}: {counted} valid px ({counted / EXPECTED_TOTAL_PX - 1:+.2%} vs expected)")
        if os.path.isfile(report_path()):
            with open(report_path()) as f:
                report = json.load(f)
            check_coverage(report)
            assert report["total_px"] == counted, (report["total_px"], counted)
        else:
            print(f"{report_path()} absent — skipping the per-region coverage tier "
                  "(the next build writes it)")
        check_benchmarks()
    else:
        print(f"{OUT} absent — skipping the benchmark-station tier "
              f"(build it with: uv run python datum_grid.py --bundle <dir>)")


def main():
    parser = argparse.ArgumentParser(
        description="Compose the NAVD88->MLLW reference surface from the NOAA VDatum bundle.")
    parser.add_argument("--bundle", help="an already-extracted vdatum/ directory "
                                         "(default: fetch and extract the pinned zip)")
    parser.add_argument("--out", default=OUT)
    args = parser.parse_args()
    # This module composes exactly one surface. The datum_surface rule templates --out from
    # its {name} wildcard, so a second registered surface would silently receive THIS
    # surface's content — refuse any name this build doesn't produce.
    if os.path.basename(args.out) != os.path.basename(OUT):
        sys.exit(f"datum_grid composes {os.path.basename(OUT)}, not "
                 f"{os.path.basename(args.out)} — a new surface needs its own composer")
    bundle = args.bundle or harvest(STORE)
    build(bundle, args.out)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        _check()
    else:
        main()
