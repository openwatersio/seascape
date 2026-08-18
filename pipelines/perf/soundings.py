#!/usr/bin/env -S uv run --script
"""Conformance measurements for the sounding field. Unlike gates.py these are not before/after
comparisons — each one is an absolute property of one stem's output, measured so a redesign is
scoped by evidence instead of a guess.

  1. interpolation  S-4 B-410a's final test of depth selection, per display zoom
  2. rings          every closed isobath must carry the least depth inside it (S-4 B-410b)
  3. density        spacing distribution vs the practice figures, per display zoom
  4. floor          what SOUND_MIN_DEPTH_M actually admits and drops, per candidate value

Gate 1 is the one that matters. S-4, Ed 4.10.0, B-410a:

    "The final test of depth selection is that no source material should contain depths shoaler
     than the mariner would expect by interpolating the depth in any position from the charted
     soundings and depth contours."

That is testable directly, and testing it is far cheaper than implementing the triangular
selection method it validates. The mariner's interpolation is modelled as linear interpolation
over a Delaunay triangulation of everything charted at that zoom — the soundings that display
there plus the isobath vertices — which is what the "triangle" language in B-410a describes.

Measuring per zoom is the point: the shoalest pixel of every cell IS emitted at the finest level,
so violations only appear once the pyramid thins the field. Where that starts is the number that
scopes any selection change.

    soundings.py interpolation <window.tif> <soundings.geojsons> <contour.fgb> --zoom Z [--tol-m M]
    soundings.py rings <window.tif> <soundings.geojsons> <contour.fgb> --zoom Z
    soundings.py density <soundings.geojsons> --zoom Z [--tile-px 512]
    soundings.py floor <window.tif> [--floors 1.0,0.5,0.3,0.2,0.0]
    soundings.py --check
"""
import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NODATA = -9999.0
# Practice figures for label spacing at chart scale, quoted second-hand (Skopeliti et al. 2020
# via the NOAA Nautical Chart Manual) — a target to report against, never a pass/fail threshold.
SPACING_MM = {"critical": 6.0, "supporting": 10.0, "fill": 15.0}
# A CSS pixel is defined as a visual angle at arm's length, the same distance ECDIS displays are
# engineered for, so screen px convert to millimetres exactly (S-52 Ed 6.1.1 §5.1 sizing basis).
MM_PER_PX = 0.2646


def _displayed(feat, zoom):
    """Does this sounding render at `zoom`? Per-feature tippecanoe placement: minzoom always,
    maxzoom only on levels that swap out when a finer field arrives (soundings_run._tc)."""
    tc = feat.get("tippecanoe", {})
    lo = tc.get("minzoom", 0)
    hi = tc.get("maxzoom", math.inf)
    return lo <= zoom <= hi


def _read_soundings(path, zoom):
    """[(lon, lat, depth_pos)] for the soundings that display at `zoom`."""
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            ft = json.loads(line)
            if not _displayed(ft, zoom):
                continue
            lon, lat = ft["geometry"]["coordinates"]
            out.append((lon, lat, float(ft["properties"]["depth_m"])))
    return out


def _read_contours(path):
    """[(lon, lat, depth_pos)] over every vertex of the metre ladder's isobaths. The fathom-curve
    duplicates cover the same water, so including them would just double-weight the same lines."""
    import geopandas as gpd
    from shapely import get_coordinates
    gdf = gpd.read_file(path)
    if gdf.empty:
        return []
    if "sys" in gdf.columns:
        gdf = gdf[gdf["sys"] != "ft"]
    col = "depth_abs_m" if "depth_abs_m" in gdf.columns else "depth_m"
    out = []
    for depth, geom in zip(gdf[col], gdf.geometry):
        if geom is None or depth is None:
            continue
        for x, y in get_coordinates(geom):
            out.append((float(x), float(y), abs(float(depth))))
    return out


def _to_dem_xy(pts, dem_crs):
    """[(lon, lat, d)] in 4326 -> (N,2) array in the DEM's CRS, plus the depths."""
    from pyproj import Transformer
    if not pts:
        return np.empty((0, 2)), np.empty(0)
    lon, lat, d = (np.asarray(c, float) for c in zip(*pts))
    tf = Transformer.from_crs("EPSG:4326", dem_crs, always_xy=True)
    x, y = tf.transform(lon, lat)
    return np.column_stack([x, y]), d


def interpolation(window_tif, soundings_path, contour_path, zoom, tol_m=0.0, stride=4):
    """S-4 B-410a: no source pixel may be shoaler than interpolation from the charted field.

    `stride` subsamples the DEM — a fixture window is millions of pixels and the interpolant is
    the expensive part. It is a sample, and the reported counts say so.
    """
    import rasterio
    from scipy.interpolate import LinearNDInterpolator

    with rasterio.open(window_tif) as src:
        arr = src.read(1)
        transform, crs, nd = src.transform, src.crs, (src.nodata if src.nodata is not None else NODATA)

    snd = _read_soundings(soundings_path, zoom)
    cnt = _read_contours(contour_path) if contour_path and os.path.exists(contour_path) else []
    xy_s, d_s = _to_dem_xy(snd, crs)
    xy_c, d_c = _to_dem_xy(cnt, crs)
    xy = np.vstack([xy_s, xy_c])
    dd = np.concatenate([d_s, d_c])
    if len(xy) < 3:
        return {"pass": False, "zoom": zoom, "charted_points": int(len(xy)),
                "failures": ["fewer than 3 charted features — nothing to interpolate from"]}

    sub = arr[::stride, ::stride]
    rows, cols = np.mgrid[0:arr.shape[0]:stride, 0:arr.shape[1]:stride]
    wet = (sub != nd) & (sub < 0)
    if not wet.any():
        return {"pass": True, "zoom": zoom, "charted_points": int(len(xy)),
                "sampled_water_px": 0, "failures": []}

    # Pixel centres in DEM CRS; positive-down actual depth.
    px_x, px_y = rasterio.transform.xy(transform, rows[wet], cols[wet])
    actual = -sub[wet].astype(float)

    expected = LinearNDInterpolator(xy, dd)(np.column_stack([px_x, px_y]))
    inside = ~np.isnan(expected)          # outside the convex hull the mariner has nothing to go on
    shortfall = expected[inside] - actual[inside]   # >0 => real ground shoaler than charted
    # The interpolant reproduces a control point to ~1e-15, not exactly; a micron of that is not
    # a charting defect.
    viol = shortfall > max(tol_m, 1e-6)

    n = int(inside.sum())
    return {
        "pass": not bool(viol.any()),
        "zoom": zoom,
        "charted_points": int(len(xy)),
        "displayed_soundings": int(len(xy_s)),
        "sampled_water_px": n,
        "violating_px": int(viol.sum()),
        "violating_fraction": (float(viol.sum()) / n) if n else 0.0,
        "max_shortfall_m": float(shortfall.max()) if n else 0.0,
        "p99_shortfall_m": float(np.percentile(shortfall, 99)) if n else 0.0,
        "stride": stride,
        "failures": ([] if not viol.any() else
                     [f"{int(viol.sum())} of {n} sampled water px are shoaler than the charted "
                      f"field implies (max {float(shortfall.max()):.2f} m) — S-4 B-410a"]),
    }


def rings(window_tif, soundings_path, contour_path, zoom):
    """Every closed isobath must carry the least depth inside it.

    S-4, Ed 4.10.0, B-410b puts "least depths over shoals, banks and sills in navigable channels"
    at the top of what must survive selection. A closed contour with no sounding inside it is a
    charted shoal with no charted least depth: a mariner interpolating across the ring reads the
    ring's own level, so the shoal is understated by however much shoaler the ground really is.

    This is the sharpest form of the B-410a failure, and unlike the aggregate violation fraction
    it names the specific features at fault.
    """
    import geopandas as gpd
    import rasterio
    from rasterio.features import geometry_mask
    from shapely import get_coordinates
    from shapely.geometry import Point, Polygon

    gdf = gpd.read_file(contour_path)
    if "sys" in gdf.columns:
        gdf = gdf[gdf["sys"] != "ft"]
    col = "depth_abs_m" if "depth_abs_m" in gdf.columns else "depth_m"

    closed = []
    for depth, geom in zip(gdf[col], gdf.geometry):
        if geom is None or depth is None:
            continue
        parts = geom.geoms if geom.geom_type.startswith("Multi") else [geom]
        for part in parts:
            c = get_coordinates(part)
            if len(c) > 3 and np.allclose(c[0], c[-1]):
                closed.append((abs(float(depth)), Polygon(c)))

    pts = [Point(lo, la) for lo, la, _ in _read_soundings(soundings_path, zoom)]
    shoal_rings, bare, worst = 0, 0, 0.0
    with rasterio.open(window_tif) as src:
        arr = src.read(1)
        nd = src.nodata if src.nodata is not None else NODATA
        for depth, poly in closed:
            # Mask by the ring itself. Its bounding box is not the ring: for a small ring in shoal
            # ground the box is mostly water outside it, which reads as an understated shoal that
            # is not there.
            geom = gpd.GeoSeries([poly], crs="EPSG:4326").to_crs(src.crs).iloc[0]
            try:
                inside = geometry_mask([geom], arr.shape, src.transform, invert=True)
            except Exception:
                continue
            wet = inside & (arr != nd) & (arr < 0)
            if not wet.any():
                continue
            shoalest = -float(arr[wet].max())
            # A closed isobath can enclose a shoal or a deep. Only a shoal ring owes a least
            # depth: the interior has to actually rise above the ring's own level.
            if shoalest >= depth - 0.05:
                continue
            shoal_rings += 1
            if not any(poly.contains(p) for p in pts):
                bare += 1
                worst = max(worst, depth - shoalest)

    return {
        "pass": bare == 0,
        "zoom": zoom,
        "closed_rings": len(closed),
        "shoal_rings": shoal_rings,
        "shoal_rings_without_least_depth": bare,
        "worst_understatement_m": worst,
        "failures": ([] if bare == 0 else
                     [f"{bare} of {shoal_rings} closed isobaths enclosing a shoal carry no "
                      f"sounding (worst {worst:.2f} m understated) — S-4 B-410b"]),
    }


def density(soundings_path, zoom, tile_px=512):
    """Nearest-neighbour spacing of the displayed field, in mm at chart scale.

    At its native zoom a tile of `tile_px` renders at that many CSS px, so ground distance
    converts to screen px through the zoom's own resolution and then to mm exactly.
    """
    from scipy.spatial import cKDTree
    snd = _read_soundings(soundings_path, zoom)
    if len(snd) < 2:
        return {"zoom": zoom, "displayed": len(snd), "failures": []}
    lon, lat, _ = (np.asarray(c, float) for c in zip(*snd))
    # Local equirectangular metres is plenty for a nearest-neighbour statistic within one stem.
    R = 6378137.0
    lat0 = math.radians(float(lat.mean()))
    x = np.radians(lon) * R * math.cos(lat0)
    y = np.radians(lat) * R
    d, _i = cKDTree(np.column_stack([x, y])).query(np.column_stack([x, y]), k=2)
    nn_m = d[:, 1]
    m_per_px = (2 * math.pi * R * math.cos(lat0)) / (tile_px * 2 ** zoom)
    nn_mm = nn_m / m_per_px * MM_PER_PX
    q = lambda t: float(np.percentile(nn_mm, t))
    median = q(50)
    band = ("tighter than critical" if median < SPACING_MM["critical"] else
            "critical" if median < SPACING_MM["supporting"] else
            "supporting" if median < SPACING_MM["fill"] else "fill")
    return {"zoom": zoom, "displayed": len(snd), "m_per_px": m_per_px,
            "nn_mm": {"p10": q(10), "median": median, "p90": q(90)},
            "band": band, "targets_mm": SPACING_MM, "failures": []}


def floor(window_tif, floors):
    """What each candidate SOUND_MIN_DEPTH_M admits. Finding 3: the 1.0 m default drops exactly
    the least depths S-4 B-410b puts at the top of what must survive selection, so the question is
    how much of what it drops is real shoal and how much is waterline artifact."""
    import rasterio
    import soundings_run
    rows = []
    with rasterio.open(window_tif) as src:
        nd = src.nodata if src.nodata is not None else NODATA
        for f in floors:
            g, _cx, _cy = soundings_run._shoalest_grid(src, nd, f)
            ok = ~np.isnan(g)
            rows.append({"floor_m": f, "cells": int(ok.sum()),
                         "shoalest_m": float(np.nanmin(g)) if ok.any() else None})
    base = rows[0]["cells"]
    for r in rows:
        r["added_vs_first"] = r["cells"] - base
    return {"floors": rows, "failures": []}


def _check():
    """Each measurement must fire on ground it should reject and stay quiet on ground it should
    accept. Synthetic stems only — no store, no network."""
    import tempfile
    import geopandas as gpd
    import rasterio
    from rasterio.transform import from_origin
    from shapely.geometry import LineString

    tmp = tempfile.mkdtemp()

    # A 20 m flat basin with an undetected 2 m pinnacle in the middle. 100 m px, EPSG:3857.
    n, px = 128, 100.0
    arr = np.full((n, n), -20.0, "float32")
    arr[64, 64] = -2.0
    tr = from_origin(0, n * px, px, px)
    dem = f"{tmp}/w.tif"
    with rasterio.open(dem, "w", driver="GTiff", height=n, width=n, count=1, dtype="float32",
                       nodata=NODATA, crs="EPSG:3857", transform=tr) as dst:
        dst.write(arr, 1)

    from pyproj import Transformer
    inv = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)

    def write_snd(path, cells, zoom_lo=8):
        with open(path, "w") as f:
            for (row, col, depth) in cells:
                x, y = tr * (col + 0.5, row + 0.5)
                lon, lat = inv.transform(x, y)
                f.write(json.dumps({
                    "type": "Feature", "tippecanoe": {"minzoom": zoom_lo},
                    "properties": {"depth_m": depth},
                    "geometry": {"type": "Point", "coordinates": [lon, lat]}}) + "\n")

    # Charting only the flat 20 m ground hides the pinnacle: the interpolant says 20 m where the
    # ground is 2 m, so B-410a must fail by ~18 m.
    corners = [(4, 4, 20.0), (4, 123, 20.0), (123, 4, 20.0), (123, 123, 20.0), (100, 20, 20.0)]
    miss = f"{tmp}/miss.geojsons"
    write_snd(miss, corners)
    r = interpolation(dem, miss, None, zoom=8, stride=2)
    assert not r["pass"], "B-410a must fail when a pinnacle is left uncharted"
    assert r["max_shortfall_m"] > 17.0, r["max_shortfall_m"]

    # Charting the pinnacle too clears it.
    hit = f"{tmp}/hit.geojsons"
    write_snd(hit, corners + [(64, 64, 2.0)])
    r = interpolation(dem, hit, None, zoom=8, stride=2)
    assert r["pass"], f"charted pinnacle must pass: {r['failures']}"

    # Zoom filtering: a sounding that only displays at z12 must not rescue z8.
    late = f"{tmp}/late.geojsons"
    with open(late, "w") as f:
        f.write(open(hit).read().replace('"minzoom": 8}', '"minzoom": 12}', 1))
    lines = open(late).read().splitlines()
    with open(late, "w") as f:
        for i, ln in enumerate(lines):
            o = json.loads(ln)
            o["tippecanoe"] = {"minzoom": 12} if o["properties"]["depth_m"] == 2.0 else {"minzoom": 8}
            f.write(json.dumps(o) + "\n")
    assert not interpolation(dem, late, None, zoom=8, stride=2)["pass"], \
        "a sounding that does not display at this zoom must not count as charted"
    assert interpolation(dem, late, None, zoom=12, stride=2)["pass"], \
        "…and must count at a zoom where it does display"

    # Contours are charted features too: a 2 m isobath ringing the pinnacle carries the shoal.
    ring = gpd.GeoDataFrame(
        [{"sys": "m", "depth_abs_m": 2.0,
          "geometry": LineString([tr * (60.5, 60.5), tr * (68.5, 60.5),
                                  tr * (68.5, 68.5), tr * (60.5, 68.5), tr * (60.5, 60.5)])}],
        crs="EPSG:3857").to_crs("EPSG:4326")
    cpath = f"{tmp}/c.fgb"
    ring.to_file(cpath, driver="FlatGeobuf")
    assert interpolation(dem, miss, cpath, zoom=8, stride=2)["pass"], \
        "an isobath around the shoal must satisfy B-410a on its own"

    # rings: a closed isobath around the pinnacle with no sounding inside is the B-410b failure;
    # putting the least depth on it clears the check.
    # The 2 m isobath exactly on a 2 m pinnacle does not rise above its own level, so it is not a
    # shoal ring and owes nothing.
    r = rings(dem, miss, cpath, zoom=8)
    assert r["closed_rings"] == 1 and r["shoal_rings"] == 0 and r["pass"], r

    # Labelled 5 m over the same 2 m ground it IS a shoal ring, and bare it understates by 3 m.
    deep_ring = ring.copy()
    deep_ring["depth_abs_m"] = 5.0
    dpath = f"{tmp}/c5.fgb"
    deep_ring.to_file(dpath, driver="FlatGeobuf")
    r = rings(dem, miss, dpath, zoom=8)
    assert r["shoal_rings"] == 1 and not r["pass"], r
    assert abs(r["worst_understatement_m"] - 3.0) < 0.01, r["worst_understatement_m"]
    r = rings(dem, hit, dpath, zoom=8)
    assert r["pass"], f"a sounding inside the ring must clear B-410b: {r['failures']}"

    # A ring around a DEEP pocket is not a shoal ring: only the bounding box of a small ring
    # touches shallower ground, and sampling that box instead of the ring invents a failure.
    deep_hole = arr.copy()
    deep_hole[:] = -1.0
    deep_hole[60:69, 60:69] = -8.0
    hp = f"{tmp}/hole.tif"
    with rasterio.open(hp, "w", driver="GTiff", height=n, width=n, count=1, dtype="float32",
                       nodata=NODATA, crs="EPSG:3857", transform=tr) as dst:
        dst.write(deep_hole, 1)
    r = rings(hp, miss, dpath, zoom=8)
    assert r["closed_rings"] == 1 and r["shoal_rings"] == 0 and r["pass"], r

    # density: spacing scales with zoom, and the band label tracks the practice figures.
    d8 = density(hit, zoom=8)
    d14 = density(hit, zoom=14)
    assert d14["nn_mm"]["median"] > d8["nn_mm"]["median"], "same ground is wider apart when zoomed in"
    assert d8["displayed"] == len(corners) + 1, d8["displayed"]
    assert density(hit, zoom=7)["displayed"] == 0, "minzoom 8 must not display at z7"

    # floor: lowering it can only admit cells, never drop them, and finds the shallower ground.
    shallow = arr.copy()
    shallow[0:8, 0:8] = -0.4
    sp = f"{tmp}/shallow.tif"
    with rasterio.open(sp, "w", driver="GTiff", height=n, width=n, count=1, dtype="float32",
                       nodata=NODATA, crs="EPSG:3857", transform=tr) as dst:
        dst.write(shallow, 1)
    import soundings_run
    soundings_run.SOUND_CELL_PX = 8
    f = floor(sp, [1.0, 0.3])
    assert f["floors"][1]["cells"] > f["floors"][0]["cells"], "a lower floor must admit more cells"
    assert f["floors"][0]["shoalest_m"] >= 1.0 and f["floors"][1]["shoalest_m"] < 1.0

    print("soundings self-check ok (B-410a fires on a hidden shoal, clears on a charted one)")


if __name__ == "__main__":
    a = sys.argv[1:]
    opt = lambda k, d: (type(d)(a[a.index(k) + 1]) if k in a else d)
    if a[:1] == ["--check"]:
        _check()
    elif a[:1] == ["interpolation"] and len(a) >= 4:
        r = interpolation(a[1], a[2], a[3] if a[3] != "-" else None,
                          opt("--zoom", 12), opt("--tol-m", 0.0), opt("--stride", 4))
        print(json.dumps(r, indent=2))
        sys.exit(0 if r["pass"] else 1)
    elif a[:1] == ["rings"] and len(a) >= 4:
        r = rings(a[1], a[2], a[3], opt("--zoom", 12))
        print(json.dumps(r, indent=2))
        sys.exit(0 if r["pass"] else 1)
    elif a[:1] == ["density"] and len(a) >= 2:
        print(json.dumps(density(a[1], opt("--zoom", 12), opt("--tile-px", 512)), indent=2))
    elif a[:1] == ["floor"] and len(a) >= 2:
        floors = [float(x) for x in opt("--floors", "1.0,0.5,0.3,0.2,0.0").split(",")]
        print(json.dumps(floor(a[1], floors), indent=2))
    else:
        sys.exit(__doc__.strip().split("\n\n")[-1])
