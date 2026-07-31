#!/usr/bin/env -S uv run --script
"""Safety gates for shallow-band generalization. Every gate is a hard assertion with a stated
reason; none of them is a tolerance you may widen to make a change pass.

Raster gates (an operator that rewrites the DEM):
  1. shoal-bias      never chart deeper than the source          IHO S-4 B-411.5
  2. connectivity    the water network must not fragment          separates pond-fill from closing
  3. drying growth   drying may not grow along connected water    scoped, see below

Vector gates (an operator that rewrites the partition):
  4. partition       bands stay disjoint, area within the budget  depare_run's own contract
  5. displacement    boundary movement bounded by the tolerance   Peters et al. §2
  6. routes          named channels stay navigable in both A/B    the gate that catches a
                                                                 closed GIWW

Gate 3 is deliberately scoped to connected water. Unscoped ("drying area must not increase") it
would reject filling an enclosed sub-legible pond, which is the licensed behaviour — USGS NHD
breaks marsh for clearings >= 0.05in and charts the rest as one area.

    gates.py raster <before.tif> <after.tif>
    gates.py vector <before.fgb> <after.fgb> [--tol-m 2.23] [--routes routes.geojson]
    gates.py --check
"""
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

NODATA = -9999.0
DRYING_CAP = 16.0
# 0.3 mm at compilation scale (S-58 check 571) = 2.23 m at z15, 29.5degN.
DEFAULT_TOL_M = 2.23


def _water_network(arr, nd):
    """Water connected to the tile's network: every component touching an edge, plus the
    largest. The same rule the pond-fill operator uses to decide what it may not touch."""
    from scipy.ndimage import label
    w = (arr != nd) & (arr < 0)
    lab, n = label(w)
    if n == 0:
        return np.zeros_like(w), lab
    sizes = np.bincount(lab.ravel())
    sizes[0] = 0
    keep = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]])))
    keep.discard(0)
    keep.add(int(sizes.argmax()))
    return np.isin(lab, list(keep)), lab


def raster(before, after):
    import rasterio
    with rasterio.open(before) as s:
        a, nd = s.read(1), (s.nodata if s.nodata is not None else NODATA)
    with rasterio.open(after) as s:
        b = s.read(1)
    fails = []
    valid = a != nd

    # 1. shoal-bias, exact. `-r average` fails this; pond-fill and closing pass.
    deepened = np.maximum(a[valid] - b[valid], 0.0)
    worst = float(deepened.max()) if deepened.size else 0.0
    if worst > 1e-9:
        n = int((deepened > 1e-9).sum())
        fails.append(f"shoal-bias: {n} px charted deeper than source, worst {worst:.4f} m")

    # 2. connectivity: components must not fragment, largest share must not fall.
    from metrics import water_stats
    _na, ca, la = water_stats(a, nd)
    _nb, cb, lb = water_stats(b, nd)
    if lb < la - 1e-3:
        fails.append(f"connectivity: largest water component share {la:.4f} -> {lb:.4f} "
                     f"(a channel closed)")

    # 3. drying may not grow inside water that was connected to the network.
    net, _ = _water_network(a, nd)
    grew = int((net & (b >= 0) & (b <= DRYING_CAP) & (a < 0)).sum())
    if grew:
        fails.append(f"drying growth: {grew} px of connected water became drying")

    report = {"shoal_worst_deepening_m": worst, "components": [ca, cb],
              "largest_share": [la, lb], "connected_water_to_drying_px": grew,
              "pass": not fails, "failures": fails}
    return report


def _ladders(g):
    """The partitions the contract actually makes: each sys ladder's bands plus the sys-less
    drying/nodata rows both ladders share. m-vs-ft rows cover the SAME water by design and are
    never a violation, so disjointness is only meaningful within a ladder."""
    if "sys" not in g:
        return {"all": g}
    shared = g[g["sys"].isna()]
    out = {}
    for s in ("m", "ft"):
        rows = g[g["sys"] == s]
        if len(rows):
            import pandas as pd
            out[s] = pd.concat([rows, shared]) if len(shared) else rows
    return out or {"all": g}


def _overlap_pairs(geoms, tol_m, min_area_m2=1.0):
    """Interior-overlapping pairs via STRtree — O(n·neighbours), not O(n²). Returns
    (pairs, worst width in metres).

    WIDTH is the instrument, not area. Two rows that share a contour chain always overlap along
    it by a hairline — the drying is derived through unions, differences and a make_valid that
    re-node the chain, and the sink writes at finite precision — and a hairline's AREA grows with
    the chain's length. Shipped raw output carries up to ~3.9 m² of it; the same hairline over a
    generalized chain, where one segment can be a kilometre, measures tens of m² while being
    millimetres wide. 2·area/perimeter is that sliver's width, which stays pixel-scale however
    long the chain, so the threshold can be the thing that actually matters: no pair may overlap
    WIDER than the tolerance the operator was given, because a coverage-simplified partition
    moves both sides of a shared edge together."""
    import numpy as np
    import shapely
    tree = shapely.STRtree(geoms)
    li, ri = tree.query(geoms, predicate="intersects")
    n, worst = 0, 0.0
    for i, j in zip(li, ri):
        if i >= j:
            continue
        ov = shapely.intersection(geoms[i], geoms[j])
        if ov.is_empty or ov.area <= min_area_m2:
            continue
        parts = shapely.get_parts(ov)
        a, p = shapely.area(parts), shapely.length(parts)
        w = float(np.divide(2 * a, p, out=np.zeros_like(a), where=p > 0).max()) if len(parts) else 0.0
        worst = max(worst, w)
        if w > tol_m:
            n += 1
    return n, worst


def _vector_frames(ga, gb, tol_m, rr):
    """Gates 4-6 on already-loaded EPSG:3857 frames (split out so _check needs no file IO)."""
    import numpy as np
    import shapely
    fails = []
    report = {"tol_m": tol_m}

    # 4. partition contract, per ladder: disjoint interiors, area preserved, all valid.
    # nodata rows are excluded from the disjointness check: they are simplified to the stem's
    # resolution (depare_run.NODATA_SIMPLIFY_PX), so they wobble over band edges by design —
    # that is why the `rank` tie-breaker exists. Bands + drying share exact contour edges and
    # are held to the strict check.
    def strict(g):
        return g[g["drval1"].notna()] if "drval1" in g else g
    la, lb = _ladders(ga), _ladders(gb)
    report["overlapping_pairs"] = {}
    report["worst_overlap_width_m"] = {}
    report["area_drift"] = {}
    for key in lb:
        geoms = list(strict(lb[key]).geometry.values)
        ov, width = _overlap_pairs(geoms, tol_m)
        report["overlapping_pairs"][key] = ov
        report["worst_overlap_width_m"][key] = round(width, 4)
        if ov:
            fails.append(f"partition[{key}]: {ov} pair(s) overlapping wider than {tol_m} m "
                         f"(worst {width:.3f} m)")
        if key in la:
            aa = float(la[key].geometry.area.sum())
            ab = float(lb[key].geometry.area.sum())
            drift = abs(ab - aa) / aa if aa else 0.0
            report["area_drift"][key] = drift
            # An operator that MOVES a boundary cannot hold an exact area identity — every edge
            # it shifts trades area across itself, and where the drying is cut back to effective
            # water the trade does not net out. So the drift is budgeted like gate 5's churn
            # (tolerance x boundary length) rather than asserted to zero; at tol_m = 0, where
            # nothing may move, it collapses back to the identity. The tight instrument for a
            # simplifier is gate 5; this one is here to catch geometry that vanished outright.
            budget = max(1e-5 * aa, tol_m * 0.5 * float(
                la[key].geometry.length.sum() + lb[key].geometry.length.sum()))
            if abs(ab - aa) > budget:
                fails.append(f"partition[{key}]: area drifted {drift:.6%} "
                             f"({abs(ab - aa):.0f} m2 > budget {budget:.0f} m2)")
    if not bool(shapely.is_valid(gb.geometry.values).all()):
        fails.append("partition: invalid geometry in output")

    # 5. displacement bounded by the tolerance, per band (grouped: depare subdivides a band
    # into many rows, so row-by-row pairing is meaningless). Simplification is NOT shoal-biased
    # by construction, so the claim is a bound, never exactness (Peters et al. §2).
    def keyed(g):
        if "drval1" not in g:
            return {None: shapely.union_all(list(g.geometry.values))}
        out = {}
        bands = g[g["drval1"].notna()]
        for k, rows in bands.groupby(["sys", "drval1", "drval2"], dropna=False):
            out[k] = shapely.union_all(list(rows.geometry.values))
        return out
    ka, kb = keyed(ga), keyed(gb)
    if set(ka) != set(kb):
        fails.append(f"band set changed: -{sorted(set(ka) - set(kb))} +{sorted(set(kb) - set(ka))}")
    # Churn (symmetric difference vs a tol-wide band along the boundary) is the displacement
    # instrument. NOT hausdorff: GEOS's discrete hausdorff is O(n·m) over segment pairs, and
    # a single identity run on a 9.6k-row fixture spent 38 min in it — while a sub-tol
    # excursion long enough to matter shows up in churn anyway.
    worst_churn = 0.0
    for k in set(ka) & set(kb):
        if ka[k].equals_exact(kb[k], 0):  # identity fast path
            continue
        churn = float(ka[k].symmetric_difference(kb[k]).area)
        worst_churn = max(worst_churn, churn)
        budget = tol_m * 1.5 * 0.5 * float(ka[k].length + kb[k].length)
        if churn > budget:
            fails.append(f"displacement[{k}]: churn {churn:.1f} m2 > band budget {budget:.1f} m2")
    report["max_churn_m2"] = worst_churn

    # 6. named-route continuity, paired BY NAME (a route emptying on one side only must fail,
    # not silently mis-pair the zip). drval is positive-down: drval2 >= project depth means the
    # band's deep edge reaches it — the comparative instrument (drval1 >= would read ~11% on a
    # demonstrably open GIWW, since a 12 ft channel lives in the [2,5] band).
    if rr is not None and len(rr):
        # One clip window for BOTH sides: the intersection of the two layers' bounds — clipping
        # to each side's own qualifying-band envelope would inflate the after-side fraction.
        bounds = [max(ga.total_bounds[0], gb.total_bounds[0]),
                  max(ga.total_bounds[1], gb.total_bounds[1]),
                  min(ga.total_bounds[2], gb.total_bounds[2]),
                  min(ga.total_bounds[3], gb.total_bounds[3])]
        window = shapely.box(*bounds)
        cov = {}
        for side, g in (("before", ga), ("after", gb)):
            # metre ladder only — ft covers the same water and doubles the union cost.
            bands = g[(g.get("sys") == "m") & g["drval2"].notna()] if "sys" in g else g
            for r in rr.itertuples():
                name = getattr(r, "name", "?")
                need = float(getattr(r, "project_depth_m", 0.0))
                seg = r.geometry.intersection(window)
                if seg.is_empty or seg.length == 0:
                    continue
                deep = bands[bands["drval2"].astype(float) >= need]
                covered = shapely.union_all(list(deep.geometry.values)) if len(deep) \
                    else shapely.Polygon()
                frac = float(seg.intersection(covered).length / seg.length)
                cov.setdefault(name, {})[side] = round(frac, 4)
        report["routes"] = cov
        report["routes_checked"] = sum(1 for v in cov.values() if v)
        for name, v in cov.items():
            if "before" in v and "after" not in v:
                fails.append(f"route {name}: present before, absent after")
            elif v.get("after", 1.0) < v.get("before", 0.0) - 1e-3:
                fails.append(f"route {name}: coverage {v['before']:.3f} -> {v['after']:.3f}")

    report["pass"] = not fails
    report["failures"] = fails
    return report


def vector(before, after, tol_m=DEFAULT_TOL_M, routes=None):
    import geopandas as gpd
    # depare FGBs are published EPSG:4326; all gate arithmetic is in metres.
    ga = gpd.read_file(before).to_crs("EPSG:3857")
    gb = gpd.read_file(after).to_crs("EPSG:3857")
    rr = gpd.read_file(routes).to_crs("EPSG:3857") if routes and os.path.exists(routes) else None
    return _vector_frames(ga, gb, tol_m, rr)


def _check():
    """Each gate fires on the failure it exists to catch, and passes the licensed change."""
    from metrics import water_stats
    a = np.full((40, 40), 1.0, np.float32)
    a[:, 10:13] = -3.0        # a channel spanning the array (connected)
    a[25:28, 25:28] = -1.0    # an enclosed sub-legible pond

    # licensed: fill the enclosed pond -> shoaler, network intact, no connected water lost
    ok = a.copy()
    ok[25:28, 25:28] = 1.0
    net, _ = _water_network(a, NODATA)
    assert not (net & (ok >= 0) & (a < 0)).any(), "pond was not connected; gate 3 must ignore it"
    _n, _c, la = water_stats(a, NODATA)
    _n2, _c2, lb = water_stats(ok, NODATA)
    assert lb >= la - 1e-3, (la, lb)

    # violation 1: deepen a pixel (what `-r average` does)
    bad = a.copy()
    bad[0, 0] = 0.5
    deepened = np.maximum(a - bad, 0.0)
    assert deepened.max() > 1e-9, "gate 1 must see a deepened pixel"

    # violation 2: close the channel -> network fragments, largest share falls
    cut = a.copy()
    cut[20, 10:13] = 1.0
    _n3, c3, l3 = water_stats(cut, NODATA)
    assert l3 < la - 1e-3, f"gate 2 must see the split ({la} -> {l3})"

    # violation 3: raise connected channel water into the drying bucket
    dry = a.copy()
    dry[:, 10:13] = 2.0
    grew = int((net & (dry >= 0) & (dry <= DRYING_CAP) & (a < 0)).sum())
    assert grew > 0, "gate 3 must see connected water become drying"

    # ── vector gates, on synthetic frames (each assertion pins a bug found in review:
    # positive-down drval sign, m-vs-ft not an overlap, pair-by-name, shared clip window) ──
    import geopandas as gpd
    import shapely
    from shapely.geometry import LineString, box as sbox

    def frames(deep_geom):
        rows = [
            {"sys": "m", "drval1": 2.0, "drval2": 5.0, "geometry": deep_geom},
            {"sys": "m", "drval1": 0.0, "drval2": 2.0, "geometry": sbox(0, 100, 200, 200)},
            {"sys": "ft", "drval1": 1.8288, "drval2": 5.4864, "geometry": deep_geom},
            {"sys": None, "drval1": -16.0, "drval2": 0.0, "geometry": sbox(0, 200, 200, 260)},
        ]
        return gpd.GeoDataFrame(rows, crs="EPSG:3857")

    ga = frames(sbox(0, 0, 200, 100))
    rr = gpd.GeoDataFrame(
        [{"name": "test-channel", "project_depth_m": 3.66,
          "geometry": LineString([(0, 50), (200, 50)])}], crs="EPSG:3857")

    r = _vector_frames(ga, ga.copy(), 2.23, rr)
    assert r["pass"], f"identity must pass: {r['failures']}"
    # the ft row covers the same water as the m row — per-ladder grouping must not flag it
    assert all(v == 0 for v in r["overlapping_pairs"].values()), r["overlapping_pairs"]
    # positive-down sign: the [2,5] band's drval2=5 >= 3.66 must cover the route fully
    assert r["routes"]["test-channel"] == {"before": 1.0, "after": 1.0}, r["routes"]

    # a genuine within-ladder overlap fires gate 4
    bad4 = ga.copy()
    bad4.loc[len(bad4)] = {"sys": "m", "drval1": 2.0, "drval2": 5.0,
                           "geometry": sbox(100, 0, 300, 100)}
    r = _vector_frames(ga, bad4, 2.23, None)
    assert any("overlapping" in f for f in r["failures"]), r["failures"]

    # shrinking the deep band halves route coverage -> gate 6 fires (paired by name)
    gb2 = frames(sbox(0, 0, 100, 100))
    r = _vector_frames(ga, gb2, 2.23, rr)
    assert any(f.startswith("route test-channel") for f in r["failures"]), r["failures"]
    assert r["routes"]["test-channel"]["after"] < 0.51, r["routes"]

    print("gates self-check ok (each gate fires on its own failure; pond-fill passes)")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["--check"]:
        _check()
    elif a[:1] == ["raster"] and len(a) == 3:
        r = raster(a[1], a[2])
        print(json.dumps(r, indent=2))
        sys.exit(0 if r["pass"] else 1)
    elif a[:1] == ["vector"] and len(a) >= 3:
        tol = float(a[a.index("--tol-m") + 1]) if "--tol-m" in a else DEFAULT_TOL_M
        rt = a[a.index("--routes") + 1] if "--routes" in a else None
        r = vector(a[1], a[2], tol, rt)
        print(json.dumps(r, indent=2))
        sys.exit(0 if r["pass"] else 1)
    else:
        sys.exit(__doc__.strip().split("\n\n")[-1])
