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
import keys
import utils

# An overview's key = H(its children's terrain/overview keys ‖ these modules ‖ config). A changed
# child key propagates upward by construction (a rebuilt overview writes a new sidecar its parent
# reads), so the mtime staleness-cascade is gone; missing-artifact self-heal is inherent (no
# artifact → no fresh key).
DOWNSAMPLE_MODULES = ["downsampling", "encode", "utils"]


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
    """Build one parent pmtiles. Returns the set of missing child filenames (empty if none).
    Pure per-filepath unit of work — safe to run in a process Pool."""
    aggregation_id, filename = filepath.split("/")[-2:]
    z, x, y, parent_zoom = (int(a) for a in filename.replace("-downsampling.csv", "").split("-"))
    out_folder = utils.get_pmtiles_folder(x, y, z)
    utils.create_folder(out_folder)
    out_filepath = f"{out_folder}/{z}-{x}-{y}-{parent_zoom}.pmtiles"
    # Invalidate before writing (the crash rule every keyed writer follows): under FORCE the
    # key is unchanged, so a crash mid-archive would otherwise leave the old sidecar reading
    # a torn overview as fresh forever.
    sc = keys.sidecar(out_filepath)
    if os.path.isfile(sc):
        os.remove(sc)

    extent = mercantile.Tile(x=x, y=y, z=z)
    tmp_folder = filepath.replace("-downsampling.csv", "-tmp")
    utils.create_folder(tmp_folder)
    pmtiles_filenames = _children(filepath)

    missing = set()
    parents = [extent] if z == parent_zoom else list(mercantile.children(extent, zoom=parent_zoom))
    for parent in parents:
        missing |= create_tile(parent.x, parent.y, parent.z, tmp_folder, pmtiles_filenames)
    utils.create_archive(tmp_folder, out_filepath)
    shutil.rmtree(tmp_folder)
    # Don't key it when a referenced child was missing — leave it stale so a rerun (after the
    # upstream gap is fixed) rebuilds it. The children were all rebuilt earlier this run (finest
    # first), so their sidecars are final now — _overview_key reads their settled keys.
    if not missing:
        keys.write_key(out_filepath, _overview_key(filepath))
    return missing


def _children(filepath):
    with open(filepath) as f:
        return [line.strip() for line in f.readlines()[1:] if line.strip()]


def _child_pmtiles_path(child_filename):
    z, x, y, _cz = (int(a) for a in child_filename.replace(".pmtiles", "").split("-"))
    return f"{utils.get_pmtiles_folder(x, y, z)}/{child_filename}"


def _overview_key(filepath):
    """A content-hash key from the children's own key sidecars — the terrain key of an aggregate
    child, the overview key of a coarser child. A missing child sidecar contributes an empty key,
    so the child's stem alone keeps a vanished/rebuilt child moving the overview key."""
    inputs = sorted(f"{c}:{keys.read_key(_child_pmtiles_path(c)) or ''}" for c in _children(filepath))
    return keys.stage_key(inputs, DOWNSAMPLE_MODULES, {"num_overviews": utils.num_overviews})


def _overview_artifact(filepath):
    z, x, y, parent_zoom = (int(a) for a in
                            filepath.split("/")[-1].replace("-downsampling.csv", "").split("-"))
    return f"{utils.get_pmtiles_folder(x, y, z)}/{z}-{x}-{y}-{parent_zoom}.pmtiles"


def _parent_zoom(filepath):
    return int(filepath.split("/")[-1].replace("-downsampling.csv", "").split("-")[3])


def dirty_filepaths():
    """Sorted -downsampling.csv to (re)build: an overview whose own key is stale (its artifact
    missing — self-heal — or its sidecar not matching the key its current children imply), OR any
    of whose children will be rewritten this run (their key will change, so this overview's will
    too). Processing finest-overview-first and carrying dirty stems forward makes the staleness
    cascade all the way up the pyramid in one pass — the key equivalent of the old mtime cascade,
    with no listing/mtime inputs: aggregate wrote each terrain child's sidecar before downsampling
    runs, so the lowest overviews read settled child keys, and each dirty overview propagates."""
    aggregation_id = utils.get_aggregation_ids()[-1]
    dirty_stems = set()
    out = []
    for filepath in sorted(glob(f"store/aggregation/{aggregation_id}/*-downsampling.csv"),
                           key=lambda fp: (-_parent_zoom(fp), fp)):
        own_stem = filepath.split("/")[-1].replace("-downsampling.csv", "")
        child_stems = [c.replace(".pmtiles", "") for c in _children(filepath)]
        if any(cs in dirty_stems for cs in child_stems):
            dirty = True  # a child overview will be rewritten (its key changes) -> so will this
        else:
            dirty = not keys.is_fresh(_overview_artifact(filepath), _overview_key(filepath))
        if dirty:
            out.append(filepath)
            dirty_stems.add(own_stem)  # cascade to the coarser overview that averages this one
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
