#!/usr/bin/env -S uv run --script
"""Cost and safety metrics for the shallow-band work.

Two things drive depare cost and they move independently: **parts** drive wall time (per-part
overhead in the polygonizer and the GEOS pass) and **vertices** drive bytes and memory (the
partition set, the per-feature GeoJSON ceiling). Measured on a marsh crop, pond-filling cut parts
11.6x and vertices only 1.4x — so a single "size" number hides the answer.

Feature count is NOT a cost metric: `gdal_contour -p` emits one MultiPolygon per band, so it is a
constant (4 on a shallow crop) regardless of complexity.

    metrics.py raster <dem.tif> [--ref <before.tif>]
    metrics.py vector <bands.fgb>
    metrics.py --check
"""
import json
import os
import sys

import numpy as np

LEVELS = (0, -2, -5, -10)
BANDS = ((0, -2), (-2, -5), (-5, -10), (-10, -20))
DRYING_CAP = 16.0
NODATA = -9999.0
# 16 mm^2 at z15 (1:7425, 7.425 m per map mm) — the sub-legible threshold the plan gates on.
SMALL_PART_M2 = 16 * (7.425 ** 2)


def crossings(arr, nd, level):
    """Mean sign changes of (arr >= level) per raster row — how many times a band edge is
    crossed scanning across the tile. The plan's headline cost number."""
    m = (arr != nd) & (arr >= level)
    return float(np.abs(np.diff(m.astype(np.int8), axis=1)).sum(axis=1).mean())


def band_parts(arr, nd, lo, hi):
    """4-connected component count of the band [hi, lo) — what the polygonizer will emit."""
    from scipy.ndimage import label
    band = (arr != nd) & (arr < lo) & (arr >= hi)
    return int(label(band)[1]) if band.any() else 0


def water_stats(arr, nd):
    """Water pixels, connected-component count, and the share held by the largest component.

    The largest-share number is the channel-network proxy: if a generalization operator closes a
    narrow channel the network splits and the share drops. It is what separates pond-fill
    (unchanged) from morphological closing at r>=4 (0.49 -> 0.34)."""
    from scipy.ndimage import label
    w = (arr != nd) & (arr < 0)
    n = int(w.sum())
    if n == 0:
        return 0, 0, 0.0
    lab, ncomp = label(w)
    sizes = np.bincount(lab.ravel())[1:]
    return n, int(ncomp), float(sizes.max() / n)


def raster(path, ref=None):
    import rasterio
    with rasterio.open(path) as s:
        arr, nd = s.read(1), (s.nodata if s.nodata is not None else NODATA)
        res = s.res[0]
    m = {"path": path, "res_m": res, "shape": list(arr.shape)}
    for lv in LEVELS:
        m[f"crossings@{lv}"] = crossings(arr, nd, lv)
    for lo, hi in BANDS:
        m[f"parts@{lo}"] = band_parts(arr, nd, lo, hi)
    m["water_px"], m["water_components"], m["largest_share"] = water_stats(arr, nd)
    m["drying_px"] = int(((arr != nd) & (arr >= 0) & (arr <= DRYING_CAP)).sum())
    if ref:
        with rasterio.open(ref) as s:
            b = s.read(1)
        valid = arr != nd
        # The safety gate: never chart deeper than the source (IHO S-4 B-411.5).
        m["shoal_ok"] = bool((arr[valid] >= b[valid] - 1e-9).all())
        m["max_deepening_m"] = float(np.max(np.maximum(b[valid] - arr[valid], 0.0)))
        rw = int(((b != nd) & (b < 0)).sum())
        m["water_lost"] = 1.0 - m["water_px"] / rw if rw else 0.0
    return m


def vector(path):
    import geopandas as gpd
    import shapely
    g = gpd.read_file(path)
    geoms = g.geometry.values
    parts = int(sum(shapely.get_num_geometries(x) for x in geoms))
    vtx = int(shapely.get_num_coordinates(geoms).sum())
    small_parts = small_vtx = 0
    for gm in geoms:
        for i in range(shapely.get_num_geometries(gm)):
            p = shapely.get_geometry(gm, i)
            if p.area < SMALL_PART_M2:
                small_parts += 1
                small_vtx += int(shapely.get_num_coordinates(p))
    m = {"path": path, "features": len(g), "parts": parts, "vertices": vtx,
         "fgb_bytes": os.path.getsize(path), "small_parts": small_parts,
         "small_part_vertices": small_vtx}
    if parts:
        m["vertices_per_part"] = vtx / parts
        m["small_part_vertex_share"] = small_vtx / vtx if vtx else 0.0
    for col in ("drval1", "drval2"):
        if col in g:
            m[f"{col}_values"] = sorted({float(v) for v in g[col].dropna()})
    return m


def _check():
    """Crossings count band-edge transitions; components and largest-share detect a closed
    channel; feature count is deliberately not used as a cost metric."""
    a = np.full((32, 32), 1.0, np.float32)      # marsh above datum
    a[:, 8:11] = -3.0                            # one channel spanning the array
    a[20:23, 20:23] = -1.0                       # one enclosed pond
    n, ncomp, largest = water_stats(a, NODATA)
    assert ncomp == 2, f"expected channel + pond = 2 components, got {ncomp}"
    assert n == 32 * 3 + 9, n
    assert abs(largest - (96 / 105)) < 1e-6, largest
    # two edges per channel per row, plus the pond's two on its rows
    assert abs(crossings(a, NODATA, 0) - (2 + 2 * 3 / 32)) < 1e-6, crossings(a, NODATA, 0)
    # a level deeper than the pond sees only the channel
    assert abs(crossings(a, NODATA, -2) - 2.0) < 1e-6, crossings(a, NODATA, -2)
    # closing the pond drops a component but must not touch the largest share's numerator
    b = a.copy()
    b[20:23, 20:23] = 1.0
    _n2, ncomp2, largest2 = water_stats(b, NODATA)
    assert ncomp2 == 1 and largest2 == 1.0, (ncomp2, largest2)
    # closing the CHANNEL splits the network — the failure the gate must catch
    c = a.copy()
    c[16, 8:11] = 1.0
    _n3, ncomp3, largest3 = water_stats(c, NODATA)
    assert ncomp3 == 3 and largest3 < largest, (ncomp3, largest3, largest)
    print("metrics self-check ok")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["--check"]:
        _check()
    elif a[:1] == ["raster"] and len(a) >= 2:
        ref = a[a.index("--ref") + 1] if "--ref" in a else None
        print(json.dumps(raster(a[1], ref), indent=2))
    elif a[:1] == ["vector"] and len(a) == 2:
        print(json.dumps(vector(a[1]), indent=2))
    else:
        sys.exit(__doc__.strip().split("\n\n")[-1])
