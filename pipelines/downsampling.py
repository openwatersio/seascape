"""Build the overview pyramid below each source's native maxzoom.

Vendored from mapterhorn (BSD-3), covering + run combined. ``cover`` plans which
parent tiles to build per zoom (coalescing aggregation extents); ``run`` assembles
each parent from its 4 children, 2x2-averages to 512x512, and re-encodes. Overviews
keep full Terrarium precision (no per-zoom quantization) but route through
encode.py so the conservative (never-deepen) rounding still applies. WebP is
decoded with imagecodecs (no PIL dependency).

``run`` builds the whole dirty pyramid on one machine; ``run shard i n`` /
``run tail`` split it across CI runners (see ``run`` for the cut).

Usage (from pipelines/):
  downsampling.py cover
  downsampling.py run [shard <i> <n> | tail]
  downsampling.py matrix <max>            # CI shard matrix JSON, sized to the dirt
"""

import json
import os
import shutil
import sys
import time
from glob import glob
from multiprocessing import Pool

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
            # A tile the covering referenced but no aggregate shard produced (or that
            # didn't sync): treat that quadrant as empty rather than abort the whole
            # pyramid, and record it so the gap is visible (not silently dropped).
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


# Below this zoom an overview archive's parent spans 4 different ancestors, so the
# work can't be partitioned spatially — every aggregation work tile sits at or above
# it (it's the covering's seed zoom). At or above it, extent zoom only ever climbs as
# tiles coarsen along a lineage, so a finer child shares its parent's ancestor: a
# shard owning an ancestor reads only tiles already inside it. So: z >= here → shard
# by ancestor; below → the single-runner tail (a few cheap global levels).
SHARD_ROOT_Z = max(utils.macrotile_z - utils.num_overviews, 0)


def ancestor_id(z, x, y):
    """The SHARD_ROOT_Z-ancestor id of a tile, or None if below the root zoom."""
    if z < SHARD_ROOT_Z:
        return None
    tile = mercantile.Tile(x=x, y=y, z=z)
    a = tile if z == SHARD_ROOT_Z else mercantile.parent(tile, zoom=SHARD_ROOT_Z)
    return f"{a.z}-{a.x}-{a.y}"


def shard_ancestor(filepath):
    """The SHARD_ROOT_Z-ancestor id a deep downsampling csv belongs to, or None if
    it's a tail csv (extent below the root zoom, so its output spans ancestors)."""
    z, x, y, _ = (int(a) for a in filepath.split("/")[-1].replace("-downsampling.csv", "").split("-"))
    return ancestor_id(z, x, y)


def owned_ancestors(i, n):
    """The strided slice of dirty deep ancestors shard i of n owns (the same split
    run() and matrix() use), so shard-keys and run agree on what a shard touches."""
    ancestors = sorted({a for fp in dirty_filepaths() if (a := shard_ancestor(fp)) is not None})
    return set(ancestors[i::n])


def shard_keys(i, n):
    """Filter store/pmtiles-keys.txt (the R2 listing) to the tiles shard i reads —
    those under its owned ancestors — and write them to store/shard-keys.txt. Lets CI
    pull a shard's slice of the pmtiles store instead of the whole tens-of-GB store
    (a tile's extent zoom is monotonic with content, so everything a shard reads sits
    under one of its ancestors)."""
    owned = owned_ancestors(i, n)
    with open("store/pmtiles-keys.txt") as f:
        keys = [line.strip() for line in f if line.strip()]
    out = []
    for key in keys:
        name = key.split("/")[-1]
        if not name.endswith(".pmtiles"):
            continue
        try:
            z, x, y, _ = (int(a) for a in name.replace(".pmtiles", "").split("-"))
        except ValueError:
            continue
        if ancestor_id(z, x, y) in owned:
            out.append(key)
    with open("store/shard-keys.txt", "w") as f:
        f.write("".join(k + "\n" for k in out))
    print(f"shard {i}/{n}: {len(out)} of {len(keys)} pmtiles selected")


def dirty_filepaths():
    """Sorted -downsampling.csv to (re)build — the aggregate stage's dirty-diff
    lifted to the downsampling coverings (changed tiles, or whose pmtiles is gone)."""
    aggregation_ids = utils.get_aggregation_ids()
    aggregation_id = aggregation_ids[-1]

    dirty_tiles = []
    if len(aggregation_ids) >= 2:
        for name in utils.get_dirty_aggregation_filenames(aggregation_id, aggregation_ids[-2]):
            z, x, y, _ = (int(a) for a in name.replace("-aggregation.csv", "").split("-"))
            dirty_tiles.append(mercantile.Tile(x=x, y=y, z=z))

    def is_dirty(tile, filename):
        if len(aggregation_ids) < 2:
            return True
        if any(tiles_intersect(d, tile) for d in dirty_tiles):
            return True
        return len(glob(f"store/aggregation/{aggregation_ids[-2]}/{filename}")) == 0

    out = []
    for filepath in sorted(glob(f"store/aggregation/{aggregation_id}/*-downsampling.csv")):
        filename = filepath.split("/")[-1]
        z, x, y, _ = (int(a) for a in filename.replace("-downsampling.csv", "").split("-"))
        if is_dirty(mercantile.Tile(x=x, y=y, z=z), filename):
            out.append(filepath)
    return out


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
    with Pool() as pool:
        for child_zoom in sorted(by_child_zoom, reverse=True):
            level = by_child_zoom[child_zoom]
            print(f"child_zoom={child_zoom}: {len(level)} parent(s)", flush=True)
            for m in pool.imap_unordered(run_one, level, chunksize=1):
                missing |= m
                done += 1
                if time.monotonic() - last > 30:
                    print(f"  {done}/{total} parents built", flush=True)
                    last = time.monotonic()

    if missing:
        sample = ", ".join(sorted(missing)[:15])
        print(f"WARN: {len(missing)} referenced pmtiles missing — "
              f"left as gaps in the pyramid: {sample}{' …' if len(missing) > 15 else ''}")


def run(shard=None, tail=False):
    """Build the dirty overview pyramid. Default = everything on one machine.

    Across CI runners (the cut keeps the level barrier inside one machine):
      ``shard=(i, n)`` — only the deep levels under the i-th strided slice of
        SHARD_ROOT_Z ancestors; each shard's subtree is read-closed, so they run
        concurrently with no coordination and push disjoint tiles.
      ``tail=True``    — only the coarse levels whose archives span ancestors
        (extent < SHARD_ROOT_Z); a few cheap global levels, run on one runner once
        every shard has landed (a tail parent reads tiles the shards built)."""
    filepaths = dirty_filepaths()
    if tail:
        filepaths = [fp for fp in filepaths if shard_ancestor(fp) is None]
    elif shard is not None:
        owned = owned_ancestors(*shard)
        filepaths = [fp for fp in filepaths if shard_ancestor(fp) in owned]
    execute(filepaths)


def matrix(maxn):
    """Print the CI deep-shard matrix JSON: <= maxn shards, >= 1, sized to the
    number of distinct ancestors in the dirty deep set."""
    ancestors = {a for fp in dirty_filepaths() if (a := shard_ancestor(fp)) is not None}
    n = min(maxn, max(len(ancestors), 1))
    print(json.dumps([{"i": i, "n": n} for i in range(n)]))


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv == ["cover"]:
        cover()
    elif argv == ["run"]:
        run()
    elif argv[:2] == ["run", "tail"]:
        run(tail=True)
    elif argv[:2] == ["run", "shard"] and len(argv) == 4:
        run(shard=(int(argv[2]), int(argv[3])))
    elif argv[:1] == ["matrix"] and len(argv) == 2:
        matrix(int(argv[1]))
    elif argv[:1] == ["shard-keys"] and len(argv) == 3:
        shard_keys(int(argv[1]), int(argv[2]))
    else:
        sys.exit("usage: downsampling.py <cover | run [shard <i> <n> | tail] | "
                 "matrix <max> | shard-keys <i> <n>>")
