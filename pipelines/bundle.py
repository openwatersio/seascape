"""Concatenate the single-zoom PMTiles into a planet base + fixed-grid overlays.

Everything at ``child_z <= PLANET_MAX_ZOOM`` (the GEBCO-native base cap, default =
macrotile_z) goes into a complete ``planet.pmtiles`` (z0..cap). Higher-res tiles go
into one ``overlay-{z}-{x}-{y}.pmtiles`` per populated cell of a fixed
``OVERLAY_SPLIT_Z`` grid — each holding every z(cap+1)..zmax tile under that cell,
from whatever sources are there.

The grid is deliberately source-agnostic. Grouping overlays by source made each
archive's size a function of a source's footprint: one deep source whose bbox
swallowed the continent's interior became a catch-all that outgrew a CI runner's
disk, and every new source made it worse. A grid cell is a fixed fraction of the
globe, so new sources add *cells*, never bytes-per-cell; if a cell ever outgrows a
runner, raise OVERLAY_SPLIT_Z. It also gives the serving Worker an O(1) route: the
owning cell is computed from the tile address (no footprint search).

``manifest.json`` records planet metadata + ``overlay`` ({split_z, cells: {cell:
max_zoom}}) + the configured source ids (the viewer's provenance palette).

Pure concat in tile-id order. Bundling everything (incremental rebuild is Phase E3).

Usage (from pipelines/):  bundle.py
"""

import json
import math
import os
import sys
from glob import glob

import mercantile
from pmtiles.tile import zxy_to_tileid, TileType, Compression
from pmtiles.reader import Reader, MmapSource, all_tiles
from pmtiles.writer import Writer

import config
import utils

# Base cap = the GEBCO-native zoom (the planet is complete + overzoomable below it).
PLANET_MAX_ZOOM = int(os.environ.get("PLANET_MAX_ZOOM", str(utils.macrotile_z)))
# The overlay grid zoom. Every overlay tile (child_z > PLANET_MAX_ZOOM) belongs to
# its ancestor cell at this zoom; one archive per populated cell.
SPLIT_Z = int(os.environ.get("OVERLAY_SPLIT_Z", "5"))
# cell_of shifts by (z - SPLIT_Z), so the grid must sit at or above the shallowest
# overlay content zoom — fail fast here, not as a ValueError inside a matrix job.
if SPLIT_Z > PLANET_MAX_ZOOM + 1:
    sys.exit(f"OVERLAY_SPLIT_Z ({SPLIT_Z}) must be <= PLANET_MAX_ZOOM + 1 ({PLANET_MAX_ZOOM + 1})")


def cell_of(z, x, y):
    """The SPLIT_Z grid cell name owning tile (z,x,y), z >= SPLIT_Z."""
    return f"{SPLIT_Z}-{x >> (z - SPLIT_Z)}-{y >> (z - SPLIT_Z)}"


def stem_groups(stem):
    """Group name(s) a pmtiles stem {z}-{x}-{y}-{child_z} contributes to: 'planet'
    for base zooms, else the overlay cell(s) its content tiles live under. Overlay
    parents normally sit at z >= SPLIT_Z (exactly one cell — downsample extents
    stay at z >= child_z - num_overviews); a coarser parent spans its descendant
    cells, and create_archive keeps only each cell's own tiles."""
    z, x, y, child_z = (int(a) for a in stem.split("-"))
    if child_z <= PLANET_MAX_ZOOM:
        return ["planet"]
    if z >= SPLIT_Z:
        return [cell_of(z, x, y)]
    parent = mercantile.Tile(x=x, y=y, z=z)
    return [f"{c.z}-{c.x}-{c.y}" for c in mercantile.children(parent, zoom=SPLIT_Z)]


def archive_filename(name):
    return f"{name}.pmtiles" if name == "planet" else f"overlay-{name}.pmtiles"


def covering_stems(aggregation_id):
    """{z}-{x}-{y}-{child_z} of every tile the current covering builds (aggregate AND
    downsample). The only pmtiles that belong in a bundle. A source's footprint/maxzoom
    shift re-tiles its area to new stems, but the R2 sync has no --delete and the
    dirty-diff only adds work, so the superseded pmtiles lingers; bundling it draws a
    stale tile over the live tiling. Filter every glob/listing through this."""
    return {
        c.split("/")[-1].replace("-aggregation.csv", "").replace("-downsampling.csv", "")
        for c in glob(f"store/aggregation/{aggregation_id}/*-aggregation.csv")
        + glob(f"store/aggregation/{aggregation_id}/*-downsampling.csv")
    }


def group_filepaths(aggregation_id):
    """{'planet': [...], '<z>-<x>-<y>': [...]} — the grid partition of every
    single-zoom pmtiles in the local store."""
    stems = covering_stems(aggregation_id)
    groups = {}
    for fp in sorted(glob("store/pmtiles/*.pmtiles") + glob("store/pmtiles/*/*.pmtiles")):
        stem = fp.split("/")[-1].replace(".pmtiles", "")
        if stem not in stems:  # orphan from a re-tiled covering (see covering_stems)
            continue
        for name in stem_groups(stem):
            groups.setdefault(name, []).append(fp)
    return groups


def read_full_archive(filepath):
    out = {}
    with open(filepath, "r+b") as f:
        reader = Reader(MmapSource(f))
        for tile_tuple, tile_bytes in all_tiles(reader.get_bytes):
            out[zxy_to_tileid(*tile_tuple)] = tile_bytes
    return out


def create_archive(filepaths, name):
    """Concat the single-zoom pmtiles into store/bundle/<archive_filename(name)>.
    For an overlay cell, tiles outside the cell (a coarse parent spanning cells)
    are left to the sibling cells' archives."""
    utils.create_folder("store/bundle")
    out_filepath = f"store/bundle/{archive_filename(name)}"
    min_z, max_z = math.inf, 0
    min_lon, min_lat, max_lon, max_lat = math.inf, math.inf, -math.inf, -math.inf

    with open(out_filepath, "wb") as f1:
        hash_writer = utils.HashWriter(f1)
        writer = Writer(hash_writer)

        tile_ids_and_filepaths = []
        for filepath in filepaths:
            z, x, y, child_z = (int(a) for a in filepath.split("/")[-1].replace(".pmtiles", "").split("-"))
            parent = mercantile.Tile(x=x, y=y, z=z)
            tiles = [parent] if z == child_z else list(mercantile.children(parent, zoom=child_z))
            if name != "planet":
                tiles = [t for t in tiles if cell_of(t.z, t.x, t.y) == name]
            for tile in tiles:
                tile_ids_and_filepaths.append((zxy_to_tileid(tile.z, tile.x, tile.y), filepath))
                max_z, min_z = max(max_z, tile.z), min(min_z, tile.z)
                west, south, east, north = mercantile.bounds(tile)
                min_lon, min_lat = min(min_lon, west), min(min_lat, south)
                max_lon, max_lat = max(max_lon, east), max(max_lat, north)

        last_filepath = None
        tile_id_to_bytes = None
        for tile_id, filepath in sorted(tile_ids_and_filepaths):
            if filepath != last_filepath:
                last_filepath = filepath
                tile_id_to_bytes = read_full_archive(filepath)
            writer.write_tile(tile_id, tile_id_to_bytes[tile_id])

        min_lon_e7, min_lat_e7 = int(min_lon * 1e7), int(min_lat * 1e7)
        max_lon_e7, max_lat_e7 = int(max_lon * 1e7), int(max_lat * 1e7)
        writer.finalize(
            {
                "tile_type": TileType.WEBP, "tile_compression": Compression.NONE,
                "min_zoom": min_z, "max_zoom": max_z,
                "min_lon_e7": min_lon_e7, "min_lat_e7": min_lat_e7,
                "max_lon_e7": max_lon_e7, "max_lat_e7": max_lat_e7,
                "center_zoom": int(0.5 * (min_z + max_z)),
                "center_lon_e7": int(0.5 * (min_lon_e7 + max_lon_e7)),
                "center_lat_e7": int(0.5 * (min_lat_e7 + max_lat_e7)),
            },
            {"attribution": utils.ATTRIBUTION},
        )
        checksum = hash_writer.md5.hexdigest()

    return {"file": archive_filename(name), "size": os.path.getsize(out_filepath),
            "md5sum": checksum, "min_zoom": min_z, "max_zoom": max_z,
            "bbox": [min_lon, min_lat, max_lon, max_lat]}


def attribution():
    """One HTML attribution string crediting every configured source. terrain and
    contours both come from the all-source merged DEM, so they share it.
    Lists all configured sources, not just those a regional BBOX actually touched —
    filter by manifest bbox intersection if a partial build ever needs exact credit."""
    parts = [utils.ATTRIBUTION]
    for sid in config.sources():
        m = config.load_metadata(sid)
        web, name = m.get("website"), m.get("name", sid)
        parts.append(f'<a href="{web}">{name}</a>' if web else name)
    return " | ".join(parts)


def bundle_group(name, filepaths):
    print(f"bundling {archive_filename(name)} ({len(filepaths)} pmtiles)...")
    return create_archive(filepaths, name)


def verify_complete(aggregation_id):
    """Every covering must have produced a pmtiles, or the pyramid has a silent hole.
    create_tile (aggregate) and run_one (downsample) emit one pmtiles per covering, so a
    covering with no pmtiles means a shard never ran or didn't sync — the Worker overzooms
    GEBCO into that hole, so it renders as missing high-zoom terrain. Fail rather than
    publish it. (downsampling.execute catches gaps a *running* shard sees; this catches a
    shard that produced nothing at all, which leaves nothing for execute to notice.)"""
    coverings = covering_stems(aggregation_id)
    have = {fp.split("/")[-1].replace(".pmtiles", "")
            for fp in glob("store/pmtiles/*.pmtiles") + glob("store/pmtiles/*/*.pmtiles")}
    missing = sorted(coverings - have)
    if missing:
        raise SystemExit(
            f"pyramid incomplete: {len(missing)} of {len(coverings)} coverings have no pmtiles "
            f"(a failed/unsynced aggregate or downsample shard) — e.g. "
            f"{', '.join(missing[:15])}{' …' if len(missing) > 15 else ''}")


def _fragment(name, meta):
    """One per-group manifest fragment so a matrix bundle job's metadata survives to
    the merge step. Planet keeps its full metadata; an overlay cell only needs its
    max_zoom (the Worker computes the cell from the tile address — no bbox search)."""
    if name == "planet":
        return {"kind": "planet", **meta}
    return {"kind": "cell", "cell": name, "max_zoom": meta["max_zoom"]}


def _manifest_from_fragments(frags):
    manifest = {"planet": None, "overlay": {"split_z": SPLIT_Z, "cells": {}}}
    for frag in frags:
        if frag["kind"] == "planet":
            manifest["planet"] = {k: v for k, v in frag.items() if k != "kind"}
        else:
            manifest["overlay"]["cells"][frag["cell"]] = frag["max_zoom"]
    manifest["source_ids"] = config.sources()  # the viewer's provenance palette
    manifest["attribution"] = attribution()
    return manifest


def groups_matrix(maxn):
    """Verify the pyramid is whole, then print the CI bundle matrix: <= maxn chunks,
    each a comma-joined strided slice of the group names. The partition rides IN the
    matrix (not re-derived per job from a live R2 listing), so every job bundles the
    exact set this full-store runner saw — the same freeze-the-plan reasoning as the
    aggregate/downsample shards."""
    aggregation_id = utils.get_aggregation_ids()[-1]
    verify_complete(aggregation_id)
    names = sorted(group_filepaths(aggregation_id))
    n = min(maxn, max(len(names), 1))
    print(json.dumps([{"cells": ",".join(names[i::n])} for i in range(n)]))


def group_keys(name):
    """Write store/keys.txt: the R2 pmtiles keys belonging to one group, derived from the
    R2 listing (store/pmtiles-keys.txt) by the same rule group_filepaths uses on local
    files — so a matrix job pulls ONLY its group's slice, never the whole store."""
    aggregation_id = utils.get_aggregation_ids()[-1]
    stems = covering_stems(aggregation_id)
    out = []
    with open("store/pmtiles-keys.txt") as f:
        for key in f:
            key = key.strip()
            if not key.endswith(".pmtiles"):
                continue
            stem = key.split("/")[-1].replace(".pmtiles", "")
            if stem not in stems:  # orphan from a re-tiled covering (see covering_stems)
                continue
            try:
                if name in stem_groups(stem):
                    out.append(key)
            except ValueError:
                continue
    with open("store/keys.txt", "w") as f:
        f.write("".join(k + "\n" for k in out))
    print(f"group {name}: {len(out)} pmtiles selected")


def group(name):
    """Bundle one group from the tiles pulled locally (its slice only) + write its
    fragment. Disk stays bounded by one group's tiles + output, not the whole planet."""
    filepaths = sorted(glob("store/pmtiles/*.pmtiles") + glob("store/pmtiles/*/*.pmtiles"))
    meta = bundle_group(name, filepaths)
    utils.create_folder("store/bundle")
    with open(f"store/bundle/{name}.json", "w") as f:
        json.dump(_fragment(name, meta), f)


def merge():
    """Assemble manifest.json from the per-group fragments the matrix jobs produced."""
    frags = [json.load(open(jf)) for jf in sorted(glob("store/bundle/*.json"))]
    manifest = _manifest_from_fragments(frags)
    utils.create_folder("store/bundle")
    with open("store/bundle/manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"merged manifest: planet + {len(manifest['overlay']['cells'])} overlay cell(s)")


def main():
    """Local / single-runner: bundle every group sequentially, biggest first. The pmtiles
    writer spools each tile to a temp file then copies it into the archive (finalize ~2x's
    a bundle on disk), so building all groups at once piled every temp+final onto one disk
    and blew it at planet scale — CI fans this out in cell chunks instead."""
    aggregation_id = utils.get_aggregation_ids()[-1]
    verify_complete(aggregation_id)
    groups = group_filepaths(aggregation_id)
    frags = []
    for name, filepaths in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        frags.append(_fragment(name, bundle_group(name, filepaths)))
    manifest = _manifest_from_fragments(frags)
    with open("store/bundle/manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"created {len(groups)} bundle(s): {', '.join(sorted(groups))} + manifest.json")


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        main()
    elif a[:1] == ["matrix"] and len(a) == 2:
        groups_matrix(int(a[1]))
    elif a[:1] == ["group-keys"] and len(a) == 2:
        group_keys(a[1])
    elif a[:1] == ["group"] and len(a) == 2:
        group(a[1])
    elif a[:1] == ["merge"]:
        merge()
    else:
        sys.exit("usage: bundle.py [matrix <max> | group-keys <name> | group <name> | merge]")
