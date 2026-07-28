"""Slope- and depth-selective DEM smoothing.

Blurs flat seafloor to cut noise-driven contour stairstepping and abyssal stipple.
A depth gate escalates the blur with depth and never reduces the shallow baseline:
shallow water keeps its light, measured-precision-preserving smoothing (σ=DEM_SIGMA);
the kernel grows toward DEM_SIGMA_DEEP through the shelf; the informational deep below
the 200 m shelf break is smoothed hard. A slope gate preserves steep detail (canyon
walls, seamounts) at every depth. Navigation safety is bounded by under-keel
clearance, so the shallow band (≤ DEPTH_FULL = 30 m, the ECDIS default safety
contour) — where measured precision matters most — stays at the light baseline.
Applied to each aggregation tile's merged DEM, so the raster encode and the contour
fork share one smoothed surface. Processed in overlapping windows (halo = the gaussian truncation radius),
so peak memory is one padded block, not the whole raster — a z14 macrotile is
32768px ≈ 4 GB/band, which a whole-array read would OOM.

Sigma is in merged-DEM pixels, so the physical blur scale tracks the tile's zoom
(coarse base tiles blur more in metres, fine regional tiles less) — roughly what we
want (coarse data is noisier). Revisit with a physical-scale sigma if it
over/under-blurs. SKIP_SMOOTH=1 disables it.
"""

import glob
import os

import numpy as np
import rasterio
from rasterio.windows import Window
from scipy.ndimage import gaussian_filter

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

# Deep-water DEM coarsening: below the threshold, band edges carry no navigational detail, but
# source noise at fine child_z resolution makes gdal_contour's rings astronomically complex (the
# deep-basin z14 stems burned 8.5 h+ of pure CPU; docs/plans/2026-07-21-depare-perf.md). Pixels
# whose 8x-block mean is deeper than the threshold take that mean, so the deep interior contours at
# ~8x resolution while everything shoaler keeps full detail. -250 m sits between ladder levels (m:
# -200/-300; ft: -182.88/-365.76), so no level rides the transition. Both the depth-area bands and
# the contour lines coarsen off this one surface so deep isobaths track the band edges. Only fine
# windows (child_z >= 12) coarsen — coarse windows are already at or past 8x.
DEEP_COARSEN_THRESHOLD_M = -250.0
DEEP_COARSEN_FACTOR = 8
DEEP_COARSEN_MIN_CHILD_Z = 12
MERC_ORIGIN = 20037508.342789244  # EPSG:3857 half-extent; block grid anchors here (seam alignment)


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


def deep_coarsen(dem, threshold=DEEP_COARSEN_THRESHOLD_M, factor=DEEP_COARSEN_FACTOR):
    """Rewrite `dem` in place with deep pixels replaced by their block mean. The block grid is
    anchored to the EPSG:3857 origin, NOT the window, so overlapping windows (neighbor stems'
    buffers) average identical blocks and band edges still match at macrotile seams. A pixel is
    replaced only when it AND its block mean are below the threshold: shelf-edge blocks whose
    mean dips deep keep their shallow pixels, and a deep canyon inside a shallow block stays
    fine rather than taking a shallow mean. Nodata pixels are never touched (the nodata pass
    reads this same raster), and nodata is excluded from block means."""
    with rasterio.open(dem, "r+") as d:
        nd = d.nodata
        res = abs(d.transform.a)
        h, w = d.height, d.width
        # Global block alignment: pad to the origin-anchored grid (pads land in the buffer).
        col0 = round((d.transform.c + MERC_ORIGIN) / res)
        row0 = round((MERC_ORIGIN - d.transform.f) / res)
        padt, padl = row0 % factor, col0 % factor
        padr = (-(w + padl)) % factor
        # Strip-streamed on block-row boundaries: a z15 window is 17 GB as one array, and
        # blocks never straddle an aligned strip, so per-strip means equal the whole-array
        # ones. Vertical edge pads repeat the strip's own edge row — the same rows the
        # full-array pad used.
        step = max(factor, (2048 // factor) * factor)
        y = -padt
        while y < h:
            y0, y1 = max(0, y), min(h, y + step)
            arr = d.read(1, window=Window(0, y0, w, y1 - y0))
            spt = y0 - y                    # top pad only on the first (origin-cut) strip
            spb = (-(arr.shape[0] + spt)) % factor
            p = np.pad(arr, ((spt, spb), (padl, padr)), mode="edge")
            valid = p != nd
            ph, pw = p.shape
            vals = np.where(valid, p, 0.0)
            bsum = vals.reshape(ph // factor, factor, pw // factor, factor).sum(axis=(1, 3))
            bcnt = valid.reshape(ph // factor, factor, pw // factor, factor).sum(axis=(1, 3))
            del p, vals, valid
            bmean = (bsum / np.maximum(bcnt, 1)).astype(np.float32)
            bmean[bcnt == 0] = nd
            up = np.repeat(np.repeat(bmean, factor, 0), factor, 1)[spt:spt + arr.shape[0],
                                                                   padl:padl + w]
            mask = (arr != nd) & (arr <= threshold) & (up != nd) & (up <= threshold)
            arr[mask] = up[mask]
            d.write(arr, 1, window=Window(0, y0, w, y1 - y0))
            y += step


def _check():
    """Deep flat smooths harder than shallow; shallow stays denoised; a steep step is preserved."""
    rng = np.random.default_rng(0)
    noise = rng.normal(0, 5, (256, 256)).astype("float32")
    shallow_in = (-10 + noise).astype("float32")    # ≤30 m → light baseline blur
    deep_in = (-4000 + noise).astype("float32")     # >200 m → heavy blur
    s_out = smooth_array(shallow_in, res=300.0)
    d_out = smooth_array(deep_in, res=300.0)
    assert s_out.std() < shallow_in.std(), (s_out.std(), shallow_in.std())  # shallow still denoised, not turned off
    assert d_out.std() < s_out.std(), (d_out.std(), s_out.std())            # deep smoothed harder than shallow

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

    # deep_coarsen: shallow and nodata pixels preserved; deep pixels take their block mean;
    # and the block grid is origin-anchored, so two windows offset against the block grid
    # produce identical values where they overlap (the macrotile-seam contract).
    def _write_dem(path, arr, left, top, res_m):
        with rasterio.open(path, "w", driver="GTiff", width=arr.shape[1], height=arr.shape[0],
                           count=1, dtype="float32", nodata=-9999.0,
                           transform=from_origin(left, top, res_m, res_m)) as dst:
            dst.write(arr.astype(np.float32), 1)

    f = DEEP_COARSEN_FACTOR
    big = (-400.0 + rng.uniform(-40, 40, (8 * f, 8 * f))).astype(np.float32)  # noisy deep field
    big[:f, :] = -10.0            # one shallow block-row: must stay untouched
    big[f, 0] = -9999.0           # a nodata pixel inside a deep block: preserved + excluded
    left0, top0 = -MERC_ORIGIN, MERC_ORIGIN  # window A: block-aligned at the origin
    _write_dem(f"{d}/a.tiff", big, left0, top0, 10.0)
    deep_coarsen(f"{d}/a.tiff", factor=f)
    with rasterio.open(f"{d}/a.tiff") as src:
        a = src.read(1)
    assert np.array_equal(a[:f, :], big[:f, :]), "shallow pixels must be untouched"
    assert a[f, 0] == -9999.0, "nodata must be preserved"
    blk = big[f:2 * f, :f]
    want = blk[blk != -9999.0].mean()
    got = a[2 * f - 1, f - 1]  # a deep pixel of that block (away from the nodata cell)
    assert abs(got - want) < 1e-3, f"deep block mean: {got} != {want}"
    # Window B: the same field cropped at a NON-block-aligned offset (3 px right/down). Values
    # in the overlap must match window A exactly — the origin-anchored grid, not the window,
    # defines the blocks. Interior only: A's edge blocks average pad-replicated pixels.
    off = 3
    _write_dem(f"{d}/b.tiff", big[off:, off:], left0 + off * 10.0, top0 - off * 10.0, 10.0)
    deep_coarsen(f"{d}/b.tiff", factor=f)
    with rasterio.open(f"{d}/b.tiff") as src:
        b = src.read(1)
    assert np.array_equal(a[f:7 * f, f:7 * f], b[f - off:7 * f - off, f - off:7 * f - off]), \
        "overlapping windows must coarsen identically (seam alignment)"

    print("smooth.py self-check ok")


def prepare_window(stem, out_tif):
    """The forks' shared read surface: the stem's buffered window materialized once,
    smoothed, and deep-coarsened. Consumers must treat it as read-only — contour and
    soundings clamp a private copy; depare reads it directly."""
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
        if child_z >= DEEP_COARSEN_MIN_CHILD_Z:
            deep_coarsen(tmp)
    with open(tmp, "rb") as f:
        os.fsync(f.fileno())  # teardown is a power cut; a rename must not outlive its data
    os.replace(tmp, out_tif)
    print(f"fork window {stem}: {out_tif}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "prepare-window":
        prepare_window(sys.argv[2], sys.argv[3])
    else:
        _check()
