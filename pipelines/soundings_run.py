"""Static soundings: shoalest-per-cell point depths off each aggregation tile's merged DEM.

A second consumer of the same smoothed mosaic window that depare_run.py reads. On a real chart,
point soundings are the primary depth cue.

A client-side plugin (maplibre-contour's spot-soundings fork) point-samples one jittered
DEM pixel per grid node, which can miss a shoal between nodes. We own the whole DEM at build
time, so each grid cell emits its SHOALEST wet pixel (hazard-correct) with the depth floored
toward shallower — a charted sounding is never deeper than reality.

A shoalest-wins quadtree tags each point with the coarsest zoom it survives to (tippecanoe
minzoom), so zoomed-out views carry only the shoalest few per area and low-zoom tiles stay
small; the viewer's symbol collision (sorted shallow-first) de-conflicts what remains.

Each sounding sits on the pixel it was read from, never on a synthesized grid node: "Spatial
object must be encoded at the true position. There is no 'sounding, out of position' in an ENC"
(S-57 App B.1 Annex A UOC, Ed 4.4.0, §5.3). Cell size therefore sets the spacing, not the
position, so the field is irregular over irregular ground — S-4 §B-410d's pattern.

Per tile: read merged DEM (3857) in row strips -> a shoalest-wins PYRAMID (one cell grid per zoom
level, each point valued and positioned by the shoalest wet pixel in its cell) -> keep points in
the unbuffered tile bbox -> reproject to 4326 -> store/soundings/{stem}.geojsons (per-feature
tippecanoe minzoom==maxzoom, so each zoom shows one field, densifying inward). The vector
bundle folds these into the `soundings` layer of the sharded variable-depth run (contour_run).

Each field is also thinned by depth (SOUND_THIN_TIERS): a point deeper than a cutoff displays from
a 2^shift coarser level of the same pyramid, so the abyss reads sparse at every zoom while
navigable depths keep full density — chart practice, and the project's navigational depth bands.

The per-stem file is line-delimited GeoJSON (one Feature per line, `.geojsons`), NOT a single
FeatureCollection: the bundle passthrough streams it line by line (no whole-file json.load), so a
parallel/streaming consumer reads one feature at a time regardless of tile size.

Chart-cartography grounding (shoal-bias, the radius method, SCAMIN, spacing rules) and the
seamap fork this mirrors: ../docs/nautical-chart-references.md.
"""

import json
import math
import os
import sys

import mercantile

import config
import landmask
import utils

NODATA = -9999
M_PER_FT = 0.3048
M_PER_FATHOM = 1.8288
# Grid cell in merged-DEM pixels — sets the on-screen sounding spacing (decimation holds it
# ~constant across zoom). A native output tile is ~512 DEM px, so 64 → ~8x8 soundings per tile.
# Smaller = denser everywhere. Env-tunable on a re-run.
SOUND_CELL_PX = int(os.environ.get("SOUND_CELL_PX", "64"))
# Drop soundings shallower than this (metres). S-4 B-410b puts least depths over shoals at the top
# of what must survive selection, so this floor is set as low as the data supports, not as high as
# is convenient. Measured on the four Gulf marsh fixtures (perf/soundings.py floor): admitted cells
# grow smoothly from 1.0 m down to 0.1 m, then jump 2-5x at the 0.1 -> 0.0 step, which is the
# waterline-noise regime. 0.2 m clears that with headroom and is a decimetre the DEM can defend.
SOUND_MIN_DEPTH_M = float(os.environ.get("SOUND_MIN_DEPTH_M", "0.2"))
# Depth-tiered thinning: at/beyond each cutoff a sounding rides a 2^shift coarser lattice at every
# zoom, so deep water reads sparse (chart practice: dense over banks, sparse over the abyss) while
# navigational depths keep full density. "depth_m:shift" pairs. Env-tunable on a re-run.


def _parse_thin_tiers(spec):
    # Sorted so _thin_shift's last-match-wins scan is correct whatever order the env string uses.
    try:
        return sorted((float(d), int(s)) for d, s in
                      (t.split(":") for t in spec.split(",") if t.strip()))
    except ValueError:
        raise SystemExit(f"SOUND_THIN_TIERS {spec!r}: expected comma-separated depth_m:shift pairs, "
                         'e.g. "200:1,1000:2"')


SOUND_THIN_TIERS = _parse_thin_tiers(os.environ.get("SOUND_THIN_TIERS", "200:1,1000:2"))


def _thin_shift(depth_pos):
    s = 0
    for d, k in SOUND_THIN_TIERS:
        if depth_pos >= d:
            s = k
    return s


# Charted levels walked for enclosed shoals, positive-down metres. Bounded to the navigational
# band: past it an enclosed rise is a seabed feature, not a least depth a vessel can touch, and
# each level costs a labelling pass over the window.
SHOAL_RING_MAX_DEPTH_M = 200.0
SHOAL_RING_LEVELS = sorted(-l for l in config.CONTOUR_LEVELS
                           if 0 < -l <= SHOAL_RING_MAX_DEPTH_M)


def _ring_minzoom(level_pos):
    """The zoom a charted level's isobath first draws at (contour_run.CONTOUR_TIERS holds
    cumulative level sets per zoom band, so a level's minzoom is the floor of the first band
    listing it; a level in no band draws only at/above the last ceiling).

    A prime sounding inherits this: the least depth displays exactly where its enclosing ring
    displays. B-410b's "must always be shown" is a claim about the charted feature — once
    generalization drops the ring at a scale, the chart makes no claim there for the sounding to
    complete. Promoting primes past their rings is what put decimetre least depths over the
    generalized coastline of a 1:500k view."""
    from contour_run import CONTOUR_TIERS   # lazy: sibling stage, imported for its display table
    prev = 0
    for ceil_z, lvls in CONTOUR_TIERS:
        if -level_pos in lvls:
            return prev
        prev = ceil_z
    return prev


# ── Error-driven repair (S-4 B-410a as the selector, not just the gate) ─────────────────────
# After the lattice + primes, each display zoom's field is tested by interpolation against the
# DEM and repaired where it lies: a pixel shoaler than the charted field implies is the B-410a
# safety direction (tight tolerance); a pixel much deeper is the B-403.1a "full range of depth"
# direction (loose tolerance — this is what makes a dredged channel's deep line appear, with no
# thalweg extraction — measured on the New York window: 26 insertions at 26-31 m through the
# ~10 m ambient of the Ambrose corridor, including the Narrows scour, from the deep rule alone).
# Insertions land at their measured pixel with the repaired zoom as minzoom,
# so a sounding's minzoom is the coarsest zoom that needed it — significance grading by
# construction. All knobs env-tunable on a re-run; SOUND_REPAIR=0 leaves the field as built.
SOUND_REPAIR = os.environ.get("SOUND_REPAIR", "1") == "1"
# Shoal-side: insert where ground is shoaler than interpolation by more than this (fraction of
# charted depth, with an absolute floor to ride over DEM noise). OFF by default: B-410a's mariner
# interpolates from soundings AND contours, and with the isobaths charted (cut from this same DEM)
# the measured shoal residual at charted precision is 0.08% before any repair — while a
# soundings-only interpolant manufactures shoal "violations" in the tent around every deep
# insertion, cascading pitch-spaced points zoom after zoom (measured: every zoom capped out and
# z13 nearest-neighbour medians of 0.5 px). The shoal side is already guarded by the shoalest-
# per-cell lattice, the prime rings, and the band tints; enable for a source where those fail.
REPAIR_SHOAL = os.environ.get("REPAIR_SHOAL", "0") == "1"
REPAIR_SHOAL_ABS_M = float(os.environ.get("REPAIR_SHOAL_ABS_M", "0.5"))
REPAIR_SHOAL_FRAC = float(os.environ.get("REPAIR_SHOAL_FRAC", "0.10"))
# Deep-side: insert where ground is deeper than interpolation by more than this.
REPAIR_DEEP_ABS_M = float(os.environ.get("REPAIR_DEEP_ABS_M", "2.0"))
REPAIR_DEEP_FRAC = float(os.environ.get("REPAIR_DEEP_FRAC", "0.5"))
# Minimum screen-px spacing (at the zoom being repaired) between an insertion and any sounding
# already displayed there. 0 = no spacing constraint (label collision is the only thinning).
REPAIR_SPACING_PX = float(os.environ.get("REPAIR_SPACING_PX", "8"))
# One pass per zoom, judged against the field as it stood — iterating re-judges against the
# freshly inserted points, and each deep insertion tents the interpolant into shoal-side
# "violations" around itself (and vice versa), a seesaw that piled 3,400 sub-pixel points onto
# the marsh fixture without converging. The cap is a runaway guard, not a tuning knob.
REPAIR_BATCH = int(os.environ.get("REPAIR_BATCH", "400"))
_M_PER_PX_Z0 = 40075016.685578488 / 512   # 3857 metres per 512px-tile screen pixel at z0


# Sounding kinds: how a point entered the field decides its zoom capping and tile properties.
KIND_LATTICE = 0   # capped to its pyramid level, except the finest level
KIND_PRIME = 1     # least depth in a closed isobath: uncapped, carries prime=1
KIND_REPAIR = 2    # B-410a/B-403.1a insertion: uncapped from the zoom that needed it


def _displayed_at(pts, zoom, child_z):
    """Indices of pts displayed at `zoom` under _tc's placement: capped lattice levels show at
    exactly their zoom; primes and repair insertions ride uncapped from their minzoom."""
    out = []
    for i, (_d, _x, _y, mz, kind) in enumerate(pts):
        uncapped = kind != KIND_LATTICE or mz >= child_z
        if (uncapped and zoom >= mz) or (not uncapped and zoom == mz):
            out.append(i)
    return out


def _repair(src, nodata, bbox, pts, z, child_z):
    """Per display zoom, coarse to fine: interpolate the displayed field, find where the DEM
    contradicts it beyond tolerance, insert the offending ground at its measured pixel, repeat.
    Inserted soundings are uncapped from the repaired zoom, so they join every finer field.
    Returns (pts, per-zoom insertion counts)."""
    import numpy as np
    from scipy.interpolate import LinearNDInterpolator
    from rasterio.windows import Window

    transform = src.transform
    W, H = src.width, src.height
    k = max(1, -(-max(W, H) // SHOAL_DETECT_PX))
    small = src.read(1, out_shape=(max(1, H // k), max(1, W // k)))
    wet = (small != nodata) & (small < 0)
    rows, cols = np.nonzero(wet)
    sx, sy = transform * (cols * k + k / 2.0, rows * k + k / 2.0)
    inb = (sx >= bbox.left) & (sx <= bbox.right) & (sy >= bbox.bottom) & (sy <= bbox.top)
    rows, cols, sx, sy = rows[inb], cols[inb], sx[inb], sy[inb]
    actual = -small[rows, cols].astype(float)

    def pin(r, c, shoal, want=None):
        """One small full-res read around decimated pixel (r, c) -> a measured pixel.

        Shoal-side takes the shoalest pixel: understating depth is the safe error. Deep-side
        takes the pixel CLOSEST to the decimated value instead — pinning extremes mints sharp
        control points whose own interpolation tents then violate the opposite tolerance, and
        the repair chases its tail (measured: marsh shoal violations rose 0.08% -> 6.6% while
        3,600 points piled in at sub-pixel pitch)."""
        pad = k + 1
        r0, c0 = max(0, r * k - pad), max(0, c * k - pad)
        r1, c1 = min(H, r * k + k + pad), min(W, c * k + k + pad)
        fine = src.read(1, window=Window(c0, r0, c1 - c0, r1 - r0))
        fwet = (fine != nodata) & (fine < 0)
        if not fwet.any():
            return None
        if shoal:
            pick = np.where(fwet, fine, -np.inf)
            by, bx = np.unravel_index(np.argmax(pick), fine.shape)
        else:
            pick = np.where(fwet, np.abs(-fine - want), np.inf)
            by, bx = np.unravel_index(np.argmin(pick), fine.shape)
        return -float(fine[by, bx]), c0 + bx + 0.5, r0 + by + 0.5

    inserted = {}
    for zoom in range(z, child_z + 1):
        spacing_m = REPAIR_SPACING_PX * _M_PER_PX_Z0 / (1 << zoom)
        idx = _displayed_at(pts, zoom, child_z)
        if len(idx) < 3:
            continue
        fx = np.array([pts[i][1] for i in idx]); fy = np.array([pts[i][2] for i in idx])
        fd = np.array([pts[i][0] for i in idx])
        expected = LinearNDInterpolator(np.column_stack([fx, fy]), fd)(
            np.column_stack([sx, sy]))
        ok = ~np.isnan(expected)
        shoal_err = np.where(ok, expected - actual, -np.inf)   # >0: ground shoaler than charted
        deep_err = np.where(ok, actual - expected, -np.inf)    # >0: ground deeper than charted
        shoal_bad = (shoal_err > np.maximum(REPAIR_SHOAL_ABS_M, REPAIR_SHOAL_FRAC * actual)) \
            if REPAIR_SHOAL else np.zeros_like(shoal_err, dtype=bool)
        deep_bad = (deep_err > np.maximum(REPAIR_DEEP_ABS_M, REPAIR_DEEP_FRAC * actual)) \
            & (actual <= SHOAL_RING_MAX_DEPTH_M)
        # Deep-side repair stops at the navigational band. B-403.1a's "full range of depth"
        # is about navigable water — a channel, an anchorage; abyssal slope texture belongs
        # to contours and the relief. Without the bound the flat-deep control fixture drew
        # 1,018 insertions (99% closer together than a label) bounding 3,000 m slopes.
        err = np.where(shoal_bad, shoal_err, np.where(deep_bad, deep_err, -np.inf))
        order = np.argsort(-err)
        order = order[err[order] > 0]
        if not order.size:
            continue
        # Greedy, worst first: a DEEP insertion must clear the spacing floor against the
        # displayed field and this pass's own insertions — it exists for legibility, so
        # legibility may thin it. A SHOAL insertion is safety data (B-410a): spacing never
        # suppresses it, only the decimation pitch dedupes it, and label collision resolves
        # crowding at render time with the shoalest number winning.
        taken = []
        fxy = np.column_stack([fx, fy])
        pitch = k * abs(transform.a)
        for j in order:
            px_, py_ = sx[j], sy[j]
            floor_m = pitch if shoal_bad[j] else max(spacing_m, pitch)
            # Both sides keep at least the decimation pitch from the existing field: a shoal
            # violation within a pitch of a charted sounding is a near-duplicate of it, not a
            # suppressed hazard, and without this each zoom's pass stacks sub-pixel shoal points
            # around the previous zoom's deep insertions (measured: NN medians of 0.5 px at z13).
            if np.hypot(fxy[:, 0] - px_, fxy[:, 1] - py_).min() < floor_m:
                continue
            if any((px_ - tx) ** 2 + (py_ - ty) ** 2 < floor_m * floor_m for tx, ty in taken):
                continue
            got = pin(int(rows[j]), int(cols[j]), bool(shoal_bad[j]), want=float(actual[j]))
            if got is None or got[0] < SOUND_MIN_DEPTH_M:
                continue
            d, pc, pr = got
            x, y = transform * (pc, pr)
            pts.append((d, x, y, zoom, KIND_REPAIR))
            taken.append((x, y))
            if len(taken) >= REPAIR_BATCH:
                break
        if taken:
            inserted[zoom] = len(taken)
    return pts, inserted


def _tc(minz, child_z, kind=KIND_LATTICE):
    """Per-feature tippecanoe zoom placement. Coarser levels swap out as the next
    finer field arrives (maxzoom == their own zoom). The finest level (minz ==
    child_z) declares NO maxzoom so it persists to the tileset max: child_z is the
    LOCAL best-source ceiling (z10 in Danish waters, z13+ where CUDEM reaches),
    and the global tileset tiles past it wherever any region goes deeper — capping
    at child_z blanked the layer there (soundings gone at z11+ over Denmark while
    contours carried on).

    A prime sounding (least depth inside a closed isobath) is never CAPPED, so it can never be
    swapped out by a finer field as the mariner zooms in; its minzoom is its enclosing isobath's
    display zoom (_ring_minzoom). A repair insertion is likewise uncapped: it exists because the
    field failed B-410a/B-403.1a at its minzoom, and the finer fields it joins only interpolate
    better for having it."""
    if kind != KIND_LATTICE:
        return {"minzoom": minz}
    return {"minzoom": minz} if minz == child_z else {"minzoom": minz, "maxzoom": minz}


# S-4, Ed 4.10.0, B-412 rounds by depth band, always toward shoal: decimetres to 21 m, half metres
# from 21 to 31, whole metres beyond.
DECIMETRE_MAX_M = 21.0
HALF_METRE_MAX_M = 31.0


def _floor(x):
    """Floor a quotient that should have landed on a whole number. 1.2192 / 0.3048 evaluates to
    3.9999999999999996, and a bare floor charts that as 3 feet; 0.3 * 10 lands at 2.9999999999999996
    and charts 0.2 m. A nanometre of slack recovers the intended digit and is far too small to turn
    a genuinely deeper value into a shoaler chart number."""
    return math.floor(x + 1e-9)


def _depths(depth_pos):
    """Depth (metres, positive down) → chart labels, each floored toward shallower so the printed
    number is never deeper than the surface (S-4 B-412).

    Metres carry decimetres in the tenths slot where the band allows. Fathoms stay whole: a chart
    sets fathoms-and-feet below 11 fathoms, but a fathom is exactly six feet, so the viewer derives
    both from depth_ft rather than this shipping a mixed-radix number no reader could guess."""
    if depth_pos < DECIMETRE_MAX_M:
        m = _floor(depth_pos * 10) / 10
    elif depth_pos < HALF_METRE_MAX_M:
        m = _floor(depth_pos * 2) / 2
    else:
        m = _floor(depth_pos)
    return {
        "depth_m": int(m) if m == int(m) else m,   # integral values stay ints in the tiles
        "depth_ft": _floor(depth_pos / M_PER_FT),
        "depth_fm": _floor(depth_pos / M_PER_FATHOM),
    }


def _props(depth_pos, kind):
    """Tile properties for one sounding. A least depth inside a closed isobath is the top of
    S-4 B-410b's selection hierarchy, so the chart has to be able to SET it apart, not merely keep
    it — `prime` is what lets the style weight it. Emitted only when true: an absent key costs a
    vector tile nothing, and prime soundings are a small minority of the field."""
    props = _depths(depth_pos)
    if kind == KIND_PRIME:
        props["prime"] = 1
    return props


def _shoalest_grid(src, nodata, min_depth):
    """Per SOUND_CELL_PX cell, the shoalest wet pixel: depth (m, positive down) in g[gy, gx] and
    that pixel's own DEM column/row in cx/cy. NaN where dry / no wet pixel / shallower than
    min_depth (near-waterline "0" clutter). Row-strip reads bound peak memory to one strip, not
    the whole (multi-GB) DEM.

    The location travels with the depth because a sounding is charted at the position it was
    measured — "Spatial object must be encoded at the true position. There is no 'sounding, out
    of position' in an ENC" (S-57 App B.1 Annex A UOC, Ed 4.4.0, §5.3)."""
    import numpy as np
    from rasterio.windows import Window
    W, H = src.width, src.height
    shape = ((H + SOUND_CELL_PX - 1) // SOUND_CELL_PX,
             (W + SOUND_CELL_PX - 1) // SOUND_CELL_PX)
    g = np.full(shape, np.nan)
    cx = np.full(shape, np.nan)
    cy = np.full(shape, np.nan)
    for gy, r0 in enumerate(range(0, H, SOUND_CELL_PX)):
        h = min(SOUND_CELL_PX, H - r0)
        strip = src.read(1, window=Window(0, r0, W, h))
        for gx, c0 in enumerate(range(0, W, SOUND_CELL_PX)):
            block = strip[:, c0:c0 + SOUND_CELL_PX]
            wet = (block != nodata) & (block < 0)
            if not wet.any():
                continue
            # Shoalest = greatest (least negative) elevation among wet pixels; -inf parks the
            # dry ones so argmax can't pick them.
            by, bx = np.unravel_index(np.argmax(np.where(wet, block, -np.inf)), block.shape)
            depth = -float(block[by, bx])
            if depth >= min_depth:
                g[gy, gx] = depth
                cx[gy, gx] = c0 + bx + 0.5   # pixel centre, in DEM pixel coordinates
                cy[gy, gx] = r0 + by + 0.5
    return g, cx, cy


# Working size cap for enclosed-shoal detection, in pixels per side. The window is never held
# whole — a z8 stem at cz14 is 32898 px square, 4.33 GB as float32 — so detection runs on one
# decimated read bounded to this, and refines each hit at full resolution.
SHOAL_DETECT_PX = 4096


def _enclosed_shoals(src, nodata, min_depth, levels, view):
    """The least depth inside every closed isobath, as [(depth, col, row, ring_minzoom)],
    positions in full-res DEM pixels.

    S-4, Ed 4.10.0, B-410b puts "least depths over shoals, banks and sills" at the top of what
    must survive selection, and a closed contour with no sounding on it is a charted shoal with no
    charted least depth — a mariner interpolating across the ring reads the ring's own level and
    the shoal is understated. Measured on the terrebonne fixture before this existed: 4 of 4 shoal
    rings carried no sounding, understating by up to 1.30 m.

    A closed isobath at level L is the boundary of a connected component of {depth < L} that does
    not reach the window edge, so this reads off the DEM and needs no contour file — soundings
    stay a sibling of the contour stage rather than becoming its child. Using the same config
    levels the contours are cut at is what makes the two agree.

    Detection and position come from two different structures, because neither alone works:

    - Detection needs resolution finer than the sounding cell. A cell spans ~150 m and these
      rings are ~70 m across, so labelling the cell grid loses every one of them. One decimated
      read, capped at SHOAL_DETECT_PX a side, resolves them at flat cost for any stem.
    - Position must be a pixel that was actually measured (S-57 App B.1 Annex A UOC, Ed 4.4.0,
      §5.3), and a decimated pixel is not one. It comes from the cell grid, which already carries
      each cell's shoalest pixel and that pixel's own coordinates.

    Re-reading the raster per component to find an exact peak was tried and is what made this
    unshippable: the windows have no overviews, so thousands of small full-res reads grind the
    whole 4.33 GB file over and over. Nothing here reads the raster more than once.

    Only the metric ladder is walked. A fathom curve encircles the same physical shoal and would
    resolve to the same peak, which the caller dedupes anyway.
    """
    import numpy as np
    from scipy.ndimage import find_objects, label
    from rasterio.windows import Window

    small, wet, k = view
    dh, dw = small.shape
    W, H = src.width, src.height
    depth = np.where(wet, -small, np.inf)
    peaks = []
    for lvl in levels:
        mask = wet & (depth < lvl)
        if not mask.any():
            continue
        lab, n = label(mask)
        if n == 0:
            continue
        # A component touching the edge is not enclosed by anything we can see — its isobath
        # continues into the neighbouring tile, where that tile charts it.
        edge = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]])).tolist())
        for lb, box in enumerate(find_objects(lab), start=1):
            if lb in edge or box is None:
                continue
            ys, xs = box
            # Shoalest DECIMATED pixel of this component, from the array already in memory.
            win = lab[ys, xs] == lb
            sub = np.where(win, small[ys, xs], -np.inf)
            dy, dx = np.unravel_index(np.argmax(sub), sub.shape)
            if not np.isfinite(sub[dy, dx]):
                continue
            peaks.append((ys.start + int(dy), xs.start + int(dx), lvl))

    # A decimated pixel is not a measured position, so each peak is pinned by ONE small full-res
    # read of a box just wider than the decimation — a couple of raster blocks, not the component
    # (reading whole component boxes on these overview-less 4.33 GB windows is what OOMed).
    out = {}
    pad = k + 1
    for dy, dx, lvl in peaks:
        r0, c0 = max(0, dy * k - pad), max(0, dx * k - pad)
        r1, c1 = min(H, dy * k + k + pad), min(W, dx * k + k + pad)
        fine = src.read(1, window=Window(c0, r0, c1 - c0, r1 - r0))
        fwet = (fine != nodata) & (fine < 0)
        if not fwet.any():
            continue
        by, bx = np.unravel_index(np.argmax(np.where(fwet, fine, -np.inf)), fine.shape)
        d = -float(fine[by, bx])
        if d >= min_depth:
            # Same peak reached from several levels collapses on its pixel position, keeping the
            # earliest-drawing (deepest) enclosing ring's zoom.
            key = (c0 + bx + 0.5, r0 + by + 0.5)
            rmz = _ring_minzoom(lvl)
            if key not in out or rmz < out[key][1]:
                out[key] = (d, rmz)
    return [(d, c, r, rmz) for (c, r), (d, rmz) in out.items()]


def _reduce_shoalest(g, cx, cy):
    """2x2 shoalest (minimum positive-down depth) reduction → the next coarser level, carrying the
    winning cell's pixel location with it so the coarse sounding keeps the true position of the
    shoal it represents. NaN-aware (an all-dry quad stays NaN)."""
    import numpy as np

    def quads(a):
        ny, nx = a.shape
        p = np.full((ny + ny % 2, nx + nx % 2), np.nan)
        p[:ny, :nx] = a
        return np.stack([p[0::2, 0::2], p[0::2, 1::2], p[1::2, 0::2], p[1::2, 1::2]])

    qg = quads(g)
    allnan = np.isnan(qg).all(0)
    win = np.argmin(np.where(np.isnan(qg), np.inf, qg), axis=0)[None]
    pick = lambda q: np.where(allnan, np.nan, np.take_along_axis(q, win, 0)[0])
    return pick(qg), pick(quads(cx)), pick(quads(cy))


def _pyramid(src, nodata, bbox, min_depth, z, child_z):
    """A shoalest-wins pyramid: for each zoom level, one sounding per cell at that level's spacing,
    valued by the SHOALEST wet pixel in the cell (so a cell's shoal is never hidden at coarse zoom)
    and placed AT that pixel — the depth and the position describe the same spot, which is what
    makes the field chart-legal rather than a decorative lattice. Cell size sets the spacing;
    within a cell the point sits wherever the shoal actually is, so the pattern is irregular over
    irregular ground and even over flat ground, as S-4 §B-410d describes.
    Deep points display SHIFTED: a point at/beyond a SOUND_THIN_TIERS cutoff shows each zoom from a
    2^shift coarser level (its finest levels never display), so deep water thins at every zoom.
    The pyramid extends max-shift extra levels so the coarsest displayed zoom still gets a
    (thinned) deep field.
    Alongside the lattice, the least depth inside every closed isobath is emitted as a PRIME
    sounding, which displays at every zoom of the tile rather than riding a pyramid level: those
    are the depths S-4 B-410b says must always be shown, and the graduated SCAMIN policy puts the
    most significant soundings at the top of the ladder (S-57 App B.1 Annex A UOC, Ed 4.4.0,
    table 2.5).
    Returns [(depth_pos, x3857, y3857, display_z, prime)] — for lattice points display_z is that
    level's maxzoom, so each zoom shows exactly one field and it densifies as you zoom in."""
    import numpy as np
    transform = src.transform
    pts = []
    in_bbox = lambda x, y: bbox.left <= x <= bbox.right and bbox.bottom <= y <= bbox.top

    # ONE decimated read, shared with ring detection; the DEM is
    # never held whole (a z8 stem at cz14 is 4.33 GB as float32).
    W, H = src.width, src.height
    k = max(1, -(-max(W, H) // SHOAL_DETECT_PX))
    small = src.read(1, out_shape=(max(1, H // k), max(1, W // k)))
    wet = (small != nodata) & (small < 0)
    view = (small, wet, k)

    # The cell grid is the only other full-window structure, and it is 1/SOUND_CELL_PX a side.
    g, cx, cy = _shoalest_grid(src, nodata, min_depth)

    for d, c, r, rmz in _enclosed_shoals(src, nodata, min_depth, SHOAL_RING_LEVELS, view):
        x, y = transform * (c, r)        # a measured pixel centre, carried by the grid
        if in_bbox(x, y):
            # Displays exactly where its enclosing isobath displays (_ring_minzoom); never
            # capped, so the least depth accompanies the ring at every zoom the ring survives.
            pts.append((float(d), x, y, max(z, rmz), KIND_PRIME))

    max_shift = max((s for _, s in SOUND_THIN_TIERS), default=0)
    for level in range(max(0, child_z - z) + 1 + max_shift):
        minz = child_z - level
        ny, nx = g.shape
        for gy in range(ny):
            for gx in range(nx):
                d = g[gy, gx]
                if np.isnan(d):
                    continue
                disp = minz + _thin_shift(d)
                if not z <= disp <= child_z:
                    continue
                x, y = transform * (cx[gy, gx], cy[gy, gx])
                if in_bbox(x, y):
                    pts.append((float(d), x, y, disp, KIND_LATTICE))
        g, cx, cy = _reduce_shoalest(g, cx, cy)
    return pts


def _sound_dem(dem, tile_obj, z, child_z, tmp, label):
    """Sound any DEM covering the tile's extent: grid-decimated shoalest picks into a tmp
    line-delimited geojson. Returns (final_path, count), or None when dry / all-land."""
    import rasterio
    from pyproj import Transformer
    bbox = mercantile.xy_bounds(tile_obj)  # unbuffered, tile-aligned (EPSG:3857)
    with rasterio.open(dem) as src:
        nodata = src.nodata if src.nodata is not None else NODATA
        pts = _pyramid(src, nodata, bbox, SOUND_MIN_DEPTH_M, z, child_z)
        if SOUND_REPAIR and pts:
            pts, ins = _repair(src, nodata, bbox, pts, z, child_z)
            if ins:
                print(f"repair {label}: " + " ".join(f"z{k}+{v}" for k, v in sorted(ins.items())))
    if not pts:
        print(f"soundings: no wet cells for {label}")
        return None

    to4326 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    feats = []
    for d, x3857, y3857, minz, kind in pts:
        lon, lat = to4326.transform(x3857, y3857)
        # Each level shows at exactly one zoom (one field per zoom);
        # the finest level, primes and repair insertions ride uncapped — see _tc.
        feats.append({"type": "Feature", "tippecanoe": _tc(minz, child_z, kind),
                      "properties": _props(d, kind),
                      "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]}})

    final = f"{tmp}/soundings.geojsons"
    with open(final, "w") as f:  # line-delimited features — see module docstring
        for ft in feats:
            f.write(json.dumps(ft))
            f.write("\n")
    return final, len(feats)


def tile(stem):
    """The per-stem Snakemake job: sound one stem from a BUFFERED mosaic window, smoothed at read
    with the one shared f(depth, zoom), output at store/soundings/<stem>.geojsons. A dry tile writes
    a 0-byte sentinel; bundling filters empties by size."""
    import shutil
    import tempfile

    z, x, y, child_z = (int(a) for a in stem.split("-"))
    out = f"store/soundings/{stem}.geojsons"
    tmp = tempfile.mkdtemp(prefix=f"soundings-{stem}-")  # local scratch; publish crosses to the store
    # Private copy of the shared smoothed window; the land clamp mutates in place, and
    # without it smeared/rim negatives under the land mask mint soundings on islands.
    dem = f"{tmp}/dem.tiff"
    shutil.copyfile(f"store/window/{stem}.tif", dem)
    landmask.clamp_dem_to_land(dem)
    res = _sound_dem(dem, mercantile.Tile(x=x, y=y, z=z), z, child_z, tmp, stem)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    if res:
        final, n = res
        utils.publish(final, out)  # scratch and store are separate filesystems
        print(f"soundings tile {stem}: {n} points")
    else:
        open(out, "w").close()
        print(f"soundings tile {stem}: empty")
    shutil.rmtree(tmp)


def _check():
    """Grid shoalest per cell; <min_depth dropped; 2x2 reduction keeps the shoalest and its
    pixel; the pyramid places every sounding on that pixel and carries a cell's shoal up to coarse
    zoom; depth floors shallower with one decimal in the shoal band."""
    import tempfile
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    # S-4 B-412 bands, always floored toward shoal: decimetres to 21 m, half metres to 31, whole
    # metres beyond. Feet are whole throughout.
    assert _depths(3.94)["depth_m"] == 3.9 and _depths(9.8)["depth_m"] == 9.8
    assert _depths(20.99)["depth_m"] == 20.9, _depths(20.99)["depth_m"]
    assert _depths(23.49)["depth_m"] == 23 and _depths(23.81)["depth_m"] == 23.5   # B-412's example
    assert _depths(31.85)["depth_m"] == 31 and _depths(30.99)["depth_m"] == 30.5
    assert _depths(5.0)["depth_m"] == 5.0 and _depths(9.8)["depth_ft"] == 32
    assert "depth_dm" not in _depths(3.0)   # collapsed into depth_m

    # A fathom is exactly six feet, so the viewer reads fathoms-and-feet off depth_ft; the tiles
    # only owe it whole feet. 3 fm 4 ft is 3*1.8288 + 4*0.3048 = 6.7056 m = 22 ft.
    assert _depths(6.7056)["depth_ft"] == 22 and _depths(6.7056)["depth_fm"] == 3
    assert _depths(1.8287)["depth_ft"] == 5     # just under 1 fathom is 5 feet, never 6
    for d in (0.3, 2.7, 6.7056, 20.9, 25.0, 100.0):           # never chart deeper than the ground
        r = _depths(d)
        assert r["depth_m"] <= d + 1e-9
        assert r["depth_ft"] * M_PER_FT <= d + 1e-9
        assert r["depth_fm"] * M_PER_FATHOM <= d + 1e-9

    # 256x256 DEM (2x2 cells at CELL=128): deep -100 except a -3 m shoal in cell (gx1,gy1); cell
    # (0,0) a <1 m near-waterline patch (drop); a NODATA hole must never sound.
    arr = np.full((256, 256), -100.0, "float32")
    arr[200, 200] = -3.0
    arr[0:128, 0:128] = -0.5
    arr[10:20, 10:20] = NODATA
    tmp = tempfile.mkdtemp()
    p = f"{tmp}/dem.tif"
    tr = from_origin(0, 25600, 100, 100)  # 100 m px, origin top-left, EPSG:3857
    with rasterio.open(p, "w", driver="GTiff", height=256, width=256, count=1,
                       dtype="float32", nodata=NODATA, crs="EPSG:3857", transform=tr) as dst:
        dst.write(arr, 1)
    global SOUND_CELL_PX, SOUND_THIN_TIERS
    SOUND_CELL_PX = 128
    SOUND_THIN_TIERS = [(200.0, 1), (1000.0, 2)]  # the documented default; a dev shell's env must not steer the check
    from types import SimpleNamespace
    bbox = SimpleNamespace(left=0, bottom=0, right=25600, top=25600)
    with rasterio.open(p) as src:
        g, cx, cy = _shoalest_grid(src, NODATA, 1.0)
    assert np.isnan(g[0, 0])                        # <1 m patch dropped
    assert g[1, 1] == 3.0 and g[0, 1] == 100.0      # shoalest per cell
    assert (cx[1, 1], cy[1, 1]) == (200.5, 200.5)   # the -3 m pixel itself, not the cell centre
    rg, rcx, rcy = _reduce_shoalest(g, cx, cy)
    assert rg.tolist() == [[3.0]]                   # 2x2 shoalest = the -3 m
    assert (rcx[0, 0], rcy[0, 0]) == (200.5, 200.5) # …and it keeps that pixel's position

    with rasterio.open(p) as src:
        pts = _pyramid(src, NODATA, bbox, 1.0, z=8, child_z=9)  # level 0 (z9) + level 1 (z8)
    assert all(d >= 1.0 for d, *_ in pts)                       # no near-waterline "0"
    # true position: the -3 m sounding lands on the -3 m pixel at both zooms it appears at
    want = tr * (200.5, 200.5)
    for zoom in (8, 9):
        got = next((x, y) for d, x, y, mz, _p in pts if mz == zoom and d == 3.0)
        assert got == want, f"z{zoom} shoal at {got}, want the source pixel {want}"

    # prime rides to the tiles as a property so the style can weight it; an ordinary sounding
    # carries no key at all, so it costs the tiles nothing.
    assert _props(3.0, KIND_PRIME) == {"depth_m": 3, "depth_ft": 9, "depth_fm": 1, "prime": 1}
    assert "prime" not in _props(3.0, KIND_LATTICE) and "prime" not in _props(3.0, KIND_REPAIR)

    # zoom placement: coarser levels swap out at their own zoom; the finest level
    # (minz == child_z) is uncapped so it persists above the local source ceiling
    assert _tc(8, 10) == {"minzoom": 8, "maxzoom": 8}
    assert _tc(10, 10) == {"minzoom": 10}
    assert _tc(8, 10, KIND_PRIME) == {"minzoom": 8}   # a least depth is never capped
    assert _tc(8, 10, KIND_REPAIR) == {"minzoom": 8}  # …and neither is a repair insertion
    # …but never past its ring: a prime's minzoom is its enclosing isobath's display zoom.
    # Here the deepest enclosing ring (50 m) draws from z7, so the base zoom (8) binds.
    with rasterio.open(p) as src:
        promoted = {mz for _d, _x, _y, mz, kind in
                    _pyramid(src, NODATA, bbox, 1.0, z=8, child_z=9) if kind == KIND_PRIME}
    assert promoted == {8}, promoted

    # Enclosed shoals run on the CELL GRID, never the pixels — the whole window is never held in
    # memory (a z8 stem at cz14 would be 4.3 GB). Synthesized directly: a 20 m field with a 3 m
    # rise in the middle, and a second rise pressed against the edge.
    with rasterio.open(p) as src:
        _k = 1
        _small = src.read(1)
        found = _enclosed_shoals(src, NODATA, 1.0, [5.0, 20.0],
                                 (_small, (_small != NODATA) & (_small < 0), _k))
    # The -3 m pinnacle is enclosed by the 5 m and 20 m levels, and is reported at its OWN pixel
    # (not a decimated approximation) with its deepest ring's display zoom (20 m -> z9).
    assert any(f[:3] == (3.0, 200.5, 200.5) and f[3] == 9 for f in found), found
    # The <1 m patch fills cell (0,0) out to two window edges, so no isobath closes around it.
    assert not any(c < 128 and r < 128 for _d, c, r, _mz in found), found

    # Ring display zooms come straight off contour_run.CONTOUR_TIERS (cumulative bands).
    assert [_ring_minzoom(l) for l in (200, 50, 10, 2)] == [0, 7, 9, 11]


    # depth-tiered thinning: the tier a depth falls in, at and just under each cutoff
    assert [_thin_shift(d) for d in (0, 99, 199.9, 200, 999, 1000, 5000)] == [0, 0, 0, 1, 1, 2, 2]

    # tier parsing: order-insensitive (sorted for the last-match-wins scan), loud on malformed input
    assert _parse_thin_tiers("1000:2, 200:1") == [(200.0, 1), (1000.0, 2)]
    try:
        _parse_thin_tiers("200")
        raise AssertionError("malformed SOUND_THIN_TIERS must exit")
    except SystemExit as e:
        assert "depth_m:shift" in str(e)

    def uniform_field(depth_m, z, child_z, n=1024, px=100):
        """{display_zoom: point count} over a uniformly depth_m-deep n x n DEM."""
        q = f"{tmp}/uniform-{depth_m}-{z}.tif"
        with rasterio.open(q, "w", driver="GTiff", height=n, width=n, count=1, dtype="float32",
                           nodata=NODATA, crs="EPSG:3857",
                           transform=from_origin(0, n * px, px, px)) as dst:
            dst.write(np.full((n, n), -float(depth_m), "float32"), 1)
        whole = SimpleNamespace(left=0, bottom=0, right=n * px, top=n * px)
        with rasterio.open(q) as src:
            pts = _pyramid(src, NODATA, whole, 1.0, z, child_z)
        counts = {}
        for _d, _x, _y, dz, _p in pts:
            counts[dz] = counts.get(dz, 0) + 1
        return counts

    # An unthinned ladder over the same geometry: one 4x-denser field per zoom, deepest densest.
    ladder = uniform_field(100, z=6, child_z=9)
    assert sorted(ladder) == [6, 7, 8, 9] and ladder[9] > ladder[8] > ladder[7] > ladder[6]
    # Each tier rides that same lattice `shift` levels coarser at EVERY zoom — its finest `shift`
    # levels never display, so deep water thins 4^-shift while < 200 m keeps full density.
    for depth_m, shift in ((100, 0), (500, 1), (5000, 2)):
        field = uniform_field(depth_m, z=8, child_z=9)
        assert sorted(field) == [8, 9], f"{depth_m} m displays at {sorted(field)}, want z8 and z9"
        assert field == {8: ladder[8 - shift], 9: ladder[9 - shift]}, \
            f"{depth_m} m must ride the lattice {shift} level(s) coarser: {field} vs {ladder}"
    assert _tc(9, 9) == {"minzoom": 9}  # the shifted level reaching child_z is still a finest field

    # mixed field: a 190 m shoal (tier 0) in a 1200 m plain (tier 2). A block's representative takes
    # the SHOALEST value's shift, so a shelf-break shoal displays at every zoom it would unthinned —
    # thinning is never allowed to hide a shoal.
    m = f"{tmp}/mixed.tif"
    arr = np.full((1024, 1024), -1200.0, "float32")
    arr[512:640, 512:640] = -190.0  # exactly one CELL=128 block
    with rasterio.open(m, "w", driver="GTiff", height=1024, width=1024, count=1, dtype="float32",
                       nodata=NODATA, crs="EPSG:3857",
                       transform=from_origin(0, 102400, 100, 100)) as dst:
        dst.write(arr, 1)
    span = SimpleNamespace(left=0, bottom=0, right=102400, top=102400)
    with rasterio.open(m) as src:
        mixed = _pyramid(src, NODATA, span, 1.0, z=8, child_z=9)
    for dz in (8, 9):
        assert any(d == 190.0 and mz == dz for d, _x, _y, mz, _p in mixed), \
            f"the 190 m shoal must display at z{dz}"
    # the plain still shows where its shifted lattice has cells the shoal didn't claim (z9 here);
    # at z8 the whole field's coarse representative IS the shoal — deep is what gets suppressed
    assert any(d == 1200.0 and mz == 9 for d, _x, _y, mz, _p in mixed)
    assert all(d == 190.0 for d, _x, _y, mz, _p in mixed if mz == 8)
    # …and the shoal is enclosed by the 200 m isobath, so it is also charted as a prime sounding
    assert any(d == 190.0 and kk == KIND_PRIME for d, _x, _y, _mz, kk in mixed), "enclosed 190 m shoal must be prime"
    # Repair: two shoals share one cell, so the lattice emits only the shoaler and the field
    # lies about the other; a trench pixel is hidden by shoalest-wins entirely. Shoal-side and
    # deep-side repair must chart both, at their measured pixels, uncapped from the failing zoom.
    global SOUND_REPAIR, REPAIR_SHOAL, REPAIR_SPACING_PX
    rrr = np.full((512, 512), -40.0, "float32")
    rrr[100, 100] = -1.0     # cell (0,0)'s shoalest — the lattice charts this one
    rrr[120, 119] = -1.2     # same cell, 2 km away: invisible to the lattice, in the field's hull
    rrr[240, 140] = -150.0   # deep trench pixel, in the navigational band and inside the z8
                             # field's convex hull (single-pass repair cannot test beyond it):
                             # shoalest-wins can never emit it
    rrr[350, 60] = -150.0    # second trench, outside the z8 hull but inside z9's — visible
                             # only to the finer zoom's pass
    rp = f"{tmp}/repair.tif"
    with rasterio.open(rp, "w", driver="GTiff", height=512, width=512, count=1, dtype="float32",
                       nodata=NODATA, crs="EPSG:3857",
                       transform=from_origin(0, 51200, 100, 100)) as dst:
        dst.write(rrr, 1)
    rbox = SimpleNamespace(left=0, bottom=0, right=51200, top=51200)
    SOUND_REPAIR = True
    REPAIR_SHOAL = True
    with rasterio.open(rp) as src:
        base = _pyramid(src, NODATA, rbox, 1.0, z=8, child_z=9)
        fixed, ins = _repair(src, NODATA, rbox, list(base), 8, 9)
    reps = [t for t in fixed if t[4] == KIND_REPAIR]
    assert ins and reps, "repair must insert where the field fails B-410a/B-403.1a"
    assert any(abs(d - 1.2) < 1e-6 for d, *_ in reps), reps[:4]      # the hidden shoal, exact pixel value
    assert any(d > 100 for d, *_ in reps), "the trench (deep side) must be charted"
    # EVERY zoom gets its own pass — a violation visible only in a finer zoom's field must be
    # repaired there. The trench at (350, 60) sits outside the z8 field's convex hull (4 points)
    # but inside z9's (16 points), so it is only detectable at z9: insertions must be recorded at
    # BOTH zooms or the loop returned early (the bug an adversarial review caught after the
    # z8-only symptom was rationalized away instead of read).
    assert set(ins) == {8, 9}, ins
    assert any(t[3] == 9 and t[4] == KIND_REPAIR and t[0] > 100 for t in fixed), \
        "the z9-only trench must be inserted with minzoom 9"

    # The spacing knob thins: a huge floor suppresses insertions near the displayed field.
    REPAIR_SPACING_PX = 1e9
    with rasterio.open(rp) as src:
        _, ins2 = _repair(src, NODATA, rbox, list(base), 8, 9)
    assert sum(ins2.values() or [0]) < sum(ins.values()), (ins, ins2)
    REPAIR_SPACING_PX = 0.0
    SOUND_REPAIR = False
    REPAIR_SHOAL = False

    print("soundings_run self-check ok")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["tile"] and len(a) == 2:
        tile(a[1])
    elif a[:1] == ["check"]:
        _check()
    else:
        sys.exit("usage: soundings_run.py tile <stem> | check")
