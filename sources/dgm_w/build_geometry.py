#!/usr/bin/env python3
"""Regenerate the river corridor + centerline geometry for dgm_w from Overture Maps.

NOT part of the build (like harvest_gauges.py): the build reads the committed ``*_river.wkt`` /
``*_centerline.wkt``; this refreshes them from the Overture ``base/water`` theme — the same
anonymous S3 GeoParquet ``landmask.py`` already reads. Overture over Overpass: name-filterable,
bbox pushdown, and no public-instance timeouts.

  uv run python build_geometry.py            # regenerate every river
  uv run python build_geometry.py mosel      # just one

Per river:
  - **corridor** ``<river>_river.wkt`` = union of the named river's linestrings (the fill mask;
    a MultiLineString is fine). Overture names the main channel exactly (e.g. "Elbe"), so the side
    arms ("Alte Elbe …") and canals are excluded by the name filter.
  - **centerline** ``<river>_centerline.wkt`` (impounded step rivers only) = a single ordered
    LineString, shortest path through the river graph between the downstream-most and upstream-most
    barrage in ``<river>_stau.csv``. build_impounded projects pixels onto it for arc-length pools;
    free-flowing/tidal reaches don't need one (the gauge line carries the value).

Overture's base/water theme has no weirs or CEMT navigation locks, so the Main's weir lines and the
upper Rhein's lock latitudes stay as the coordinates already checked into their ``*_stau.csv`` — the
build never re-extracts those. Reads are anonymous (AWS_NO_SIGN_REQUEST); pin AWS_DEFAULT_REGION so a
stray S3 profile can't poison the bucket host (same reason as landmask.py). Stdlib + shapely + GDAL CLI.
"""

import csv
import heapq
import math
import os
import subprocess
import sys
import tempfile

from shapely import from_wkt, get_parts, line_merge, union_all
from shapely.geometry import LineString

OVERTURE_RELEASE = "2026-06-17.0"  # keep in step with landmask.py
WATER_PARQUET_URL = f"/vsis3/overturemaps-us-west-2/release/{OVERTURE_RELEASE}/theme=base/type=water/"
HERE = os.path.dirname(__file__)

# river: (Overture names.primary values, bbox "W,S,E,N", stau csv for a centerline or None)
RIVERS = {
    "rhein": (["Rhein", "Rhin"], "6.0,48.7,8.6,51.9", None),        # free-flowing lower reach (GlW ramp)
    "elbe":  (["Elbe"], "10.0,50.8,14.3,53.5", None),               # free-flowing (MNW ramp)
    "mosel": (["Mosel", "Moselle"], "6.2,49.4,7.65,50.4", "mosel_stau.csv"),
    "saar":  (["Saar"], "6.5,49.0,7.15,49.75", "saar_stau.csv"),
    "lahn":  (["Lahn"], "7.5,50.24,8.06,50.44", "lahn_stau.csv"),
}


def _haversine(a, b):
    R = 6371000
    la1, la2 = math.radians(a[1]), math.radians(b[1])
    dl, dn = math.radians(b[1] - a[1]), math.radians(b[0] - a[0])
    return 2 * R * math.asin(math.sqrt(math.sin(dl / 2) ** 2 +
                                       math.cos(la1) * math.cos(la2) * math.sin(dn / 2) ** 2))


def fetch_lines(names, bbox):
    """Union of the named river's Overture linestrings (subtype river/canal) in the bbox.

    Two passes, like landmask.py: the remote read uses plain -spat + -where so the spatial and
    subtype filters push down to the parquet (a -dialect SQLITE -sql read would defeat the pushdown
    and scan the whole planet); the name filter runs locally over the small bbox extract."""
    w, s, e, n = bbox.split(",")
    names_sql = ", ".join(f"'{x}'" for x in names)
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw.gpkg", f"{d}/lines.csv"
        subprocess.run(
            f"ogr2ogr -f GPKG -overwrite -nln w -lco SPATIAL_INDEX=NO "
            f"-spat {w} {s} {e} {n} -where \"subtype IN ('river','canal')\" "
            f"-select \"names.primary,subtype\" {raw} {WATER_PARQUET_URL}",
            shell=True, check=True,
            env={**os.environ, "AWS_NO_SIGN_REQUEST": "YES", "AWS_DEFAULT_REGION": "us-west-2"})
        subprocess.run(
            f"ogr2ogr -f CSV -overwrite -dialect SQLITE "
            f"-sql \"SELECT geometry FROM w WHERE \\\"names.primary\\\" IN ({names_sql})\" "
            f"-lco GEOMETRY=AS_WKT {out} {raw}",
            shell=True, check=True)
        lines = []
        with open(out, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                g = from_wkt(row.get("WKT") or row.get("geometry") or "")
                lines += [p for p in get_parts(g) if p.geom_type == "LineString"]
    if not lines:
        sys.exit(f"no Overture linestrings for {names} in {bbox} (name/release changed?)")
    return line_merge(union_all(lines))


def _read_barrage_ends(stau_csv):
    """(downstream_pt, upstream_pt) = the min-km and max-km barrage coords from the stau table."""
    rows = []
    with open(os.path.join(HERE, stau_csv), encoding="utf-8") as f:
        for r in csv.DictReader(line for line in f if not line.lstrip().startswith("#")):
            rows.append((float(r["km"]), (float(r["lon"]), float(r["lat"]))))
    rows.sort()
    return rows[0][1], rows[-1][1]


def shortest_centerline(corridor, ds_pt, us_pt):
    """Single ordered LineString: shortest path through the river graph from ds_pt to us_pt,
    bridging disconnected components (Overture/OSM gaps) by nearest node pairs first."""
    G = {}
    def node(c): return (round(c[0], 7), round(c[1], 7))
    for part in get_parts(corridor):
        cs = list(part.coords)
        for a, b in zip(cs, cs[1:]):
            na, nb, d = node(a), node(b), _haversine(a, b)
            G.setdefault(na, []).append((nb, d))
            G.setdefault(nb, []).append((na, d))
    # connected components
    seen, comps = set(), []
    for start in G:
        if start in seen:
            continue
        stack, comp = [start], []
        while stack:
            u = stack.pop()
            if u in seen:
                continue
            seen.add(u); comp.append(u)
            stack += [v for v, _ in G[u] if v not in seen]
        comps.append(comp)
    while len(comps) > 1:  # bridge the two nearest components (sampled) until one graph
        best = None
        for i in range(len(comps)):
            for j in range(i + 1, len(comps)):
                ci = comps[i][:: max(1, len(comps[i]) // 60)]
                cj = comps[j][:: max(1, len(comps[j]) // 60)]
                for a in ci:
                    for b in cj:
                        d = _haversine(a, b)
                        if best is None or d < best[0]:
                            best = (d, i, j, a, b)
        d, i, j, a, b = best
        G[a].append((b, d)); G[b].append((a, d))
        comps[i] = comps[i] + comps[j]; comps.pop(j)
    nodes = list(G)
    nearest = lambda pt: min(nodes, key=lambda nd: _haversine(nd, pt))
    src, dst = nearest(ds_pt), nearest(us_pt)
    seen_d, prev, pq = {src: 0.0}, {}, [(0.0, src)]
    while pq:
        d, u = heapq.heappop(pq)
        if u == dst:
            break
        if d > seen_d.get(u, 1e18):
            continue
        for v, wt in G[u]:
            nd = d + wt
            if nd < seen_d.get(v, 1e18):
                seen_d[v] = nd; prev[v] = u; heapq.heappush(pq, (nd, v))
    if dst not in seen_d:
        sys.exit("centerline: no path between barrage ends even after bridging")
    path = [dst]
    while path[-1] != src:
        path.append(prev[path[-1]])
    return LineString(path[::-1])


def build(name):
    names, bbox, stau = RIVERS[name]
    corridor = fetch_lines(names, bbox)
    rp = os.path.join(HERE, f"{name}_river.wkt")
    open(rp, "w", encoding="utf-8").write(corridor.wkt)
    print(f"{name}: corridor {corridor.geom_type} {len(get_parts(corridor))} parts, "
          f"bounds {[round(v, 3) for v in corridor.bounds]} -> {os.path.basename(rp)}")
    if stau:
        cl = shortest_centerline(corridor, *_read_barrage_ends(stau))
        cp = os.path.join(HERE, f"{name}_centerline.wkt")
        open(cp, "w", encoding="utf-8").write(cl.wkt)
        print(f"{name}: centerline {len(cl.coords)} pts, {cl.length * 111:.1f} km -> {os.path.basename(cp)}")


def main():
    want = sys.argv[1:] or list(RIVERS)
    for name in want:
        if name not in RIVERS:
            sys.exit(f"unknown river {name!r}; known: {', '.join(RIVERS)}")
        build(name)


if __name__ == "__main__":
    main()
