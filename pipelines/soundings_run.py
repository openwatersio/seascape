"""Static soundings: shoalest-per-cell point depths off each aggregation tile's merged DEM.

A second consumer of the same merged, smoothed DEM the aggregation stage builds — the one
contour_run.py forks off. On a real chart, point soundings are the primary depth cue.

A client-side plugin (maplibre-contour's spot-soundings fork) point-samples one jittered
DEM pixel per grid node, which can miss a shoal between nodes. We own the whole DEM at build
time, so each grid cell emits its SHOALEST wet pixel (hazard-correct) with the depth floored
toward shallower — a charted sounding is never deeper than reality.

A shoalest-wins quadtree tags each point with the coarsest zoom it survives to (tippecanoe
minzoom), so zoomed-out views carry only the shoalest few per area and low-zoom tiles stay
small; the viewer's symbol collision (sorted shallow-first) de-conflicts what remains.

Per tile: read merged DEM (3857) in row strips -> a staggered-grid PYRAMID (one jittered quincunx
grid per zoom level, each point valued by the shoalest wet pixel in its block) -> keep points in
the unbuffered tile bbox -> reproject to 4326 -> store/soundings/{stem}.geojson (per-feature
tippecanoe minzoom==maxzoom, so each zoom shows one even field, densifying inward). bundle()
tippecanoes them into a `soundings` layer.

Chart-cartography grounding (shoal-bias, the radius method, SCAMIN, spacing rules) and the
seamap fork this mirrors: ../docs/nautical-chart-references.md.
"""

import json
import math
import os
import subprocess
import sys
from glob import glob

import mercantile

import contour_run
import keys
import utils

NODATA = -9999
M_PER_FT = 0.3048
M_PER_FATHOM = 1.8288
# Grid cell in merged-DEM pixels — sets the on-screen sounding spacing (decimation holds it
# ~constant across zoom). A native output tile is ~512 DEM px, so 64 → ~8x8 soundings per tile.
# Smaller = denser everywhere. Env-tunable on a re-run.
SOUND_CELL_PX = int(os.environ.get("SOUND_CELL_PX", "64"))
# Drop soundings shallower than this (metres): near-waterline cells just read "0" and clutter.
SOUND_MIN_DEPTH_M = float(os.environ.get("SOUND_MIN_DEPTH_M", "1.0"))


def _tc(minz, child_z):
    """Per-feature tippecanoe zoom placement. Coarser levels swap out as the next
    finer field arrives (maxzoom == their own zoom). The finest level (minz ==
    child_z) declares NO maxzoom so it persists to the tileset max: child_z is the
    LOCAL best-source ceiling (z10 in Danish waters, z13+ where CUDEM reaches),
    and the global tileset tiles past it wherever any region goes deeper — capping
    at child_z blanked the layer there (soundings gone at z11+ over Denmark while
    contours carried on)."""
    return {"minzoom": minz} if minz == child_z else {"minzoom": minz, "maxzoom": minz}


def _depths(depth_pos):
    """Depth (metres, positive down) → chart labels, each floored toward shallower so the printed
    number is never deeper than the surface. Metres carry one decimal in the shoal band (< 6 m,
    chart practice) and a floored integer below it — a single display-ready value, so the viewer
    just prints depth_m (no separate depth_dm)."""
    return {
        "depth_m": math.floor(depth_pos * 10) / 10 if depth_pos < 6 else int(math.floor(depth_pos)),
        "depth_ft": int(math.floor(depth_pos / M_PER_FT)),
        "depth_fm": int(math.floor(depth_pos / M_PER_FATHOM)),
    }


def _jit(a, b):
    """Deterministic pseudo-random in [0, 1) per cell (stable across rebuilds — no churn in the
    incremental store), a seeded LCG like the seamap fork's jitter."""
    s = ((a * 73856093) ^ (b * 19349663)) & 0xFFFFFFFF
    s = (s * 1664525 + 1013904223) & 0xFFFFFFFF
    return s / 0x100000000


def _shoalest_grid(src, nodata, min_depth):
    """Level-0 grid g[gy, gx] = shoalest depth (m, positive down) per SOUND_CELL_PX cell; NaN
    where dry / no wet pixel / shallower than min_depth (near-waterline "0" clutter). Row-strip
    reads bound peak memory to one strip, not the whole (multi-GB) DEM."""
    import numpy as np
    from rasterio.windows import Window
    W, H = src.width, src.height
    g = np.full(((H + SOUND_CELL_PX - 1) // SOUND_CELL_PX,
                 (W + SOUND_CELL_PX - 1) // SOUND_CELL_PX), np.nan)
    for gy, r0 in enumerate(range(0, H, SOUND_CELL_PX)):
        h = min(SOUND_CELL_PX, H - r0)
        strip = src.read(1, window=Window(0, r0, W, h))
        for gx, c0 in enumerate(range(0, W, SOUND_CELL_PX)):
            block = strip[:, c0:c0 + SOUND_CELL_PX]
            wet = (block != nodata) & (block < 0)
            if wet.any():
                depth = -float(block[wet].max())
                if depth >= min_depth:
                    g[gy, gx] = depth
    return g


def _reduce_shoalest(g):
    """2x2 shoalest (minimum positive-down depth) reduction → the next coarser level, NaN-aware
    (an all-dry quad stays NaN)."""
    import numpy as np
    ny, nx = g.shape
    gp = np.full((ny + ny % 2, nx + nx % 2), np.nan)
    gp[:ny, :nx] = g
    quads = np.stack([gp[0::2, 0::2], gp[0::2, 1::2], gp[1::2, 0::2], gp[1::2, 1::2]])
    with np.errstate(invalid="ignore"):
        return np.where(np.isnan(quads).all(0), np.nan, np.nanmin(quads, axis=0))


def _pyramid(src, nodata, bbox, min_depth, z, child_z):
    """A staggered-grid pyramid: for each zoom level, a quincunx grid at that level's spacing, each
    point valued by the SHOALEST wet pixel in its block (so a block's shoal is never hidden at
    coarse zoom) and placed on a per-row-offset lattice jittered into the cell — an even, chart-
    like field at EVERY zoom. (A single baked stagger can't do that: uniform decimation drops the
    offset rows at coarse zoom, leaving a square grid — the reason a per-level pyramid is needed.)
    Returns [(depth_pos, x3857, y3857, minz)] with minz==maxz per level, so each zoom shows exactly
    one level and it densifies as you zoom in."""
    import numpy as np
    transform = src.transform
    g = _shoalest_grid(src, nodata, min_depth)
    pts = []
    for level in range(max(0, child_z - z) + 1):
        cell = SOUND_CELL_PX * (1 << level)
        minz = child_z - level
        ny, nx = g.shape
        for gy in range(ny):
            stagger = cell * 0.5 if gy % 2 else 0.0  # offset odd rows → quincunx at this level
            for gx in range(nx):
                d = g[gy, gx]
                if np.isnan(d):
                    continue
                cx = gx * cell + stagger + cell * (0.25 + 0.5 * _jit(gx, gy))  # jitter, middle half
                cy = gy * cell + cell * (0.25 + 0.5 * _jit(gy, gx))
                x, y = transform * (cx, cy)
                if bbox.left <= x <= bbox.right and bbox.bottom <= y <= bbox.top:
                    pts.append((float(d), x, y, minz))
        g = _reduce_shoalest(g)
    return pts


def generate(filepath):
    import rasterio
    from pyproj import Transformer
    agg_id, filename = filepath.split("/")[-2:]
    z, x, y, child_z = (int(a) for a in filename.replace("-aggregation.csv", "").split("-"))
    tile = mercantile.Tile(x=x, y=y, z=z)
    tmp = f"store/aggregation/{agg_id}/{z}-{x}-{y}-{child_z}-tmp"
    stem = f"{z}-{x}-{y}-{child_z}"
    dem = f"{tmp}/{len(glob(f'{tmp}/*.tiff')) - 1}-3857.tiff"
    if not os.path.exists(dem):
        print(f"soundings: no merged DEM for {filename}")
        return

    # Drop a previous run's artifact AND its key sidecar up front: a re-run that now yields no
    # points must not leave the old one behind to bundle stale, and a crash after this point
    # must read STALE next run — under FORCE the key is unchanged, so a surviving sidecar would
    # read fresh forever over the deleted artifact (see the contour fork; same rule all three).
    out = f"store/soundings/{stem}.geojson"
    for stale in (out, keys.sidecar(out)):
        if os.path.isfile(stale):
            os.remove(stale)

    bbox = mercantile.xy_bounds(tile)  # unbuffered, tile-aligned (EPSG:3857)
    with rasterio.open(dem) as src:
        nodata = src.nodata if src.nodata is not None else NODATA
        pts = _pyramid(src, nodata, bbox, SOUND_MIN_DEPTH_M, z, child_z)
    if not pts:
        print(f"soundings: no wet cells for {filename}")
        return

    to4326 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    feats = []
    for d, x3857, y3857, minz in pts:
        lon, lat = to4326.transform(x3857, y3857)
        # Each level shows at exactly one zoom (one clean staggered field per zoom);
        # the finest level rides uncapped to the tileset max — see _tc.
        feats.append({"type": "Feature", "tippecanoe": _tc(minz, child_z),
                      "properties": _depths(d),
                      "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]}})

    utils.create_folder("store/soundings")
    with open(out, "w") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f)
    print(f"soundings: {filename} -> {len(feats)} points")


def _live(paths, stems):
    """Drop soundings orphaned by a covering re-tiling (same class as contour's _live_fgbs: a
    re-tiled source area leaves the old stem's file behind, sync has no --delete, and bundling it
    alongside the new tiling doubles up). Keep only current-covering stems; keep all when None."""
    if stems is None:
        return paths
    return [p for p in paths if p.split("/")[-1].rsplit(".", 1)[0] in stems]


def bundle():
    """tippecanoe the per-tile soundings into store/bundle/soundings.pmtiles (layer `soundings`).
    Per-feature tippecanoe.minzoom places each point from the zoom the grid decimation assigned,
    so no density dropping is needed (-r1 keeps every surviving point). Bundled before the
    contour tile-join, which folds this layer into vector.pmtiles. Skips when every member
    tile's key is unchanged and the pmtiles is already on disk (the local iterative loop; the
    build box's store/bundle is never hydrated, so a box build always rebuilds it)."""
    gj = _live(sorted(glob("store/soundings/*.geojson")), contour_run._current_stems())
    if not gj:
        print("soundings bundle: no soundings")
        return
    # Shared tileset maxzoom (see contour_run.bundle_maxz): tiling only to this
    # layer's own regional max would truncate it out of deeper joined tiles.
    maxz = contour_run.bundle_maxz(
        max(int(g.split("/")[-1].replace(".geojson", "").split("-")[3]) for g in gj))
    out = "store/bundle/soundings.pmtiles"
    skey = keys.stage_key([f"{g}:{keys.read_key(g) or ''}" for g in gj],
                          ["soundings_run", "contour_run", "utils"], {"maxz": maxz})
    if keys.is_fresh(out, skey):
        print("soundings bundle: inputs unchanged — skip")
        return
    utils.create_folder("store/bundle")
    subprocess.run(
        ["tippecanoe", "-o", out, "-f", "-l", "soundings",
         "-n", "Bathymetric soundings", "-A", utils.ATTRIBUTION, "-Z", "0", "-z", str(maxz),
         "-P", "-q", "-r1", "-y", "depth_m", "-y", "depth_ft", "-y", "depth_fm",
         *gj], check=True)
    keys.write_key(out, skey)
    print(f"soundings bundle: {out} (z0-{maxz}, {len(gj)} tiles)")


def _check():
    """Grid shoalest per cell; <min_depth dropped; 2x2 reduction keeps the shoalest; the pyramid
    staggers each zoom into a quincunx and carries a block's shoal up to coarse zoom; depth floors
    shallower with one decimal in the shoal band."""
    import tempfile
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    # metres: one decimal in the shoal band (<6 m), floored int above; never rounds deeper
    assert _depths(9.8)["depth_m"] == 9 and _depths(9.8)["depth_ft"] == 32
    assert _depths(3.94)["depth_m"] == 3.9 and _depths(5.0)["depth_m"] == 5.0
    assert "depth_dm" not in _depths(3.0)   # collapsed into depth_m

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
    global SOUND_CELL_PX
    SOUND_CELL_PX = 128
    from types import SimpleNamespace
    bbox = SimpleNamespace(left=0, bottom=0, right=25600, top=25600)
    with rasterio.open(p) as src:
        g = _shoalest_grid(src, NODATA, 1.0)
    assert np.isnan(g[0, 0])                        # <1 m patch dropped
    assert g[1, 1] == 3.0 and g[0, 1] == 100.0      # shoalest per cell
    assert _reduce_shoalest(g).tolist() == [[3.0]]  # 2x2 shoalest = the -3 m

    with rasterio.open(p) as src:
        pts = _pyramid(src, NODATA, bbox, 1.0, z=8, child_z=9)  # level 0 (z9) + level 1 (z8)
    assert all(d >= 1.0 for d, *_ in pts)                       # no near-waterline "0"
    shoal0 = next(x for d, x, y, mz in pts if mz == 9 and d == 3.0)
    assert shoal0 >= 22400                                      # odd row (gy=1) → staggered right
    assert min(d for d, x, y, mz in pts if mz == 8) == 3.0      # block shoal survives to coarse zoom
    assert _jit(1, 1) == _jit(1, 1) and 0 <= _jit(3, 7) < 1     # jitter deterministic + in range

    # orphan filter: keep only current-covering stems; passthrough when stems is None
    paths = ["store/soundings/4-5-6-8.geojson", "store/soundings/11-300-400-13.geojson"]
    assert _live(paths, {"11-300-400-13"}) == ["store/soundings/11-300-400-13.geojson"]
    assert _live(paths, None) == paths

    # zoom placement: coarser levels swap out at their own zoom; the finest level
    # (minz == child_z) is uncapped so it persists above the local source ceiling
    assert _tc(8, 10) == {"minzoom": 8, "maxzoom": 8}
    assert _tc(10, 10) == {"minzoom": 10}
    print("soundings_run self-check ok")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["bundle"]:
        bundle()
    elif a[:1] == ["check"]:
        _check()
    else:
        sys.exit("usage: soundings_run.py bundle | check")
