"""Apply the bathymetry value transform: ``negate``, ``datum_offset_m``, ``offset_surface``.

Reads the knobs from ``metadata.json``:

  - ``negate``: flip positive-down depth sources (e.g. DDM, stored as +depth) to
    negative-down elevation.
  - ``datum_offset_m``: constant added to bring the source to its target datum (a
    single scalar — right for a lake surface, useless for a tidal separation).
  - ``offset_surface``: a reference raster subtracted per pixel, for the tidal case a
    scalar can't express. The reference is the target datum's height in the SOURCE's own
    vertical frame (``store/datum/navd88_chart.tif``, built by ``datum_grid.py``, is US chart
    datum — MLLW, or CRD in the Columbia River — expressed in NAVD88), so::

        elevation_re_chart_datum = bed - reference

    ``reference`` is negative almost everywhere the US coast is covered, so the bed rises
    and depths get shallower — the bias-shallow-safe direction.

Applied in that order, then ``clamp_positive``. Operates per file in
``store/source/<id>/``, in the source's native CRS (the reprojection to Web Mercator
happens later, in the aggregation stage), preserving nodata/geotransform. Only valid
pixels are transformed, so nodata never gets negated into a spurious depth. Writes a
tiled GeoTIFF; source_normalize makes the final LERC COG.

``flatten_compound_crs`` lives here too: every staged raster loses the vertical half of a
compound CRS, whether or not a value transform applies.

The same pass counts ``dome_candidates`` — NCEI-gridder interpolation domes, isolated at-datum
patches standing in deep water. It rides this stripe loop because the pixels are already in
hand; the count reaches ``datum.json`` -> ``seascape:dome_candidates`` as a source-quality
signal. Counted only: filtering a false shoal is the one edit that charts deeper than the
source, which the bias-shallow rule forbids.
"""

import argparse
import json
import math
import os
import shutil
import sys
from glob import glob

import numpy as np
import rasterio
from pyproj import CRS as ProjCRS
from rasterio.warp import Resampling, reproject
from rasterio.windows import Window
from scipy.ndimage import find_objects, label

import config

DATUM_STORE = "store/datum"

# A single file can be tens of gigapixels (CUDEM ships 8112x8112 tiles, and the reference
# resampled onto one is a second array that size), so the transform runs in row stripes.
STRIPE_ROWS = 512

# Interpolation-dome detector — a source-quality COUNT, never a filter: a flagged pixel keeps
# its value, because removing a false shoal is the one edit that charts deeper than the source.
# NCEI's gridder drapes smooth radial cones over spurious ~0 m soundings inside dredged
# channels; measured on ncei19_n37x00_w076x25_2019v1 they are ~70 m wide, peak +0.09…+0.78 m
# out of −16 m water, and none is charted. What separates them from the real drying rocks the
# pyramid must keep is the SURROUNDINGS: a charted rock belongs to a reef or shore platform, so
# something within a ring of it is shallow, while a dome stands alone in a dredged channel.
DOME_APEX_M = 0.0    # the drying domain's floor — a candidate apex stands at or above chart datum
DOME_SPAN_M = 25.0   # …across no more than this, below chart legibility at any scale
DOME_RING_M = 50.0   # the ring sits outside the cone (measured ~70 m wide, so ~35 m of radius)
# Every ring pixel must be at least this deep. Sits in the measured gap between real drying
# rocks (3.4-7.0 m of water) and the artifacts (10.8-14.5 m).
DOME_DEEP_M = -8.0

M_PER_DEG = 111320.0  # a degree of latitude, for sizing the detector on a geographic grid


def surface_path(name):
    """An ``--offset-surface`` argument -> a readable raster path. A bare name resolves in
    the datum store — the shape ``metadata.json`` declares; anything carrying a separator or
    an extension is used verbatim."""
    if "/" in name or name.endswith((".tif", ".tiff")):
        return name
    return f"{DATUM_STORE}/{name}.tif"


def horizontal_crs(crs):
    """The horizontal half of a compound source CRS (CUDEM tiles declare NAD83 + NAVD88
    height, EPSG:5498), unchanged when it carries no vertical component."""
    if crs is None:
        return crs
    proj = ProjCRS.from_wkt(crs.to_wkt())
    if not proj.is_compound:
        return crs
    return rasterio.crs.CRS.from_wkt(proj.sub_crs_list[0].to_wkt())


def flatten_compound_crs(filepath):
    """Drop a staged raster's vertical CRS — header only, pixels untouched — and report
    whether it changed. Unconditional, never gated on a value transform: a compound vertical
    frame is a lie once the values are datum-corrected, and a materialized warp would apply
    its geoid separation to them."""
    with rasterio.open(filepath) as src:
        crs = src.crs
    flat = horizontal_crs(crs)
    if flat == crs:
        return False
    # Onto a fresh inode, like every other prep step: a bare staged raster is a hardlink to
    # its raw/ download, so an in-place header write would reach through into the verbatim
    # bytes. Upstream ships COGs and normalize rewrites the layout next, so breaking it here
    # costs nothing.
    tmp = filepath + ".crs.tif"
    shutil.copyfile(filepath, tmp)
    with rasterio.open(tmp, "r+", IGNORE_COG_LAYOUT_BREAK="YES") as dst:
        dst.crs = flat
    os.replace(tmp, filepath)
    return True


def assign_declared_crs(filepath, crs):
    """Stamp the source's declared CRS onto a staged raster carrying none — header only, pixels
    untouched. Returns whether it stamped.

    Normalize assigns `crs` at the END of prep, which is too late for ``--offset-surface``: the
    reference is resampled onto the file's OWN grid, and that needs the grid's CRS. A format
    that cannot carry one (ESRI ASCII, the asc-mosaic/asc-tile modes) would otherwise make
    `crs` + `offset_surface` an unusable combination. Same assignment normalize would do
    (`-a_srs`, never a warp), just early enough to be true when the datum step reads it."""
    if not crs:
        return False
    with rasterio.open(filepath) as src:
        if src.crs is not None:
            return False
    # Onto a fresh inode: a bare staged raster is a hardlink to its raw/ download, so an
    # in-place header write would reach through into the verbatim bytes.
    tmp = filepath + ".crs.tif"
    shutil.copyfile(filepath, tmp)
    with rasterio.open(tmp, "r+", IGNORE_COG_LAYOUT_BREAK="YES") as dst:
        dst.crs = rasterio.crs.CRS.from_string(crs)
    os.replace(tmp, filepath)
    return True


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


def pixel_metres(crs, transform, lat):
    """(row, column) pixel size in metres on a file's own grid, or None when its CRS can't be
    sized (no CRS, or a projected one PROJ won't give linear units for). ``lat`` is the file's
    centre latitude, which sets how much a degree of longitude is worth. Anisotropic on purpose:
    at 60 deg N a geographic pixel is twice as wide in rows as in columns, and a metre-shaped
    detector must not become an ellipse."""
    if crs is None:
        return None
    if crs.is_geographic:
        size = (abs(transform.e) * M_PER_DEG,
                abs(transform.a) * M_PER_DEG * math.cos(math.radians(lat)))
    else:
        try:
            factor = crs.linear_units_factor[1]  # upstream grids in US survey feet exist
        except rasterio.errors.CRSError:
            return None
        size = (abs(transform.e) * factor, abs(transform.a) * factor)
    return size if min(size) > 0 else None


def _frame(a, r0, r1, c0, c1):
    """The one-pixel border of ``a[r0:r1, c0:c1]``, flattened."""
    return np.concatenate((a[r0, c0:c1], a[r1 - 1, c0:c1],
                           a[r0 + 1:r1 - 1, c0], a[r0 + 1:r1 - 1, c1 - 1]))


def dome_candidates(data, valid, row_m, col_m, core=None):
    """Count interpolation-dome candidates in ``data`` (elevation, metres, chart datum, negative
    down) — an isolated at-or-above-datum patch under ``DOME_SPAN_M`` across whose whole
    ``DOME_RING_M`` ring is deeper than ``DOME_DEEP_M``.

    ``core`` is the (start, stop) row range this call OWNS; rows outside it are context carried
    from the neighbouring stripes, and a patch is counted by the stripe holding its first row, so
    striping can neither double-count a dome nor split one. A patch whose ring runs off the array
    is skipped rather than judged on half a ring, which leaves a ring-wide blind border at the
    file's own edges (0.7 % of an 8112 px CUDEM tile)."""
    above = valid & (data >= DOME_APEX_M)
    if not above.any() or not (valid & (data <= DOME_DEEP_M)).any():
        return 0
    ring_r, ring_c = max(1, round(DOME_RING_M / row_m)), max(1, round(DOME_RING_M / col_m))
    span_r, span_c = max(1, int(DOME_SPAN_M / row_m)), max(1, int(DOME_SPAN_M / col_m))
    r_start, r_stop = core if core else (0, data.shape[0])
    height, width = data.shape
    count = 0
    labels, _n = label(above, structure=np.ones((3, 3), bool))  # 8-connected: a cone apex is a blob
    for box in find_objects(labels):
        if box is None:
            continue
        rows, cols = box
        if not (r_start <= rows.start < r_stop):
            continue  # another stripe owns it
        if rows.stop - rows.start > span_r or cols.stop - cols.start > span_c:
            continue
        r0, r1 = rows.start - ring_r, rows.stop + ring_r
        c0, c1 = cols.start - ring_c, cols.stop + ring_c
        if r0 < 0 or c0 < 0 or r1 > height or c1 > width:
            continue  # no complete ring to judge it by
        ring_valid = _frame(valid, r0, r1, c0, c1)
        ring = _frame(data, r0, r1, c0, c1)[ring_valid]
        if ring.size * 2 < ring_valid.size:  # a ring mostly outside the survey says nothing
            continue
        if ring.max() <= DOME_DEEP_M:
            count += 1
    return count


def _scan_stripe(lead, held, trail, row_m, col_m):
    """Dome candidates owned by the ``held`` stripe, scored against as much of its neighbours
    (``lead`` before, ``trail`` after) as a patch's ring can reach. Each argument is a
    (values, valid) pair, or None at the file's ends."""
    if held is None:
        return 0
    margin = max(1, round(DOME_RING_M / row_m)) + max(1, int(DOME_SPAN_M / row_m))
    parts = [held]
    if lead is not None:
        parts.insert(0, (lead[0][-margin:], lead[1][-margin:]))
    if trail is not None:
        parts.append((trail[0][:margin], trail[1][:margin]))
    start = parts[0][0].shape[0] if lead is not None else 0
    return dome_candidates(np.vstack([p[0] for p in parts]),
                           np.vstack([p[1] for p in parts]),
                           row_m, col_m, core=(start, start + held[0].shape[0]))


def transform_file(filepath, negate, offset, clamp_positive=False, surface=None,
                   valid_range=None):
    """Rewrite one file with the value transform applied. Returns (pixels the reference
    actually corrected — 0 when no ``surface`` is given, valid pixels in the file,
    interpolation-dome candidates): the first pair's ratio is this file's datum coverage, the
    third is the source-quality count ``dome_candidates`` scores on the transformed values
    (None where the grid can't be sized in metres).

    The dome scan rides this pass rather than adding one of its own, so it needs the
    neighbourhood a stripe cuts through: each stripe is scored one iteration late, between the
    tail of the stripe before it and the head of the one after.

    ``valid_range`` (min, max) voids everything outside the depths the source can physically
    contain. It is the only defence against an upstream shipping impossible values as ORDINARY
    data rather than as its declared nodata: EA multibeam carries -999, -9910, +99, +999 among
    0.5 m soundings that really span -101..+3.2, and at that resolution one of them wins every
    merge it touches. No global sentinel list can catch those safely — -999 m is a real depth
    somewhere — so the band is declared per source, by whoever knows the survey."""
    ref = rasterio.open(surface) if surface else None
    if ref is not None and ref.nodata is not None and np.float32(ref.nodata) != ref.nodata:
        # reference_on honors src_nodata only when the sentinel round-trips float32 exactly
        # (reproject on a band source matches by value; datum_grid.sample_onto documents the
        # -88.8888 failure). A non-exact sentinel would interpolate into the correction as a
        # height, so refuse it here rather than chart the coverage edge wrong.
        raise ValueError(f"{surface}: nodata {ref.nodata} is not float32-exact — "
                         "reference_on would interpolate it into the correction")
    corrected = valid = ranged = 0
    try:
        with rasterio.open(filepath) as src:
            profile = src.profile
            if surface and src.crs is None:
                raise ValueError(f"{filepath}: --offset-surface needs the file's own CRS "
                                 "(the reference is resampled onto its grid)")
            crs = horizontal_crs(src.crs)
            nodata = profile.get("nodata")
            if clamp_positive and nodata is None:
                raise ValueError(f"{filepath}: --clamp-positive needs a nodata value set")
            if valid_range is not None and nodata is None:
                raise ValueError(f"{filepath}: valid_range needs a nodata value set")
            centre = (src.bounds.bottom + src.bounds.top) / 2
            metres = pixel_metres(crs, src.transform, centre)
            domes = 0 if metres else None
            profile.update(driver="GTiff", dtype="float32", tiled=True, blockxsize=512,
                           blockysize=512, compress="deflate", crs=crs)
            tmp = filepath + ".datum.tif"
            with rasterio.open(tmp, "w", **profile) as dst:
                lead = held = None  # the stripe before last's tail, and the stripe awaiting scoring
                for window in _stripes(src.height, src.width):
                    data = src.read(1, window=window).astype("float32")
                    mask = src.read_masks(1, window=window) != 0  # True where valid
                    valid += int(mask.sum())
                    values = data[mask]
                    if negate:
                        values = -values
                    if offset:
                        values = values + np.float32(offset)
                    data[mask] = values
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
                        drop = mask & (data > 0)
                        data[drop] = np.float32(nodata)
                        mask &= ~drop
                    if valid_range is not None:
                        # Last, so the band is read in the source's FINAL frame — the same
                        # metres a chart would show, not the raw values it arrived with.
                        low, high = valid_range
                        out = mask & ((data < np.float32(low)) | (data > np.float32(high)))
                        data[out] = np.float32(nodata)
                        mask &= ~out
                        ranged += int(out.sum())
                    dst.write(data, 1, window=window)
                    if metres:
                        domes += _scan_stripe(lead, held, (data, mask), *metres)
                        lead, held = held, (data, mask)
                if metres:
                    domes += _scan_stripe(lead, held, None, *metres)
        if ranged:
            print(f"{os.path.basename(filepath)}: voided {ranged} px outside "
                  f"valid_range {list(valid_range)}")
        os.replace(tmp, filepath)
    finally:
        if ref is not None:
            ref.close()
    return corrected, valid, domes


# The reference's coverage edge is a smooth line hundreds of km long, so a file crossing it
# loses percent, not per-mille; a sliver this small is a hole or a nodata leak instead.
SLIVER_RATIO = 0.995


def coverage_report(source, per_file):
    """Log per-file datum coverage from [(name, corrected, valid), …] and return the source's
    corrected fraction (None when it has no valid pixels at all).

    A file at 0.0 is outside the reference entirely (Alaska off the SE panhandle, the Pacific
    territories) and a file between 0 and ``SLIVER_RATIO`` straddles its coverage edge — both
    are the documented no-op, reported but never warned about, or every CUDEM boundary tile
    cries wolf."""
    total_corrected = sum(c for _, c, _ in per_file)
    total_valid = sum(v for _, _, v in per_file)
    full = partial = uncovered = 0
    for name, corrected, valid in per_file:
        if not valid:
            continue
        ratio = corrected / valid
        if corrected == valid:
            full += 1
        elif not corrected:
            uncovered += 1
        else:
            partial += 1
            note = f"{source}: {name} {ratio:.4f} corrected"
            if ratio >= SLIVER_RATIO:
                note = (f"WARNING: {note} — a sliver uncorrected, too small for the "
                        "reference's coverage edge; check it for a hole")
            print(note)
    if not total_valid:
        return None
    fraction = total_corrected / total_valid
    print(f"{source}: reference corrected {fraction:.4f} of valid px "
          f"({full} file(s) fully, {partial} partly, {uncovered} outside coverage, "
          f"of {len(per_file)})")
    if not total_corrected:
        print(f"WARNING: {source}: an offset surface is declared but corrected NOTHING — "
              "the source and the reference may not overlap, or the CRS may be wrong")
    return fraction


def dome_report(source, per_file):
    """Log the files carrying interpolation-dome candidates from [(name, domes), …] and return
    the source's total (None when any file went unscored, so "not measured" can't read as
    "clean"). A count, not a verdict: nothing is filtered, and a source can be perfectly good
    and still carry a handful."""
    if any(domes is None for _, domes in per_file):
        return None
    for name, domes in per_file:
        if domes:
            print(f"{source}: {name} {domes} interpolation-dome candidate(s)")
    total = sum(domes for _, domes in per_file)
    hits = sum(1 for _, domes in per_file if domes)
    print(f"{source}: {total} interpolation-dome candidate(s) in {hits}/{len(per_file)} file(s)")
    return total


def write_sidecar(source, negate, offset, clamp_positive, surface=None, corrected=None,
                  domes=None, valid_range=None):
    """Record the applied transform in store/source/<id>/datum.json — the machine-readable
    provenance source_catalog folds into the catalog item (vertical-datum offset was invisible
    downstream when it lived only in this CLI arg). Written whenever the step runs, so a source
    whose recipe calls source_datum always leaves a sidecar.

    ``corrected`` is the fraction of the source's valid pixels the reference reached: pixels
    inside one source sit on different datums wherever coverage ends, and the surface name
    alone reads as if all of them moved. ``domes`` is the interpolation-dome candidate count the
    same pass scored, None where it did not run."""
    os.makedirs(f"store/source/{source}", exist_ok=True)
    # Canonical surface name, never a path: the value flows into the catalog as
    # seascape:datum_surface, where "navd88_chart" must compare equal however it was invoked.
    name = os.path.splitext(os.path.basename(surface))[0] if surface else None
    with open(f"store/source/{source}/datum.json", "w") as f:
        json.dump({"negate": bool(negate), "offset_m": float(offset),
                   "clamp_positive": bool(clamp_positive),
                   "offset_surface": name,
                   "valid_range": None if valid_range is None else [float(v) for v in valid_range],
                   "corrected_fraction": None if corrected is None else round(corrected, 4),
                   "dome_candidates": None if domes is None else int(domes)},
                  f, indent=2)


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
    p.add_argument("--valid-range", nargs=2, type=float, metavar=("MIN", "MAX"),
                   help="void everything outside [MIN, MAX] in the source's final frame — for "
                        "an upstream shipping impossible depths as ordinary data")
    a = p.parse_args()

    # Record what this invocation applies even when it's a no-op, so the sidecar exists for
    # every source whose recipe runs source_datum (source_catalog's invariant).
    write_sidecar(a.source, a.negate, a.offset, a.clamp_positive, a.offset_surface,
                  valid_range=a.valid_range)

    if (not a.negate and a.offset == 0 and not a.clamp_positive and not a.offset_surface
            and not a.valid_range):
        print(f"{a.source}: no datum transform (negate=False, offset=0)")
        return
    surface = surface_path(a.offset_surface) if a.offset_surface else None
    if surface and not os.path.isfile(surface):
        sys.exit(f"{a.source}: offset surface {surface} is not in the store — "
                 f"build it with {config.datum_builder(a.offset_surface)}")
    filepaths = sorted(glob(f"store/source/{a.source}/*.tif"))
    print(f"{a.source}: negate={a.negate} offset={a.offset} surface={a.offset_surface} "
          f"clamp_positive={a.clamp_positive} on {len(filepaths)} file(s)")
    per_file = []
    for filepath in filepaths:
        per_file.append((os.path.basename(filepath),
                         *transform_file(filepath, a.negate, a.offset, a.clamp_positive,
                                         surface, a.valid_range)))
    write_sidecar(a.source, a.negate, a.offset, a.clamp_positive, a.offset_surface,
                  coverage_report(a.source, [r[:3] for r in per_file]) if surface else None,
                  dome_report(a.source, [(r[0], r[3]) for r in per_file]))


def _check():
    """Self-check the value transform on synthetic rasters (no GDAL CLI): negate + offset,
    clamp_positive, and the offset-surface subtract — including the reference-nodata
    pass-through, the source's own nodata staying untouched, the corrected/valid pair each
    file reports, the coverage report built from those pairs, and the unconditional
    compound-CRS reduction (standalone, and through transform_file's no-surface path).

    Then the dome detector on planted fixtures: the same cone is a candidate in 16 m of water and
    not in 5 m, nor when a reef crosses its ring; a mound above chart legibility and a drying flat
    never are; a ring mostly outside the survey is not judged; and striping — including two domes
    straddling a stripe boundary — leaves the count identical to a whole-array scan."""
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

    # valid_range: the upstream ladder (-999/-9910/+99/+999) voids, real soundings survive, and
    # BOTH bounds are inclusive — a value exactly on the bound is inside the band, not outside.
    path3 = os.path.join(d, "t3.tif")
    arr3 = np.array([[-9910.0, -999.0, -101.0, -0.5],
                     [999.0, 99.0, 3.2, nodata],
                     [-200.0, 10.0, -200.001, 10.001]], dtype="float32")
    with rasterio.open(path3, "w", driver="GTiff", height=3, width=4, count=1,
                       dtype="float32", nodata=nodata, crs="EPSG:4326",
                       transform=from_origin(0, 3, 1, 1)) as dst:
        dst.write(arr3, 1)
    transform_file(path3, negate=False, offset=0.0, valid_range=(-200.0, 10.0))
    with rasterio.open(path3) as src:
        o3 = src.read(1)
    assert o3[0, 0] == nodata and o3[0, 1] == nodata, o3      # -9910, -999 voided
    assert o3[0, 2] == -101.0 and o3[0, 3] == -0.5, o3        # the real distribution survives
    assert o3[1, 0] == nodata and o3[1, 1] == nodata, o3      # +999, +99 voided
    assert o3[1, 2] == np.float32(3.2) and o3[1, 3] == nodata, o3
    assert o3[2, 0] == -200.0 and o3[2, 1] == 10.0, o3        # both bounds inclusive
    assert o3[2, 2] == nodata and o3[2, 3] == nodata, o3      # just outside either bound voids

    # The band is read in the source's FINAL frame, so it composes with the other knobs rather
    # than racing them: negated depth lands inside a band expressed as elevation.
    path4 = os.path.join(d, "t4.tif")
    with rasterio.open(path4, "w", driver="GTiff", height=1, width=2, count=1,
                       dtype="float32", nodata=nodata, crs="EPSG:4326",
                       transform=from_origin(0, 1, 1, 1)) as dst:
        dst.write(np.array([[50.0, 9999.0]], dtype="float32"), 1)  # positive-down depths
    transform_file(path4, negate=True, offset=0.0, valid_range=(-200.0, 10.0))
    with rasterio.open(path4) as src:
        o4 = src.read(1)
    assert o4[0, 0] == -50.0 and o4[0, 1] == nodata, o4

    # A bare name resolves in the datum store; a path is taken as given.
    assert surface_path("navd88_chart") == f"{DATUM_STORE}/navd88_chart.tif"
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
    corrected, valid, _domes = transform_file(path3, negate=False, offset=0.0, surface=ref_path)
    with rasterio.open(path3) as src:
        o3 = src.read(1)
    assert corrected == 8, corrected  # the two western columns of four rows
    assert valid == 12, valid         # three columns of four rows; the fourth is nodata
    assert o3[0, 0] == -9.0 and o3[0, 1] == -19.0, o3  # bed - (-1): shallower
    assert o3[0, 2] == -30.0, o3                       # no reference coverage: unchanged
    assert o3[0, 3] == nodata, o3                      # the file's own nodata: untouched

    # A source whose reference does not reach it at all is reported, not corrupted.
    path4 = os.path.join(d, "t4.tif")
    with rasterio.open(path4, "w", driver="GTiff", height=2, width=2, count=1,
                       dtype="float32", crs="EPSG:4326", nodata=nodata,
                       transform=from_origin(50.0, 4.0, 1.0, 1.0)) as dst:
        dst.write(np.full((2, 2), -5.0, dtype="float32"), 1)
    assert transform_file(path4, False, 0.0, surface=ref_path) == (0, 4, 0)
    with rasterio.open(path4) as src:
        assert (src.read(1) == -5.0).all(), src.read(1)

    # coverage_report: a source's fraction is corrected/valid over its files, an all-nodata
    # file contributes nothing, and a source the reference never reached returns 0.0.
    assert coverage_report("s", [("a.tif", 8, 12), ("b.tif", 0, 4), ("c.tif", 0, 0)]) == 0.5
    assert coverage_report("s", [("a.tif", 0, 4)]) == 0.0
    assert coverage_report("s", [("a.tif", 0, 0)]) is None
    assert coverage_report("s", [("a.tif", 4, 4)]) == 1.0

    # …and it warns only where a warning is assertable: a sliver (a hole's signature) and a
    # source the reference never reached. A boundary tile and an out-of-coverage tile are the
    # documented no-op — CUDEM has thousands, so warning there would train the eye to ignore it.
    def logged(per_file):
        import contextlib
        import io as _io
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            coverage_report("s", per_file)
        return buf.getvalue()

    assert "WARNING" in logged([("a.tif", 9970, 10000)]), "a 0.997 sliver must warn"
    assert "WARNING" in logged([("a.tif", 0, 10000)]), "a source corrected nothing must warn"
    assert "WARNING" not in logged([("a.tif", 9000, 10000), ("b.tif", 0, 10000)])
    assert "0.9000 corrected" in logged([("a.tif", 9000, 10000)]), "partial ratio is reported"

    # A compound source CRS (CUDEM's NAD83 + NAVD88 height) keeps its horizontal half only.
    assert horizontal_crs(rasterio.crs.CRS.from_epsg(5498)).to_epsg() == 4269
    assert horizontal_crs(rasterio.crs.CRS.from_epsg(4269)).to_epsg() == 4269
    assert horizontal_crs(None) is None

    # The reduction is unconditional, not a side effect of a transform: a compound file with
    # NO transform to apply still comes out 2D, with its values and nodata bit-identical.
    compound = os.path.join(d, "compound.tif")
    vals = np.array([[-12.5, 3.25], [nodata, 0.0]], dtype="float32")
    with rasterio.open(compound, "w", driver="GTiff", height=2, width=2, count=1,
                       dtype="float32", nodata=nodata, crs=rasterio.crs.CRS.from_epsg(5498),
                       transform=from_origin(0, 2, 1, 1)) as dst:
        dst.write(vals, 1)
    assert flatten_compound_crs(compound) is True
    with rasterio.open(compound) as src:
        assert src.crs.to_epsg() == 4269, src.crs
        assert (src.read(1) == vals).all(), src.read(1)
        assert src.nodata == nodata
    assert flatten_compound_crs(compound) is False  # already 2D: nothing to rewrite

    # …and transform_file reduces it too, even on the no-surface path.
    compound2 = os.path.join(d, "compound2.tif")
    with rasterio.open(compound2, "w", driver="GTiff", height=2, width=2, count=1,
                       dtype="float32", nodata=nodata, crs=rasterio.crs.CRS.from_epsg(5498),
                       transform=from_origin(0, 2, 1, 1)) as dst:
        dst.write(vals, 1)
    transform_file(compound2, negate=False, offset=-1.0)
    with rasterio.open(compound2) as src:
        assert src.crs.to_epsg() == 4269, src.crs
        assert src.read(1)[0, 0] == -13.5 and src.read(1)[1, 0] == nodata, src.read(1)

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

    # ── interpolation domes ──
    # pixel_metres sizes the detector on the file's own grid: a geographic pixel is anisotropic
    # away from the equator (a degree of longitude shrinks by cos(lat)), a projected one is its
    # linear unit, and a grid with no CRS can't be sized at all.
    geo = pixel_metres(rasterio.crs.CRS.from_epsg(4326), from_origin(0, 0, 1e-4, 1e-4), 60.0)
    assert abs(geo[0] - 11.132) < 1e-3 and abs(geo[1] - 5.566) < 1e-3, geo
    utm = pixel_metres(rasterio.crs.CRS.from_epsg(32618), from_origin(0, 0, 3.0, 3.0), 40.0)
    assert utm == (3.0, 3.0), utm
    assert pixel_metres(None, from_origin(0, 0, 3.0, 3.0), 0.0) is None

    PX = 3.0  # metres, close to CUDEM 1/9 arc-second

    def cone(arr, row, col, radius_m, apex):
        """A smooth radial cone — the shape NCEI's gridder drapes over a spurious sounding."""
        rr, cc = np.ogrid[:arr.shape[0], :arr.shape[1]]
        d = np.hypot(rr - row, cc - col) * PX
        np.maximum(arr, arr + (apex - arr) * np.clip(1 - d / radius_m, 0, 1), out=arr)

    # A 70 m cone peaking +0.5 m out of 16 m of water is flagged; the SAME cone in 5 m of water
    # (a drying rock on a shore platform) and one whose ring clips a −3 m reef (the Carmel Bay
    # shape) are not — the ring, not the apex, is what separates an artifact from a charted rock.
    for bed, reef, want in ((-16.0, None, 1), (-5.0, None, 0), (-16.0, -3.0, 0)):
        field = np.full((200, 200), bed, dtype="float32")
        if reef is not None:
            field[100 + int(40 / PX), :] = reef  # a ridge crossing the ring 40 m away
        cone(field, 100, 100, 70.0, 0.5)
        got = dome_candidates(field, np.ones(field.shape, bool), PX, PX)
        assert got == want, (bed, reef, got)

    # Size and isolation: a 60 m mound (above chart legibility) and a whole drying flat are not
    # dome candidates however deep the water around them.
    big = np.full((200, 200), -16.0, dtype="float32")
    cone(big, 100, 100, 200.0, 0.5)  # 60 m of it stands above datum
    assert dome_candidates(big, np.ones(big.shape, bool), PX, PX) == 0
    flat = np.full((200, 200), -16.0, dtype="float32")
    flat[80:120, 80:120] = 0.5
    assert dome_candidates(flat, np.ones(flat.shape, bool), PX, PX) == 0

    # Nodata is not deep water: a cone at the edge of the survey, whose ring is mostly outside
    # it, is not judged at all.
    edge = np.full((200, 200), -16.0, dtype="float32")
    cone(edge, 100, 100, 70.0, 0.5)
    edge_valid = np.zeros(edge.shape, bool)
    edge_valid[95:, 95:] = True  # the survey stops just short of the apex, in both axes
    assert dome_candidates(edge, edge_valid, PX, PX) == 0

    # Striping must not change the count: four domes on a file taller than three stripes, two of
    # them straddling a stripe boundary (rows 511/512), come out the same through transform_file's
    # carried context as they do from one whole-array scan.
    striped = os.path.join(d, "domes.tif")
    tall_field = np.full((STRIPE_ROWS * 3 + 40, 400), -16.0, dtype="float32")
    for row, col in ((100, 100), (STRIPE_ROWS - 1, 100), (STRIPE_ROWS, 300), (1300, 200)):
        cone(tall_field, row, col, 70.0, 0.5)
    with rasterio.open(striped, "w", driver="GTiff", height=tall_field.shape[0],
                       width=tall_field.shape[1], count=1, dtype="float32", nodata=nodata,
                       crs=rasterio.crs.CRS.from_epsg(32618),
                       transform=from_origin(500000.0, 4000000.0, PX, PX)) as dst:
        dst.write(tall_field, 1)
    whole = dome_candidates(tall_field, np.ones(tall_field.shape, bool), PX, PX)
    assert whole == 4, whole
    assert transform_file(striped, negate=False, offset=0.0)[2] == whole, "striping changed the count"

    # A source is scored only where every file was: an unscored file makes the total None, so
    # "not measured" can never read as "clean".
    assert dome_report("s", [("a.tif", 3), ("b.tif", 0)]) == 3
    assert dome_report("s", [("a.tif", 3), ("b.tif", None)]) is None
    print("source_datum.py self-check ok")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        _check()
    else:
        main()
