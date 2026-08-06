"""Reproject each source group of one aggregation tile to EPSG:3857.

Vendored from mapterhorn (BSD-3). For each (source, maxzoom) group, most-important
first: build a VRT, warp to 3857 at the group's maxzoom resolution with cubicspline
and dstnodata -9999, then short-circuit once the accumulated result has no nodata
(highest-res source wins; lower ones only fill gaps). A halo buffer is always added
so contour lines stay continuous across tile seams; the raster output crops it back.

Internal paths only (store/aggregation tmp + source filenames from the covering CSVs, which
the catalog's seascape:files feeds); shells out via utils.run_command.
"""

import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from xml.sax.saxutils import unescape

import mercantile
import numpy as np
import rasterio

import config
import landmask
import utils

# Streaming sources (CUDEM off NOAA, the locally-prepared sources off public R2) are
# all read via /vsicurl over public HTTPS — no credentials in the read path, so no
# AWS env to set here. config.source_path resolves each filename to its /vsicurl URL.

SILENT = True
NODATA = -9999

# Bathymetry note: cubicspline can ring near steep escarpments; set
# AGG_RESAMPLE=bilinear to switch if that shows.
RESAMPLE = os.environ.get("AGG_RESAMPLE", "cubicspline")

# What the mixed-CRS chain costs is one /vsicurl open per input per pass — measured at ~1.15 s
# each against ~0.09 s of CPU, and an S-102 z14 tile carries 131 of them. So the per-input warps
# fan out; the threads sit on sockets, not cores.
MIXED_WORKERS = int(os.environ.get("AGG_MIXED_WORKERS", "8"))
# gdalbuildvrt opens its inputs one at a time in a single process, so a flat combine of that many
# warped VRTs is that many serial round trips (measured 104 s vs 15 s chunked). Chunking overlaps
# them; contiguous slices keep "last input wins" on an overlap, which is the precedence the flat
# combine has always given.
COMBINE_FANOUT = 16

# Coverage-edge feathering. A group whose native grid is >= FEATHER_MIN_LEVELS zoom levels
# coarser than the render grid draws its valid/nodata boundary as source-cell staircases —
# a 30 m S-102 cell is a 13-px block on a z15 grid, and land in S-102 is simply nodata, so
# the shoreline itself staircases. Values interpolate smoothly (cubicspline) but validity
# cannot: gdalwarp keeps the source's cell-quantized mask. The fix smooths the 0/1 mask
# with a gaussian at the source-cell scale and re-thresholds at 0.5; the new boundary
# is the level-set midline through the staircase corners, within half a source cell of the
# original everywhere. Newly-covered fringe pixels copy their nearest valid neighbour
# (the fringe is the shallowest edge, so extending it errs bias-shallow). Trimming a step
# corner only re-draws a source cell's edge — the survey stays represented by the rest of
# the cell.
#
# Coverage may never be disconnected, though: a creek narrower than a source cell, or a
# short neck between two basins, is a passage, and erasing it would chart a closed channel.
# So coverage first GROWS to the blur's level set (growth can only join), then the pixels
# the blur wants gone are peeled off one topology-preserving layer at a time — a pixel is
# removed only if the coverage around it is a single arc, which is false exactly at a neck.
# Every pair of pixels connected before is still connected after, whatever the shape, so a
# sub-cell passage survives as a thread rather than being erased. What this does NOT promise
# is preserving the AREA of a feature smaller than the smoothing scale: a dead-end stub
# narrower than a source cell erodes back toward what it hangs off. That direction is the
# safe one — it charts as land or falls through to the next source, rather than inventing
# navigable water — and it is the same call the pond fill in smooth.py already makes.
FEATHER_MIN_LEVELS = int(os.environ.get("AGG_FEATHER_MIN_LEVELS", "2"))
# Ceiling on the smoothing scale, in render pixels per source cell. The boundary can move
# by up to half a source cell, so an unbounded factor would let a very coarse group (GEBCO
# under a z15 tile is 7 levels down) redraw a coastline by hundreds of metres. Such groups
# only ever fill gaps under a finer source anyway, and a global one has no coverage edge
# to feather at all.
FEATHER_MAX_FACTOR = 32

# The VRT's input references and its own extent, read by regex rather than an XML parser (the
# same XXE-avoidance source_enumerate makes for bucket listings).
_SOURCE_FILENAME_RE = re.compile(r"<SourceFilename[^>]*>([^<]*)</SourceFilename>")
_GEOTRANSFORM_RE = re.compile(r"<GeoTransform>([^<]*)</GeoTransform>")
_RASTER_SIZE_RE = re.compile(r'rasterXSize="(\d+)"\s+rasterYSize="(\d+)"')


def negate_band1(filepath):
    """Flip band-1 sign on valid pixels (depth +down -> elevation -down), leaving invalid
    pixels and the alpha band untouched. Streamed sources skip source_datum, so a
    positive-down source (S-102 stores depth) is converted here, right after warp. The file
    is a COG (translate -of COG), so the in-place band-1 update needs IGNORE_COG_LAYOUT_BREAK
    — it's a transient the merge re-reads then deletes, so breaking the COG layout costs
    nothing. read_masks honours alpha-or-nodata validity (don't compare to a NODATA value:
    ADD_ALPHA moves the mask off the data band). Drop this when the pipeline goes
    depth-canonical internally."""
    # Per-block, not full-band: a whole z14 band is ~4.3 GB and its masks another ~2 GB —
    # measured as the job's RSS peak (glibc retains the high-water for the process's life).
    with rasterio.open(filepath, "r+", IGNORE_COG_LAYOUT_BREAK="YES") as ds:
        for _, window in ds.block_windows(1):
            mask = ds.read_masks(1, window=window) != 0
            if mask.any():
                a = ds.read(1, window=window)
                a[mask] = -a[mask]
                ds.write(a, 1, window=window)


def _peel_preserving_topology(start, removable, max_iters):
    """Remove `removable` pixels from `start`, one topology-preserving layer at a time.

    A pixel may go only when its 8-neighbourhood crossing number — the number of
    background-to-coverage transitions around it — is exactly 1, i.e. the coverage touching
    it forms a single arc. A neck pixel with water on two sides scores 2 and stays; an
    isolated pixel and an interior pixel both score 0 and stay, so a component is never
    deleted and a hole is never punched. Removals are taken in four subfields so two pixels
    dropped in the same pass are never even diagonally adjacent and cannot interact.

    Only candidate pixels are visited (a band a few px wide along the boundary), not the
    whole window, which is what keeps this affordable at tile scale."""
    cur = np.pad(start, 1, constant_values=False)
    stride = cur.shape[1]
    flat = cur.ravel()
    ys, xs = np.nonzero(removable)
    idx = (ys + 1) * stride + (xs + 1)
    subfield = ((ys & 1) << 1) | (xs & 1)
    # the 8 neighbours, clockwise from north — the cyclic order the crossing number needs
    offsets = np.array([-stride, -stride + 1, 1, stride + 1,
                        stride, stride - 1, -1, -stride - 1])
    for _ in range(max_iters):
        changed = False
        for s in range(4):
            sel = (subfield == s) & flat[idx]
            if not sel.any():
                continue
            here = idx[sel]
            neighbours = flat[here[:, None] + offsets]
            crossings = (~neighbours & np.roll(neighbours, -1, axis=1)).sum(axis=1)
            drop = crossings == 1
            if drop.any():
                flat[here[drop]] = False
                changed = True
        if not changed:
            break
    return cur[1:-1, 1:-1]


def feather_coverage_edge(filepath, factor):
    """Smooth `filepath`'s coverage boundary at the source-cell scale (`factor` render px
    per source cell), writing a new raster and swapping it in.

    Reads come only from the untouched input, so no block can observe another block's
    output and the result does not depend on traversal order. Every written pixel is a
    function of the input within `pad` px, and adjacent stems share far more halo than
    that, so neighbouring stems draw the same fringe and the tile seams stay closed."""
    from scipy.ndimage import distance_transform_edt, gaussian_filter

    # Half a source cell keeps the 0.5-line within half a cell of the original boundary;
    # measured on a 45-degree staircase it cuts the stair runs from `factor` px to <= 4.
    sigma = factor / 2.0
    # The blur can only want to peel within about 2 sigma of the boundary, and each pass
    # takes one layer.
    max_iters = int(2 * sigma) + 2
    # Support of one written pixel: the blur's 4 sigma, plus one px per peeling pass.
    pad = int(4.0 * sigma) + max_iters + 2
    block = 2048
    out_path = filepath + ".feather.tif"
    with rasterio.env.Env(GDAL_CACHEMAX=256), rasterio.open(filepath) as src:
        h, w = src.height, src.width
        profile = src.profile
        profile.update(driver="GTiff", tiled=True, blockxsize=512, blockysize=512,
                       BIGTIFF="IF_SAFER", SPARSE_OK=True)
        alpha_band = src.count if src.count >= 2 else None
        with rasterio.open(out_path, "w", **profile) as dst:
            dst.colorinterp = src.colorinterp
            for row in range(0, h, block):
                for col in range(0, w, block):
                    r0, c0 = max(0, row - pad), max(0, col - pad)
                    r1, c1 = min(h, row + block + pad), min(w, col + block + pad)
                    pwin = rasterio.windows.Window(c0, r0, c1 - c0, r1 - r0)
                    ir0, ic0 = row - r0, col - c0
                    ir1 = ir0 + min(block, h - row)
                    ic1 = ic0 + min(block, w - col)
                    iwin = rasterio.windows.Window(col, row, ic1 - ic0, ir1 - ir0)
                    bands = src.read(window=pwin)
                    valid = src.read_masks(1, window=pwin) != 0
                    if valid.any() and not valid.all():
                        target = gaussian_filter(valid.astype(np.float32), sigma=sigma,
                                                 mode="nearest") >= 0.5
                        # Grow first: growth can only join coverage, never split it. Then
                        # peel what the blur wants gone, topology-preserving, so everything
                        # connected in `grown` — which contains all of `valid` — stays
                        # connected. No passage can close.
                        grown = valid | target
                        final = _peel_preserving_topology(grown, grown & ~target, max_iters)
                        added = final & ~valid
                        if added.any():
                            # nearest surveyed neighbour's value, read from the input, so
                            # the fringe never inherits a value another block wrote
                            _, (ir, ic) = distance_transform_edt(~valid, return_indices=True)
                            bands[0][added] = bands[0][ir[added], ic[added]]
                        removed = valid & ~final
                        if alpha_band is not None:
                            bands[alpha_band - 1] = np.where(final, 255, 0)
                        else:
                            bands[0][removed] = NODATA
                    dst.write(bands[:, ir0:ir1, ic0:ic1], window=iwin)
    os.replace(out_path, filepath)


def band_select(source):
    """`-b N ` (trailing space) if the source pins a band, else ''. S-102 is 2-band
    (depth/uncertainty); band 1 is depth (negated to elevation after warp)."""
    band = config.source_property(source, "band")
    return f"-b {band} " if band else ""


def create_virtual_raster(tmp_folder, i, source_items):
    source = source_items[0]["source"]
    vrt = f"{tmp_folder}/{i}.vrt"
    listpath = f"{tmp_folder}/{i}-file-list.txt"
    paths = [config.source_path(source, item["filename"]) for item in source_items]
    with open(listpath, "w") as f:
        f.write("".join(p + "\n" for p in paths))
    utils.run_command(f"gdalbuildvrt -overwrite {band_select(source)}-input_file_list {listpath} {vrt}", silent=SILENT)
    assert_vrt_complete(vrt, paths)
    return vrt


def assert_vrt_complete(vrt, paths):
    """Raise unless the built VRT references every input. gdalbuildvrt skips an input whose
    CRS differs from the first with a warning and exit 0 — and the warning can read "expected
    NAD83, got NAD83" when the two differ only by an attached vertical component, so a third of
    a source's coverage can vanish from the mosaic with nothing in the log naming it. Matched on
    basename: gdalbuildvrt may store a path relative to the VRT."""
    with open(vrt) as f:
        referenced = {os.path.basename(unescape(m)) for m in _SOURCE_FILENAME_RE.findall(f.read())}
    dropped = [p for p in paths if os.path.basename(p) not in referenced]
    if dropped:
        raise RuntimeError(f"gdalbuildvrt dropped {len(dropped)}/{len(paths)} input(s) from "
                           f"{vrt} — heterogeneous CRS? e.g. {dropped[:3]}")


def vrt_bounds(vrt):
    """A VRT's own extent, (left, bottom, right, top) in its own CRS. Every VRT this reads is a
    north-up EPSG:3857 warp output, so the rotation terms are zero."""
    with open(vrt) as f:
        xml = f.read()
    gt = [float(v) for v in _GEOTRANSFORM_RE.search(xml).group(1).split(",")]
    width, height = (int(v) for v in _RASTER_SIZE_RE.search(xml).groups())
    return gt[0], gt[3] + height * gt[5], gt[0] + width * gt[1], gt[3]


def intersects(a, b):
    """Do two (left, bottom, right, top) boxes share area? Touching is not sharing — a source
    that only abuts the window contributes no pixel."""
    return a[2] > b[0] and a[0] < b[2] and a[3] > b[1] and a[1] < b[3]


def _fan_out(job, args):
    """Run `job` over `args`, MIXED_WORKERS at a time, results in `args` order. Threads rather
    than processes: every worker is a subprocess blocked on a socket, so there is no GIL to
    fight and one shared GDAL block cache. The first failure re-raises."""
    if MIXED_WORKERS <= 1 or len(args) <= 1:
        return [job(a) for a in args]
    with ThreadPoolExecutor(max_workers=MIXED_WORKERS) as pool:
        return list(pool.map(job, args))


def combine_warped(tmp_folder, i, warped, out_vrt, window, res):
    """Mosaic same-grid 3857 warped VRTs into `out_vrt`, chunking the combine past
    COMBINE_FANOUT inputs so their opens overlap.

    assert_vrt_complete runs at every level, so a silently-dropped input is still caught where
    it was dropped — chunking moves the gate, it doesn't weaken it."""
    left, bottom, right, top = window

    def build(out, paths, te=""):
        listpath = f"{out}-list.txt"
        with open(listpath, "w") as f:
            f.write("".join(p + "\n" for p in paths))
        _run(f"gdalbuildvrt -overwrite {te}-tr {res} {res} -input_file_list {listpath} {out}",
             f"gdalbuildvrt(mixed) {out}")
        assert_vrt_complete(out, paths)
        return out

    roots = warped
    if len(warped) > COMBINE_FANOUT:
        chunks = [warped[a:a + COMBINE_FANOUT] for a in range(0, len(warped), COMBINE_FANOUT)]
        roots = _fan_out(lambda nc: build(f"{tmp_folder}/{i}-chunk{nc[0]}.vrt", nc[1]),
                         list(enumerate(chunks)))
    build(out_vrt, roots, te=f"-te {left} {bottom} {right} {top} ")


def warp_mixed(tmp_folder, i, inputs, out_vrt, zoom, aggregation_tile, buffer, band=None):
    """Mosaic heterogeneous-CRS inputs into one lazy 3857 VRT, warping each separately.

    A warped VRT holds a single source CRS, so the mixed case can't be one warp — but it can
    be one warp PER input (each of those is homogeneous), and the results all share 3857 and
    one grid, so gdalbuildvrt will merge them. The chain stays lazy end to end: `translate` is
    the only pass over the pixels, where warping straight to a raster wrote a multi-GB
    transient that then had to be read back and recompressed.

    Laziness is also what keeps a vertical datum shift out of reach. gdalwarp converts NAVD88
    to ellipsoidal height when it writes real pixels — always deeper across CONUS — and a VRT
    cannot express a pixel-value transform, so it cannot. Prep flattens compound CRSs
    (source_datum.flatten_compound_crs); this is the second line, not the first.

    -tap rather than a computed per-input window: the aggregation grid is already an integer
    multiple of `res` from the mercator origin (a tile bound is `x*512*res` off `-half`, and
    `half/res` == `256*2**z`), so aligning each input's own extent to multiples of -tr lands it
    on that same grid. gdalbuildvrt's -te/-tr then pin the exact output window, reading every
    contributing level 1:1."""
    window = buffered_bounds(aggregation_tile, buffer)
    res = get_resolution(zoom)
    # -srcband on the object itself, not a band-selecting VRT in front of it: that VRT would
    # double this phase's /vsicurl opens and it is the opens that this phase costs.
    bsel = f"-srcband {band} " if band else ""

    def warp(indexed):
        j, path = indexed
        w = f"{tmp_folder}/{i}-{j}-3857.vrt"
        _run(f"gdalwarp -of vrt -overwrite {bsel}-t_srs EPSG:3857 -tr {res} {res} -tap "
             f"-r {RESAMPLE} -dstnodata {NODATA} {path} {w}", f"gdalwarp(mixed) {w}")
        return w

    # The covering registers every file within 2*macrotile_buffer_3857 of the macrotile, which is
    # wider than this warp's buffer_pixels*res halo, so an edge file can land wholly outside the
    # window. It contributes no pixel and gdalbuildvrt -te drops it silently (exit 0, no warning)
    # — drop it here instead, or assert_vrt_complete reads that as the heterogeneous-CRS drop it
    # exists to catch.
    warped = [w for w in _fan_out(warp, list(enumerate(inputs)))
              if intersects(vrt_bounds(w), window)]
    if not warped:
        raise RuntimeError(f"no input of group {i} reaches {window} — every one of "
                           f"{len(inputs)} is outside the warp window")
    combine_warped(tmp_folder, i, warped, out_vrt, window, res)


def get_resolution(zoom):
    bounds = mercantile.xy_bounds(mercantile.Tile(x=0, y=0, z=zoom))
    return (bounds.right - bounds.left) / 512


def buffered_bounds(aggregation_tile, buffer):
    """The tile's EPSG:3857 bounds expanded by `buffer` on every side — the halo -te the warp
    builds on. The land-mask rasterize must reuse this so its grid matches the warp pixel-for-
    pixel, so it's one definition here rather than three inline copies that must stay in sync."""
    left, bottom, right, top = mercantile.xy_bounds(aggregation_tile)
    return left - buffer, bottom - buffer, right + buffer, top + buffer


def _run(cmd, what, tries=3):
    # Check the exit code, not stderr: gdal writes non-fatal warnings (e.g.
    # "Several coordinate operations" for datum transforms like 4269->3857) to
    # stderr, which must not be treated as a failure.
    #
    # Retry like _build_tile_vrt: these commands stream sources over /vsicurl, and a
    # mid-transfer blip (truncated S3 range read, HDF5 "read through page buffer failed")
    # surfaces as a nonzero exit that GDAL's request-level HTTP retry never saw. All
    # callers pass -overwrite / fresh outputs, so a re-run is a clean fresh attempt; a
    # deterministic failure (bad product, bad args) exhausts `tries` and still raises.
    for attempt in range(1, tries + 1):
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if proc.returncode == 0:
            return
        if attempt < tries:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
            print(f"RETRY {attempt}/{tries - 1}: {what} failed (exit {proc.returncode}), "
                  f"retrying: {' | '.join(tail)}", flush=True)
            time.sleep(5 * attempt)
    raise Exception(f"{what} failed (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}")


def create_warp(vrt, vrt_3857, zoom, aggregation_tile, buffer):
    left, bottom, right, top = buffered_bounds(aggregation_tile, buffer)
    res = get_resolution(zoom)
    _run(f"gdalwarp -of vrt -overwrite -t_srs EPSG:3857 -tr {res} {res} "
         f"-te {left} {bottom} {right} {top} -r {RESAMPLE} -dstnodata {NODATA} {vrt} {vrt_3857}",
         f"gdalwarp {vrt}")


def translate(in_filepath, out_filepath):
    # ZSTD (no predictor — ADD_ALPHA makes this Float32 + a Byte alpha band, and
    # PREDICTOR=3 is float-only) + NUM_THREADS. The merge reads these per-source COGs and
    # inherits the profile into the merged DEM, so compressing here propagates downstream.
    # IF_SAFER, not IF_NEEDED: with compression IF_NEEDED never fires, and z15 grids
    # whose ZSTD output still crosses 4 GB die mid-write in TIFFAppendToStrip.
    _run("GDAL_CACHEMAX=512 gdal_translate -of COG -co BIGTIFF=IF_SAFER -co ADD_ALPHA=YES "
         "-co OVERVIEWS=NONE -co SPARSE_OK=YES -co BLOCKSIZE=512 "
         f"-co COMPRESS=ZSTD -co NUM_THREADS=ALL_CPUS {in_filepath} {out_filepath}",
         f"gdal_translate {in_filepath}")


def contains_nodata_pixels(filepath):
    with rasterio.env.Env(GDAL_CACHEMAX=64):
        with rasterio.open(filepath) as src:
            block = 1024
            for row in range(0, src.height, block):
                for col in range(0, src.width, block):
                    window = rasterio.windows.Window(col, row,
                                                     min(block, src.width - col),
                                                     min(block, src.height - row))
                    data = np.nan_to_num(src.read(1, window=window), nan=NODATA)
                    if NODATA in data:
                        return True
    return False


def reproject(filepath):
    filename = filepath.split("/")[-1]
    z, x, y, child_z = (int(a) for a in filename.replace("-aggregation.csv", "").split("-"))
    aggregation_tile = mercantile.Tile(x=x, y=y, z=z)

    # Beside the CSV.
    tmp_folder = utils.merge_scratch(filepath)
    utils.create_folder(tmp_folder)
    metadata_filepath = f"{tmp_folder}/reprojection.json"
    if os.path.isfile(metadata_filepath):
        print(f"reproject {filename} already done...")
        return

    grouped = utils.get_grouped_source_items(filepath)
    # Build at the FINEST source's resolution, not the top-priority group's: merge order is
    # priority-then-maxzoom, so grouped[0] may be a coarser datum-authoritative source (S-102)
    # — don't let it lower the grid and upsample a finer source that's also present.
    maxzoom = max(g[0]["maxzoom"] for g in grouped)
    resolution = get_resolution(maxzoom)

    # Always buffer (even single-source) so the merged DEM has a halo for contour
    # seam continuity (lines traced through the overlap, then clipped to the tile
    # bbox). Raster crops it out via the buffer_pixels offset, so this is free for
    # raster.
    buffer_pixels = int(utils.macrotile_buffer_3857 / resolution)
    buffer_3857_rounded = buffer_pixels * resolution

    for i, source_items in enumerate(grouped):
        sid = source_items[0]["source"]
        out_tiff = f"{tmp_folder}/{i}-3857.tiff"
        vrt_3857 = f"{tmp_folder}/{i}-3857.vrt"
        if config.source_property(sid, "mixed_crs"):
            # Per-tile UTM zones: one warp per tile, recombined — gdalbuildvrt refuses the
            # source CRSs but accepts the warped results.
            warp_mixed(tmp_folder, i, [config.source_path(sid, it["filename"])
                                       for it in source_items],
                       vrt_3857, maxzoom, aggregation_tile, buffer_3857_rounded,
                       band=config.source_property(sid, "band"))
        else:
            create_warp(create_virtual_raster(tmp_folder, i, source_items), vrt_3857,
                        maxzoom, aggregation_tile, buffer_3857_rounded)
        translate(vrt_3857, out_tiff)
        if config.source_property(sid, "negate"):
            negate_band1(out_tiff)  # streamed positive-down source (S-102 depth) -> elevation
        # Before the land clamps, never after: this smooths a boundary the SOURCE's own
        # cells drew, while the clamps below cut nodata along the OSM water line, which is
        # vector-precise and must not be re-drawn at some source's cell scale.
        levels = maxzoom - source_items[0]["maxzoom"]
        if levels >= FEATHER_MIN_LEVELS:
            feather_coverage_edge(out_tiff, min(1 << levels, FEATHER_MAX_FACTOR))
        if config.source_property(sid, "land_clamp"):
            # Coarse source (GEBCO/EMODnet) with no land/water concept: its shoreline cells
            # read negative on land. Clamp valid ^ land ^ <0 -> 0 right after warp, so the
            # merge/contour/soundings never see the false water. The mask rasterizes onto the
            # SAME buffered -te/-tr the warp used (all groups warp at `maxzoom`), so it aligns
            # pixel-for-pixel and tile halos clamp identically. Rasterize once per tile (atomic,
            # so a crashed rasterize can't leave a truncated mask this cache would trust), reuse
            # across flagged sources. Fails loudly if LANDMASK is unreadable (ogr2ogr raises).
            mask_tif = f"{tmp_folder}/landmask.tif"
            if not os.path.isfile(mask_tif):
                landmask.rasterize(buffered_bounds(aggregation_tile, buffer_3857_rounded),
                                   resolution, mask_tif)
            landmask.clamp(out_tiff, mask_tif)
            # #24 inverse clamp: where this coarse source fabricates POSITIVE land over mapped
            # inland water (a lake it holds no bathymetry for), clear it to nodata so the merge
            # fills it to 0 and Part 3's nodata depth-area renders instead of tan false land.
            # Keys on a water-ONLY raster, never the combined mask above (ocean and lake are both
            # 0 there, so it would hole the coastal ocean). Same buffered -te/-tr, cached per tile
            # across flagged sources. Skipped cleanly when no water feed is published — the mask
            # degrades to land-clamp-only (today's behavior), like rasterize's water subtraction.
            water_tif = None
            if landmask._present(landmask.water_path()):
                water_tif = f"{tmp_folder}/watermask.tif"
                if not os.path.isfile(water_tif):
                    landmask.rasterize_water(buffered_bounds(aggregation_tile, buffer_3857_rounded),
                                             resolution, water_tif)
                landmask.clamp_positive_water(out_tiff, water_tif)
            # 4th-quadrant clamp: shoreline cells reading positive just SEAWARD of the land line
            # (combined mask==0, outside inland water) are false drying — the depare/drying bucket
            # reads this unclamped mosaic in (0, DRYING_CAP]. Clamp positive ocean to 0 so it can't.
            # Deliberately discards coarse "drying": these flagged sources can't resolve the
            # foreshore, and trusted topobathy sources are unflagged, so genuine drying survives.
            landmask.clamp_positive_ocean(out_tiff, mask_tif, water_tif)
        if len(grouped) > 1 and not contains_nodata_pixels(out_tiff):
            break

    with open(metadata_filepath, "w") as f:
        json.dump({"buffer_pixels": buffer_pixels}, f, indent=2)


def _check():
    """warp_mixed must keep tiles from *different* CRSs that gdalbuildvrt would drop one
    of. Two boxes straddling the UTM 17N/18N boundary (~78W): the 3857 mosaic of both
    must have strictly more valid pixels than either alone -> the off-zone tile survived."""
    import tempfile
    from rasterio.transform import from_origin

    d = tempfile.mkdtemp()

    def utm_box(path, epsg, west_e, north_n, val, n=120, res=100):
        arr = np.full((n, n), val, dtype="float32")
        with rasterio.open(path, "w", driver="GTiff", height=n, width=n, count=1,
                           dtype="float32", nodata=NODATA, crs=f"EPSG:{epsg}",
                           transform=from_origin(west_e, north_n, res, res)) as dst:
            dst.write(arr, 1)

    a, b = f"{d}/a_z17.tif", f"{d}/b_z18.tif"
    utm_box(a, 32617, 748000, 4438000, 10.0)  # ~ -78.1, 40.1 in UTM 17N
    utm_box(b, 32618, 240000, 4438000, 20.0)  # ~ -78.0, 40.1 in UTM 18N
    tile = mercantile.tile(-78.0, 40.0, 8)    # ~1.4deg window, both boxes inside

    def valid(path):
        with rasterio.open(path) as src:
            arr = src.read(1)
        return int(np.count_nonzero((arr != NODATA) & ~np.isnan(arr)))

    both, just_a = f"{d}/both.vrt", f"{d}/a.vrt"
    warp_mixed(d, 0, [a, b], both, 9, tile, 0)
    warp_mixed(d, 1, [a], just_a, 9, tile, 0)
    va, vboth = valid(just_a), valid(both)
    assert vboth > va > 0, (va, vboth)

    # An input the covering registered (its bounds are within 2*macrotile_buffer_3857 of the
    # macrotile) but that lands outside this warp's narrower halo must be dropped quietly, not
    # read as a silent CRS drop: gdalbuildvrt -te discards it with exit 0 and no warning, so
    # asserting on the unfiltered list turns a legal edge file into a failed build. A handful of
    # S-102 macrotiles hold one.
    far = f"{d}/far_z17.tif"
    utm_box(far, 32617, 400000, 4800000, 40.0)  # ~500 km away, nowhere near `tile`
    edge = f"{d}/edge.vrt"
    warp_mixed(d, 3, [a, far], edge, 9, tile, 0)
    assert valid(edge) == va, (valid(edge), va)  # same pixels as `a` alone
    with open(edge) as f:
        assert "far_z17" not in f.read()
    try:
        warp_mixed(d, 4, [far], f"{d}/allfar.vrt", 9, tile, 0)
        assert False, "expected a wholly-outside group to raise"
    except RuntimeError as e:
        assert "outside the warp window" in str(e), e

    # Chunking the combine past COMBINE_FANOUT must change nothing a caller can see: the same
    # pixels, including who wins an overlap (the chunks are contiguous, so the last input still
    # does). Overlapping boxes marching east, each carrying its own constant, make that visible.
    boxes = []
    for k in range(10):
        p = f"{d}/fan{k}_z17.tif"
        utm_box(p, 32617, 748000 + k * 6000, 4438000, float(k + 1), n=120, res=100)
        boxes.append(p)
    chunked, one_shot = f"{d}/chunked.vrt", f"{d}/oneshot.vrt"
    real_fanout = COMBINE_FANOUT
    try:
        globals()["COMBINE_FANOUT"] = 3                # 4 chunks over the 10 boxes
        warp_mixed(d, 5, boxes, chunked, 9, tile, 0)
        globals()["COMBINE_FANOUT"] = len(boxes) + 1   # same inputs, one flat combine
        warp_mixed(d, 6, boxes, one_shot, 9, tile, 0)
    finally:
        globals()["COMBINE_FANOUT"] = real_fanout
    with rasterio.open(chunked) as c, rasterio.open(one_shot) as o:
        ca, oa = c.read(1), o.read(1)
    assert (ca == oa).all(), "chunked combine is not pixel-identical to the flat one"
    assert set(np.unique(ca[ca != NODATA]).tolist()) == set(float(k + 1) for k in range(10)), \
        f"every chunked input must reach the mosaic: {np.unique(ca[ca != NODATA])}"
    with open(chunked) as f:
        assert "chunk" in f.read(), "expected the root to reference chunk VRTs"

    # ...and an input the combine cannot open must still raise from inside a chunk: gdalbuildvrt
    # skips it with exit 0, which is the silent-drop shape assert_vrt_complete exists to catch.
    junk = f"{d}/junk-3857.vrt"
    with open(junk, "w") as f:
        f.write("not a vrt")
    try:
        combine_warped(d, 7, [f"{d}/5-0-3857.vrt", junk], f"{d}/holed.vrt",
                       buffered_bounds(tile, 0), get_resolution(9))
        assert False, "expected a dropped input inside a chunk to raise"
    except RuntimeError as e:
        assert "dropped" in str(e) and "junk" in str(e), e

    # band: a mixed_crs source can be multi-band (S-102 is depth + uncertainty), and the mosaic
    # must carry the pinned band alone. -srcband replaces the band-selecting VRT that used to sit
    # in front of every input, so this is what proves the right band still lands.
    two = f"{d}/two_band_z17.tif"
    with rasterio.open(two, "w", driver="GTiff", height=120, width=120, count=2, dtype="float32",
                       nodata=NODATA, crs="EPSG:32617",
                       transform=from_origin(748000, 4438000, 100, 100)) as dst:
        dst.write(np.full((120, 120), 7.0, dtype="float32"), 1)
        dst.write(np.full((120, 120), 99.0, dtype="float32"), 2)
    picked = f"{d}/picked.vrt"
    warp_mixed(d, 8, [two], picked, 9, tile, 0, band=1)
    with rasterio.open(picked) as src:
        arr, count = src.read(1), src.count
    assert count == 1, f"band-pinned mosaic must be single-band, got {count}"
    got = arr[(arr != NODATA) & ~np.isnan(arr)]
    assert got.size and (got == 7.0).all(), f"expected band 1 (7.0), got {np.unique(got)[:5]}"

    # ...and must not shift a value vertically. A materialized gdalwarp converts NAVD88 to
    # ellipsoidal height (~-22 m in Puget Sound, always deeper); the warped-VRT mosaic cannot,
    # which is the second line behind source_datum.flatten_compound_crs. The materialized warp
    # is run first as a POSITIVE CONTROL: without a geoid grid installed PROJ applies nothing
    # and the real assertion would pass vacuously, so skip loudly instead of claiming a pass.
    # EPSG:5498 is NAD83 + NAVD88 height — the horizontal half is GEOGRAPHIC, so this box is
    # placed in degrees (~11 km over Puget Sound), not in the eastings the UTM boxes above use.
    compound = f"{d}/compound.tif"
    utm_box(compound, 5498, -122.45, 48.45, -80.0, n=200, res=0.0005)
    ptile = mercantile.tile(-122.4, 48.4, 11)
    shifted = f"{d}/shifted.tif"
    left, bottom, right, top = buffered_bounds(ptile, 0)
    res13 = get_resolution(13)
    _run(f"gdalwarp -overwrite -t_srs EPSG:3857 -tr {res13} {res13} -te {left} {bottom} "
         f"{right} {top} -r {RESAMPLE} -dstnodata {NODATA} {compound} {shifted}",
         "gdalwarp(control)")

    def middle(path):
        with rasterio.open(path) as src:
            arr = src.read(1)
        ok = (arr != NODATA) & ~np.isnan(arr)
        return float(np.median(arr[ok])) if ok.any() else None

    control = middle(shifted)
    if control is None or abs(control - (-80.0)) <= 1.0:
        print("  (skipped vertical-shift check: no geoid grid here, the control did not shift)")
    else:
        lazy = f"{d}/compound-3857.vrt"
        warp_mixed(d, 2, [compound], lazy, 13, ptile, 0)
        got = middle(lazy)
        assert got is not None and abs(got - (-80.0)) <= 0.1, \
            f"warp_mixed shifted a compound-CRS value: {got} (control {control}, source -80.0)"

    # assert_vrt_complete: same-CRS inputs all land in the VRT; a mixed pair (gdalbuildvrt
    # drops the off-CRS one with only a warning) must raise and name the missing file.
    c, mixed_vrt = f"{d}/c_z17.tif", f"{d}/mixed.vrt"
    utm_box(c, 32617, 760000, 4438000, 30.0)
    same_vrt = f"{d}/same.vrt"
    utils.run_command(f"gdalbuildvrt -overwrite {same_vrt} {a} {c}")
    assert_vrt_complete(same_vrt, [a, c])
    utils.run_command(f"gdalbuildvrt -overwrite {mixed_vrt} {a} {b}")
    try:
        assert_vrt_complete(mixed_vrt, [a, b])
        assert False, "expected a dropped off-CRS input to raise"
    except RuntimeError as e:
        assert "b_z18.tif" in str(e) and "1/2" in str(e), e

    # run_command must RAISE on a failed gdalbuildvrt (not swallow it) — the bug that let a
    # missing VRT reach gdalwarp as a baffling "No such file" and kill an hour-long aggregate.
    miss = f"{d}/miss.vrt"
    try:
        utils.run_command(f"gdalbuildvrt -overwrite {miss} {d}/does-not-exist.tif")
        assert False, "expected run_command to raise on a failed gdalbuildvrt"
    except RuntimeError:
        assert not os.path.exists(miss)

    # _run retries the same transient class (warp/translate over /vsicurl dying
    # mid-transfer) then succeeds; a deterministic failure exhausts and raises.
    # `false` fails every time; retry-then-succeed uses a marker file so attempt 2 differs.
    real_sleep, time.sleep = time.sleep, lambda s: None
    try:
        marker = f"{d}/ran-once"
        _run(f"test -f {marker} || {{ touch {marker}; false; }}", "flaky-cmd", tries=3)
        assert os.path.exists(marker), "first attempt must have run"
        try:
            _run("false", "always-fails", tries=2)
            assert False, "expected _run to raise after exhausting retries"
        except Exception as e:
            assert "always-fails failed" in str(e)
    finally:
        time.sleep = real_sleep

    # negate_band1 must handle the REAL pipeline file: a COG (translate -of COG) with an
    # ADD_ALPHA mask band. A plain GTiff would miss the COG-layout update error the build hit.
    src = f"{d}/depth.tif"
    arr = np.array([[5.0, NODATA], [10.0, -3.0]], dtype="float32")  # depths + a nodata cell
    with rasterio.open(src, "w", driver="GTiff", height=2, width=2, count=1, dtype="float32",
                       nodata=NODATA, crs="EPSG:3857", transform=from_origin(0, 2, 1, 1)) as dst:
        dst.write(arr, 1)
    cog = f"{d}/depth_cog.tif"
    translate(src, cog)        # -of COG -co ADD_ALPHA=YES, exactly as reproject() does
    negate_band1(cog)          # must not raise on the COG layout, and flip only valid pixels
    with rasterio.open(cog) as r:
        o, valid = r.read(1), (r.read_masks(1) != 0)
    assert o[0, 0] == -5.0 and o[1, 0] == -10.0 and o[1, 1] == 3.0, o   # valid pixels flipped
    assert not valid[0, 1], "nodata cell must stay masked (and unflipped)"

    # feather_coverage_edge. Fixtures are 256px COGs at factor 16 (a source 4 zoom levels
    # coarser than the render grid); the staircase is a 45-degree coverage edge quantized
    # to 16px source cells, which is the artifact this removes.
    F, N = 16, 256
    def staircase():
        a = np.full((N, N), NODATA, dtype="float32")
        for cr in range(N // F):
            for cc in range(N // F):
                if cc <= cr:  # coverage below-left of the diagonal
                    a[cr*F:(cr+1)*F, cc*F:(cc+1)*F] = -3.0
        return a

    def as_cog(arr, name):
        raw = f"{d}/{name}.tif"
        with rasterio.open(raw, "w", driver="GTiff", height=arr.shape[0], width=arr.shape[1],
                           count=1, dtype="float32", nodata=NODATA, crs="EPSG:3857",
                           transform=from_origin(0, arr.shape[0], 1, 1)) as dst:
            dst.write(arr, 1)
        cog = f"{d}/{name}_cog.tif"
        translate(raw, cog)
        return cog

    def read(path):
        with rasterio.open(path) as r:
            return r.read(1), r.read_masks(1) != 0

    def edge_runs(valid):
        """Max vertical run of an identical boundary-x: the stair pitch (F before, a few
        px after, since a feathered 45-degree boundary steps about one px per row)."""
        flips = valid[:, 1:] != valid[:, :-1]
        best, prev = 0, {}
        for row in range(valid.shape[0]):
            xs = set(np.nonzero(flips[row])[0].tolist())
            for x in list(prev):
                if x in xs:
                    prev[x] += 1
                    xs.discard(x)
                else:
                    best = max(best, prev.pop(x))
            for x in xs:
                prev[x] = 1
        return max([best] + list(prev.values()))

    def splits(before, after):
        """Original coverage components that ended up in more than one final component.
        Counting components is not the property — the feather may legitimately ADD one by
        refusing to delete an isolated pixel, and may remove one by joining two across a
        sub-cell gap. What must never happen is a component coming apart."""
        from scipy.ndimage import label
        lo, n = label(before)
        ln, _ = label(after)
        return sum(1 for c in range(1, n + 1)
                   if len(np.unique(ln[lo == c])[np.unique(ln[lo == c]) > 0]) > 1)

    # 1. the staircase alone: quantized boundary -> near-diagonal, added fringe pixels carry
    #    real depths (never NODATA), and the whole op is deterministic.
    plain = staircase()
    cog_a, cog_b = as_cog(plain, "stair"), as_cog(plain, "stair2")
    before = edge_runs(read(cog_a)[1])
    feather_coverage_edge(cog_a, F)
    out_data, out_valid = read(cog_a)
    after = edge_runs(out_valid)
    assert before >= F and after <= 6, (before, after)
    assert (out_data[out_valid] != NODATA).all(), "no covered pixel may hold NODATA"
    added = out_valid & (plain == NODATA)
    assert added.any() and (out_data[added] < 0).all(), \
        "added fringe pixels must carry a surveyed neighbour's depth"
    feather_coverage_edge(cog_b, F)
    again_data, again_valid = read(cog_b)
    assert (again_data == out_data).all() and (again_valid == out_valid).all(), \
        "feather must be deterministic"

    # 2. connectivity, the case that matters for a chart: coverage that is connected before
    #    must be connected after, whatever the shape. Three ways to break it, all of which a
    #    distance-based guard misses, because every erased pixel sits near surviving water:
    #    a long sub-cell creek, a passage crossing the window, and a short neck joining two
    #    basins. The neck is the one a purely distance-based rule cannot see at all.
    creek = staircase()
    creek[2*F:2*F + F//2, 3*F:14*F] = -4.0                      # long, 11 cells
    through = staircase()
    through[6*F:6*F + F//2, 7*F:N] = -4.0                       # a passage leaving the window
    neck = np.full((120, 200), NODATA, dtype="float32")
    neck[40:80, 20:90] = -5.0
    neck[40:80, 110:180] = -5.0
    neck[57:63, 90:110] = -3.0                                  # one-cell bottleneck
    for name, arr in (("creek", creek), ("through-creek", through), ("neck", neck)):
        cog = as_cog(arr, name.replace("-", ""))
        feather_coverage_edge(cog, F)
        data, valid = read(cog)
        broke = splits(arr != NODATA, valid)
        assert broke == 0, f"{name}: feather split {broke} coverage component(s)"
        assert (data[valid] != NODATA).all(), f"{name}: covered pixel holding NODATA"

    print(f"aggregation_reproject.py self-check ok (valid pixels: A={va}, A+B={vboth}; "
          f"feather stair {before}px -> {after}px)")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        _check()
    else:
        sys.exit("aggregation_reproject is a library; run with --check for the self-check")
