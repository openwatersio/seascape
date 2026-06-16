"""Concatenate the single-zoom PMTiles into a planet base + per-source overlays.

Everything at ``child_z <= PLANET_MAX_ZOOM`` (the GEBCO-native base cap, default =
macrotile_z) goes into a complete ``planet.pmtiles`` (z0..cap). Higher-res tiles
go into one ``<source>.pmtiles`` per **dominant high-res source** (e.g.
``cudem_ne.pmtiles``, z(cap+1)..source-max), so each source is independently
publishable. A ``manifest.json`` records planet + per-source coverage so the
serving Worker can resolve regional-vs-planet and overzoom the base on miss (see
SERVING / the plan's §Serving architecture).

Pure concat in tile-id order. Bundling everything (incremental rebuild is Phase E3).

Usage (from pipelines/):  bundle.py
"""

import json
import math
import os
from glob import glob

import mercantile
from pmtiles.tile import zxy_to_tileid, TileType, Compression
from pmtiles.reader import Reader, MmapSource, all_tiles
from pmtiles.writer import Writer

import utils

# Base cap = the GEBCO-native zoom (the planet is complete + overzoomable below it).
PLANET_MAX_ZOOM = int(os.environ.get("PLANET_MAX_ZOOM", str(utils.macrotile_z)))


def high_res_sources(aggregation_id):
    """{source_id: {'bbox': [w,s,e,n], 'max_zoom': int}} for sources that own
    regional (child_z > PLANET_MAX_ZOOM) aggregation tiles. The owner of a tile is
    its deepest source (maxzoom == child_z), lex-first on a tie."""
    sources = {}
    for csv in glob(f"store/aggregation/{aggregation_id}/*-aggregation.csv"):
        z, x, y, child_z = (int(a) for a in csv.split("/")[-1].replace("-aggregation.csv", "").split("-"))
        if child_z <= PLANET_MAX_ZOOM:
            continue
        with open(csv) as f:
            rows = [line.strip().split(",") for line in f.readlines()[1:]]
        owner = sorted(s for s, fn, mz in rows if int(mz) == child_z)[0]
        w, s, e, n = mercantile.bounds(x, y, z)
        info = sources.setdefault(owner, {"bbox": [math.inf, math.inf, -math.inf, -math.inf], "max_zoom": 0})
        b = info["bbox"]
        b[0], b[1], b[2], b[3] = min(b[0], w), min(b[1], s), max(b[2], e), max(b[3], n)
        info["max_zoom"] = max(info["max_zoom"], child_z)
    return sources


def assign_source(z, x, y, sources):
    """Pick the deepest high-res source whose footprint the tile extent overlaps."""
    w, s, e, n = mercantile.bounds(x, y, z)
    best, best_mz = None, -1
    for src, info in sources.items():
        bw, bs, be, bn = info["bbox"]
        if w < be and e > bw and s < bn and n > bs and info["max_zoom"] > best_mz:
            best, best_mz = src, info["max_zoom"]
    return best


def group_filepaths(aggregation_id):
    """{'planet': [...], '<source>': [...]} grouping every single-zoom pmtiles."""
    sources = high_res_sources(aggregation_id)
    groups = {}
    for fp in sorted(glob("store/pmtiles/*.pmtiles") + glob("store/pmtiles/*/*.pmtiles")):
        z, x, y, child_z = (int(a) for a in fp.split("/")[-1].replace(".pmtiles", "").split("-"))
        name = "planet" if child_z <= PLANET_MAX_ZOOM else (assign_source(z, x, y, sources) or "planet")
        groups.setdefault(name, []).append(fp)
    return groups, sources


def read_full_archive(filepath):
    out = {}
    with open(filepath, "r+b") as f:
        reader = Reader(MmapSource(f))
        for tile_tuple, tile_bytes in all_tiles(reader.get_bytes):
            out[zxy_to_tileid(*tile_tuple)] = tile_bytes
    return out


def create_archive(filepaths, name):
    utils.create_folder("store/bundle")
    out_filepath = f"store/bundle/{name}.pmtiles"
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
            for tile in tiles:
                tile_ids_and_filepaths.append((zxy_to_tileid(tile.z, tile.x, tile.y), filepath))
            max_z, min_z = max(max_z, child_z), min(min_z, child_z)
            west, south, east, north = mercantile.bounds(x, y, z)
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

    return {"file": f"{name}.pmtiles", "size": os.path.getsize(out_filepath), "md5sum": checksum,
            "min_zoom": min_z, "max_zoom": max_z,
            "bbox": [min_lon, min_lat, max_lon, max_lat]}


def main():
    aggregation_id = utils.get_aggregation_ids()[-1]
    groups, _ = group_filepaths(aggregation_id)
    manifest = {"planet": None, "sources": []}
    for name, filepaths in groups.items():
        print(f"bundling {name} ({len(filepaths)} pmtiles)...")
        meta = create_archive(filepaths, name)
        if name == "planet":
            manifest["planet"] = meta
        else:
            manifest["sources"].append({"id": name, **meta})
    # deepest first so the Worker picks the highest-res overlay where they overlap.
    manifest["sources"].sort(key=lambda s: -s["max_zoom"])
    with open("store/bundle/manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"created {len(groups)} bundle(s): {', '.join(groups)} + manifest.json")


if __name__ == "__main__":
    main()
