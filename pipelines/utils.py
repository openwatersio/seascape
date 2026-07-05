"""Shared helpers: PMTiles archive writing, the aggregation covering store, the
z7-sharded pmtiles layout, terrarium tile encode, and the dirty-diff for
incremental rebuilds.

Vendored from mapterhorn (BSD-3, (c) 2025 mapterhorn; see LICENSE.mapterhorn).
"""

import subprocess
from pathlib import Path
from glob import glob
import math
import os
import hashlib

import numpy as np

from rasterio.warp import transform_bounds
import mercantile
from pmtiles.tile import zxy_to_tileid, tileid_to_zxy, TileType, Compression
from pmtiles.writer import Writer

# macrotile_z is the covering granularity AND the universal tiling floor: every
# source is tiled to at least this zoom, so aggregation-tile zoom never exceeds a
# tile's content zoom so it isn't upsampled. num_overviews bounds aggregation-tile size
# to 2**num_overviews * 512 px. Env-overridable for tests/tuning.
macrotile_z = int(os.environ.get("MACROTILE_Z", "8"))
macrotile_buffer_3857 = 150
num_overviews = int(os.environ.get("NUM_OVERVIEWS", "4"))

ATTRIBUTION = '<a href="https://openwaters.io/charts/seascape#license">© Open Water Software, LLC</a> '

X_MIN_3857, _, X_MAX_3857, __ = transform_bounds('EPSG:4326', 'EPSG:3857', -180, 0, 180, 0)


def run_command(command, silent=True, env=None):
    """Run a shell command and return (stdout, stderr). Raise on non-zero exit — don't
    silently swallow failures."""
    if env is None:
        env = os.environ.copy()
    if not silent:
        print(command)
    p = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    stdout, stderr = p.communicate()
    out, err = stdout.decode(), stderr.decode()
    if p.returncode != 0:
        raise RuntimeError(f"command failed (exit {p.returncode}): {command}\n{err}")
    if err and not silent:
        print(err)
    if out and not silent:
        print(out)
    return out, err


def create_folder(path):
    Path(path).mkdir(parents=True, exist_ok=True)


def http_download(url, dest, chunk=1 << 20, retries=5):
    '''Stream a URL to dest with requests (handles query-string URLs; no shell).
    Retries with backoff on transient network errors — the public data servers
    (EMODnet, SDFE, …) reset connections under load.'''
    import time
    import requests
    for attempt in range(retries):
        try:
            with requests.get(url, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(dest, 'wb') as f:
                    for part in r.iter_content(chunk):
                        f.write(part)
            return
        except requests.exceptions.RequestException as e:
            if attempt == retries - 1:
                raise
            wait = 2 ** attempt
            print(f"  download {url} failed ({e}); retry {attempt + 1}/{retries - 1} in {wait}s")
            time.sleep(wait)


def get_aggregation_ids():
    '''returns aggregation ids ordered from oldest to newest'''
    return list(sorted([path.split('/')[-1] for path in glob('store/aggregation/*')]))


def save_terrarium_tile(data, filepath, conservative=True):
    '''Encode a 512x512 elevation array as a lossless WebP terrarium tile.

    Zoom is parsed from the filename (``{z}-{x}-{y}.webp``); the per-zoom
    quantization + conservative bathymetry rounding live in encode.py.
    '''
    import imagecodecs
    import encode

    z = int(filepath.split('/')[-1].split('-')[0])
    rgb = encode.encode(data, z, conservative=conservative)
    with open(filepath, 'wb') as f:
        f.write(imagecodecs.webp_encode(rgb, lossless=True))


def create_archive(tmp_folder, out_filepath):
    with open(out_filepath, 'wb') as f1:
        writer = Writer(f1)
        min_z, max_z = math.inf, 0
        min_lon, min_lat = math.inf, math.inf
        max_lon, max_lat = -math.inf, -math.inf

        tile_ids = []
        for filepath in glob(f'{tmp_folder}/*.webp'):
            filename = filepath.split('/')[-1]
            z, x, y = [int(a) for a in filename.replace('.webp', '').split('-')]
            tile_ids.append(zxy_to_tileid(z=z, x=x, y=y))
        tile_ids = sorted(tile_ids)

        for tile_id in tile_ids:
            z, x, y = tileid_to_zxy(tile_id)
            filepath = f'{tmp_folder}/{z}-{x}-{y}.webp'
            with open(filepath, 'rb') as f2:
                writer.write_tile(tile_id, f2.read())
            max_z, min_z = max(max_z, z), min(min_z, z)
            west, south, east, north = mercantile.bounds(x, y, z)
            min_lon, min_lat = min(min_lon, west), min(min_lat, south)
            max_lon, max_lat = max(max_lon, east), max(max_lat, north)

        min_lon_e7, min_lat_e7 = int(min_lon * 1e7), int(min_lat * 1e7)
        max_lon_e7, max_lat_e7 = int(max_lon * 1e7), int(max_lat * 1e7)

        writer.finalize(
            {
                'tile_type': TileType.WEBP,
                'tile_compression': Compression.NONE,
                'min_zoom': min_z,
                'max_zoom': max_z,
                'min_lon_e7': min_lon_e7,
                'min_lat_e7': min_lat_e7,
                'max_lon_e7': max_lon_e7,
                'max_lat_e7': max_lat_e7,
                'center_zoom': int(0.5 * (min_z + max_z)),
                'center_lon_e7': int(0.5 * (min_lon_e7 + max_lon_e7)),
                'center_lat_e7': int(0.5 * (min_lat_e7 + max_lat_e7)),
            },
            {'attribution': ATTRIBUTION},
        )


def get_aggregation_item_string(aggregation_id, filename):
    filepath = f'store/aggregation/{aggregation_id}/{filename}'
    if not os.path.isfile(filepath):
        return None
    with open(filepath) as f:
        return ''.join([l.strip() for l in f.readlines()]).strip()


def get_dirty_aggregation_filenames(current_aggregation_id, last_aggregation_id):
    filepaths = sorted(glob(f'store/aggregation/{current_aggregation_id}/*-aggregation.csv'))
    # FORCE_REBUILD treats every tile as changed — the escape hatch for pipeline-code
    # changes (smoothing, contour generation), which the source-coverage diff can't see.
    if last_aggregation_id is None or os.environ.get("FORCE_REBUILD"):
        return [filepath.split('/')[-1] for filepath in filepaths]
    dirty_filenames = []
    for filepath in filepaths:
        filename = filepath.split('/')[-1]
        current = get_aggregation_item_string(current_aggregation_id, filename)
        last = get_aggregation_item_string(last_aggregation_id, filename)
        if current != last:
            dirty_filenames.append(filename)
    return dirty_filenames


def get_pmtiles_folder(x, y, z):
    if z < 7:
        return 'store/pmtiles'
    if z == 7:
        return f'store/pmtiles/{z}-{x}-{y}'
    parent = mercantile.parent(mercantile.Tile(x=x, y=y, z=z), zoom=7)
    return f'store/pmtiles/{parent.z}-{parent.x}-{parent.y}'


def existing_pmtiles():
    """Basenames of pmtiles already in the store. From a CI-provided listing
    (store/pmtiles-keys.txt = the R2 keys) when present, else a local scan. Shared by the
    aggregate and downsample dirty-diffs so both agree on what's actually built — a
    covering whose pmtiles isn't here is rebuilt, which is how a dropped/unsynced shard's
    hole self-heals instead of staying 'clean' forever."""
    keyfile = "store/pmtiles-keys.txt"
    if os.path.isfile(keyfile):
        with open(keyfile) as f:
            return {line.strip().split("/")[-1] for line in f if line.strip()}
    return {p.split("/")[-1] for p in glob("store/pmtiles/**/*.pmtiles", recursive=True)}


def pmtiles_mtimes():
    """Basename -> last-modified epoch seconds, for the overview staleness check. From a
    prebuilt store/pmtiles-mtimes.txt listing (``<YYYY-MM-DD HH:MM:SS>\\t<key>`` per line) when
    present, else each pmtiles' own file mtime. The downsample dirty-diff rebuilds any overview
    older than a child it averages: a child rebuilt by a later run/self-heal never re-dirtied its
    parent, so the coarse overview kept averaging the old child and went stale. Empty/absent =>
    {}, and the diff falls back to its other signals (so a missing listing never marks all stale)."""
    import calendar
    import time

    path = "store/pmtiles-mtimes.txt"
    out = {}
    if os.path.isfile(path):
        with open(path) as f:
            for line in f:
                line = line.rstrip("\n")
                if "\t" not in line:
                    continue
                ts, key = line.split("\t", 1)
                try:
                    out[key.split("/")[-1]] = calendar.timegm(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
                except ValueError:
                    continue
        return out
    for p in glob("store/pmtiles/**/*.pmtiles", recursive=True):
        out[p.split("/")[-1]] = os.path.getmtime(p)
    return out


def get_grouped_source_items(filepath):
    '''Group source items per (priority, maxzoom, source), most-important first. Merge
    order is priority DESC then maxzoom DESC: a source with metadata `priority` > 0 (e.g.
    S-102, already on a chart datum) wins the overlap even over a finer source; ties fall
    back to native resolution. This sets merge ORDER only — build resolution is the finest
    source's (see aggregation_reproject), so a coarse high-priority source can't lower the grid.'''
    import config
    with open(filepath) as f:
        lines = f.readlines()[1:]  # skip header
    prio = {}
    line_tuples = []
    for line in lines:
        source, filename, maxzoom = line.strip().split(',')
        if source not in prio:
            prio[source] = config.load_metadata(source).get('priority', 0)
        line_tuples.append((-prio[source], -int(maxzoom), source, filename))
    line_tuples = sorted(line_tuples)

    grouped_source_items = []
    last_signature = line_tuples[0][:3]  # (-priority, -maxzoom, source)
    current_group = []
    for line_tuple in line_tuples:
        signature = line_tuple[:3]
        if signature != last_signature:
            grouped_source_items.append(current_group)
            current_group = []
            last_signature = signature
        current_group.append({
            'maxzoom': -line_tuple[1],
            'source': line_tuple[2],
            'filename': line_tuple[3],
        })
    grouped_source_items.append(current_group)
    return grouped_source_items


class HashWriter:
    def __init__(self, f):
        self.f = f
        self.md5 = hashlib.md5()

    def write(self, data):
        self.md5.update(data)
        return self.f.write(data)

    def tell(self):
        return self.f.tell()

    def flush(self):
        return self.f.flush()

    def close(self):
        return self.f.close()
