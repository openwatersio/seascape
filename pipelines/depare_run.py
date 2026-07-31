"""Depth-area polygons (ENC DEPARE) as a fork off each aggregation tile's merged DEM.

The vector twin of the raster depth shading — the ENC model, where DEPARE partitions the
whole water surface. Three feature kinds share the one `depare` layer, distinguished by
their attributes (the fill just switches on them), so bands, drying, and unknown-depth
water compose without extra layers or archives:

  1. depth bands — water partitioned into ranges between the charted isobath levels, each
     polygon carrying its range as drval1/drval2 (ENC DEPARE, positive-down metres) and a
     `sys` tag (m ladder / ft fathom curves). drval1 >= 0.
  2. drying — the green foreshore, DEPARE with a NEGATIVE drval1 (ENC-true: drying = DEPARE,
     DRVAL1 < 0): drval1 = -DRYING_CAP, drval2 = 0 (one band for now). Derived from the SAME
     gdal_contour -p pass as the bands: the metre ladder carries DRYING_CAP as an extra
     positive level, so its [0, DRYING_CAP] bucket is the foreshore and shares its 0 m
     seaward edge with the shoal band's amax=0 edge — the same ring, so tippecanoe's
     --detect-shared-borders simplifies both identically with no crack. Cut to effective
     water (seaward of the OSM land line, but kept inside mapped inland water — the
     ICW/tidal-channel case), see generate().
  3. nodata — water OSM maps as a polygon but the DEM holds no depth for (a #24-cleared
     lake, unsurveyed margins beside a fairway). NO drval1/drval2 — absence IS the encoding
     (MVT has no null; the fill's "no drval1" case renders S-52's NODATA fill) — plus a
     `kind` passthrough of the Overture subtype (river/lake/canal/reservoir).

The three are pairwise disjoint by construction — bands are the DEM's water pixels
(amax <= 0), drying is the disjoint [0, DRYING_CAP] bucket ∩ effective water, and nodata is
the mapped water MINUS both — so style ordering is cosmetic. The `rank` sort attribute
survives only as a stable tie-breaker at an incidental simplification-wobble edge (nodata 0
< bands 1 < drying 2: real depth over no-data, foreshore over the shoal band it abuts).

sys multiplexing: the bands duplicate per sys (the ladders differ), but drying and nodata
are unit-independent, so they ship ONCE with NO `sys` — the style filters them by drval
semantics (drval1 < 0 / no drval1), not by sys, and showing them in both m and ft modes
needs no duplication. Halves their feature bytes vs a per-sys copy for identical pixels.
Drying rides the metre pass only (the cap is a metre level) and is emitted once.

gdal_contour -p buckets the same merged, smoothed DEM the contour lines trace, with the
same contour generator — band edges and contour lines coincide by construction. No Chaikin
and no per-feature shapely simplify: adjacent partitions share edges, and per-feature
smoothing treats the shared chain differently in each polygon, opening see-through cracks
between bands. Generalization is COVERAGE simplification instead (simplify_coverage), which
simplifies each shared edge once, for both its owners, at the S-58 vertex floor. It selects a
subset of the contour's own vertices, so the drawn line and the band edge stay pinned to the
same points and part company by at most the tolerance — 1.1 m measured at cz15, against the
1 MVT pixel tippecanoe already spends on every zoom below the leaf.

Per tile: bands (gdal_contour -p at DEPARE_LEVELS / DEPARE_LEVELS_FT, drop land, drval/sys)
+ drying (the metre ladder's [0, DRYING_CAP] bucket ∩ effective water, drval1 < 0) + nodata
(inland-water polygons minus the DEM's water coverage and the drying) -> clip to the
unbuffered tile bbox in shapely (polygon-only by construction, see _polys) -> 4326 ->
store/depare/{stem}.fgb. Same seam contract as contours: deterministic on the buffered grid,
so neighbouring tiles' features abut exactly at the clip line. The vector bundle folds these into
the `depare` layer of the sharded variable-depth run (contour_run), gated at z6 by a per-feature
tippecanoe.minzoom (contour_run.DEPARE_MINZOOM).
"""

import os
import sys

import mercantile
import rasterio

import config
import contour_run
import utils
from aggregation_reproject import get_resolution

# The bands' zoom floor (z6, matching the style's depth-areas/contour-lines minzoom) is now applied
# per feature by the vector bundle (contour_run.DEPARE_MINZOOM): partitions can't be level-thinned
# per zoom like the lines' CONTOUR_TIERS (dropping one leaves a hole), so the floor is the low-zoom
# cost control — the raster depth shading carries z<6.

# DEPARE_LEVELS derives from CONTOUR_LEVELS, which the style hand-mirrors (style/index.ts
# DEPARE_LADDER_M/FT) — warn when an env override diverges the bands from the style. Upgrade
# path: generate the style constants from config instead of mirroring.
if config.CONTOUR_LEVELS != config.CONTOUR_LEVELS_DEFAULT:
    print("WARNING: CONTOUR_LEVELS overridden — depare bands will diverge from the style's "
          "hand-mirrored DEPARE_LADDER_M/FT (style/index.ts); update it to match.",
          file=sys.stderr)

# Fill draw order for the `rank` sort attribute (a style fill-sort-key draws higher on top).
# All three kinds are disjoint by construction, so rank is cosmetic — a stable tie-breaker at
# an incidental simplification-wobble edge: real depth (bands) over no-data (nodata), and
# drying over the shoal band it abuts along their shared 0 m seam.
NODATA_RANK = 0
BAND_RANK = 1
DRYING_RANK = 2

# Sliver filter: the vector edges (OSM water outline, effective-land cut) and the raster
# depth-band edge (pixel-staircased) never coincide exactly, so `water minus coverage` (nodata)
# and `bucket minus land` (drying) both leave crumbs along every near-coincident boundary. Drop
# any polygon smaller than this many DEM pixels to clear the registration noise. Sized from
# measurement, not a guess: over a whole ICW macrotile the crumbs top out at ~3 px (95% are thin,
# compactness < 0.2), so 4 px removes them all — while real intertidal flats and small unsurveyed
# ponds bottom out around 4 px and survive. The old 64 px was tuned for nodata lakes alone and
# culled ~1000 genuine small drying flats (only ~2% of the area, but most of the visible detail).
# Ceiling: a pure area gate can't tell a LONG thin ribbon (1 px × 100 px ≈ 100 px area) from a
# compact real flat; none occurred here (the difference yields corner fragments, not ribbons), but
# a width/compactness gate is the targeted tool if thin ribbons ever appear. Env-tunable.
SLIVER_MIN_PX = float(os.environ.get("SLIVER_MIN_PX", "4"))

# S-58 Ed. 7.0.0 check 571 caps ENC vertex density at 0.3 mm at compilation scale — the only hard
# numeric geometry rule in the standards, and a raw gdal_contour partition carries ~5x more than it
# allows. A rendering pixel is 0.28 mm, so the floor is 0.3/0.28 of the stem's own pixel, applied in
# EPSG:3857 where the projected scale IS the compilation scale (2.56 projected units at cz15 = 2.23
# ground metres at 29.5degN). Set to 0 to emit raw geometry. NOAA compiles at 0.4 mm to guarantee
# 0.3 mm on output; this pipeline's own tile-time simplification is the 1 px = 0.28 mm below.
SIMPLIFY_MM = float(os.environ.get("DEPARE_SIMPLIFY_MM", "0.3"))
MM_PER_PX = 0.28

# Nodata outlines are full-detail OSM geometry regardless of the stem's real resolution, so under
# the variable-depth pyramid coarse inland tiles would keep subdividing to carry vertices no coarse
# stem can resolve. Generalizing nodata rows to the stem's child_z resolution (this many MVT pixels)
# is what lets those tiles leaf early (measured −54% at 1 px / −67% at 2 px on a cz8 stem; 21.5 M →
# 1.4 M vertices, sub-resolution features dropping out naturally). Bands and drying take SIMPLIFY_MM
# through coverage simplification instead, which is what keeps their shared edges bit-identical.
NODATA_SIMPLIFY_PX = float(os.environ.get("NODATA_SIMPLIFY_PX", "1"))

# Fixed-precision grid (metres) for overlays against multi-piece unions: GEOS 3.13's
# float OverlayNG returns an empty overlay against some unions whose pairwise overlays are
# correct (verified on 6-21-22-9; point-in-polygon arbitration); snap-rounded overlay is the
# robust mode, and 1 µm moves no vertex cartographically.
GRID = 1e-6

# Preformatted so the MemoryError path in tile() need not allocate to report itself.
_OOM_NOTE = (b"depare tile %s: MemoryError - the kernel refused an allocation "
             b"(peak RSS %d kB). Needs a bigger box or a bounded partition pass.\n")


def _save_traceback(stem):
    """Append the current exception to store/depare/<stem>.err — retry-proof, unlike the
    rule's truncating `2> {log}` redirect."""
    try:
        import time
        import traceback
        os.makedirs("store/depare", exist_ok=True)
        with open(f"store/depare/{stem}.err", "a") as fh:
            fh.write("--- %s\n" % time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            traceback.print_exc(file=fh)
    except Exception:
        pass


def _rss_kb():
    """Peak RSS of this process in kB. /proc's VmHWM is preferred (it survives a child's exit);
    getrusage is the fallback, and the only source on macOS, where local profiling runs."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1])
    except Exception:
        pass
    try:
        import resource
        import sys as _sys
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak // 1024 if _sys.platform == "darwin" else peak  # bytes on macOS, kB on Linux
    except Exception:
        return -1


class ContourTimeout(Exception):
    """A bounded gdal_contour invocation exceeded DEPARE_TIMEOUT."""


def _run_bounded(cmd, what, timeout):
    """contour_run._run with a coreutils-timeout bound; exit 124/137 -> ContourTimeout."""
    if not timeout:
        return contour_run._run(cmd, what)
    try:
        contour_run._run(f"timeout -k 30 {int(timeout)} {cmd}", what)
    except Exception as e:
        if "(exit 124)" in str(e) or "(exit 137)" in str(e):
            raise ContourTimeout(f"{what} exceeded {timeout}s") from e
        raise


def _uniform_coarsen(dem, factor, out):
    """Whole-window average downsample — the retry rescue when even the deep-coarsened window
    times out (shallow-complexity stems the depth gate can't help). gdal_contour reads the small
    raster directly; _depare_dem re-reads res per file, so the sliver gate adapts."""
    contour_run._run(
        f"gdal_translate -q -r average -outsize {100 // factor}% {100 // factor}% {dem} {out}",
        "gdal_translate -r average")
    return out


_mark_last = None
_mark_label = None  # the last COMPLETED phase — the heartbeat names the one after it


def _mark(label):
    """DEPARE_TIMING=1: print wall seconds since the previous mark — the phase-1 profile's
    section attribution (docs/plans/2026-07-21-depare-perf.md)."""
    global _mark_last, _mark_label
    if not os.environ.get("DEPARE_TIMING"):
        return
    import time
    now = time.monotonic()
    if _mark_last is not None and label:
        print(f"depare-timing {label}: {now - _mark_last:.1f}s", flush=True)
    _mark_last = now
    _mark_label = label


def _heartbeat(stem, interval=60):
    """A daemon thread that names the live phase and the process RSS once a minute, so a
    wedged tile identifies itself in its own log. Marks print on phase COMPLETION, so the
    running phase is the one after _mark_label."""
    import threading
    import time

    def beat():
        t0 = time.monotonic()
        while True:
            time.sleep(interval)
            since = time.monotonic() - (_mark_last or t0)
            print(f"depare {stem} alive: {time.monotonic() - t0:.0f}s total, "
                  f"{since:.0f}s past mark '{_mark_label or 'start'}', "
                  f"rss {_rss_kb() // 1024} MB", flush=True)

    threading.Thread(target=beat, daemon=True).start()


def _polys(geom):
    """Every non-empty Polygon inside a geometry, recursing into Multi/GeometryCollection and
    dropping the line/point slivers a clip or make_valid can leave — so the output layer is
    uniformly polygon (FlatGeobuf rejects a mixed layer)."""
    t = geom.geom_type
    if t == "Polygon":
        return [] if geom.is_empty else [geom]
    if t in ("MultiPolygon", "GeometryCollection"):
        return [p for g in geom.geoms for p in _polys(g)]
    return []


def _subdivide(geom, max_pts=512, depth=0):
    """Bisect a polygon along its longer envelope axis until every piece is under max_pts
    vertices (the ST_Subdivide pattern). Bounded pieces keep the nodata differences local in
    DENSE windows, where every water feature truly intersects a harbor-wide band polygon."""
    from shapely import get_num_coordinates, make_valid
    from shapely.geometry import box
    if get_num_coordinates(geom) <= max_pts or depth >= 16:
        return [geom]
    l, b, r, t = geom.bounds
    m = (l + r) / 2 if r - l >= t - b else (b + t) / 2
    halves = (box(l, b, m, t), box(m, b, r, t)) if r - l >= t - b else \
             (box(l, b, r, m), box(l, m, r, t))
    return [piece
            for h in halves
            for p in _polys(make_valid(geom.intersection(h)))
            for piece in _subdivide(p, max_pts, depth + 1)]


def valid_union(geoms):
    """unary_union after make_valid per input. gdal_contour's polygon mode can emit a
    self-touching ring (GEOS raises "side location conflict" on a raw union of it); make_valid
    splits it into valid parts first. A no-op on already-valid geometry (bands are make_valid'd
    per feature; this guards the coverage / mask unions)."""
    from shapely import make_valid
    from shapely.ops import unary_union
    return unary_union([make_valid(g) for g in geoms])


def repaired_parts(geoms):
    """Every part of `geoms`, make_valid'd only where shapely.is_valid rejects it.

    NEVER make_valid a whole bucket: GEOS routes a MultiPolygon through BuildArea, i.e. a cascaded
    union of every part, and on a marsh drying bucket (88,997 parts / 20.7 M vertices on
    8-63-105-15) that does not finish in an hour — while exactly 1 of those parts is invalid. One
    gdal_contour -p bucket's parts are disjoint by construction, so per-part repair is equivalent
    and linear."""
    import shapely
    from shapely import make_valid
    parts = [p for g in geoms for p in _polys(g)]
    return [q for p, ok in zip(parts, shapely.is_valid(parts))
            for q in ([p] if ok else _polys(make_valid(p)))]


def repaired_multi(geoms):
    """repaired_parts as ONE MultiPolygon. No union: a bucket's parts are already disjoint, so
    collecting them is the whole job — this is the make_valid a partition read actually needs."""
    import shapely
    from shapely.geometry import MultiPolygon
    parts = repaired_parts(geoms)
    return shapely.multipolygons(parts) if parts else MultiPolygon()


def coverage_union(geoms):
    """Dissolve a polygonal COVERAGE — parts with disjoint interiors, which the parts of one
    gdal_contour -p bucket are by construction. Exploding to those parts (repaired_parts) is what
    makes a marsh drying bucket tractable, twice over: the per-part repair above, and
    coverage_union_all merging along shared edges instead of running the full overlay, 194 s
    against unary_union's 1,310 s on the same parts.

    coverage_union_all is silently wrong on input that is not a coverage, so the RESULT is what
    gets checked; the input cannot be, since coverage_is_valid does not finish in 33 min on that
    bucket. Both failure modes surface in the result anyway: parts that overlap or share only
    part of an edge come back unmerged, and members that overlap or touch along a line make the
    output MultiPolygon invalid, while geometry dropped outright breaks the area identity. That
    identity alone proves nothing — .area sums a MultiPolygon's members, so unmerged overlaps
    still add up to the same total. Either way, fall back to unary_union and say so."""
    import shapely
    from shapely.ops import unary_union
    parts = repaired_parts(geoms)
    if not parts:
        return unary_union(parts)
    total = float(shapely.area(parts).sum())
    try:
        u = shapely.coverage_union_all(parts)
        if abs(u.area - total) <= 1e-9 * total and u.is_valid:
            return u
        if abs(u.area - total) <= 1e-9 * total:
            # Every observed guard trip (12 of 2,170 stems, run 30634360224) preserves the area
            # to ~1e-15 and fails only validity: members of the dissolve touch along an edge or a
            # ring self-touches at a pinch. Re-union the DISSOLVE OUTPUT, not the input — the
            # dissolve already merged the interior, so this unions a handful of mostly-disjoint
            # regions instead of re-noding every input part (the 89k-part marsh bucket's full
            # union costs 22 min; this path is seconds).
            r = unary_union(list(shapely.get_parts(u)))
            if abs(r.area - total) <= 1e-9 * total and r.is_valid:
                print(f"depare: coverage dissolve of {len(parts)} parts re-noded from "
                      f"{shapely.get_num_geometries(u)} dissolved regions", flush=True)
                return r
        why = f"area {u.area!r} vs parts {total!r}, valid {u.is_valid}"
    except shapely.errors.GEOSException as e:
        why = str(e)
    print(f"depare: {len(parts)} parts are not a valid polygonal coverage ({why}) - "
          "falling back to unary_union", flush=True)
    return unary_union(parts)


# A ring this short (EPSG:3857 metres) cannot be written: _RowSink rounds to 1e-9 degrees, which
# is 1.1e-4 m, so a smaller ring loses points and lands in the FGB as an invalid "too few points"
# component. Coverage simplification collapses holes to exactly this scale — it must keep every
# ring to preserve the coverage's topology, so it shrinks them instead of deleting them. Only
# rings below the write grid may be dropped: a hole in a depth band is where the NEXT band sits,
# so dropping a representable one would open an overlap.
MIN_RING_M = 1e-3

# How much of a ladder's area coverage simplification may move before the result is rejected —
# see simplify_coverage for why an exact identity is not on offer.
AREA_GUARD = 1e-3


def _drop_subgrid_rings(geom):
    """`geom` with any hole shorter than MIN_RING_M removed. Vectorized over parts, and returns
    the input untouched when nothing qualifies, so the marsh's hole-free parts cost one C call."""
    import numpy as np
    import shapely
    from shapely.geometry import Polygon
    parts = shapely.get_parts(geom)
    holes = shapely.get_num_interior_rings(parts)
    changed = False
    for i in np.nonzero(holes > 0)[0]:
        p = parts[i]
        keep = [r for r in p.interiors if r.length >= MIN_RING_M]
        if len(keep) != holes[i]:
            parts[i] = Polygon(p.exterior, keep)
            changed = True
    if not changed:
        return geom
    return shapely.multipolygons(parts) if geom.geom_type == "MultiPolygon" else parts[0]


def simplify_coverage(geoms, tol):
    """Simplify one ladder's partition to the S-58 vertex floor, keeping it a partition.

    Per-geometry simplify is what the crack warning in this module's docstring forbids: it moves
    a shared chain differently in each polygon it belongs to. Coverage simplification takes the
    whole ladder at once, decomposes it into the edges between its polygons, and simplifies each
    edge ONCE — so adjacent bands come back sharing the identical polyline (measured: every vertex
    of a shared chain is present in both members, no pair overlaps, and the coverage's total area
    holds to 5e-15). It also never invents a vertex, which is what keeps the drawn contour lines
    pinned to the band edges they trace: every surviving band vertex is still a vertex of the line.

    The algorithm is Visvalingam-Whyatt, so `tol` bounds the area of what it removes rather than
    the displacement; measure the displacement, don't assert it (perf/gates.py gate 5). It is also
    LOCAL enough that two neighbouring stems simplify their shared geometry identically — the seam
    contract holds by measurement (0.00 m of per-band seam sym-diff across a simulated seam), not
    by construction, which is why seam_check is the gate on it.

    Silently wrong on input that is not a coverage, like every coverage_* entry point. The input
    IS one by construction — one gdal_contour -p pass, verified offline with
    coverage_invalid_edges (0 invalid edges on all four fixtures and on 8-63-105-15's 52.5 M-vertex
    metre ladder, 173 s), which is too expensive to spend per tile. What runs here is the cheap
    result gate: every member still valid, and no geometry gone. AREA_GUARD is what "gone" means —
    an exact identity is not available, because simplifying the coverage's outer boundary trades
    area across it; the theoretical bound, tol x boundary length, is ~1% of a marsh coverage's area
    and so is no gate at all, while the measured drift is 1.5e-6 (30,375 m2 over 90,464 km of
    boundary — 0.3 mm of mean displacement). This sits between: tighter than the bound, and loose
    enough that only a member that VANISHED trips it."""
    import shapely
    geoms = list(geoms)
    if not geoms or tol <= 0:
        return geoms
    before = float(shapely.area(geoms).sum())
    try:
        out = [_drop_subgrid_rings(g)
               for g in shapely.coverage_simplify(geoms, tol, simplify_boundary=True)]
        after = float(shapely.area(out).sum())
        if abs(after - before) <= AREA_GUARD * before and bool(shapely.is_valid(out).all()):
            return out
        why = f"area {after!r} vs {before!r}, valid {bool(shapely.is_valid(out).all())}"
    except MemoryError:
        raise                      # the tile's own MemoryError path reports the footprint
    except Exception as e:
        # Not only GEOSException: an invalid ring reaches the simplifier as a ZeroDivisionError.
        why = f"{type(e).__name__}: {e}"
    print(f"depare: {len(geoms)} partitions did not simplify as a coverage ({why}) - "
          "keeping raw geometry", flush=True)
    return geoms


def partitions(dem, levels, raw_fgb, timeout=0):
    """Water/foreshore partitions off `dem`: gdal_contour -p buckets the DEM between `levels`,
    tagging each bucket its range amin/amax -> drval1/drval2 (ENC: shallow/deep bound,
    positive-down metres). Writes the bucketed FGB and returns its path; read it back
    bucket-at-a-time with read_bucket, so nothing holds both ladders at once.
    Callers select depth bands (amax <= 0), the [0, DRYING_CAP] drying bucket
    (0 < amax <= cap), and drop land (amax above the shallowest positive level)."""
    fl = " ".join(str(l) for l in levels)
    # DEPARE_CONTOUR_BIN picks the polygon-contour binary: builds set it to contour-p, whose
    # output is byte-identical to gdal_contour -p but near-linear where marsh ring counts make
    # stock quadratic (see the Dockerfile stanza). Identical output is why switching it back
    # forces nothing.
    bin_ = os.environ.get("DEPARE_CONTOUR_BIN", "gdal_contour")
    if bin_ == "gdal_contour":
        cmd = f"gdal_contour -q -p -amin amin -amax amax -fl {fl} -f FlatGeobuf {dem} {raw_fgb}"
    else:
        cmd = f"{bin_} {dem} {raw_fgb} {fl}"
    _run_bounded(cmd, f"{bin_} -p", timeout)
    _mark(f"gdal_contour[{os.path.basename(raw_fgb)}]")
    return raw_fgb


def read_bucket(raw_fgb, where):
    """One bucket (attribute-filtered) of a partitions() FGB, with drval1/drval2 derived.
    Select on amax, NOT amin: GDAL 3.8's polygon mode writes a garbage amin (0) on the
    deepest bucket, which then read as land and vanished — amax is correct on every version.
    So drval1 keys off amax; drval2 (off amin) is right for the interior bands but unreliable
    on that deepest bucket, and the drying emit uses a literal drval2 = 0 anyway."""
    import geopandas as gpd
    g = gpd.read_file(raw_fgb, where=where)
    if len(g):
        g["drval1"] = 0.0 - g["amax"]  # 0.0 - keeps the shoalest bound 0.0, not -0.0
        g["drval2"] = 0.0 - g["amin"]
    return g


class _RowSink:
    """Incremental depare-row writer: batches of {geometry (3857), drval1, drval2, sys, kind,
    rank} append to a GeoJSONSeq in 4326; finish() converts to the final FGB. Absent values are
    None -> JSON null -> FGB NULL, which tippecanoe encodes as an ABSENT MVT property — so
    nodata truly has no drval1, the fill's switch key."""

    FLUSH = 50_000
    # Decimal degrees kept on write: 1e-9 deg is 0.11 mm, four orders below the S-58 vertex floor
    # and below anything the pipeline's own geometry means.
    COORD_DECIMALS = 9

    def __init__(self, seq):
        self.seq = seq
        self.count = 0
        self.pending = []

    def write(self, rows, flush=True):
        self.pending += rows
        if flush or len(self.pending) >= self.FLUSH:
            self.flush()

    def flush(self):
        if not self.pending:
            return
        import geopandas as gpd
        import numpy as np
        import shapely
        gdf = gpd.GeoDataFrame(self.pending, crs="EPSG:3857").to_crs("EPSG:4326")
        # Round to the write grid HERE, not in the driver, so what gets validated below is what
        # lands in the FGB. COORD_DECIMALS beats the driver's default 7 (~1.1 cm) because that
        # rounds each ROW independently: a vertex one row carries mid-segment and its neighbour
        # does not (an overlay node on a shared chain) lands off that segment, opening a hairline
        # sliver the length of the segment — and a generalized band's segments are long. Measured
        # worst-pair band overlap: 3.9 m2 raw, 18.9 m2 simplified at 7 decimals, 0.2 m2 at 9.
        geoms = shapely.set_coordinates(
            gdf.geometry.values.copy(),
            np.round(shapely.get_coordinates(gdf.geometry.values), self.COORD_DECIMALS))
        # A ring valid in metres can fold on itself once reprojected and rounded — the transform is
        # non-linear in y and the grid is finite (measured: 12 of 243,265 pieces on 8-63-105-15,
        # none of them invalid in EPSG:3857). Repair those rather than ship an invalid ring into
        # tippecanoe's wagyu: is_valid over a batch is ~18 s at 243k rows, make_valid seconds on
        # the largest offender. Exploding keeps the layer uniformly polygon (FlatGeobuf rejects a
        # mixed one), since make_valid can return a MultiPolygon or a collection.
        ok = shapely.is_valid(geoms)
        if not ok.all():
            geoms = [g if v else shapely.make_valid(g) for g, v in zip(geoms, ok)]
        gdf = gdf.set_geometry(gpd.GeoSeries(geoms, index=gdf.index, crs="EPSG:4326"))
        if not ok.all():
            gdf = gdf.explode(index_parts=False)
            gdf = gdf[gdf.geom_type == "Polygon"]
            print(f"depare: repaired {int((~ok).sum())} row(s) the 4326 write folded", flush=True)
        gdf.to_file(self.seq, driver="GeoJSONSeq", mode="a" if self.count else "w",
                    COORDINATE_PRECISION=self.COORD_DECIMALS)
        self.count += len(gdf)   # rows WRITTEN: a repair can split one row into parts
        self.pending = []

    def finish(self, final_fgb):
        self.flush()
        # --config OGR_GEOJSON_MAX_OBJ_SIZE 0 disables GDAL's 200 MB per-feature ceiling in the
        # GeoJSONSeq reader (ogrgeojsonseqdriver.cpp: the limit is only enforced while
        # m_nMaxObjectSize > 0, and the option parses to 0). A marsh stem's depth band is one
        # MultiPolygon of 40k+ parts and exceeds it: 8-63-105-15 and 8-63-106-15 both died here,
        # after the full partition pass had already run. Keep the band ONE feature — splitting it
        # would change feature counts and ids, i.e. the partition contract.
        contour_run._run(f"ogr2ogr --config OGR_GEOJSON_MAX_OBJ_SIZE 0 "
                         f"-f FlatGeobuf -overwrite {final_fgb} {self.seq}",
                         "ogr2ogr depare")
        os.remove(self.seq)
        return self.count


def _depare_dem(dem, tile_obj, child_z, tmp, label, timeout=0):
    """Partition any DEM covering the tile's buffered extent into depth-area / drying / nodata
    rows. Returns (final_path, count) inside ``tmp``, or None when there is no water."""
    import geopandas as gpd
    import landmask
    import shapely
    from shapely import make_valid
    from shapely.geometry import box
    from shapely.strtree import STRtree

    clip = box(*mercantile.xy_bounds(tile_obj))  # unbuffered, tile-aligned (EPSG:3857)
    with rasterio.open(dem) as d:
        b, res = d.bounds, abs(d.transform.a)
    bbox = (b.left, b.bottom, b.right, b.top)  # the DEM's full (buffered) extent, EPSG:3857
    buffered = box(*bbox)
    min_area = SLIVER_MIN_PX * res * res       # slivers where a vector edge meets the raster shore
    stem_res = get_resolution(child_z)          # the stem's own MVT pixel, EPSG:3857 metres
    nodata_tol = NODATA_SIMPLIFY_PX * stem_res  # generalize nodata to the stem's resolution
    band_tol = SIMPLIFY_MM / MM_PER_PX * stem_res  # the S-58 vertex floor at this stem's scale
    sink = _RowSink(f"{tmp}/depare-rows.geojsons")

    # ── depth bands + drying ── the metre + fathom partition ladders, each off one gdal_contour -p
    # pass, read back bucket by bucket (amax == a ladder level, exactly what gdal_contour wrote).
    # Clip in shapely (not ogr2ogr -clipsrc — the GDAL-3.8 GeometryCollection trap). The metre pass
    # carries DRYING_CAP as an extra positive level, so it ALSO yields the [0, cap] drying bucket,
    # whose 0 m seaward edge is the same ring as the shoal band's amax=0 edge. The metre bands'
    # pre-clip (buffered) coverage parts are the water footprint the nodata pass subtracts; both
    # ladders cover the same water pixels, so the metre parts stand for it.
    #
    # A ladder is simplified as ONE coverage before anything else touches it — simplify first and
    # the clip, the subdivision, the nodata differences and the write all run on 5x less geometry.
    # The DRYING BUCKET is a member of BOTH ladders' coverages even though only the metre pass
    # emits it: it is the shoal band's neighbour along the 0 m ring in each ladder, and if only one
    # ladder simplified it the other's shoalest band would part company with the drying that ships
    # (measured: with the raw bucket in both, the two ladders simplify it to within 0-1.64 m and
    # 66 m2; without, 2-3x that, plus overlaps). m and ft can never be ONE coverage — they cover the
    # same water by design.
    coverage_parts = []
    drying_raw = []       # the bucket as contoured — the ft ladder's coverage member
    drying_geoms = []     # the same bucket simplified with the metre ladder — what ships
    _mark(None)
    for sys_tag, levels in (("m", config.DEPARE_LEVELS + [config.DRYING_CAP]),
                            ("ft", config.DEPARE_LEVELS_FT)):
        raw = partitions(dem, levels, f"{tmp}/depare-raw-{sys_tag}.fgb", timeout=timeout)
        drvals, bands = [], []
        for lvl in [l for l in levels if l <= 0]:
            for r in read_bucket(raw, f"amax = {lvl}").itertuples():
                drvals.append((r.drval1, r.drval2))
                bands.append(repaired_multi([r.geometry]))
        if sys_tag == "m":
            # The [0, DRYING_CAP] bucket, keyed on amax alone: 0 and the cap are discrete levels
            # and every other level is negative, so 0 < amax <= cap uniquely picks it regardless
            # of the garbage amin. Land above the cap (amax > cap) is dropped.
            bucket = repaired_multi(read_bucket(
                raw, f"amax > 0 AND amax <= {config.DRYING_CAP}").geometry)
            drying_raw = [] if bucket.is_empty else [bucket]
        _mark(f"bands-read-{sys_tag}")
        simplified = simplify_coverage(bands + drying_raw, band_tol)
        bands = simplified[:len(drvals)]
        if sys_tag == "m":
            drying_geoms = simplified[len(drvals):]
        _mark(f"bands-simplify-{sys_tag}")
        for (drval1, drval2), geom in zip(drvals, bands):
            if sys_tag == "m":
                coverage_parts += [piece for p in _polys(geom) for piece in _subdivide(p)]
            # flush=False: the sink batches at its own FLUSH, so one band's rows are not one
            # GeoDataFrame conversion of the whole ladder.
            sink.write([{"geometry": p, "drval1": drval1, "drval2": drval2,
                         "sys": sys_tag, "kind": None, "rank": BAND_RANK}
                        for p in _polys(geom.intersection(clip))], flush=False)
        _mark(f"bands-clip-{sys_tag}")
    # Coverage stays a PARTS list (exploded to single polygons for tight envelopes), never one
    # union: the nodata pass differences each water feature against only the parts its envelope
    # intersects — identical output (a disjoint part is a no-op), bounded local work. The
    # monolithic union made every feature pay the whole window's vertex count (the 8.9 h stems).

    # Inland-water feed, read once by bbox (the nodata pass iterates its features for `kind`; the
    # drying cut unions its geometry). Optional: absent -> no water term (today's land-only gate).
    water_src = landmask.water_path()
    water = None
    if landmask._present(water_src):
        w = gpd.read_file(water_src, bbox=bbox)
        if len(w):
            water = w.to_crs("EPSG:3857")
    _mark("water-read")
    # Parts, same reason as coverage: the union's one consumer (the drying water term) only
    # needs the parts near the bucket.
    water_parts = [make_valid(g) for g in water.geometry] if water is not None else []
    _mark("water-make-valid")

    # ── drying ── fold the [0, DRYING_CAP] foreshore in as DEPARE with a negative drval1. Cut the
    # landward side by EFFECTIVE land = OSM land ∖ OSM inland water — the load-bearing point: the
    # osmdata land product does NOT punch inland water out as holes, so a tidal channel OSM maps as
    # a water polygon sits INSIDE the land coverage; cutting by raw land would delete the drying
    # flats in and along that channel (the ICW/tidal-river failure). effective_water = (NOT land)
    # OR water, so drying = bucket.difference(land) ∪ bucket.intersection(water) — matching the
    # raster gate (rasterize burns land=1 then water=0) without materialising land ∖ water. Absent
    # land.fgb -> no landward cut (degrade; land.fgb is effectively always present); absent
    # water.fgb -> effective_water = NOT land (the union term is empty). The bucket arrives
    # simplified in the SAME coverage pass as the bands, which is what keeps the shared 0 m edge
    # aligned; clip in shapely; the min-area filter drops seam slivers.
    drying_area = None
    if drying_geoms:
        bucket = coverage_union(drying_geoms)
        _mark("drying-bucket-union")
        land_src = landmask.path()
        land_geom = None
        if landmask._present(land_src):
            land = gpd.read_file(land_src, bbox=bbox)
            if len(land):
                land_geom = valid_union(list(land.to_crs("EPSG:3857").geometry))
        _mark("land-read-union")
        if land_geom is None:
            effective = bucket  # no land coverage here -> nothing to cut
        else:
            effective = bucket.difference(land_geom)
            _mark("drying-diff-land")
            if water_parts:
                # Prune to water parts that truly intersect the bucket (predicate, like the nodata
                # pass — cut drying-water-terms 63->17 s on 6-19-18-9), then ONE float intersection
                # against their union: the window-spanning BUCKET is the unbounded operand, so
                # pairwise would be quadratic (912 s+ measured). No GRID here — this shape measured
                # byte-exact on every profile stem, and snap-rounding traded that for nothing.
                near = [water_parts[i]
                        for i in STRtree(water_parts).query(bucket, predicate="intersects")]
                if near:
                    effective = valid_union([effective, bucket.intersection(shapely.union_all(near))])
                _mark("drying-water-terms")
        effective = make_valid(effective)
        _mark("drying-make-valid")
        if not effective.is_empty:
            drying_area = effective  # subtracted from nodata below (over the buffered extent)
            drying_rows = []
            for full in _polys(effective):  # gate the PRE-clip polygon so a seam sliver of a big flat survives both sides
                if full.area >= min_area:
                    for p in _polys(full.intersection(clip)):  # clip the survivors; no re-filter on the piece
                        drying_rows.append({"geometry": p, "drval1": -config.DRYING_CAP,
                                            "drval2": 0.0, "sys": None, "kind": None,
                                            "rank": DRYING_RANK})
            sink.write(drying_rows)
            _mark("drying-emit")

    # ── nodata ── inland water we hold no depth for: the OSM water polygons (bbox-read, clipped to
    # the buffered tile) MINUS the water-coverage footprint (depth bands ∪ drying) — a #24-cleared
    # lake the merge left as nodata produces no band, so its whole polygon survives; a surveyed lake
    # nets to slivers the min-area filter drops. No drval (absence is the encoding) + a `kind`
    # passthrough. Ocean has no water polygon, so it gains nothing. Skipped when no water feed.
    if water is not None:
        parts = list(coverage_parts)
        if drying_area is not None:
            parts += [piece for p in _polys(drying_area) for piece in _subdivide(p)]
        tree = STRtree(parts) if parts else None
        _mark("nodata-tree")
        for r in water.itertuples():
            geom = make_valid(r.geometry).intersection(buffered)
            if tree is not None and not geom.is_empty:
                # predicate="intersects" (prepared), not bare envelope query: most inland lakes
                # never truly touch coverage and skip the difference entirely. Parts are
                # subdivided to bounded vertex counts so the local union stays small —
                # differencing one window-wide union per feature was the original 8.9 h tail,
                # and sequential pairwise differences are O(hits × |geom|) (31 min on the
                # harbor stem). GRID makes the union-operand overlay robust (see GRID).
                hits = tree.query(geom, predicate="intersects")
                if len(hits):
                    u = shapely.union_all([parts[i] for i in hits], grid_size=GRID)
                    geom = shapely.difference(geom, u, grid_size=GRID)
            kind = getattr(r, "kind", None)
            for full in _polys(geom):  # gate the PRE-clip polygon (buffered window) so a seam sliver survives both sides
                if full.area >= min_area:
                    for p in _polys(full.intersection(clip)):  # then clip to the seam; no re-filter on the piece
                        # Simplify POST-clip, per-piece: kept vertices are a subset inside the clip
                        # box, so the ring can never cross the seam outward — at worst it recedes
                        # ≤ tol from the clip line (resolution-scale wobble, like the raster staircase).
                        # Pre-clip would let far-away geometry change vertex picks near the seam.
                        s = p.simplify(nodata_tol, preserve_topology=True)
                        if s.is_empty:
                            continue
                        sink.write([{"geometry": sp, "drval1": None, "drval2": None,
                                     "sys": None, "kind": kind, "rank": NODATA_RANK}
                                    for sp in _polys(s)], flush=False)
        _mark("nodata-loop")

    if not sink.count and not sink.pending:
        print(f"depare: no water in tile bbox for {label}")
        return None

    final = f"{tmp}/depare-final.fgb"
    n = sink.finish(final)
    _mark("write-fgb")
    return final, n


def tile(stem):
    """The per-stem Snakemake job: partition one stem from a BUFFERED mosaic window, smoothed at
    read with the one shared f(depth, zoom), output at store/depare/<stem>.fgb (depare also reads
    the land + water masks). A waterless tile writes a 0-byte sentinel; bundling filters empties by
    size."""
    import shutil
    import signal
    import tempfile

    # DEPARE_TIMEOUT (seconds; unset = no bound): each gdal_contour -p pass runs under this
    # bound; on expiry the tile retries once on a uniform 4x-average window, then fails
    # honestly. The SIGALRM backstop (8x: 2 ladders x 2 attempts x contour + GEOS slack) still
    # bounds the whole tile so no phase can hang a run (docs/plans/2026-07-21-depare-perf.md).
    timeout = int(os.environ.get("DEPARE_TIMEOUT", "0"))
    if timeout:
        def _alarm(*_):
            print(f"depare tile {stem}: exceeded {timeout * 8}s wall clock — failing honestly",
                  file=sys.stderr, flush=True)
            sys.exit(124)
        signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(timeout * 8)
    if os.environ.get("DEPARE_TIMING"):
        _heartbeat(stem)
    z, x, y, child_z = (int(a) for a in stem.split("-"))
    out = f"store/depare/{stem}.fgb"
    tmp = tempfile.mkdtemp(prefix=f"depare-{stem}-")  # local scratch; publish crosses to the store
    try:
        _mark(None)
        # The shared smoothed window is read-only; the timeout fallback derives its
        # coarsened copy in tmp rather than touching it.
        dem = f"store/window/{stem}.tif"
        _mark("window-dem")
        tile_obj = mercantile.Tile(x=x, y=y, z=z)
        try:
            res = _depare_dem(dem, tile_obj, child_z, tmp, stem, timeout=timeout)
        except ContourTimeout as e:
            print(f"depare tile {stem}: {e} — retrying on a uniform 4x-average window",
                  file=sys.stderr, flush=True)
            dem = _uniform_coarsen(dem, 4, f"{tmp}/dem-4x.tiff")
            res = _depare_dem(dem, tile_obj, child_z, tmp, stem, timeout=timeout)
        except MemoryError:
            # An allocation the kernel refused (overcommit says no when a single request
            # exceeds free RAM+swap) — no OOM kill, and formatting a traceback at that point
            # can itself fail, so a bare MemoryError otherwise exits 1 with an EMPTY log.
            # Report the phase and footprint from a preallocated string: this path must not
            # allocate. Marsh stems are the ones that get here (see the perf backlog).
            os.write(2, _OOM_NOTE % (stem.encode(), _rss_kb()))
            _save_traceback(stem)
            raise
        except BaseException:
            # The rule redirects stderr with `2> {log}`, which TRUNCATES when snakemake starts the
            # retry, so a failed attempt's traceback is destroyed within a second of being written
            # and the job looks like it failed silently. Keep a copy beside the output, where a
            # retry cannot erase it. (Changing the redirect to `2>>` would edit the rule's
            # shellcmd, which snakemake hashes as rule CODE — that re-runs every depare tile.)
            _save_traceback(stem)
            raise
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if res:
            final, n = res
            utils.publish(final, out)  # scratch and store are separate filesystems
            # Window bytes alongside the polygon count: a COMPRESSED window's size tracks its
            # geometric detail, which is what drives this rule's peak RSS (marsh coastlines
            # compress worst and cost most). Pairs with the benchmark row so DEPARE_GB can be
            # fitted against it instead of child_z alone — see the backlog.
            print(f"depare tile {stem}: {n} polygons, window {os.path.getsize(dem) / 1e9:.2f} GB")
        else:
            open(out, "w").close()
            print(f"depare tile {stem}: empty")
    finally:
        # Always disarm + clean up: a body exception with the alarm still armed could fire during
        # unwinding and mask the real error as exit 124, and would leak the tmp dir.
        if timeout:
            signal.alarm(0)
        shutil.rmtree(tmp, ignore_errors=True)


def _check():
    """gdal_contour -p buckets on a synthetic DEM: land above the cap dropped, the metre ladder's
    extra DRYING_CAP level yields the [0, DRYING_CAP] drying bucket (selected by 0 < amax <= cap,
    never amin), each water depth lands in its ladder bucket, the water bands are pairwise disjoint
    and jointly cover the water, the ladders ascend and end at 0, and the buckets are deterministic
    (the seam contract reduces to this). The effective-land drying cut is exercised end-to-end
    against real masks in test_engine.check_depare_drying."""
    import tempfile

    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    from shapely.geometry import Point
    from shapely.ops import unary_union

    for levels in (config.DEPARE_LEVELS, config.DEPARE_LEVELS_FT):
        assert levels == sorted(levels) and levels[-1] == 0, "levels must ascend and end at 0"
    assert config.DRYING_CAP > 0, "DRYING_CAP must be a positive level above 0"

    # valid_union must make_valid before unioning: gdal_contour emits self-touching rings that
    # poison a raw union ("side location conflict"). A bowtie is invalid on every GEOS version,
    # and a raw union of it stays invalid — valid_union must return valid geometry.
    from shapely.geometry import Polygon
    bowtie = Polygon([(0, 0), (2, 2), (2, 0), (0, 2), (0, 0)])
    assert not bowtie.is_valid and valid_union([bowtie]).is_valid, \
        "valid_union must make_valid before union (guards the contour side-location-conflict fix)"
    assert coverage_union([bowtie]).is_valid, "coverage_union must repair before dissolving"

    # The second chance: parts sharing an edge segment dissolve into an INVALID multi with the
    # area preserved (the shape of every guard trip in run 30634360224). Re-noding the dissolve
    # output must return valid geometry without paying for the full input union.
    from shapely.geometry import box as _box
    _u = coverage_union([_box(0, 0, 2, 1), _box(1, 1, 3, 2)])
    assert _u.is_valid and abs(_u.area - 4.0) < 1e-9, \
        "coverage_union second chance must re-node an edge-sharing dissolve"

    # coverage_union dissolves a true coverage along its shared edges, and its result gate must
    # catch input that is not one: overlapping parts come back unmerged with an area sum that
    # still matches, so only the validity half of the gate sees them.
    from shapely.geometry import box as _box
    assert coverage_union([_box(0, 0, 1, 1), _box(1, 0, 2, 1)]).equals(_box(0, 0, 2, 1)), \
        "coverage_union must merge along shared edges"
    assert coverage_union([_box(0, 0, 2, 1), _box(1, 0, 3, 1)]).equals(_box(0, 0, 3, 1)), \
        "coverage_union must fall back to unary_union when the parts overlap"

    # Coverage simplification is the whole partition contract in one call: two bands sharing a
    # dense chain must come back sharing the IDENTICAL chain — same vertices, in both members —
    # while shedding vertices, holding the area, and inventing no point the raw geometry lacked
    # (the contour lines are pinned to those points). A per-geometry simplify is what fails this.
    import shapely
    _nc = shapely.get_num_coordinates
    xs = np.linspace(0.0, 400.0, 2000)
    chain = [(float(x), 200.0 + 0.8 * np.sin(x / 3.0) + 0.5 * np.sin(x)) for x in xs]
    upper = Polygon(chain + [(400.0, 400.0), (0.0, 400.0)])
    lower = Polygon(chain + [(400.0, 0.0), (0.0, 0.0)])
    tol_mm = SIMPLIFY_MM / MM_PER_PX * get_resolution(15)
    up, lo = simplify_coverage([upper, lower], tol_mm)
    assert _nc(up) + _nc(lo) < (_nc(upper) + _nc(lower)) / 2, "coverage simplify must shed vertices"
    raw_pts = {tuple(p) for p in shapely.get_coordinates([upper, lower])}
    assert not {tuple(p) for p in shapely.get_coordinates([up, lo])} - raw_pts, \
        "coverage simplify must not invent vertices (the contour lines are pinned to them)"
    shared = shapely.intersection(up, lo)
    up_pts = {tuple(p) for p in shapely.get_coordinates(up)}
    lo_pts = {tuple(p) for p in shapely.get_coordinates(lo)}
    assert {tuple(p) for p in shapely.get_coordinates(shared)} <= (up_pts & lo_pts), \
        "simplified adjacent bands must share an identical boundary chain (no crack)"
    assert shapely.intersection(up, lo).area == 0 and up.is_valid and lo.is_valid, \
        "simplified bands must stay a partition"
    # The result gate rejects a non-coverage rather than shipping a silent crack. Both ways in:
    # members whose "shared" chain does not match lose area to the simplifier, and an invalid ring
    # either loses area or raises out of it as a ZeroDivisionError, not a GEOSException.
    off = Polygon([(x, y + 1.0) for x, y in chain] + [(400.0, 0.0), (0.0, 0.0)])
    assert all(a is b for a, b in zip(simplify_coverage([upper, off], 100.0), [upper, off])), \
        "a broken coverage must fall back to raw geometry"
    assert simplify_coverage([bowtie], tol_mm) == [bowtie], \
        "a ring the simplifier cannot handle must fall back, not fail the tile"

    # The sink must not write an invalid ring: the 3857 -> 4326 reprojection folds a small number
    # of generalized rings, so it repairs whatever comes back invalid and keeps the layer polygon.
    import geopandas as gpd
    d0 = tempfile.mkdtemp()
    sink = _RowSink(f"{d0}/rows.geojsons")
    sink.write([{"geometry": bowtie, "drval1": 0.0, "drval2": 2.0, "sys": "m",
                 "kind": None, "rank": BAND_RANK}])
    n = sink.finish(f"{d0}/out.fgb")
    written = gpd.read_file(f"{d0}/out.fgb")
    assert n == len(written) and len(written), f"sink count {n} vs {len(written)} rows written"
    assert bool(shapely.is_valid(written.geometry.values).all()), \
        "the sink must repair rows the 4326 write leaves invalid"
    assert set(written.geom_type) == {"Polygon"}, \
        f"the layer must stay uniformly polygon, got {set(written.geom_type)}"

    # ContourTimeout mapping: a bounded command that exceeds its budget must surface as
    # ContourTimeout (the tile's retry trigger), not a generic failure.
    try:
        _run_bounded("sleep 5", "sleep", timeout=1)
        raise AssertionError("_run_bounded must raise on timeout")
    except ContourTimeout:
        pass

    # nodata simplification: a dense OSM-style outline generalizes to the stem's resolution,
    # shedding vertices while its area barely moves — and the post-clip, per-piece simplify
    # never pushes a piece's boundary outward across the clip line (the seam contract).
    from shapely import get_num_coordinates
    tol = NODATA_SIMPLIFY_PX * get_resolution(14)
    ring = [(1000.0 * np.cos(t) + 0.37 * tol * np.sin(60 * t),
             1000.0 * np.sin(t) + 0.37 * tol * np.cos(60 * t))
            for t in np.linspace(0, 2 * np.pi, 4000, endpoint=False)]  # dense, sub-tol wobble
    dense = Polygon(ring + [ring[0]])
    simp = dense.simplify(tol, preserve_topology=True)
    assert get_num_coordinates(simp) < get_num_coordinates(dense) / 4, "nodata simplify must shed vertices"
    assert abs(simp.area - dense.area) < 0.02 * dense.area, "nodata simplify must preserve area"
    # A piece clipped to a box, then simplified: its vertices are a subset inside the clip box, so
    # the shared edge stays exactly on the clip line (post-clip simplify never crosses the seam).
    clipbox = _box(0, -2000, 2000, 2000)  # cuts the disc through its centre at x=0
    for piece in _polys(dense.intersection(clipbox)):
        s = piece.simplify(tol, preserve_topology=True)
        if s.is_empty:
            continue
        for sp in _polys(s):
            xs = [c[0] for c in sp.exterior.coords]
            assert min(xs) >= -1e-6, "post-clip simplify must not push a vertex across the clip line"

    d = tempfile.mkdtemp()
    h = w = 60
    res = 100.0
    tr = from_origin(0, h * res, res, res)  # top-left origin, EPSG:3857
    cap = config.DRYING_CAP
    levels_m = config.DEPARE_LEVELS + [cap]
    # Top-down: land above the cap (dropped), a [0, cap] foreshore (the drying bucket), then four
    # water bands stepping deeper — each 10 rows, values strictly inside a bucket. Step transitions
    # interpolate through the intervening levels, so extra sliver partitions are expected and fine.
    dem = np.full((h, w), cap + 50, dtype="float32")     # rows 0-9: land above the cap
    dem[10:20, :] = 2.0                                  # rows 10-19: [0, cap] foreshore -> drying
    for i, v in enumerate([-1.0, -7.0, -25.0, -150.0]):
        dem[(i + 2) * 10:(i + 3) * 10, :] = v            # rows 20-59: four water bands
    p = f"{d}/dem.tif"
    with rasterio.open(p, "w", driver="GTiff", height=h, width=w, count=1, dtype="float32",
                       nodata=-9999, crs="EPSG:3857", transform=tr) as dst:
        dst.write(dem, 1)

    raw = partitions(p, levels_m, f"{d}/raw.fgb")
    bands = read_bucket(raw, "amax <= 0")
    drying = read_bucket(raw, f"amax > 0 AND amax <= {cap}")
    assert len(bands) and (bands["drval1"] >= 0).all(), "water bands must have drval1 >= 0"
    assert (bands["drval1"] < bands["drval2"]).all(), "drval1 must be the shallow bound"

    def bucket_at(gdf, row):
        pt = Point(tr * (w / 2 + 0.5, row + 0.5))
        hit = gdf[gdf.covers(pt)]
        assert len(hit) == 1, f"exactly one partition must cover row {row}, got {len(hit)}"
        return (hit.iloc[0]["drval1"], hit.iloc[0]["drval2"])

    assert bucket_at(bands, 25) == (0.0, 2.0), "-1 m must land in the [0,2] bucket"
    assert bucket_at(bands, 35) == (5.0, 10.0), "-7 m must land in the [5,10] bucket"
    assert bucket_at(bands, 45) == (20.0, 30.0), "-25 m must land in the [20,30] bucket"
    assert bucket_at(bands, 55) == (100.0, 200.0), "-150 m must land in the [100,200] bucket"

    # The drying bucket: the [0, cap] foreshore, NOT a water band, NOT the above-cap land.
    fore = Point(tr * (w / 2 + 0.5, 15.5))
    land = Point(tr * (w / 2 + 0.5, 5.5))
    assert len(drying) == 1 and drying.covers(fore).any(), "the [0, cap] foreshore is the drying bucket"
    assert not bands.covers(fore).any(), "the foreshore is not a water band (amax > 0)"
    assert not drying.covers(land).any() and not bands.covers(land).any(), \
        "land above the cap is dropped from bands and drying alike"

    # The fill contract for the water bands: pairwise disjoint (sum of areas == union area) and
    # jointly covering the water (union area == the 40 water rows, ± the interpolated band edges).
    union = unary_union(list(bands.geometry))
    assert abs(bands.geometry.area.sum() - union.area) < 1e-6 * union.area, "bands overlap"
    water = 40 * w * res * res
    assert abs(union.area - water) < 1.5 * w * res * res, \
        f"bands must tile the water ({union.area:.0f} vs {water:.0f})"

    # Fathom-curve set (no cap — drying rides the metre ladder only): -7 m sits between 3 fm and 5 fm.
    gft_bands = read_bucket(partitions(p, config.DEPARE_LEVELS_FT, f"{d}/raw-ft.fgb"),
                            "amax <= 0")
    d1, d2 = bucket_at(gft_bands, 35)
    assert abs(d1 - 3 * 1.8288) < 1e-6 and abs(d2 - 5 * 1.8288) < 1e-6, (d1, d2)

    # Deterministic: same DEM -> byte-identical buckets (the drying bucket included).
    g2 = read_bucket(partitions(p, levels_m, f"{d}/raw2.fgb"), "amax IS NOT NULL")
    g = read_bucket(raw, "amax IS NOT NULL")
    assert sorted(x.wkb for x in g.geometry) == sorted(x.wkb for x in g2.geometry), \
        "partitions not deterministic"

    # A uniform-0 DEM (terrain exactly at datum) yields NO depth band — it falls in the
    # [0, DRYING_CAP] drying bucket, so a merge-filled-0 area tints as drying foreshore, never a
    # false shoal. (The cleared-lake NODATA path is separate: gdal_contour skips NODATA pixels, so
    # a genuinely-unfilled lake interior carries no bucket and renders as nodata — see
    # check_depare_water. Only a thin 0-filled rim at a cleared lake's edge lands in this bucket.)
    flat = np.zeros((h, w), dtype="float32")
    fp = f"{d}/flat.tif"
    with rasterio.open(fp, "w", driver="GTiff", height=h, width=w, count=1, dtype="float32",
                       nodata=-9999, crs="EPSG:3857", transform=tr) as dst:
        dst.write(flat, 1)
    flat_g = read_bucket(partitions(fp, levels_m, f"{d}/flat-raw.fgb"), "amax <= 0")
    assert len(flat_g) == 0, \
        "a uniform-0 surface must produce no depth band (it's the drying bucket, not a shoal tint)"
    print(f"depare_run self-check ok ({len(bands)} m-bands, {len(drying)} drying, "
          f"{len(gft_bands)} ft-bands)")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["tile"] and len(a) == 2:
        tile(a[1])
    elif a[:1] == ["check"]:
        _check()
    else:
        sys.exit("usage: depare_run.py tile <stem> | check")
