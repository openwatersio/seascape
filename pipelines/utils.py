"""Shared helpers: PMTiles archive writing, the aggregation covering store, the
z7-sharded pmtiles layout, terrarium tile encode, priority-grouped source items,
the merge-weight estimate, and the publish-time file hash / toolchain identity.

Vendored from mapterhorn (BSD-3, (c) 2025 mapterhorn; see LICENSE.mapterhorn).
"""

import subprocess
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from glob import glob
import errno
import gzip
import math
import os
import hashlib
import shutil
import tempfile
import time

import numpy as np

from rasterio.warp import transform_bounds
import mercantile
import pmtiles.tile as _pmtiles_tile
import pmtiles.writer as _pmtiles_writer
from pmtiles.tile import zxy_to_tileid, tileid_to_zxy, TileType, Compression
from pmtiles.writer import Writer


# Reproducible pmtiles: the Writer gzip-compresses its directory + metadata with gzip's
# default mtime = wall-clock, so two archives with byte-identical tiles differed in the 4
# mtime bytes of each gzip header — non-reproducible builds (the bundle md5s in
# manifest.json churned every run) and a whole-file diff that couldn't confirm identical
# content. Pin mtime=0 for pmtiles' gzip only (rebind the name in the two library modules,
# not the process-wide gzip — source tarballs etc. keep their real mtimes), so an archive
# is a pure function of its tiles. Both modules reference module-level `gzip.compress` /
# `gzip.decompress` at call time, so rebinding the attribute suffices.
class _DeterministicGzip:
    @staticmethod
    def compress(data, *args, **kwargs):
        kwargs.setdefault("mtime", 0)
        return gzip.compress(data, *args, **kwargs)

    decompress = staticmethod(gzip.decompress)


_pmtiles_tile.gzip = _DeterministicGzip
_pmtiles_writer.gzip = _DeterministicGzip

# macrotile_z is the covering granularity AND the universal tiling floor: every
# source is tiled to at least this zoom, so aggregation-tile zoom never exceeds a
# tile's content zoom so it isn't upsampled. num_overviews bounds aggregation-tile size
# to 2**num_overviews * 512 px. Env-overridable for tests/tuning.
macrotile_z = int(os.environ.get("MACROTILE_Z", "8"))
macrotile_buffer_3857 = 150
num_overviews = int(os.environ.get("NUM_OVERVIEWS", "4"))

ATTRIBUTION = '<a href="https://openwaters.io/charts/seascape#license">© Open Waters</a> '

X_MIN_3857, _, X_MAX_3857, __ = transform_bounds('EPSG:4326', 'EPSG:3857', -180, 0, 180, 0)


@contextmanager
def log_group(title):
    """Collapsible in Actions; the same stage remains readable locally."""
    actions = os.environ.get("GITHUB_ACTIONS") == "true"
    print(f"::group::{title}" if actions else f"── {title} ──", flush=True)
    try:
        yield
    finally:
        if actions:
            print("::endgroup::", flush=True)


def _process_tree(root):
    """Root + descendants from Linux /proc; just the root on other platforms."""
    if not os.path.isdir("/proc"):
        return {root}
    parents = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            fields = open(f"/proc/{name}/stat").read().rsplit(")", 1)[1].split()
            parents[int(name)] = int(fields[1])
        except (FileNotFoundError, PermissionError, ValueError, IndexError):
            pass
    tree = {root}
    while True:
        children = {pid for pid, parent in parents.items() if parent in tree}
        if children <= tree:
            return tree
        tree |= children


def _process_metrics(root):
    ticks = rss = read = written = deleted = 0
    for pid in _process_tree(root):
        try:
            fields = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
            ticks += int(fields[11]) + int(fields[12])
            status = open(f"/proc/{pid}/status").read().splitlines()
            rss += next((int(line.split()[1]) for line in status if line.startswith("VmRSS:")), 0)
            io = dict(line.split(": ", 1) for line in open(f"/proc/{pid}/io").read().splitlines())
            read += int(io.get("read_bytes", 0))
            written += int(io.get("write_bytes", 0))
            for fd in os.listdir(f"/proc/{pid}/fd"):
                path = f"/proc/{pid}/fd/{fd}"
                try:
                    if os.readlink(path).endswith(" (deleted)"):
                        deleted += os.stat(path).st_size
                except (FileNotFoundError, PermissionError, OSError):
                    pass
        except (FileNotFoundError, PermissionError, ValueError, StopIteration):
            pass
    return ticks, rss * 1024, read, written, deleted


def _annotation(kind, title, message):
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{kind} title={title}::{message}", flush=True)
    else:
        print(f"[{title}] {message}", flush=True)


def run_monitored(command, title, output=None):
    """Run a long command with one-minute Actions/local resource heartbeats."""
    interval = int(os.environ.get("BUILD_HEARTBEAT_SECONDS", "60"))
    started = last_time = time.monotonic()
    process = subprocess.Popen(command)
    last = _process_metrics(process.pid)
    while True:
        try:
            rc = process.wait(timeout=interval)
            break
        except subprocess.TimeoutExpired:
            now = time.monotonic()
            current = _process_metrics(process.pid)
            elapsed = now - last_time
            cores = (current[0] - last[0]) / os.sysconf("SC_CLK_TCK") / elapsed if os.path.isdir("/proc") else 0
            out_size = os.path.getsize(output) if output and os.path.exists(output) else 0
            out_dir = os.path.dirname(os.path.abspath(output)) if output else os.getcwd()
            free = shutil.disk_usage(out_dir).free
            tmp_free = shutil.disk_usage(tempfile.gettempdir()).free
            msg = (f"elapsed={int(now-started)}s cpu={cores:.1f} cores rss={current[1]/2**30:.1f}GiB "
                   f"io_read={(current[2]-last[2])/elapsed/2**20:.1f}MiB/s "
                   f"io_write={(current[3]-last[3])/elapsed/2**20:.1f}MiB/s "
                   f"output={out_size/2**30:.1f}GiB deleted_tmp={current[4]/2**30:.1f}GiB "
                   f"free={free/2**30:.0f}GiB tmp_free={tmp_free/2**30:.0f}GiB")
            _annotation("warning" if min(free, tmp_free) < 20 * 2**30 else "notice", title, msg)
            last, last_time = current, now
    if rc:
        raise subprocess.CalledProcessError(rc, command)
    size = os.path.getsize(output) if output and os.path.exists(output) else 0
    _annotation("notice", title, f"complete elapsed={int(time.monotonic()-started)}s output={size/2**30:.1f}GiB")


def vector_scratch(name):
    root = os.environ.get("VECTOR_SCRATCH", tempfile.gettempdir())
    os.makedirs(root, exist_ok=True)
    return os.path.join(root, f"seascape-{os.getpid()}-{name}")


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


def write_if_changed(path, content):
    """Write path only when its content differs — mtimes don't churn on a no-op rewrite,
    so engine provenance sees an unchanged artifact as unchanged (a code-mtime re-prep
    with identical output stops cascading downstream). Temp + rename so a crash never
    leaves a half-written file at the declared path."""
    if os.path.isfile(path):
        with open(path) as f:
            if f.read() == content:
                return False
    with open(path + ".tmp", "w") as f:
        f.write(content)
    os.replace(path + ".tmp", path)
    return True


# Identifying User-Agent for every upstream request (requests here, GDAL /vsicurl via
# GDAL_HTTP_USERAGENT in the Dockerfile): gives data providers something to allowlist
# instead of the default python-requests fingerprint their WAFs throttle.
USER_AGENT = "seascape/1.0 (+https://openwaters.io/charts/seascape)"


def http_download(url, dest, chunk=1 << 20, retries=5):
    '''Stream a URL to dest with requests (handles query-string URLs; no shell).
    Retries with backoff on transient network errors — the public data servers
    (EMODnet, SDFE, …) reset connections under load.'''
    import time
    import requests
    for attempt in range(retries):
        try:
            with requests.get(url, stream=True, timeout=120,
                              headers={'User-Agent': USER_AGENT}) as r:
                r.raise_for_status()
                written = 0
                with open(dest, 'wb') as f:
                    for part in r.iter_content(chunk):
                        written += f.write(part)
                # a truncated body can still look like success (a cut zip keeps its PK magic)
                expected = r.headers.get('Content-Length', '')
                if expected.isdigit() and r.headers.get('Content-Encoding', 'identity') == 'identity' \
                        and written != int(expected):
                    raise requests.exceptions.RequestException(
                        f"truncated body: {written} of {expected} bytes")
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


def get_pmtiles_folder(x, y, z):
    if z < 7:
        return 'store/pmtiles'
    if z == 7:
        return f'store/pmtiles/{z}-{x}-{y}'
    parent = mercantile.parent(mercantile.Tile(x=x, y=y, z=z), zoom=7)
    return f'store/pmtiles/{parent.z}-{parent.x}-{parent.y}'


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
            prio[source] = config.source_property(source, 'priority', 0)
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


# ── merge-weight estimate (the mosaic_tile reservation seed) ─────────────────────────────────
# build.smk's mosaic_tile / terrain_render rules read `weight(stem)` to seed each job's mem_gb
# reservation. A deterministic estimate from the tile geometry — it only needs to ORDER and BOUND
# tiles, not be exact; the benchmarks re-fit it.

# The halo (buffer) each read carries for smoothing continuity is a small additive px term next to
# the 2**(child_z-macrotile_z)*512 core; one constant is close enough for the merge and terrain
# reads alike.
HALO_PX = 64

# Peak-over-rest multiplier: a merge holds roughly the merged array + reprojected sources + masks
# at once. Env-tunable (AGG_MEM_FACTOR); build.smk passes a lower factor for the merge job (the
# vector forks are separate jobs, not held in the merge's memory).
DEFAULT_FACTOR = float(os.environ.get("AGG_MEM_FACTOR", "4"))


def weight(stem, budget_gb=0, factor=DEFAULT_FACTOR):
    """Estimated peak GB for a tile `z-x-y-cz` (z = macrotile_z, cz = child_z), rounded up to a
    whole GB and floored at 1. Monotonic in child_z (each level quadruples the pixel area). When a
    budget is given, the weight is CLAMPED to it (with a warning): a tile heavier than the whole
    budget must still be admittable alone."""
    z, _x, _y, cz = (int(a) for a in stem.split("-"))
    side_px = (2 ** (cz - z)) * 512 + 2 * HALO_PX
    base_gb = side_px * side_px * 4 / 1e9
    w = max(1, math.ceil(base_gb * factor))
    if budget_gb and w > budget_gb:
        print(f"weight: tile {stem} weight {w} GB exceeds budget {budget_gb} GB — clamping to "
              f"{budget_gb} (runs alone; better one-at-a-time than deadlock)", flush=True)
        w = budget_gb
    return w


# ── publish-time file hash + toolchain identity ──────────────────────────────────────────────────
# The mosaic publish content-addresses the finished plain COGs by hashing their bytes; build.smk's
# mosaic_tile carries the toolchain tag as a rerun param. Both are R2-agnostic — no bucket names here.

@lru_cache(maxsize=1)
def toolchain():
    """The toolchain identity: TOOLCHAIN (the workflow passes the GHCR image tag, which pins one
    GDAL/tippecanoe) when set, else the local GDAL version so a laptop stays honest about GDAL
    skew. A GDAL bump correctly invalidates the world."""
    t = os.environ.get("TOOLCHAIN")
    if t:
        return t
    for cmd in (["gdal-config", "--version"], ["gdalinfo", "--version"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True)
        except (FileNotFoundError, OSError):
            continue
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().splitlines()[0]
    return "no-toolchain"


@lru_cache(maxsize=None)
def _file_hash_cached(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def file_hash(path):
    """A content hash of a local file (its bytes) — the publish-time R2 name discriminator. None
    for a missing or /vsi (remote) path. Cached per absolute path — inputs don't change under a
    running build."""
    if path is None or path.startswith("/vsi") or not os.path.isfile(path):
        return None
    return _file_hash_cached(os.path.abspath(path))


def publish(tmp_path, final_path):
    """Publish a fully-written temp file atomically, including across filesystems (NVMe scratch and
    the persistent store are separate mounts in production). os.replace is the fast path; on EXDEV,
    copy to a temp beside the final path first, then rename there — the final name only ever appears
    complete, and a crash leaves a temp no freshness check considers."""
    os.makedirs(os.path.dirname(final_path), exist_ok=True)
    try:
        os.replace(tmp_path, final_path)
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
        destination_tmp = final_path + ".tmp"
        shutil.copyfile(tmp_path, destination_tmp)
        os.replace(destination_tmp, final_path)
        os.remove(tmp_path)


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
