"""Build the overview pyramid below each source's native maxzoom.

Vendored from mapterhorn (BSD-3), covering + run combined. ``cover`` plans which
parent tiles to build per zoom (coalescing aggregation extents); ``run`` assembles
each parent from its 4 children, 2x2-averages to 512x512, and re-encodes. Overviews
keep full Terrarium precision (no per-zoom quantization) but route through
encode.py so the conservative (never-deepen) rounding still applies. WebP is
decoded with imagecodecs (no PIL dependency).

Usage (from pipelines/):  downsampling.py cover   |   downsampling.py run
"""

import os
import shutil
import sys
from glob import glob

import imagecodecs
import mercantile
import numpy as np
from pmtiles.reader import Reader, MmapSource

import encode
import utils

# Child pmtiles the covering referenced but that weren't present on disk (a tile no
# aggregate shard produced, or that didn't sync). Collected so run() can report them.
MISSING_CHILDREN = set()


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
    tile_to_filename = get_tile_to_pmtiles_filename(pmtiles_filenames)
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
                MISSING_CHILDREN.add(filename)
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


def run_one(filepath):
    aggregation_id, filename = filepath.split("/")[-2:]
    if os.path.isfile(filepath.replace("-downsampling.csv", "-downsampling.done")):
        return
    z, x, y, parent_zoom = (int(a) for a in filename.replace("-downsampling.csv", "").split("-"))
    out_folder = utils.get_pmtiles_folder(x, y, z)
    utils.create_folder(out_folder)
    out_filepath = f"{out_folder}/{z}-{x}-{y}-{parent_zoom}.pmtiles"

    extent = mercantile.Tile(x=x, y=y, z=z)
    tmp_folder = filepath.replace("-downsampling.csv", "-tmp")
    utils.create_folder(tmp_folder)
    with open(filepath) as f:
        pmtiles_filenames = [a.strip() for a in f.readlines()[1:]]

    parents = [extent] if z == parent_zoom else list(mercantile.children(extent, zoom=parent_zoom))
    for parent in parents:
        create_tile(parent.x, parent.y, parent.z, tmp_folder, pmtiles_filenames)
    utils.create_archive(tmp_folder, out_filepath)
    shutil.rmtree(tmp_folder)
    utils.run_command(f'touch {filepath.replace("-downsampling.csv", "-downsampling.done")}')


def tiles_intersect(a, b):
    if a == b:
        return True
    if a.z < b.z and mercantile.parent(b, zoom=a.z) == a:
        return True
    if b.z < a.z and mercantile.parent(a, zoom=b.z) == b:
        return True
    return False


def run():
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

    by_child_zoom = {}
    for filepath in sorted(glob(f"store/aggregation/{aggregation_id}/*-downsampling.csv")):
        filename = filepath.split("/")[-1]
        z, x, y, child_zoom = (int(a) for a in filename.replace("-downsampling.csv", "").split("-"))
        if is_dirty(mercantile.Tile(x=x, y=y, z=z), filename):
            by_child_zoom.setdefault(child_zoom, []).append(filepath)

    # high child_zoom first so each level feeds the next.
    for child_zoom in sorted(by_child_zoom, reverse=True):
        for filepath in by_child_zoom[child_zoom]:
            run_one(filepath)

    if MISSING_CHILDREN:
        sample = ", ".join(sorted(MISSING_CHILDREN)[:15])
        print(f"WARN: {len(MISSING_CHILDREN)} referenced pmtiles missing — "
              f"left as gaps in the pyramid: {sample}{' …' if len(MISSING_CHILDREN) > 15 else ''}")


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "cover":
        cover()
    elif len(sys.argv) == 2 and sys.argv[1] == "run":
        run()
    else:
        sys.exit("usage: downsampling.py <cover|run>")
