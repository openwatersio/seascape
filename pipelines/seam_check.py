"""Seam gate: verify contour and depth-area continuity across mosaic tile edges.

The stage-3 forks each window one continuous mosaic through a BUFFERED read and restrict output to
the unbuffered tile (see contour_run.tile / depare_run.tile). The promise of that buffer-input /
restrict-output design is that adjacent tiles' lines and polygons meet EXACTLY at the shared tile
boundary — no gap, no doubled line, no kink. This module checks that promise on a built store, so a
regression in the windowing (wrong buffer, wrong clip, resolution slip at a hi-res/GEBCO boundary)
fails a gate instead of shipping cracked isobaths.

The idea, per shared edge (the mercantile boundary two adjacent tiles share, in EPSG:4326 since the
FGBs are 4326):

  contours — every point where one tile's isobath meets the edge must have a matching point from the
    neighbour at the SAME depth level, within a few pixels. Outputs are clipped to the tile bbox, so
    an isobath that spanned the seam terminates ON it from each side; the two termini must coincide.

  depare — every (drval1, drval2, sys) band's coverage of the edge, projected to 1-D intervals along
    the boundary, must match the neighbour's. A band that abuts the seam from both tiles must cover
    the same span; the symmetric-difference length must be under a pixel-scaled tolerance.

Tolerance is derived from the tile pixel size in degrees (via mercantile), using the COARSER of the
two neighbours (a coarse tile's halo upsamples into its finer neighbour — the equivalent-not-equal
behaviour of window_dem). Handled: 0-byte sentinel files (an empty tile — a
pair where one side is empty and the other still crosses the seam is a FAIL), antimeridian pairs
(the ±180 seam, skipped with a note — their coordinates don't touch numerically), and neighbours
with differing child_z (coarser wins the tolerance).

Decoupled by construction: reads store paths directly and shells out to ogr2ogr; imports no pipeline
module, so a change to the stage-3 code it validates can't break the gate.

CLI:
  seam_check.py contours <stemA> <stemB>   # two ADJACENT covering tiles (z-x-y-cz)
  seam_check.py depare   <stemA> <stemB>
  seam_check.py auto                        # every adjacent built pair in the covering; nonzero exit on any FAIL
  seam_check.py --check                     # self-test on synthetic FGB pairs
"""

import json
import math  # noqa: F401  (kept available for tolerance math; degree spans stay linear here)
import os
import subprocess
import sys

import mercantile

# Tolerances, all in units of the coarser tile's pixel (get_resolution treats a web-mercator tile
# as 512 px; we mirror that in degrees below). A crossing/interval within TOL_PIXELS matches; a
# vertex within ON_EDGE_PIXELS of the boundary counts as sitting ON it (clip endpoints land there);
# -spat reads a SPAT_PAD_PIXELS halo so a feature merely touching the edge is still selected.
TOL_PIXELS = 3.0
ON_EDGE_PIXELS = 0.1
SPAT_PAD_PIXELS = 4.0
# Tile boundaries are dyadic (360/2**z divisions), so neighbours' shared coordinates are equal to
# float precision — this epsilon only guards the exact-match comparisons, not real tolerance.
BOUND_EPS = 1e-9

COVERING = "store/aggregation/covering.txt"


def pixel_deg(child_z):
    """Degrees per pixel at zoom child_z, matching aggregation_reproject.get_resolution's 512-px
    tile: a tile spans 360/2**child_z degrees of longitude, so one pixel is that / 512. Longitude
    degrees are used uniformly (latitude degrees shrink under mercator, but the tolerance is a
    pixel-scale slack, not a metric distance, and a single scale keeps it simple)."""
    b = mercantile.bounds(mercantile.Tile(x=0, y=0, z=child_z))
    return (b.east - b.west) / 512.0


class Edge:
    """A shared tile boundary as a 1-D line. kind 'lon' = a meridian (constant longitude; the
    crossing coordinate is latitude), kind 'lat' = a parallel (constant latitude; the coordinate is
    longitude). [lo, hi] is the shared segment's span in the crossing coordinate."""

    def __init__(self, kind, const, lo, hi):
        self.kind = kind      # 'lon' | 'lat'
        self.const = const    # the constant coordinate of the boundary line
        self.lo = lo
        self.hi = hi


def parse_stem(stem):
    """(z, x, y, child_z) from a z-x-y-cz stem string."""
    parts = stem.split("-")
    if len(parts) != 4:
        raise SystemExit(f"seam_check: bad stem {stem!r} (want z-x-y-childz)")
    return tuple(int(a) for a in parts)


def _bounds(stem):
    z, x, y, _cz = stem
    return mercantile.bounds(mercantile.Tile(x=x, y=y, z=z))


def shared_edge(A, B):
    """The Edge two parsed stems share, the string "antimeridian" for a ±180 wrap pair (skip with a
    note), or None when they are not edge-adjacent. Adjacency is geometric — touching bounds with an
    overlapping span — so same-zoom and cross-zoom neighbours both resolve."""
    ba, bb = _bounds(A), _bounds(B)
    # meridian adjacency: one tile's east edge equals the other's west edge, latitude spans overlap
    for l, r in ((ba, bb), (bb, ba)):
        if abs(l.east - r.west) <= BOUND_EPS:
            lo, hi = max(l.south, r.south), min(l.north, r.north)
            if hi - lo > BOUND_EPS:
                return Edge("lon", l.east, lo, hi)
    # parallel adjacency: one tile's south edge equals the other's north edge, longitude spans overlap
    for t, b in ((ba, bb), (bb, ba)):
        if abs(t.south - b.north) <= BOUND_EPS:
            lo, hi = max(t.west, b.west), min(t.east, b.east)
            if hi - lo > BOUND_EPS:
                return Edge("lat", t.south, lo, hi)
    # antimeridian: opposite ±180 seams with an overlapping latitude span — real neighbours whose
    # coordinates don't touch numerically, so a project-and-compare would be meaningless. Skip.
    for east, west in ((ba, bb), (bb, ba)):
        if abs(east.east - 180.0) <= BOUND_EPS and abs(west.west + 180.0) <= BOUND_EPS:
            if min(east.north, west.north) - max(east.south, west.south) > BOUND_EPS:
                return "antimeridian"
    return None


# ── reading FGBs (ogr2ogr -> GeoJSON; no shapely/fiona dependency) ─────────────

def _read(fgb, edge, tol):
    """Features (as GeoJSON dicts) from an FGB whose extent touches the edge. A 0-byte sentinel (an
    empty tile) reads as no features. -spat SELECTS features near the edge — never -clipsrc, which
    would inject boundary-following vertices and forge on-edge segments that aren't in the data."""
    if not os.path.exists(fgb):
        raise SystemExit(f"seam_check: missing {fgb}")
    if os.path.getsize(fgb) == 0:      # empty-tile sentinel
        return []
    pad = SPAT_PAD_PIXELS * (tol / TOL_PIXELS)  # a few pixels of selection halo
    if edge.kind == "lon":
        spat = (edge.const - pad, edge.lo - pad, edge.const + pad, edge.hi + pad)
    else:
        spat = (edge.lo - pad, edge.const - pad, edge.hi + pad, edge.const + pad)
    p = subprocess.run(
        ["ogr2ogr", "-f", "GeoJSON", "/vsistdout/", "-spat", *[repr(c) for c in spat], fgb],
        capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(f"seam_check: ogr2ogr failed on {fgb}:\n{p.stderr}")
    return json.loads(p.stdout).get("features", [])


def _lines(geom):
    """Every LineString as a [[x, y], ...] list from a (Multi)LineString GeoJSON geometry."""
    if geom["type"] == "LineString":
        return [geom["coordinates"]]
    if geom["type"] == "MultiLineString":
        return geom["coordinates"]
    return []


def _rings(geom):
    """Every ring (exterior + holes) as a [[x, y], ...] list from a (Multi)Polygon geometry."""
    if geom["type"] == "Polygon":
        return list(geom["coordinates"])
    if geom["type"] == "MultiPolygon":
        return [ring for poly in geom["coordinates"] for ring in poly]
    return []


# ── contours: crossing points per depth level ─────────────────────────────────

def _crossings(line, edge, on_eps):
    """Positions (the crossing coordinate) where a polyline meets the edge line: a vertex sitting on
    the boundary (the usual case — clipped outputs terminate ON the seam) or a segment straddling it.
    Returns raw positions within the shared span; the caller dedupes."""
    ax = 0 if edge.kind == "lon" else 1   # index of the constant coordinate
    pos = 1 - ax                          # index of the coordinate along the edge
    out = []
    for p, q in zip(line, line[1:]):
        d1, d2 = p[ax] - edge.const, q[ax] - edge.const
        if abs(d1) <= on_eps:
            out.append(p[pos])
        if abs(d2) <= on_eps:
            out.append(q[pos])
        if abs(d1) > on_eps and abs(d2) > on_eps and (d1 < 0) != (d2 < 0):
            f = d1 / (d1 - d2)            # edge.const = p[ax] + f*(q[ax]-p[ax])
            out.append(p[pos] + f * (q[pos] - p[pos]))
    return [v for v in out if edge.lo - on_eps <= v <= edge.hi + on_eps]


def _dedupe(vals, eps):
    """Collapse positions within eps (an isobath touches the seam once even if two segments share
    that vertex)."""
    out = []
    for v in sorted(vals):
        if not out or v - out[-1] > eps:
            out.append(v)
    return out


def _match(a, b, tol):
    """Greedy 1-D match of two sorted position lists within tol. Returns (matched, a_only, b_only)."""
    a, b = sorted(a), sorted(b)
    i = j = matched = 0
    a_only, b_only = [], []
    while i < len(a) and j < len(b):
        if abs(a[i] - b[j]) <= tol:
            matched += 1
            i += 1
            j += 1
        elif a[i] < b[j]:
            a_only.append(a[i])
            i += 1
        else:
            b_only.append(b[j])
            j += 1
    return matched, a_only + a[i:], b_only + b[j:]


def check_contours(fgbA, fgbB, edge, coarser_cz):
    """Per depth level, every isobath crossing of the edge from A must match one from B within tol.
    Returns (ok, report_lines)."""
    pix = pixel_deg(coarser_cz)
    tol, on = TOL_PIXELS * pix, ON_EDGE_PIXELS * pix
    fa, fb = _read(fgbA, edge, tol), _read(fgbB, edge, tol)

    def by_level(feats):
        d = {}
        for f in feats:
            g = f.get("geometry")
            if not g:
                continue
            props = f.get("properties") or {}
            key = (props.get("depth_abs_m"), props.get("sys"))
            for line in _lines(g):
                d.setdefault(key, []).extend(_crossings(line, edge, on))
        return {k: _dedupe(v, on) for k, v in d.items()}

    la, lb = by_level(fa), by_level(fb)
    ok, lines = True, []
    for key in sorted(set(la) | set(lb), key=repr):
        matched, a_only, b_only = _match(la.get(key, []), lb.get(key, []), tol)
        depth, sys = key
        tag = "ok" if not (a_only or b_only) else "MISMATCH"
        if a_only or b_only:
            ok = False
        lines.append(f"  depth={depth} sys={sys}: matched {matched}, "
                     f"A-only {len(a_only)}, B-only {len(b_only)} [{tag}]")
    if not lines:
        lines.append("  no isobaths reach this edge from either tile")
    return ok, lines


# ── depare: band coverage intervals along the edge ────────────────────────────

def _union(intervals):
    """Merge a list of (lo, hi) into a sorted, disjoint interval union."""
    out = []
    for lo, hi in sorted(intervals):
        if lo > hi:
            lo, hi = hi, lo
        if out and lo <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def _length(union):
    return sum(hi - lo for lo, hi in union)


def _intersect_length(A, B):
    """Total length covered by BOTH interval unions (each already sorted/disjoint)."""
    i = j = 0
    total = 0.0
    while i < len(A) and j < len(B):
        lo, hi = max(A[i][0], B[j][0]), min(A[i][1], B[j][1])
        if hi > lo:
            total += hi - lo
        if A[i][1] < B[j][1]:
            i += 1
        else:
            j += 1
    return total


def _sym_diff_length(A, B):
    """Length covered by exactly one of the two unions: |A| + |B| - 2|A∩B|."""
    return _length(A) + _length(B) - 2 * _intersect_length(A, B)


def check_depare(fgbA, fgbB, edge, coarser_cz):
    """Per (drval1, drval2, sys) band, the edge coverage from A and B (as 1-D intervals along the
    boundary) must match: symmetric-difference length under tol. Returns (ok, report_lines)."""
    pix = pixel_deg(coarser_cz)
    tol_len, on = TOL_PIXELS * pix, ON_EDGE_PIXELS * pix
    fa, fb = _read(fgbA, edge, tol_len), _read(fgbB, edge, tol_len)
    ax = 0 if edge.kind == "lon" else 1
    pos = 1 - ax

    def by_band(feats):
        d = {}
        for f in feats:
            g = f.get("geometry")
            if not g:
                continue
            props = f.get("properties") or {}
            key = (props.get("drval1"), props.get("drval2"), props.get("sys"))
            for ring in _rings(g):
                for u, v in zip(ring, ring[1:]):
                    # a ring segment lying ON the boundary (both ends on the line) is this band's
                    # coverage of the edge; clip its span to the shared segment.
                    if abs(u[ax] - edge.const) <= on and abs(v[ax] - edge.const) <= on:
                        a, b = min(u[pos], v[pos]), max(u[pos], v[pos])
                        a, b = max(a, edge.lo), min(b, edge.hi)
                        if b > a:
                            d.setdefault(key, []).append((a, b))
        return {k: _union(v) for k, v in d.items()}

    ba, bb = by_band(fa), by_band(fb)
    ok, lines = True, []
    for key in sorted(set(ba) | set(bb), key=repr):
        sym = _sym_diff_length(ba.get(key, []), bb.get(key, []))
        d1, d2, sys = key
        tag = "ok" if sym <= tol_len else "MISMATCH"
        if sym > tol_len:
            ok = False
        lines.append(f"  drval1={d1} drval2={d2} sys={sys}: sym-diff {sym:.3e}° "
                     f"(tol {tol_len:.3e}°) [{tag}]")
    if not lines:
        lines.append("  no depth-area bands reach this edge from either tile")
    return ok, lines


# ── CLI drivers ───────────────────────────────────────────────────────────────

def _run_pair(mode, stemA, stemB):
    """Run one check on one adjacent pair; print a per-pair verdict + report. Returns True on PASS
    (an antimeridian pair prints a note and counts as PASS — it's skipped, not failed)."""
    A, B = parse_stem(stemA), parse_stem(stemB)
    edge = shared_edge(A, B)
    if edge == "antimeridian":
        print(f"SKIP {mode} {stemA} | {stemB}: antimeridian seam")
        return True
    if edge is None:
        raise SystemExit(f"seam_check: {stemA} and {stemB} are not edge-adjacent")
    coarser_cz = min(A[3], B[3])   # coarser resolution (smaller child_z) sets the tolerance
    folder = "contour" if mode == "contours" else "depare"
    fa, fb = f"store/{folder}/{stemA}.fgb", f"store/{folder}/{stemB}.fgb"
    ok, report = (check_contours if mode == "contours" else check_depare)(fa, fb, edge, coarser_cz)
    print(f"{'PASS' if ok else 'FAIL'} {mode} {stemA} | {stemB}")
    for line in report:
        print(line)
    return ok


def auto():
    """Every adjacent built pair in the covering, both checks each. A stem is 'built' when its
    contour FGB exists and is nonzero. Same-zoom neighbours are paired by index lookup (the common
    case within a covering region); antimeridian wraps are noted and skipped. Exit nonzero on any
    FAIL, after a per-pair summary."""
    if not os.path.exists(COVERING):
        raise SystemExit(f"seam_check auto: no covering at {COVERING}")
    with open(COVERING) as f:
        stems = f.read().split()
    # A 0-byte sentinel counts as built-and-empty: an empty tile beside a neighbor whose
    # features cross their shared edge is exactly the discontinuity this gate exists to catch.
    built = [s for s in stems if os.path.exists(f"store/contour/{s}.fgb")]
    index = {}
    for s in built:
        z, x, y, _cz = parse_stem(s)
        index[(z, x, y)] = s

    pairs, seen, anti = [], set(), 0
    for s in built:
        z, x, y, _cz = parse_stem(s)
        span = 2 ** z
        for nx, ny in (((x + 1) % span, y), ((x - 1) % span, y), (x, y + 1), (x, y - 1)):
            t = index.get((z, nx, ny))
            if not t:
                continue
            key = tuple(sorted((s, t)))
            if key in seen:
                continue
            seen.add(key)
            edge = shared_edge(parse_stem(s), parse_stem(t))
            if edge == "antimeridian":
                anti += 1
            elif edge is not None:
                pairs.append((s, t))

    note = f" ({anti} antimeridian pair(s) skipped)" if anti else ""
    if not pairs:
        print(f"seam_check auto: no adjacent built pairs in the covering{note}")
        return 0

    results, failures = [], 0
    for a, b in pairs:
        ok = _run_pair("contours", a, b)
        results.append((ok, "contours", a, b))
        failures += 0 if ok else 1
        # depare only where both tiles have a depare output (a tile may be contour-only under
        # SKIP_DEPARE) — a missing depare file is noted, not a failure.
        if os.path.exists(f"store/depare/{a}.fgb") and os.path.exists(f"store/depare/{b}.fgb"):
            ok = _run_pair("depare", a, b)
            results.append((ok, "depare", a, b))
            failures += 0 if ok else 1
        else:
            print(f"SKIP depare {a} | {b}: depare output missing for one side")

    print(f"\nseam_check auto summary ({len(pairs)} pair(s){note}):")
    for ok, mode, a, b in results:
        print(f"  {'PASS' if ok else 'FAIL'} {mode} {a} | {b}")
    print(f"seam_check auto: {failures} failure(s)")
    return 1 if failures else 0


# ── self-check ────────────────────────────────────────────────────────────────

def _write_fgb(path, features):
    """Write a tiny FlatGeobuf via ogr2ogr from an in-memory GeoJSON (GeoJSON's default CRS is
    WGS84, matching the 4326 FGBs the pipeline emits). An empty feature list writes a 0-byte
    sentinel — exactly the empty-tile marker the stage-3 jobs leave."""
    if not features:
        open(path, "w").close()
        return
    gj = path + ".geojson"
    with open(gj, "w") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)
    p = subprocess.run(["ogr2ogr", "-f", "FlatGeobuf", path, gj], capture_output=True, text=True)
    os.remove(gj)
    if p.returncode != 0:
        raise SystemExit(f"seam_check --check: ogr2ogr write failed:\n{p.stderr}")


def _check():
    """Synthetic FGB pairs across one shared meridian edge, asserting the three required verdicts
    (continuous crossing PASS / shifted-crossing FAIL / empty-vs-crossing FAIL), plus a depth-area
    band PASS/FAIL to exercise the depare path."""
    d = os.path.join(os.environ.get("TMPDIR") or "/tmp", f"seam_check_selftest_{os.getpid()}")
    os.makedirs(d, exist_ok=True)
    made = []

    def fgb(name):
        p = os.path.join(d, name)
        made.append(p)
        return p

    z = 10
    A, B = (z, 300, 400, z), (z, 301, 400, z)   # adjacent in x -> a shared meridian edge
    edge = shared_edge(A, B)
    assert isinstance(edge, Edge) and edge.kind == "lon", "expected a meridian edge"
    pix = pixel_deg(z)
    tol = TOL_PIXELS * pix
    lon, lat = edge.const, (edge.lo + edge.hi) / 2   # a latitude inside the shared span

    def line_feat(coords, depth=100, sys="m"):
        return {"type": "Feature",
                "properties": {"depth_abs_m": depth, "sys": sys},
                "geometry": {"type": "LineString", "coordinates": coords}}

    # (a) PASS — one isobath spanning the seam, clipped into an A-half ending on the edge and a
    #     B-half starting on the edge at the SAME latitude.
    a_fgb, b_fgb = fgb("a_A.fgb"), fgb("a_B.fgb")
    _write_fgb(a_fgb, [line_feat([[lon - 5 * pix, lat], [lon, lat]])])
    _write_fgb(b_fgb, [line_feat([[lon, lat], [lon + 5 * pix, lat]])])
    ok_a, _ = check_contours(a_fgb, b_fgb, edge, z)
    assert ok_a, "continuous crossing must PASS"

    # (b) FAIL — B's crossing shifted 10x tol along the edge: A's terminus has no match.
    b_shift = fgb("b_B.fgb")
    _write_fgb(b_shift, [line_feat([[lon, lat + 10 * tol], [lon + 5 * pix, lat + 10 * tol]])])
    ok_b, _ = check_contours(a_fgb, b_shift, edge, z)
    assert not ok_b, "shifted crossing must FAIL"

    # (c) FAIL — one side an empty sentinel, the other still crossing the seam.
    empty = fgb("c_A.fgb")
    _write_fgb(empty, [])
    ok_c, _ = check_contours(empty, b_fgb, edge, z)
    assert not ok_c, "empty vs crossing must FAIL"

    # (d/e) depare — a band abutting the seam from both tiles over the same interval PASSes; a
    #       shifted interval FAILs. Boxes whose inner side runs along the edge from lat-h to lat+h.
    h = 15 * pix

    def box_feat(xs, y0, y1, d1=0.0, d2=5.0, sys="m"):
        ring = [[xs[0], y0], [xs[1], y0], [xs[1], y1], [xs[0], y1], [xs[0], y0]]
        return {"type": "Feature",
                "properties": {"drval1": d1, "drval2": d2, "sys": sys},
                "geometry": {"type": "Polygon", "coordinates": [ring]}}

    da, db = fgb("d_A.fgb"), fgb("d_B.fgb")
    _write_fgb(da, [box_feat([lon - 5 * pix, lon], lat - h, lat + h)])   # east side on the edge
    _write_fgb(db, [box_feat([lon, lon + 5 * pix], lat - h, lat + h)])   # west side on the edge
    ok_d, _ = check_depare(da, db, edge, z)
    assert ok_d, "matching band coverage must PASS"

    db_shift = fgb("e_B.fgb")
    _write_fgb(db_shift, [box_feat([lon, lon + 5 * pix], lat + h, lat + 3 * h)])
    ok_e, _ = check_depare(da, db_shift, edge, z)
    assert not ok_e, "shifted band coverage must FAIL"

    for p in made:
        if os.path.exists(p):
            os.remove(p)
    os.rmdir(d)
    print("seam_check self-check ok: contour PASS/shifted-FAIL/empty-FAIL + depare PASS/shifted-FAIL")


def main(argv):
    if argv[:1] == ["--check"]:
        _check()
        return 0
    if argv[:1] == ["auto"] and len(argv) == 1:
        return auto()
    if argv[:1] in (["contours"], ["depare"]) and len(argv) == 3:
        return 0 if _run_pair(argv[0], argv[1], argv[2]) else 1
    sys.exit("usage: seam_check.py contours <stemA> <stemB> | depare <stemA> <stemB> "
             "| auto | --check")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
