"""Build the overview pyramid below each source's native maxzoom.

Vendored from mapterhorn (BSD-3), covering + run combined. ``cover`` plans which
parent tiles to build per zoom (coalescing aggregation extents); ``run`` assembles
each parent from its 4 children, 2x2-averages to 512x512, and re-encodes. Overviews
keep full Terrarium precision (no per-zoom quantization) but route through
encode.py so the conservative (never-deepen) rounding still applies. WebP is
decoded with imagecodecs (no PIL dependency).

``run`` builds the whole dirty pyramid on one machine, high child_zoom first so each
level feeds the next.

Usage (from pipelines/):
  downsampling.py cover
  downsampling.py run
"""

import os
import shutil
import sys
import time
from glob import glob
from multiprocessing import get_context

import imagecodecs
import mercantile
import numpy as np
from pmtiles.reader import Reader, MmapSource

import encode
import utils


# ── cover ────────────────────────────────────────────────────────────────────

def get_extents_from_coverings(aggregation_id, zoom):
    extents = []
    for filepath in glob(f"store/aggregation/{aggregation_id}/*-*-*-{zoom}-*.csv"):
        parts = filepath.split("/")[-1].replace(".csv", "").split("-")
        z, x, y = (int(a) for a in parts[:3])
        extents.append(mercantile.Tile(x=x, y=y, z=z))
    return extents


def get_tile_to_extent_map(extents, zoom):
    out = {}
    for extent in extents:
        for child in mercantile.children(extent, zoom=zoom):
            out[child] = extent
    return out


def get_simplified_extents(extents, zoom):
    simplified = []
    for unlimited in mercantile.simplify(extents):
        if unlimited.z == zoom:
            simplified.append(mercantile.parent(unlimited, zoom=zoom - 1))
        elif unlimited.z >= zoom - utils.num_overviews:
            simplified.append(unlimited)
        else:
            simplified += list(mercantile.children(unlimited, zoom=zoom - utils.num_overviews))
    return simplified


def cover():
    aggregation_id = utils.get_aggregation_ids()[-1]
    utils.run_command(f"rm -f store/aggregation/{aggregation_id}/*-downsampling.csv")
    for child_zoom in reversed(range(1, 32)):
        extents = get_extents_from_coverings(aggregation_id, child_zoom)
        if not extents:
            continue
        print(f"child_zoom={child_zoom}: {len(extents)} extent(s)")
        tile_to_extent = get_tile_to_extent_map(extents, child_zoom)
        for simplified in get_simplified_extents(extents, child_zoom):
            involved = set()
            for child in mercantile.children(simplified, zoom=child_zoom):
                if child in tile_to_extent:
                    involved.add(tile_to_extent[child])
            lines = ["filename\n"] + [f"{e.z}-{e.x}-{e.y}-{child_zoom}.pmtiles\n" for e in involved]
            name = f"{simplified.z}-{simplified.x}-{simplified.y}-{child_zoom - 1}-downsampling.csv"
            with open(f"store/aggregation/{aggregation_id}/{name}", "w") as f:
                f.writelines(lines)


# ── run ──────────────────────────────────────────────────────────────────────

def get_tile_to_pmtiles_filename(pmtiles_filenames):
    out = {}
    for filename in pmtiles_filenames:
        z, x, y, child_zoom = (int(a) for a in filename.replace(".pmtiles", "").split("-"))
        tile = mercantile.Tile(x=x, y=y, z=z)
        children = [tile] if z == child_zoom else list(mercantile.children(tile, zoom=child_zoom))
        for child in children:
            out[child] = filename
    return out


def create_tile(parent_x, parent_y, parent_z, tmp_folder, pmtiles_filenames):
    """Build one parent webp from its 4 children. Returns the set of child pmtiles
    filenames the covering referenced but that weren't on disk (gaps to report)."""
    tile_to_filename = get_tile_to_pmtiles_filename(pmtiles_filenames)
    missing = set()
    full = np.zeros((1024, 1024), dtype=np.float32)
    for row in range(2):
        for col in range(2):
            child = mercantile.Tile(x=2 * parent_x + col, y=2 * parent_y + row, z=parent_z + 1)
            if child not in tile_to_filename:
                continue
            filename = tile_to_filename[child]
            fz, fx, fy, _ = (int(a) for a in filename.replace(".pmtiles", "").split("-"))
            folder = utils.get_pmtiles_folder(fx, fy, fz)
            child_path = f"{folder}/{filename}"
            # A tile the covering referenced but the aggregate/downsample run never produced
            # (a prior failed/interrupted build): record it and leave this quadrant empty for
            # now — execute() raises once the level finishes, failing the build rather than
            # publishing a holed pyramid (the Worker overzooms GEBCO into such holes, so
            # they surface as missing high-zoom terrain).
            if not os.path.isfile(child_path):
                missing.add(filename)
                continue
            with open(child_path, "r+b") as f:
                child_bytes = Reader(MmapSource(f)).get(child.z, child.x, child.y)
            if child_bytes is None:
                continue
            rgb = imagecodecs.webp_decode(child_bytes).astype(np.float32)
            full[512 * row:512 * (row + 1), 512 * col:512 * (col + 1)] = encode.decode(rgb)

    parent_data = full.reshape((512, 2, 512, 2)).mean(axis=(1, 3))  # 2x2 average
    rgb = encode.encode(parent_data, encode.FULL_RESOLUTION_ZOOM, conservative=True)
    with open(f"{tmp_folder}/{parent_z}-{parent_x}-{parent_y}.webp", "wb") as f:
        f.write(imagecodecs.webp_encode(rgb, lossless=True))
    return missing


def run_one(filepath):
    """Build one parent pmtiles. Returns the set of missing child filenames (empty if
    already done). Pure per-filepath unit of work — safe to run in a process Pool."""
    aggregation_id, filename = filepath.split("/")[-2:]
    if os.path.isfile(filepath.replace("-downsampling.csv", "-downsampling.done")):
        return set()
    z, x, y, parent_zoom = (int(a) for a in filename.replace("-downsampling.csv", "").split("-"))
    out_folder = utils.get_pmtiles_folder(x, y, z)
    utils.create_folder(out_folder)
    out_filepath = f"{out_folder}/{z}-{x}-{y}-{parent_zoom}.pmtiles"

    extent = mercantile.Tile(x=x, y=y, z=z)
    tmp_folder = filepath.replace("-downsampling.csv", "-tmp")
    utils.create_folder(tmp_folder)
    with open(filepath) as f:
        pmtiles_filenames = [a.strip() for a in f.readlines()[1:]]

    missing = set()
    parents = [extent] if z == parent_zoom else list(mercantile.children(extent, zoom=parent_zoom))
    for parent in parents:
        missing |= create_tile(parent.x, parent.y, parent.z, tmp_folder, pmtiles_filenames)
    utils.create_archive(tmp_folder, out_filepath)
    shutil.rmtree(tmp_folder)
    # Don't mark done when a referenced child was missing — leave it dirty so a rerun
    # (after the upstream gap is fixed) rebuilds it instead of skipping it forever.
    if not missing:
        utils.run_command(f'touch {filepath.replace("-downsampling.csv", "-downsampling.done")}')
    return missing


def tiles_intersect(a, b):
    if a == b:
        return True
    if a.z < b.z and mercantile.parent(b, zoom=a.z) == a:
        return True
    if b.z < a.z and mercantile.parent(a, zoom=b.z) == b:
        return True
    return False


def dirty_filepaths():
    """Sorted -downsampling.csv to (re)build: changed tiles, OR whose pmtiles is gone, OR a
    STALE overview — older than a child it averages, or reading a child about to be rebuilt.

    The missing-pmtiles case is load-bearing (same as aggregation_run): a covering whose
    pmtiles a prior run failed to produce/sync would otherwise diff clean forever.

    The stale-overview cases are equally load-bearing and were the gap. The source-change diff
    and the missing-own-pmtiles check only fire when a tile's OWN covering changes or its OWN
    pmtiles vanishes — neither notices when a *child* is rebuilt for any other reason (a dropped
    tile self-healing from current data, a re-tiling). So the coarse overview above kept
    averaging the old child and went stale forever (observed: a z6 tile 4 days older than the
    z7 it averages). So also rebuild an overview when a child it references is missing now
    (about to self-heal) or is newer than it. Processing finest-overview-first and treating a
    rebuilt overview as newest makes the staleness cascade all the way up the pyramid in one pass."""
    aggregation_ids = utils.get_aggregation_ids()
    aggregation_id = aggregation_ids[-1]

    dirty_tiles = []
    if len(aggregation_ids) >= 2:
        for name in utils.get_dirty_aggregation_filenames(aggregation_id, aggregation_ids[-2]):
            z, x, y, _ = (int(a) for a in name.replace("-aggregation.csv", "").split("-"))
            dirty_tiles.append(mercantile.Tile(x=x, y=y, z=z))

    have = utils.existing_pmtiles()
    mtimes = utils.pmtiles_mtimes()

    def base_dirty(tile, filename):
        if filename.replace("-downsampling.csv", ".pmtiles") not in have:
            return True
        if len(aggregation_ids) < 2:
            return True
        if any(tiles_intersect(d, tile) for d in dirty_tiles):
            return True
        return len(glob(f"store/aggregation/{aggregation_ids[-2]}/{filename}")) == 0

    def children_of(filepath):
        with open(filepath) as f:
            return [line.strip() for line in f.readlines()[1:] if line.strip()]

    def parent_zoom(filepath):
        return int(filepath.split("/")[-1].replace("-downsampling.csv", "").split("-")[3])

    # Finest overview first (high parent_zoom -> low) so a child is decided before the overview
    # that reads it; a child marked dirty here is bumped to "newest" so the rebuild cascades up.
    out = []
    for filepath in sorted(glob(f"store/aggregation/{aggregation_id}/*-downsampling.csv"),
                           key=lambda fp: (-parent_zoom(fp), fp)):
        filename = filepath.split("/")[-1]
        z, x, y, _ = (int(a) for a in filename.replace("-downsampling.csv", "").split("-"))
        own = filename.replace("-downsampling.csv", ".pmtiles")
        dirty = base_dirty(mercantile.Tile(x=x, y=y, z=z), filename)
        if not dirty:
            children = children_of(filepath)
            own_mt = mtimes.get(own)
            if any(c not in have for c in children):
                dirty = True  # a referenced child is missing -> self-healed this run -> so is this
            elif own_mt is not None:
                newest_child = max((mtimes[c] for c in children if c in mtimes), default=None)
                if newest_child is not None and newest_child > own_mt:
                    dirty = True  # overview older than a child it averages -> stale
        if dirty:
            out.append(filepath)
            mtimes[own] = float("inf")  # rebuilt now = newest, so overviews above rebuild too
    return sorted(out)


def execute(filepaths):
    by_child_zoom = {}
    for filepath in filepaths:
        child_zoom = int(filepath.split("/")[-1].replace("-downsampling.csv", "").split("-")[3])
        by_child_zoom.setdefault(child_zoom, []).append(filepath)

    # High child_zoom first so each level feeds the next. Within a level the parents
    # are independent → fan out across cores; draining each level before the next
    # keeps the level barrier (a parent reads children built one level down).
    total = sum(len(v) for v in by_child_zoom.values())
    done = 0
    last = time.monotonic()
    missing = set()
    # SPAWN, not fork: downsampling imports rasterio (utils), which inits GDAL at module
    # load — GDAL is not fork-safe, so forked workers carry a broken copy that segfaults at
    # teardown (no Python traceback, just exit 1 after the work prints). A Pool()=all-cores
    # fork over the full store reliably crashed this way; spawned workers re-import GDAL fresh,
    # so teardown is clean.
    with get_context("spawn").Pool() as pool:
        for child_zoom in sorted(by_child_zoom, reverse=True):
            level = by_child_zoom[child_zoom]
            print(f"child_zoom={child_zoom}: {len(level)} parent(s)", flush=True)
            for m in pool.imap_unordered(run_one, level, chunksize=1):
                missing |= m
                done += 1
                if time.monotonic() - last > 30:
                    print(f"  {done}/{total} parents built", flush=True)
                    last = time.monotonic()
            # Fail at the level barrier (before the gap cascades into blank parents one
            # level down): a referenced child missing here means a failed/interrupted
            # aggregate or downsample run, not a publishable pyramid.
            if missing:
                sample = ", ".join(sorted(missing)[:15])
                raise SystemExit(
                    f"pyramid incomplete at child_zoom={child_zoom}: {len(missing)} referenced "
                    f"child pmtiles missing (a failed/interrupted run). Fix the gap and rerun — "
                    f"the affected parents stay dirty: {sample}{' …' if len(missing) > 15 else ''}")


def run():
    """Build the whole dirty overview pyramid on one machine."""
    execute(dirty_filepaths())


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv == ["cover"]:
        cover()
    elif argv == ["run"]:
        run()
    else:
        sys.exit("usage: downsampling.py <cover | run>")
