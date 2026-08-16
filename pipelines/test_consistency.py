"""Cross-layer consistency: the raster shading, the isobaths, and the depth areas agree.

Three products reduce the SAME merged mosaic through three different paths, and nothing else
asserts they still describe the same seabed:

  * raster shading   mosaic -> class-aware shoal pyramid (utils._block_reduce) -> read at the
                     target zoom -> smooth -> classify (terrain._encode_tile) -> Terrarium
  * isobaths         mosaic -> fork window (smooth + pond_fill) -> land clamp ->
                     gdal_contour -> Chaikin/simplify -> clip
  * depth areas      the same fork window -> gdal_contour -p -> coverage simplify -> land cut

One synthetic tile carries the structures where those paths have historically parted company —
a channel through a land bar, a shoal inside a deeper basin, a drying flat with an enclosed
sub-legible pond, and a deep basin below the coarsening threshold — and is pushed through the
real stages at two zooms: the native child_z and the coarse overview stem terrain.render_stems
plans above it (where the raster reads the pyramid and the vectors do not re-derive).

What is asserted, and the bound each rests on:

  1. ONE SURFACE. Over the navigable band the served raster and the forks' window are the same
     array, up to the encode's quantization step: the raster never charts more than one step
     shallower than the vector surface, and never charts deeper than it at all — EXCEPT where
     smooth.pond_fill licensed the vectors to shoal (the enclosed pond), which is asserted to
     be the only place the second inequality fails.
  2. DOMAIN AGREEMENT. A point the raster charts as land carries no depth area and no drying;
     a point it charts in the water domain (depth, drying, or unknown) is covered by a depare
     feature. Named feature centres, then every 4th pixel of the whole tile, where the budget is
     one SERVED pixel: the land mask rasterizes by pixel centre at each zoom's own resolution
     while the partition edge was cut at native resolution, so the class line each product draws
     can differ by the width of the pixel the raster reduced into, and no further.
  3. ZOOMING OUT MUST NOT DEEPEN. The coarse tile against the shoalest water the native tiles
     hold under it, at zero tolerance: terrain._FinerTiles clamps every rendered tile shoal-ward
     against exactly that reduction, so the served pyramid is monotone by construction.
  4. BAND BRACKETING. The raster's charted depth at a pixel lies between the shallowest drval1
     and the deepest drval2 of the depare bands under that pixel's footprint, per ladder — the
     guaranteed form of "the band contains the pyramid's value", since both reductions are
     shoal-biased but neither is the other's decimation. The deep side is exact at every zoom
     (it rides property 3's clamp); the shallow side is exact at the native zoom and carries
     COARSE_SHOAL_ALLOWANCE_M one zoom out, and below the deep-coarsening threshold no bracket
     exists by design.
  5. THE CHANNEL STAYS OPEN. Along the channel centreline the raster charts water domain (never
     the land code) at BOTH zooms, and the depare water coverage contains the whole line.
  6. THE ISOBATH SEPARATES THE RASTER. The closed -5 m contour around the shoal encloses the
     shoal and excludes the basin, and the served raster is at-or-shallower than -5 m inside it
     and at-or-deeper outside — with a 2 px geometric margin (gdal_contour's sub-pixel
     interpolation plus the ~1 px coverage simplify) and one quantization step of value slack.
  7. LINE ON EDGE. The -5 m isobath and the 5 m depth-area edge are the same geometry to
     CONTOUR_BAND_TOL_PX — the line IS the band edge (derived from it, contour_run), so the
     budget is only the 4326→3857 round trip of the write grid.

6 and 7 are native-zoom only: the vectors are cut once at child_z and carry their own per-feature
minzoom, so at the coarse zoom there is no re-derived line to compare and 4 is the whole claim.

Run from pipelines/ (independently, or through `just test-engine`):
  uv run python test_consistency.py
"""

import math
import os
import shutil
import sys
import tempfile

import mercantile
import numpy as np
import rasterio
from rasterio.transform import from_origin

PIPE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PIPE)

import aggregation_reproject  # noqa: E402
import contour_run  # noqa: E402
import depare_run  # noqa: E402
import encode  # noqa: E402
import smooth  # noqa: E402
import terrain  # noqa: E402
import utils  # noqa: E402

# ── the fixture ────────────────────────────────────────────────────────────────────────────────
# One aggregation tile at child_z 14: 1024 px of core at res(14), which is small enough to run in
# the test suite and deep enough that the extra fork generalization is live (pond_fill from
# child_z 14) — the divergence property 1 has to bound.
ANCHOR = mercantile.Tile(x=4100, y=4100, z=13)
CHILD_Z = 14
CORE = 1024

BASIN, SHOAL, CHANNEL, DEEP, DRYING_H, POND, LAND_H = -32.0, -3.0, -7.0, -400.0, 5.0, -1.0, 50.0
# Every depth sits strictly inside a DEPARE_LEVELS bucket, so no fixture value is ambiguous
# between two bands: -32 -> [30,50], -3 -> [2,5], -7 -> [5,10], -400 -> [300,500].

BAR = slice(300, 420)             # rows: a land bar splitting the basin in two
# 2 px of water cut clean through the bar, straddling the 2x2 grid _block_reduce decimates on
# (cols 497/498 fall in different blocks), so EVERY block over the channel is mixed land/water:
# only the class-aware reduction keeps it open at the coarse zoom, a plain max closes it.
CHANNEL_COLS = slice(497, 499)
# A LOBED shoal, not a rectangle: a rectangle's isobath and band edge both generalize to the same
# four segments whatever the tolerance, so only a boundary with real curvature can tell whether the
# line and the edge are still being generalized together.
SHOAL_CENTRE, SHOAL_R, SHOAL_LOBE, SHOAL_LOBES = (650.0, 250.0), 50.0, 6.0, 9
DRY_BOX = (slice(150, 230), slice(700, 780))
POND_BOX = (slice(187, 193), slice(737, 743))   # enclosed, sub-legible: smooth.pond_fill's target
DEEP_BOX = (slice(850, 1000), slice(100, 900))

# (row, col) of the core pixel each property samples. Feature CENTRES — every one is >= 20 px
# from the nearest class boundary, which is the margin property 2 documents.
POINTS = {
    "basin_north": (200, 300),
    "basin_south": (550, 600),
    "shoal": (int(SHOAL_CENTRE[0]), int(SHOAL_CENTRE[1])),
    "channel": (360, 497),
    "drying": (170, 710),
    "pond": (190, 740),
    "land": (360, 100),
    "deep": (920, 500),
}
CHANNEL_LINE_ROWS = (290, 430)    # from the north basin, through the bar, into the south basin
CHANNEL_LINE_COL = 498.0          # the channel's centreline
NOTCH_COLS = slice(496, 500)      # the OSM water gap in the land bar, one pixel clear of the cut

CONTOUR_LEVEL = -5.0              # the closed isobath ringing the shoal
GEOMETRY_TOL_PX = 2.0             # gdal_contour interpolates within a pixel; the coverage simplify ~1 px
CONTOUR_BAND_TOL_PX = 0.01        # the line IS the band edge; slack is the write grid round trip

SWEEP_STRIDE_PX = 4               # native pixels between whole-raster samples

# How far a COARSE zoom's raster may sit SHOALER than vectors cut at native resolution. Zero at
# the native zoom, where the products share one array. One zoom out the raster is smoothed at a
# sigma measured in ITS OWN pixels (smooth.py: "sigma is in merged-DEM pixels"), so the same
# seabed gets a physically wider blur, which pulls harder toward datum near land — measured 1.75 m
# over this fixture's drying flat. A generalization is licensed to shoal, so this is a tolerance
# and not a defect; the DEEPENING direction carries no budget at any zoom (properties 3 and 4),
# because terrain._FinerTiles clamps every tile against the shoal reduction of the zoom below it.
COARSE_SHOAL_ALLOWANCE_M = 2.0


def _res(zoom):
    return aggregation_reproject.get_resolution(zoom)


def _xy(row, col):
    """A core pixel centre in EPSG:3857."""
    b = mercantile.xy_bounds(ANCHOR)
    res = _res(CHILD_Z)
    return b.left + (col + 0.5) * res, b.top - (row + 0.5) * res


def _dem():
    rng = np.random.default_rng(7)
    arr = np.full((CORE, CORE), BASIN, dtype="float32")
    # Noise on the deep basin, so the deep blur has a gradient to work on rather than a flat field.
    arr[DEEP_BOX] = DEEP + rng.normal(0, 30, arr[DEEP_BOX].shape)
    row, col = np.indices(arr.shape)
    dr, dc = row - SHOAL_CENTRE[0], col - SHOAL_CENTRE[1]
    arr[np.hypot(dr, dc) < SHOAL_R + SHOAL_LOBE * np.sin(SHOAL_LOBES * np.arctan2(dr, dc))] = SHOAL
    arr[BAR, :] = LAND_H
    arr[BAR, CHANNEL_COLS] = CHANNEL
    arr[DRY_BOX] = DRYING_H
    arr[POND_BOX] = POND
    return arr


def _write_masks(d):
    """OSM-shaped masks: the land bar with the channel notched out of it (a dredged cut is mapped
    as water, so the contour land clamp must not close it), and a far-away water polygon — the
    depare nodata pass needs a water feed present, and none of it may reach the fixture."""
    import geopandas as gpd
    from shapely.geometry import box

    b = mercantile.xy_bounds(ANCHOR)
    res = _res(CHILD_Z)

    def cell_box(r0, c0, r1, c1):
        return box(b.left + c0 * res, b.top - r1 * res, b.left + c1 * res, b.top - r0 * res)

    land = [cell_box(BAR.start, -100, BAR.stop, NOTCH_COLS.start),
            cell_box(BAR.start, NOTCH_COLS.stop, BAR.stop, CORE + 100)]
    gpd.GeoDataFrame(geometry=land, crs="EPSG:3857").to_file(f"{d}/land.fgb", driver="FlatGeobuf")
    gpd.GeoDataFrame({"kind": ["lake"]}, geometry=[box(2.0e7, 2.0e7, 2.001e7, 2.001e7)],
                     crs="EPSG:3857").to_file(f"{d}/water.fgb", driver="FlatGeobuf")
    os.environ["LANDMASK"], os.environ["WATERMASK"] = f"{d}/land.fgb", f"{d}/water.fgb"


def _write_mosaic_tile(tile, arr):
    """One mosaic tile COG, exactly as mosaic.tile writes it: Float32, nodata -9999, and the
    class-aware shoal pyramid attached through a VRT (utils.shoal_cog_source) — the pyramid the
    coarse render reads is therefore the real one, not a gdaladdo stand-in. Returns the stem."""
    stem = f"{tile.z}-{tile.x}-{tile.y}-{CHILD_Z}"
    b = mercantile.xy_bounds(tile)
    res = _res(CHILD_Z)
    os.makedirs("store/mosaic/tiles", exist_ok=True)
    raw = f"store/mosaic/tiles/{stem}-raw.tif"
    with rasterio.open(raw, "w", driver="GTiff", height=CORE, width=CORE, count=1,
                       dtype="float32", nodata=terrain.NODATA, crs="EPSG:3857",
                       transform=from_origin(b.left, b.top, res, res)) as dst:
        dst.write(arr, 1)
    with utils.shoal_cog_source(raw) as src:
        utils.run_command(f"gdal_translate -q -of COG -a_nodata {terrain.NODATA} "
                          f"-co OVERVIEWS=FORCE_USE_EXISTING -co BLOCKSIZE=512 "
                          f"{src} store/mosaic/tiles/{stem}.tif")
    os.remove(raw)
    return stem


def _write_mosaic(centre):
    """The fixture tile plus its eight neighbours, all basin. Every smoothing halo then reads real
    water instead of the beyond-build nodata, which each zoom's own sigma would otherwise pull
    shoal-ward by a different amount — a genuine build-edge effect, but not the one under test, and
    it would force a 130 px margin out of every sweep."""
    stems = [_write_mosaic_tile(centre, _dem())]
    flat = np.full((CORE, CORE), BASIN, dtype="float32")
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            if dx or dy:
                stems.append(_write_mosaic_tile(
                    mercantile.Tile(x=centre.x + dx, y=centre.y + dy, z=centre.z), flat))
    os.makedirs("store/aggregation", exist_ok=True)
    with open("store/aggregation/covering.txt", "w") as f:
        f.write("\n".join(stems) + "\n")
    return stems[0]


# ── reading the served raster ──────────────────────────────────────────────────────────────────

def _served(path):
    """{(z, x, y): decoded 512x512 elevation} for one terrain pmtiles archive."""
    import imagecodecs
    from pmtiles.reader import MmapSource, Reader, all_tiles
    out = {}
    with open(path, "r+b") as f:
        for zxy, payload in all_tiles(Reader(MmapSource(f)).get_bytes):
            out[zxy] = encode.decode(imagecodecs.webp_decode(payload).astype("float32"))
    return out


def _pixel(z, x3857, y3857):
    """The served pixel holding a 3857 point: (tile, col, row, its 3857 footprint)."""
    lng, lat = mercantile.lnglat(x3857, y3857)
    t = mercantile.tile(lng, lat, z)
    b = mercantile.xy_bounds(t)
    px = (b.right - b.left) / 512
    col = min(511, max(0, int((x3857 - b.left) / px)))
    row = min(511, max(0, int((b.top - y3857) / px)))
    left, top = b.left + col * px, b.top - row * px
    return t, col, row, (left, top - px, left + px, top)


def _at(tiles, z, x3857, y3857):
    t, col, row, footprint = _pixel(z, x3857, y3857)
    return float(tiles[(z, t.x, t.y)][row, col]), footprint


def _quant_step(value, z):
    """encode.quantize's per-pixel step at this value and zoom — the bound on how far the served
    raster may sit shoal of the surface it was encoded from."""
    cap = 2.0 ** math.ceil(math.log2(max(abs(value) * encode.SHALLOW_REL, encode.SHALLOW_MIN_STEP)))
    return min(encode.quantization_factor(z), cap)


def _klass(v):
    """The published non-negative domain is three flat codes (terrain.py)."""
    if v == terrain.LAND:
        return "land"
    if v == terrain.DRYING:
        return "drying"
    if v == terrain.UNKNOWN:
        return "unknown"
    return "depth"


# ── the checks ─────────────────────────────────────────────────────────────────────────────────

def check_one_surface(stem, native):
    """1. The served raster and the forks' window are one surface over the navigable band.

    The fork window's interior is the anchor tile at res(child_z), the same grid the native render
    tiles it into, so this is a pixel-for-pixel comparison of the shipped product against the
    surface the vectors were cut from. Two inequalities, each with its own reason:

      raster <= fork + step   ALWAYS. Both descend from the same smoothed array; the encode only
                              ever rounds shoal-ward, and the fork's own generalizations
                              (pond_fill) only ever shoal.
      raster >= fork          ALWAYS, over the pixels both still call water. The one operator that
                              can break it, pond_fill, lifts its component out of the water domain
                              entirely, so it shows up as a class difference instead — asserted
                              separately, and asserted to be confined to the pond.
    """
    with rasterio.open(f"store/window/{stem}.tif") as w:
        halo = smooth.halo_px()
        fork = w.read(1, window=rasterio.windows.Window(halo, halo, CORE, CORE))
    span = 2 ** (CHILD_Z - ANCHOR.z)
    raster = np.empty((CORE, CORE), dtype="float64")
    for i in range(span):
        for j in range(span):
            raster[j * 512:(j + 1) * 512, i * 512:(i + 1) * 512] = \
                native[(CHILD_Z, ANCHOR.x * span + i, ANCHOR.y * span + j)]

    # Compare where both call it charted depth; the non-negative codes are property 2's job.
    band = (fork < 0) & (raster < 0)
    assert band.sum() > CORE * CORE // 4, f"the navigable band must dominate the fixture: {band.sum()} px"
    step = np.minimum(encode.quantization_factor(CHILD_Z),
                      2.0 ** np.ceil(np.log2(np.maximum(np.abs(raster) * encode.SHALLOW_REL,
                                                        encode.SHALLOW_MIN_STEP))))
    over = band & (raster > fork + step + 1e-6)
    assert not over.any(), \
        (f"the raster charts {int(over.sum())} px shoaler than the fork surface by more than one "
         f"quantization step (worst {float(np.max((raster - fork - step)[over])):.3f} m)")

    under = band & (raster < fork - 1e-6)
    assert not under.any(), \
        (f"the vectors chart {int(under.sum())} px shoaler than the raster inside the water domain "
         f"(worst {float(np.max((fork - raster)[under])):.3f} m)")

    # pond_fill is the licensed exception, and it leaves the water domain rather than shoaling
    # inside it: the raster still charts depth where the fork surface has risen to its ring.
    filled = (fork >= 0) & (raster < 0)
    assert filled.any(), "the enclosed pond must actually exercise smooth.pond_fill"
    rows, cols = np.nonzero(filled)
    assert (POND_BOX[0].start <= rows.min() and rows.max() < POND_BOX[0].stop
            and POND_BOX[1].start <= cols.min() and cols.max() < POND_BOX[1].stop), \
        ("the fork surface leaves the water domain outside the pond fill: rows "
         f"{rows.min()}..{rows.max()}, cols {cols.min()}..{cols.max()}")

    print(f"  one-surface ok — {int(band.sum())} water px agree within one quantization step; "
          f"{int(filled.sum())} px of pond fill are the only divergence")


def _sweep():
    """Every SWEEP_STRIDE_PX-th core pixel centre — the whole-tile sample the sweeps walk."""
    for row in range(0, CORE, SWEEP_STRIDE_PX):
        for col in range(0, CORE, SWEEP_STRIDE_PX):
            yield row, col


def check_domains(tiles, z, bands, drying, water):
    """2. The raster's class and the depare coverage describe the same seabed.

    Named feature centres first, so a failure says which structure broke; then every
    SWEEP_STRIDE_PX-th pixel of the whole tile, where the two can only part company across a class
    boundary. The budget there is one SERVED pixel: the coarse land mask rasterizes by pixel centre
    and the pyramid decimates on its own grid, while the partition edge was cut at native
    resolution, so the line each product draws can differ by the width of the pixel the raster
    reduced into — but no further, and nowhere in the interior.
    """
    from shapely.geometry import Point
    for name, (row, col) in POINTS.items():
        x, y = _xy(row, col)
        v, _ = _at(tiles, z, x, y)
        k = _klass(v)
        p = Point(x, y)
        on_band = bool(bands.intersects(p).any())
        on_drying = bool(drying.intersects(p).any())
        if name == "land":
            assert k == "land", f"z{z} {name}: raster charts {k} ({v}) where the fixture is land"
            assert not on_band and not on_drying, \
                f"z{z} {name}: the raster charts land but depare covers it (band={on_band}, drying={on_drying})"
        elif name in ("drying", "pond"):
            assert on_drying, f"z{z} {name}: the drying flat carries no depare drying"
            assert k in ("drying", "depth"), f"z{z} {name}: raster charts {k} ({v})"
        else:
            assert k == "depth", f"z{z} {name}: raster charts {k} ({v}) where the fixture is water"
            assert on_band, f"z{z} {name}: the raster charts depth but no depare band covers it"

    covered, edge, _union = water
    budget = 2 ** (CHILD_Z - z) * _res(CHILD_Z)   # one served pixel, in metres
    worst = 0.0
    strays = 0
    for row, col in _sweep():
        p = Point(*_xy(row, col))
        v, _ = _at(tiles, z, p.x, p.y)
        # A land pixel over depare water, or a water pixel with no depare feature: allowed only
        # within one served pixel of the partition edge, measured on whichever side it fell.
        if (_klass(v) == "land") == covered(p):
            gap = edge.distance(p)
            assert gap <= budget, \
                (f"z{z} row {row} col {col}: the raster charts {_klass(v)} ({v}) {gap:.1f} m inside "
                 f"the wrong side of the depare coverage, over the one-pixel budget ({budget:.1f} m)")
            worst = max(worst, gap)
            strays += 1
    print(f"  z{z} domains ok — {len(POINTS)} feature centres agree; over {CORE // SWEEP_STRIDE_PX}² "
          f"swept pixels {strays} disagree, none more than {worst / _res(CHILD_Z):.2f} native px "
          "from the partition edge")


def check_bracketing(tiles, z, bands):
    """4. The charted depth sits inside the depare bands under the served pixel's footprint.

    Guaranteed rather than incidental: the served value is a shoal-biased reduction of the same
    pixels the bands were cut from, so it can be no deeper than the deepest band under it and no
    shallower than the shallowest, up to the encode's one-step shoal bias. Swept over the whole
    tile, not sampled at feature centres — the bound is only interesting where a served pixel's
    footprint spans a depth gradient.

    Per LADDER: the metre and fathom sets are two independent partitions of the same water, so
    pooling them would widen every bracket to the union of two unrelated buckets.

    The deeper-than-the-band bound is exact at every zoom — a coarse tile is clamped against the
    shoal reduction of the zoom below (property 3), and the native zoom brackets exactly, so the
    coarse value can be no deeper than a band under its own footprint. Only the shallower-than-
    the-band bound loosens above the native zoom, by the licensed COARSE_SHOAL_ALLOWANCE_M.
    """
    from shapely.geometry import box
    from shapely.strtree import STRtree
    allow = 0.0 if z == CHILD_Z else COARSE_SHOAL_ALLOWANCE_M
    n, worst, worst_shoal = 0, 0.0, 0.0
    for sys_tag in ("m", "ft"):
        sub = bands[bands["sys"] == sys_tag]
        assert len(sub), f"the fixture must produce {sys_tag} depth areas"
        tree = STRtree(list(sub.geometry))
        drval1, drval2 = sub["drval1"].to_numpy(), sub["drval2"].to_numpy()
        for row, col in _sweep():
            x, y = _xy(row, col)
            v, footprint = _at(tiles, z, x, y)
            if v >= 0:
                continue  # flat codes are property 2's job
            under = tree.query(box(*footprint))
            assert len(under), \
                f"z{z} {sys_tag} row {row} col {col}: charted {v} m with no depare band beneath it"
            lo, hi = float(drval1[under].min()), float(drval2[under].max())
            assert -v <= hi + 1e-6, \
                (f"z{z} {sys_tag} row {row} col {col}: charted {-v:.3f} m is DEEPER than the deepest "
                 f"band under it ({hi} m) — the raster may never chart past its own depth areas")
            assert -v >= lo - _quant_step(v, z) - allow - 1e-6, \
                (f"z{z} {sys_tag} row {row} col {col}: charted {-v:.3f} m is shallower than the "
                 f"shoalest band under it ({lo} m) by more than the coarse generalization is "
                 f"licensed to shoal ({allow} m)")
            worst = max(worst, -v - hi)
            worst_shoal = max(worst_shoal, lo - _quant_step(v, z) + v)
            n += 1
    print(f"  z{z} bracketing ok — {n} charted depths lie inside their ladder's bands; worst "
          f"{worst:.2f} m deeper (budget 0) / {worst_shoal:.2f} m shoaler (budget {allow:.1f} m)")


def check_zoom_monotone(fine, coarse, z):
    """3. Zooming out must not deepen, at zero tolerance.

    The coarse tile's value against the SHOALEST water value the fine tiles hold under it — which
    is exactly the reduction terrain._FinerTiles clamps the coarse render against, and the encode
    only ever rounds shoal-ward from there, so any deepening at all means the clamp did not reach
    that tile (a finer stem missing from the render's inputs, or the cascade mis-derived).
    """
    span = 2 ** (CHILD_Z - ANCHOR.z)
    f = np.empty((CORE, CORE))
    for i in range(span):
        for j in range(span):
            f[j * 512:(j + 1) * 512, i * 512:(i + 1) * 512] = \
                fine[(CHILD_Z, ANCHOR.x * span + i, ANCHOR.y * span + j)]
    step = 2 ** (CHILD_Z - z)
    q = [f[a::step, b::step] for a in range(step) for b in range(step)]
    wet = np.logical_or.reduce([x < 0 for x in q])
    shoalest = np.maximum.reduce([np.where(x < 0, x, -np.inf) for x in q])
    c = np.empty(shoalest.shape)
    cspan = 2 ** (z - ANCHOR.z)
    for i in range(cspan):
        for j in range(cspan):
            c[j * 512:(j + 1) * 512, i * 512:(i + 1) * 512] = \
                coarse[(z, ANCHOR.x * cspan + i, ANCHOR.y * cspan + j)]
    deepened = (c < 0) & wet & (c < shoalest - 1e-6)
    worst = float(np.max((shoalest - c)[deepened])) if deepened.any() else 0.0
    assert not deepened.any(), \
        (f"z{z} charts up to {worst:.2f} m DEEPER than the shoalest water z{CHILD_Z} holds under it "
         f"over {int(deepened.sum())} px — zooming out must not deepen")
    print(f"  z{z} vs z{CHILD_Z}: 0 of {int((wet & (c < 0)).sum())} water px deepen on zoom-out")


def check_channel(tiles, z, water):
    """5. The channel through the land bar is open in both products at this zoom."""
    from shapely.geometry import LineString
    r0, r1 = CHANNEL_LINE_ROWS
    pts = [_xy(r, CHANNEL_LINE_COL - 0.5) for r in np.linspace(r0, r1, 41)]
    for (x, y), r in zip(pts, np.linspace(r0, r1, 41)):
        v, _ = _at(tiles, z, x, y)
        assert _klass(v) != "land", \
            f"z{z}: the pyramid closed the channel at row {r:.0f} (charted {v})"
    union = water[2]
    line = LineString(pts)
    assert union.covers(line), \
        (f"z{z}: the depare partition closed the channel — "
         f"{line.difference(union).length:.1f} m of the centreline has no water coverage")
    print(f"  z{z} channel ok — 41 centreline samples charted water, depare covers the whole line")


def check_isobath(tiles, contours, bands):
    """6/7. The -5 m isobath separates the served raster, and it IS the 5 m depth-area edge."""
    from shapely.geometry import Point, Polygon
    from shapely.ops import unary_union

    res = _res(CHILD_Z)
    shoal = Point(*_xy(*POINTS["shoal"]))
    basin = Point(*_xy(*POINTS["basin_south"]))
    lines = contours[(contours["sys"] == "m") & (contours["depth_m"] == CONTOUR_LEVEL)]
    assert len(lines), f"no {CONTOUR_LEVEL} m contour was drawn"

    rings = []
    for geom in lines.geometry:
        for part in (geom.geoms if geom.geom_type == "MultiLineString" else [geom]):
            c = list(part.coords)
            if len(c) > 3 and c[0] == c[-1]:
                rings.append(Polygon(c))
    around = [r for r in rings if r.contains(shoal)]
    assert len(around) == 1, f"exactly one closed {CONTOUR_LEVEL} m ring must enclose the shoal, got {len(around)}"
    ring = around[0]
    assert not ring.contains(basin), "the shoal's isobath must not enclose the basin"

    # Sample the served raster on a grid, split by the ring with a geometric margin: inside is
    # at-or-shallower than the level, outside at-or-deeper (up to one quantization step).
    inner, outer = ring.buffer(-GEOMETRY_TOL_PX * res), ring.buffer(GEOMETRY_TOL_PX * res)
    tol = _quant_step(CONTOUR_LEVEL, CHILD_Z)
    n_in = n_out = 0
    for row in range(560, 760, 4):
        for col in range(160, 360, 4):
            x, y = _xy(row, col)
            p = Point(x, y)
            v, _ = _at(tiles, CHILD_Z, x, y)
            if inner.contains(p):
                assert v >= CONTOUR_LEVEL - 1e-6, \
                    f"inside the {CONTOUR_LEVEL} m isobath the raster charts {v:.3f} m"
                n_in += 1
            elif not outer.contains(p):
                assert v <= CONTOUR_LEVEL + tol + 1e-6, \
                    f"outside the {CONTOUR_LEVEL} m isobath the raster charts {v:.3f} m"
                n_out += 1
    assert n_in > 50 and n_out > 50, f"the isobath sampling must see both sides ({n_in}/{n_out})"

    # The line and the band edge are the same geometry. Measured on a DENSIFIED ring, not its
    # vertices: both generalizations select a subset of the raw contour's own vertices, so a vertex
    # distance stays ~0 however far the chord between two of them has cut across.
    import shapely
    edge = unary_union(list(bands[bands["drval1"] >= -CONTOUR_LEVEL].geometry)).boundary
    worst = max(edge.distance(Point(c))
                for c in shapely.segmentize(ring.exterior, res / 2).coords)
    assert worst <= CONTOUR_BAND_TOL_PX * res, \
        (f"the {CONTOUR_LEVEL} m isobath parts from the depth-area edge by {worst:.2f} m "
         f"({worst / res:.2f} px), over the {CONTOUR_BAND_TOL_PX} px budget")
    print(f"  isobath ok — one ring around the shoal separates {n_in}/{n_out} raster samples; "
          f"it tracks the depth-area edge to {worst / res:.2f} px")


def main():
    import geopandas as gpd

    saved = {k: os.environ.pop(k, None) for k in ("MOSAIC_GTI", "SKIP_SMOOTH", "BBOX",
                                                  "LANDMASK", "WATERMASK")}
    cwd = os.getcwd()
    d = tempfile.mkdtemp(prefix="seascape-consistency-")
    try:
        os.chdir(d)
        _write_masks(d)
        stem = _write_mosaic(ANCHOR)

        # ── the real stages ──
        coarse = next(s for s in terrain.render_stems([stem]) if s.endswith(f"-{CHILD_Z - 1}"))
        terrain.render(stem)
        terrain.render(coarse)
        smooth.prepare_window(stem, f"store/window/{stem}.tif")
        depare_run.tile(stem)
        contour_run.tile(stem)  # derives from the depare FGB, so depare must run first

        native = _served(f"store/pmtiles/{stem}.pmtiles")
        coarse_tiles = _served(f"store/pmtiles/{coarse}.pmtiles")
        rows = gpd.read_file(f"store/depare/{stem}.fgb").to_crs("EPSG:3857")
        bands = rows[rows["drval1"].notna() & (rows["drval1"] >= 0)]
        drying = rows[rows["drval1"].notna() & (rows["drval1"] < 0)]
        contours = gpd.read_file(f"store/contour/{stem}.fgb").to_crs("EPSG:3857")
        assert len(bands) and len(drying) and len(contours), (
            f"the fixture must produce all three layers ({len(bands)} bands, "
            f"{len(drying)} drying, {len(contours)} contours)")

        # The depare water DOMAIN — bands and drying together, the ENC sense of "not land" the
        # raster's flat codes are compared against. Prepared once: the sweeps are 10^5 hit tests.
        import shapely
        from shapely.ops import unary_union
        union = unary_union(list(bands.geometry) + list(drying.geometry))
        shapely.prepare(union)
        water = (union.covers, union.boundary, union)

        check_one_surface(stem, native)
        check_zoom_monotone(native, coarse_tiles, CHILD_Z - 1)
        for z, tiles in ((CHILD_Z, native), (CHILD_Z - 1, coarse_tiles)):
            check_domains(tiles, z, bands, drying, water)
            check_bracketing(tiles, z, bands)
            check_channel(tiles, z, water)
        check_isobath(native, contours, bands)
        print(f"cross-layer consistency ok — z{CHILD_Z} and z{CHILD_Z - 1}, "
              f"{len(bands)} depth areas, {len(drying)} drying, {len(contours)} isobaths")
    finally:
        os.chdir(cwd)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    main()
