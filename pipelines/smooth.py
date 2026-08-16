"""Slope- and depth-selective DEM smoothing.

Blurs flat seafloor to cut noise-driven contour stairstepping and abyssal stipple.
A depth gate escalates the blur with depth and never reduces the shallow baseline:
shallow water keeps its light, measured-precision-preserving smoothing (σ=DEM_SIGMA);
the kernel grows toward DEM_SIGMA_DEEP through the shelf; the informational deep below
the 200 m shelf break is smoothed hard. A slope gate preserves steep detail (canyon
walls, seamounts) at every depth. Navigation safety is bounded by under-keel
clearance, so the shallow band (≤ DEPTH_FULL = 30 m, the ECDIS default safety
contour) — where measured precision matters most — stays at the light baseline,
and the blur is clamped shoal-ward so no pixel ever reads deeper than its source.
Applied to each aggregation tile's merged DEM, so the raster encode and the contour
fork share one smoothed surface. Processed in overlapping windows (halo = the gaussian truncation radius),
so peak memory is one padded block, not the whole raster — a z14 macrotile is
32768px ≈ 4 GB/band, which a whole-array read would OOM.

Sigma is in merged-DEM pixels, so the physical blur scale tracks the tile's zoom
(coarse base tiles blur more in metres, fine regional tiles less) — roughly what we
want (coarse data is noisier). Revisit with a physical-scale sigma if it
over/under-blurs — but not for cross-zoom agreement: measured, a metre-scale sigma
WIDENS the served pyramid's zoom-to-zoom disagreement instead of closing it, because
the class-aware reduction doubles narrow water's physical width at every level, so an
identical kernel attenuates the coarse copy less. terrain._FinerTiles carries that
invariant instead. SKIP_SMOOTH=1 disables it.
"""

import glob
import os

import numpy as np
import rasterio
from rasterio.windows import Window
from scipy.ndimage import binary_dilation, find_objects, gaussian_filter, label

NODATA = -9999

DEM_SIGMA = float(os.environ.get("SMOOTH_DEM_SIGMA", "4"))            # shallow baseline blur (px); unchanged from original
DEM_SIGMA_DEEP = float(os.environ.get("SMOOTH_DEM_SIGMA_DEEP", "16")) # deep blur (px); main dial — bigger = flatter deep
MASK_SIGMA = float(os.environ.get("SMOOTH_MASK_SIGMA", "4"))
SLOPE_LOW = float(os.environ.get("SMOOTH_SLOPE_LOW", "1"))    # ≤ this slope (deg): fully blurred
SLOPE_HIGH = float(os.environ.get("SMOOTH_SLOPE_HIGH", "5"))  # ≥ this slope: original kept
DEPTH_FULL = float(os.environ.get("SMOOTH_DEPTH_FULL", "30"))     # ≤ this depth (m): light baseline only (ECDIS safety contour)
DEPTH_SMOOTH = float(os.environ.get("SMOOTH_DEPTH_SMOOTH", "200")) # ≥ this depth (m): full heavy blur (shelf break)
BLOCK = int(os.environ.get("SMOOTH_BLOCK", "2048"))          # window side (px); caps peak memory
TRUNCATE = 4.0                                               # gaussian_filter default kernel cutoff (σ)

MERC_ORIGIN = 20037508.342789244  # EPSG:3857 half-extent

# Enclosed sub-legible water is filled to the shoalest elevation around it. A marsh pond the chart
# cannot draw at compilation scale holds an outline and no depth (interior Barataria water: 14
# unique values, p50 = p95 = 0.10 m) while each one costs its own ring in the shoalest bands — 81%
# of a marsh crop's parts sit under 4 mm². What may be filled is decided topologically, enclosed vs
# connected, because no value-based operator tells a pond from a channel: filling an enclosed pond
# is licensed practice (USGS NHD breaks marsh for clearings >= 0.05 in; S-57 UOC 4.7.3 puts
# QUAPOS = 4 on the marsh coastline) while closing a channel is forbidden. 16 mm² measured 11.6x
# fewer parts for 1.4% water loss with the channel network intact (2026-07-30-shallow-coarsening).
# Inland water the DEM holds no depth for is untouched by any of this: it ships through depare's
# nodata layer, the OSM water polygons minus the DEM's water coverage, which this operator never
# reads or writes.
POND_FILL_MM2 = float(os.environ.get("SMOOTH_POND_FILL_MM2", "16"))  # 0 disables
# Bounding-box diagonal cap, EPSG:3857 metres, doing two jobs. It excludes what the area gate
# cannot see — a 1-px-wide 900 m channel fragment is sub-legible by area and must never be filled —
# and it is the seam guarantee: a candidate plus its 1-px ring fits inside the halo both
# neighbouring windows share, so adjacent stems see it whole and classify it identically. In
# metres, not pixels, so a stem's child_z cannot inflate what counts as compact.
POND_FILL_EXTENT_M = float(os.environ.get("SMOOTH_POND_FILL_EXTENT_M", "75"))
# The determinacy argument only ever held for the 0-2 m band, where a marsh pond measures p50 = p95
# = 0.10 m. An enclosed basin genuinely surveyed deeper than this — a marina with a pinched
# entrance, a quarry lake inside a lidar tile — carries real navigational information, so a
# component is spared as soon as its DEEPEST pixel reaches this depth.
POND_FILL_MAX_DEPTH_M = float(os.environ.get("SMOOTH_POND_FILL_MAX_DEPTH_M", "2.0"))
# The part explosion is a native-resolution pathology, and mm-at-scale areas inflate to km² over a
# coarse stem, which would widen the fill set into true-lake territory for no cost benefit.
POND_FILL_MIN_CHILD_Z = 14
MM_PER_PX = 0.28  # a rendering pixel at compilation scale, as depare_run.MM_PER_PX


def halo_px():
    """The gaussian truncation radius (TRUNCATE·σ, +1 for the gradient) in pixels — the window
    halo the block-wise smooth, terrain's window_tiles, and the stage-3 read buffer all size from."""
    return int(np.ceil(TRUNCATE * max(DEM_SIGMA, DEM_SIGMA_DEEP, MASK_SIGMA))) + 1


def smooth_array(dem, res, nodata=NODATA):
    valid = dem != nodata
    water = valid & (dem < 0)
    # Clamp land/nodata to 0 so they don't drag the blur of nearby ocean pixels.
    work = np.where(water, dem, 0.0).astype("float32")
    # Two blur scales: a light one (the shallow baseline, unchanged from the original
    # slope-gated smooth) and a heavy one ramped in with depth, so the informational
    # deep flattens while the shallows keep their measured precision.
    blur_light = gaussian_filter(work, sigma=DEM_SIGMA, mode="nearest")
    blur_heavy = gaussian_filter(work, sigma=DEM_SIGMA_DEEP, mode="nearest")
    # Slope (degrees) from the clamped surface, accounting for pixel size.
    gy, gx = np.gradient(work, res)
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))
    # flat weight: 1 where flat (→ blurred), 0 where steep (→ original); feathered.
    flat_w = 1.0 - np.clip((slope - SLOPE_LOW) / (SLOPE_HIGH - SLOPE_LOW), 0.0, 1.0)
    flat_w = gaussian_filter(flat_w, sigma=MASK_SIGMA, mode="nearest")
    # depth weight: 0 in the navigable shallows (light baseline only), ramping to 1
    # below the 200 m shelf break (full heavy blur). Never reduces shallow smoothing.
    depth = np.where(water, -dem, 0.0)  # metres, positive down
    depth_w = np.clip((depth - DEPTH_FULL) / (DEPTH_SMOOTH - DEPTH_FULL), 0.0, 1.0)
    blurred = blur_light * (1.0 - depth_w) + blur_heavy * depth_w
    out = dem * (1.0 - flat_w) + blurred * flat_w
    # A symmetric kernel deepens the shoal side of every slope, and a chart may never read deeper
    # than its source (IHO S-4 B-411.5), so the blur is allowed to shoal only.
    out = np.maximum(out, dem)
    return np.where(water, out, dem).astype("float32")  # land + nodata untouched


def smooth_tiff(path, block=None):
    """Smooth a DEM in overlapping windows so peak memory is one padded block, not the
    whole raster. The halo (gaussian truncation radius = TRUNCATE·σ, +1 for the gradient)
    feeds each block real neighbours, so interior output is identical to a whole-array
    smooth; only the true raster edge falls back to mode='nearest', exactly as before."""
    block = block or BLOCK
    halo = halo_px()
    with rasterio.open(path) as src:
        profile = src.profile
        res = src.res[0]
        nodata = src.nodata if src.nodata is not None else NODATA
        h_total, w_total = src.height, src.width
    # Re-write as a 512-blocked GTiff (aggregation_tile asserts 512 block shapes).
    # Predictor by dtype (3=float, 2=int, else 1): the merged DEM is Float32 over regional
    # sources but Int16 where GEBCO dominates, and PREDICTOR=3 is float-only.
    dt = profile["dtype"]
    predictor = 3 if dt in ("float32", "float64") else 2 if "int" in dt else 1
    # BIGTIFF: a z15 window compresses under 4 GB, but the later in-place land clamp
    # appends rewritten blocks past the classic-TIFF offset limit; IF_SAFER sizes by
    # the uncompressed estimate so the clamp always has room.
    profile.update(driver="GTiff", count=1, tiled=True, blockxsize=512, blockysize=512,
                   compress="zstd", predictor=predictor, num_threads="all_cpus",
                   BIGTIFF="IF_SAFER")
    tmp = path + ".smooth.tif"
    with rasterio.open(path) as src, rasterio.open(tmp, "w", **profile) as dst:
        for row in range(0, h_total, block):
            for col in range(0, w_total, block):
                h = min(block, h_total - row)
                w = min(block, w_total - col)
                r0, c0 = max(0, row - halo), max(0, col - halo)
                r1, c1 = min(h_total, row + h + halo), min(w_total, col + w + halo)
                dem = src.read(1, window=Window(c0, r0, c1 - c0, r1 - r0))
                out = smooth_array(dem, res, nodata)
                dst.write(out[row - r0:row - r0 + h, col - c0:col - c0 + w], 1,
                          window=Window(col, row, w, h))
    os.replace(tmp, path)


def smooth_merged(tmp_folder):
    """Smooth the merged DEM of one aggregation tile in place."""
    n = len(glob.glob(f"{tmp_folder}/*.tiff"))
    smooth_tiff(f"{tmp_folder}/{n - 1}-3857.tiff")


def _pond_fill_array(arr, nd, max_area_px, max_diag_px, max_depth_m=POND_FILL_MAX_DEPTH_M):
    """Fill enclosed sub-legible water in `arr` in place; returns the number of components filled.

    A component qualifies only if it is enclosed — it does not touch the array edge, which is where
    the water network leaves — and passes every gate. It is filled to the maximum over its own 1-px
    ring, which is monotone-shoaling by construction: labelling is 8-connected, so no ring pixel can
    be water, and every filled pixel therefore rises from negative to at least 0. 8-connectivity is
    also what protects a diagonal thread of water; under 4-connectivity each link of one would be
    its own sub-legible pond and the channel would be erased a pixel at a time."""
    water = (arr != nd) & (arr < 0)
    if not water.any():
        return 0
    lab, n = label(water, structure=np.ones((3, 3), bool))
    if n == 0:
        return 0
    sizes = np.bincount(lab.ravel(), minlength=n + 1)
    connected = np.zeros(n + 1, bool)
    connected[np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))] = True
    connected[0] = True
    filled = 0
    for i, sl in enumerate(find_objects(lab), start=1):
        if connected[i] or sl is None or sizes[i] > max_area_px:
            continue
        h, w = sl[0].stop - sl[0].start, sl[1].stop - sl[1].start
        if h * h + w * w > max_diag_px * max_diag_px:
            continue
        # The ring is in bounds because the component does not touch the array edge.
        sub = arr[sl[0].start - 1:sl[0].stop + 1, sl[1].start - 1:sl[1].stop + 1]
        m = lab[sl[0].start - 1:sl[0].stop + 1, sl[1].start - 1:sl[1].stop + 1] == i
        if sub[m].min() < -max_depth_m:  # a surveyed basin, not an outline without a depth
            continue
        ring = sub[binary_dilation(m, np.ones((3, 3), bool)) & ~m]
        if (ring == nd).any():  # unsurveyed neighbours cannot establish enclosure
            continue
        sub[m] = ring.max()
        filled += 1
    return filled


def pond_fill(dem, child_z=POND_FILL_MIN_CHILD_Z, mm2=None, extent_m=None, block=None):
    """Rewrite `dem` in place with enclosed sub-legible water filled, in overlapping blocks so peak
    memory is one padded block rather than the whole window — a z15 window would want 17 GB for the
    label array alone.

    No union-find merge table is needed, because the extent gate bounds every candidate: a
    component that reaches a block's read edge while touching its core spans more than the halo, so
    it is already too big to fill, and one that qualifies is therefore whole inside the read with
    its ring. Excluding every component that touches the read edge is thus exactly the whole-array
    rule (asserted in _check). The pass is in place and blocks read halos their neighbours may
    already have written, which changes nothing: a filled pond leaves the water set entirely and
    carries the same ring maximum its remainder would compute, and two distinct 8-connected
    components are never in each other's rings."""
    mm2 = POND_FILL_MM2 if mm2 is None else mm2
    if mm2 <= 0 or child_z < POND_FILL_MIN_CHILD_Z:
        return 0
    extent_m = POND_FILL_EXTENT_M if extent_m is None else extent_m
    block = block or BLOCK
    with rasterio.open(dem, "r+") as d:
        nd = d.nodata
        res = abs(d.transform.a)
        h_total, w_total = d.height, d.width
        max_area_px = mm2 / (MM_PER_PX ** 2)  # a pixel IS MM_PER_PX at scale, so this is zoom-free
        # Never wider than the window halo neighbouring stems share: the seam contract outranks
        # the dial, and one pixel is left over for the ring.
        max_diag_px = min(extent_m / res, halo_px() - 1)
        halo = int(np.ceil(max_diag_px)) + 1
        total = 0
        for row in range(0, h_total, block):
            for col in range(0, w_total, block):
                h = min(block, h_total - row)
                w = min(block, w_total - col)
                r0, c0 = max(0, row - halo), max(0, col - halo)
                r1, c1 = min(h_total, row + h + halo), min(w_total, col + w + halo)
                arr = d.read(1, window=Window(c0, r0, c1 - c0, r1 - r0))
                filled = _pond_fill_array(arr, nd, max_area_px, max_diag_px)
                if filled:
                    d.write(arr[row - r0:row - r0 + h, col - c0:col - c0 + w], 1,
                            window=Window(col, row, w, h))
                    total += filled
        return total


def _check():
    """Deep flat smooths harder than shallow; shallow stays denoised; a steep step is preserved;
    no pixel comes back deeper than it went in."""
    rng = np.random.default_rng(0)
    noise = rng.normal(0, 5, (256, 256)).astype("float32")
    shallow_in = (-10 + noise).astype("float32")    # ≤30 m → light baseline blur
    deep_in = (-4000 + noise).astype("float32")     # >200 m → heavy blur
    s_out = smooth_array(shallow_in, res=300.0)
    d_out = smooth_array(deep_in, res=300.0)
    assert s_out.std() < shallow_in.std(), (s_out.std(), shallow_in.std())  # shallow still denoised, not turned off
    assert d_out.std() < s_out.std(), (d_out.std(), s_out.std())            # deep smoothed harder than shallow
    for a, b in ((shallow_in, s_out), (deep_in, d_out)):
        assert (b >= a).all(), float(np.max(a - b))  # IHO S-4 B-411.5: never deeper than the source

    step = np.where(np.arange(256)[None, :] < 128, -10.0, -2000.0).astype("float32")
    step = np.broadcast_to(step, (256, 256)).copy()
    out = smooth_array(step, res=10.0)  # 1990 m over 10 m = near-vertical → steep, kept
    assert abs(out[:, 0].mean() - (-10)) < 1 and abs(out[:, -1].mean() - (-2000)) < 1, \
        (out[:, 0].mean(), out[:, -1].mean())

    # windowed smooth_tiff must equal the whole-array smooth (halo correctness across seams)
    import tempfile
    from rasterio.transform import from_origin
    big = (-3000 + rng.normal(0, 8, (600, 600))).astype("float32")
    big[:, 300:] -= 1500  # a steep seam to stress slope+blur across many block edges
    d = tempfile.mkdtemp()
    p = f"{d}/m.tif"
    with rasterio.open(p, "w", driver="GTiff", height=600, width=600, count=1,
                       dtype="float32", nodata=NODATA, crs="EPSG:3857",
                       transform=from_origin(0, 6000, 10, 10)) as dst:
        dst.write(big, 1)
    ref = smooth_array(big, 10.0)
    smooth_tiff(p, block=128)  # tiny blocks → exercises many internal seams
    with rasterio.open(p) as src:
        got = src.read(1)
    assert np.max(np.abs(got - ref)) < 1e-2, np.max(np.abs(got - ref))

    # Int16 path (GEBCO is Int16): smooth_tiff must pick predictor=2, not the float-only 3.
    i16 = f"{d}/i16.tif"
    arr16 = big.clip(-32000, 0).astype("int16")
    with rasterio.open(i16, "w", driver="GTiff", height=600, width=600, count=1,
                       dtype="int16", nodata=-32768, crs="EPSG:3857",
                       transform=from_origin(0, 6000, 10, 10)) as dst:
        dst.write(arr16, 1)
    smooth_tiff(i16, block=128)  # must not raise on Int16 (the predictor=3 regression)
    with rasterio.open(i16) as src:
        assert src.read(1).std() < arr16.std(), "Int16 smoothing should denoise"

    def _write_dem(path, arr, left, top, res_m):
        with rasterio.open(path, "w", driver="GTiff", width=arr.shape[1], height=arr.shape[0],
                           count=1, dtype="float32", nodata=-9999.0,
                           transform=from_origin(left, top, res_m, res_m)) as dst:
            dst.write(arr.astype(np.float32), 1)

    # Deep water carries the heavy blur and nothing else: it comes back smoothed but NOT
    # quantized onto any lattice, so a deep field keeps a value per pixel rather than a value
    # per block. Distinct neighbours in the deep are what a piecewise-constant surface loses.
    deep = (-400.0 + rng.uniform(-40, 40, (128, 128))).astype(np.float32)
    _write_dem(f"{d}/deep.tiff", deep, -MERC_ORIGIN, MERC_ORIGIN, 10.0)
    smooth_tiff(f"{d}/deep.tiff", block=64)
    with rasterio.open(f"{d}/deep.tiff") as src:
        sm = src.read(1)
    assert sm.std() < deep.std(), "the deep blur must denoise"
    interior = sm[8:-8, 8:-8]
    assert (interior[:, :-1] != interior[:, 1:]).mean() > 0.9, \
        "deep pixels must stay per-pixel, not collapse onto blocks of equal value"

    # pond fill: an enclosed sub-legible pond takes its shoalest surrounding value, while the
    # channel, a thin long fragment, an over-area pond, a diagonal thread and a pond against
    # nodata all survive — and the blocked pass equals the whole-array reference on components
    # that straddle block boundaries.
    res = 2.3886571  # cz15 EPSG:3857 m/px: the gates land on 204 px of area, 31.4 px of diagonal
    pond = np.full((200, 200), 1.0, np.float32)
    pond[:, 100:103] = -3.0        # a channel spanning the array: reaches the edge, connected
    pond[60:66, 60:66] = -1.0      # enclosed, 36 px, 8.5 px diagonal: fills
    pond[59, 62] = 2.0             # the shoalest ring pixel, so the value the fill must take
    pond[30, 10:80] = -0.5         # 70 px of area but 70 px of extent: the extent gate keeps it
    pond[62:68, 126:132] = -1.0    # straddles a block boundary once blocked
    pond[150:170, 150:170] = -1.0  # 400 px: over the area gate
    pond[80:84, 40:44] = -3.0      # small and enclosed, but surveyed past the depth ceiling
    for i in range(40):
        pond[20 + i, 150 + i] = -1.0  # a diagonal thread: ONE 8-connected component, too long
    pond[95, 150] = NODATA
    pond[96:99, 149:152] = -1.0    # enclosure is not established against unsurveyed neighbours

    ref = pond.copy()
    n = _pond_fill_array(ref, NODATA, POND_FILL_MM2 / MM_PER_PX ** 2, POND_FILL_EXTENT_M / res)
    assert (ref >= pond).all(), "pond fill must never deepen"
    assert (ref[60:66, 60:66] == 2.0).all(), ref[60:66, 60:66]
    assert (ref[:, 100:103] == -3.0).all(), "the channel must survive"
    assert (ref[30, 10:80] == -0.5).all(), "a thin long fragment must survive the area gate"
    assert (ref[150:170, 150:170] == -1.0).all(), "an over-area pond must survive"
    assert ref[59, 189] == -1.0, "a diagonal thread is one component and too long to fill"
    assert (ref[96:99, 149:152] == -1.0).all(), "a pond against nodata must survive"
    assert (ref[80:84, 40:44] == -3.0).all(), "a pond deeper than the ceiling must survive"
    assert n == 2, f"expected the two enclosed sub-legible ponds, filled {n}"

    pf = f"{d}/pond.tif"
    _write_dem(pf, pond, -MERC_ORIGIN, MERC_ORIGIN, res)
    pond_fill(pf, block=64)  # 64-px cores force candidates across block boundaries
    with rasterio.open(pf) as src:
        assert np.array_equal(src.read(1), ref), "blocked pond fill must equal the whole array"
    for kw in ({"mm2": 0}, {"child_z": POND_FILL_MIN_CHILD_Z - 1}):
        _write_dem(pf, pond, -MERC_ORIGIN, MERC_ORIGIN, res)
        assert pond_fill(pf, block=64, **kw) == 0, kw
        with rasterio.open(pf) as src:
            assert np.array_equal(src.read(1), pond), f"{kw} must disable the pass"

    print("smooth.py self-check ok")


def prepare_window(stem, out_tif):
    """The forks' shared read surface: the stem's buffered window materialized once,
    smoothed and pond-filled. Consumers must treat it as read-only — contour and
    soundings clamp a private copy; depare reads it directly. Every generalization lives here so
    bands, contour lines and soundings stay coincident."""
    import mosaic
    child_z = int(stem.split("-")[3])
    os.makedirs(os.path.dirname(out_tif), exist_ok=True)
    tmp = out_tif + ".tmp.tif"  # keep the extension: gdal_translate infers the driver from it
    mosaic.window_dem(stem, tmp)
    # Cap the in-process GDAL block cache: its default is 5% of RAM, so the strip loops'
    # read+dirty blocks otherwise accumulate until RSS ~= the window size.
    with rasterio.env.Env(GDAL_CACHEMAX=256):
        if not os.environ.get("SKIP_SMOOTH"):
            smooth_tiff(tmp)
        ponds = pond_fill(tmp, child_z)
    with open(tmp, "rb") as f:
        os.fsync(f.fileno())  # teardown is a power cut; a rename must not outlive its data
    os.replace(tmp, out_tif)
    print(f"fork window {stem}: {out_tif} ({ponds} ponds filled)")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "prepare-window":
        prepare_window(sys.argv[2], sys.argv[3])
    else:
        _check()
