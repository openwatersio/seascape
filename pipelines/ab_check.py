"""Decoded A/B gate between two builds' published artifacts.

Compares what a *renderer* would see, not the archive bytes: build A's ``build/<sha>/`` against
build B's ``build/<sha>/`` (both live at ``https://data.openwaters.io/bathymetry/build/<sha>/`` or a
local dir). Byte-diffing the pmtiles is useless here — two equivalent encodings can differ in bytes,
so the question is whether the *decoded* elevation / vector content drifted, not whether the
encodings differ.

Two gates, each over a seeded, stratified sample of tile addresses:

  terrain — Terrarium WebP -> elevation (metres) via encode.decode. Deep ocean (tile mean
    depth < -1000 m) is GEBCO-stable in both lanes, so its p95 mean-delta is the drift-tolerant
    gate: it must stay under DEEP_OCEAN_P95_THRESHOLD_M or the run exits nonzero. Shallow / land
    ("other") is reported but never gates — it legitimately differs where a hi-res source or the
    depth-aware quantization enters.

  vector — MVT layers (contours / soundings / depare). With ``mapbox_vector_tile`` installed we
    report per-layer feature-count deltas; without it (the current default — it is NOT a repo dep)
    we fall back to a structural comparison: per-tile payload sizes plus the tileset's declared
    ``vector_layers``. Informational (exit 0) unless a layer present in A is entirely absent from B.

Tile addresses are enumerated from each archive's DIRECTORY only (never fetching tile payloads),
so A-only / B-only counts are exact and the HTTP path stays cheap — a handful of range reads for
the directory pages, then one fetch per sampled tile actually decoded.

Usage (from pipelines/):
  ab_check.py terrain <baseA> <baseB> [--samples N] [--seed S]
  ab_check.py vector  <baseA> <baseB> [--samples N] [--seed S]
  ab_check.py --check                 # self-test on synthetic pmtiles pairs
"""

import json
import os
import sys
import urllib.request
from collections import OrderedDict

import numpy as np
from pmtiles.reader import (
    MmapSource,
    Reader,
    deserialize_directory,
    deserialize_header,
)
from pmtiles.tile import Compression, tileid_to_zxy

import encode

# GEBCO-stable deep water is the drift-tolerant gate: its p95 per-tile mean-delta (metres) may
# not exceed this, or the terrain gate fails. 2 m is well under one ramp band at these depths.
DEEP_OCEAN_DEPTH_M = -1000.0
DEEP_OCEAN_P95_THRESHOLD_M = 2.0
# Per-tile "% pixels differing" uses a 1 m floor — below that is sub-band quantization noise.
PIXEL_DIFF_M = 1.0

DEFAULT_SAMPLES = 200
DEFAULT_SEED = 1337
# The planet archive is z0..PLANET_MAX_ZOOM; the terrain planet stratum is capped here so it stays
# "z0-8" regardless of a build's configured base cap.
PLANET_STRATUM_ZMAX = 8


# ── seekable sources ────────────────────────────────────────────────────────────
# The pmtiles Reader is get_bytes(offset, length) all the way down. Local dirs mmap; URLs issue
# range requests with a small LRU so the repeated header/root-directory reads Reader.get does per
# tile don't re-fetch.


class RangeSource:
    """A get_bytes(offset, length) over HTTP range requests with an LRU of fetched ranges."""

    def __init__(self, url, capacity=128):
        self.url = url
        self.capacity = capacity
        self._cache = OrderedDict()

    def __call__(self, offset, length):
        key = (offset, length)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        req = urllib.request.Request(
            self.url, headers={"Range": f"bytes={offset}-{offset + length - 1}"}
        )
        with urllib.request.urlopen(req) as resp:
            data = resp.read()
        self._cache[key] = data
        self._cache.move_to_end(key)
        if len(self._cache) > self.capacity:
            self._cache.popitem(last=False)
        return data


class Store:
    """One build's ``build/<sha>/`` — a URL prefix or a local dir. Opens Readers lazily and holds
    local file handles open (MmapSource maps a live fd)."""

    def __init__(self, base):
        self.base = base.rstrip("/")
        self.is_url = base.startswith(("http://", "https://"))
        self._readers = {}
        self._files = []

    def manifest(self):
        return json.loads(self._read_full("manifest.json"))

    def _read_full(self, name):
        if self.is_url:
            with urllib.request.urlopen(f"{self.base}/{name}") as resp:
                return resp.read()
        with open(os.path.join(self.base, name), "rb") as f:
            return f.read()

    def reader(self, filename):
        if filename not in self._readers:
            if self.is_url:
                self._readers[filename] = Reader(RangeSource(f"{self.base}/{filename}"))
            else:
                f = open(os.path.join(self.base, filename), "rb")
                self._files.append(f)
                self._readers[filename] = Reader(MmapSource(f))
        return self._readers[filename]


def _tile_ids(get_bytes):
    """Every (z, x, y) in an archive, read from its directory pages ONLY — no tile payload is
    fetched, so the HTTP path costs a few range reads regardless of archive size."""
    header = deserialize_header(get_bytes(0, 127))

    def walk(dir_offset, dir_length):
        for entry in deserialize_directory(get_bytes(dir_offset, dir_length)):
            if entry.run_length > 0:
                for i in range(entry.run_length):
                    yield tileid_to_zxy(entry.tile_id + i)
            else:
                yield from walk(header["leaf_directory_offset"] + entry.offset, entry.length)

    yield from walk(header["root_offset"], header["root_length"])


def _decode_elevation(payload):
    """Terrarium WebP payload -> elevation array (metres)."""
    import imagecodecs

    return encode.decode(imagecodecs.webp_decode(payload).astype("float64"))


# ── terrain gate ────────────────────────────────────────────────────────────────


def _terrain_pool(store_a, store_b):
    """The sampling universe, stratified. Returns (planet_pool, overlay_pool, sets) where each pool
    is a sorted list of routing keys and ``sets`` maps (filename, side) -> id set for O(1) presence
    checks. planet keys are ('planet', z, x, y); overlay keys carry the cell's archive filename."""
    ma, mb = store_a.manifest(), store_b.manifest()

    planet_file = ma["planet"]["file"]
    planet_zmax = min(PLANET_STRATUM_ZMAX, ma["planet"]["max_zoom"], mb["planet"]["max_zoom"])
    a_planet = {t for t in _tile_ids(store_a.reader(planet_file).get_bytes) if t[0] <= planet_zmax}
    b_planet = {t for t in _tile_ids(store_b.reader(planet_file).get_bytes) if t[0] <= planet_zmax}
    planet_pool = sorted(("planet", planet_file, *t) for t in (a_planet | b_planet))

    sets = {(planet_file, "a"): a_planet, (planet_file, "b"): b_planet}
    overlay_zmin = min(ma["planet"]["max_zoom"], mb["planet"]["max_zoom"]) + 1
    cells_a, cells_b = ma["overlay"]["cells"], mb["overlay"]["cells"]
    overlay_pool = []
    for cell in sorted(set(cells_a) & set(cells_b)):
        cell_zmax = min(cells_a[cell], cells_b[cell])
        fn = f"overlay-{cell}.pmtiles"
        a_ids = {t for t in _tile_ids(store_a.reader(fn).get_bytes) if overlay_zmin <= t[0] <= cell_zmax}
        b_ids = {t for t in _tile_ids(store_b.reader(fn).get_bytes) if overlay_zmin <= t[0] <= cell_zmax}
        sets[(fn, "a")], sets[(fn, "b")] = a_ids, b_ids
        overlay_pool.extend(("overlay", fn, *t) for t in (a_ids | b_ids))
    overlay_pool.sort()
    return planet_pool, overlay_pool, sets


def _sample(rng, pool, k):
    return pool if len(pool) <= k else rng.sample(pool, k)


def compare_terrain(base_a, base_b, samples=DEFAULT_SAMPLES, seed=DEFAULT_SEED):
    """Decode + compare a stratified sample of Terrarium tiles present in both builds. Returns a
    result dict; the CLI prints it and sets the exit code from ``gate_fail``."""
    import random

    store_a, store_b = Store(base_a), Store(base_b)
    planet_pool, overlay_pool, sets = _terrain_pool(store_a, store_b)
    rng = random.Random(seed)

    n_overlay = samples // 2 if overlay_pool else 0
    n_planet = samples - n_overlay
    chosen = _sample(rng, planet_pool, n_planet) + _sample(rng, overlay_pool, n_overlay)

    tiles, a_only, b_only = [], 0, 0
    for _kind, fn, z, x, y in chosen:
        in_a = (z, x, y) in sets[(fn, "a")]
        in_b = (z, x, y) in sets[(fn, "b")]
        if not (in_a and in_b):
            a_only += in_a and not in_b
            b_only += in_b and not in_a
            continue
        ea = _decode_elevation(store_a.reader(fn).get(z, x, y))
        eb = _decode_elevation(store_b.reader(fn).get(z, x, y))
        if ea.shape != eb.shape:
            tiles.append({"coord": (z, x, y), "shape_mismatch": True})
            continue
        delta = np.abs(ea - eb)
        mean_depth = float(ea.mean())
        tiles.append({
            "coord": (z, x, y),
            "mean_abs": float(delta.mean()),
            "max_abs": float(delta.max()),
            "pct_gt1": float((delta > PIXEL_DIFF_M).mean() * 100.0),
            "mean_depth": mean_depth,
            "deep": mean_depth < DEEP_OCEAN_DEPTH_M,
        })

    deep = [t["mean_abs"] for t in tiles if t.get("deep")]
    other = [t["mean_abs"] for t in tiles if "mean_abs" in t and not t.get("deep")]
    deep_p95 = float(np.percentile(deep, 95)) if deep else 0.0
    return {
        "n_planet_pool": len(planet_pool),
        "n_overlay_pool": len(overlay_pool),
        "n_compared": sum(1 for t in tiles if "mean_abs" in t),
        "n_shape_mismatch": sum(1 for t in tiles if t.get("shape_mismatch")),
        "a_only": a_only,
        "b_only": b_only,
        "deep_n": len(deep),
        "deep_median": float(np.median(deep)) if deep else 0.0,
        "deep_p95": deep_p95,
        "other_n": len(other),
        "other_median": float(np.median(other)) if other else 0.0,
        "other_p95": float(np.percentile(other, 95)) if other else 0.0,
        "tiles": tiles,
        "gate_fail": deep_p95 > DEEP_OCEAN_P95_THRESHOLD_M,
    }


def _print_terrain(res):
    print(f"terrain A/B: {res['n_compared']} tiles compared "
          f"(planet pool {res['n_planet_pool']}, overlay pool {res['n_overlay_pool']})")
    if res["n_shape_mismatch"]:
        print(f"  shape mismatch on {res['n_shape_mismatch']} tile(s)")
    worst = sorted((t for t in res["tiles"] if "mean_abs" in t),
                   key=lambda t: t["mean_abs"], reverse=True)[:5]
    for t in worst:
        z, x, y = t["coord"]
        print(f"  worst {z}/{x}/{y}: mean|Δ|={t['mean_abs']:.3f} m  max|Δ|={t['max_abs']:.3f} m  "
              f"{t['pct_gt1']:.1f}% px >{PIXEL_DIFF_M:g}m  depth {t['mean_depth']:.0f} m"
              f"{'  [deep]' if t['deep'] else ''}")
    print(f"  deep ocean ({res['deep_n']} tiles):  median {res['deep_median']:.3f} m  "
          f"p95 {res['deep_p95']:.3f} m  (gate threshold {DEEP_OCEAN_P95_THRESHOLD_M:g} m)")
    print(f"  other ({res['other_n']} tiles):       median {res['other_median']:.3f} m  "
          f"p95 {res['other_p95']:.3f} m  (informational)")
    print(f"  A-only tiles: {res['a_only']}   B-only tiles: {res['b_only']}")
    print("  GATE: " + ("FAIL — deep-ocean drift exceeds threshold" if res["gate_fail"] else "PASS"))


# ── vector gate ─────────────────────────────────────────────────────────────────


def _has_mvt():
    try:
        import mapbox_vector_tile  # noqa: F401

        return True
    except ImportError:
        return False


def _vector_layers(reader):
    """The tileset's declared layer ids (tippecanoe writes ``vector_layers`` into the metadata,
    sometimes nested under a ``json`` string)."""
    md = reader.metadata()
    vl = md.get("vector_layers")
    if vl is None and isinstance(md.get("json"), str):
        vl = json.loads(md["json"]).get("vector_layers")
    return {layer["id"] for layer in (vl or [])}


def _decode_mvt(payload, compressed):
    import gzip

    import mapbox_vector_tile

    if compressed:
        payload = gzip.decompress(payload)
    return mapbox_vector_tile.decode(payload)


def compare_vector(base_a, base_b, samples=DEFAULT_SAMPLES, seed=DEFAULT_SEED):
    import random

    store_a, store_b = Store(base_a), Store(base_b)
    ra, rb = store_a.reader("vector.pmtiles"), store_b.reader("vector.pmtiles")
    a_ids, b_ids = set(_tile_ids(ra.get_bytes)), set(_tile_ids(rb.get_bytes))
    pool = sorted(a_ids | b_ids)
    rng = random.Random(seed)
    chosen = _sample(rng, pool, samples)

    layers_a, layers_b = _vector_layers(ra), _vector_layers(rb)
    mvt = _has_mvt()
    compressed_a = ra.header()["tile_compression"] == Compression.GZIP
    compressed_b = rb.header()["tile_compression"] == Compression.GZIP

    a_only = b_only = 0
    size_rel = []                 # structural: relative payload-size delta
    layer_rel = {}                # mvt: per-layer relative feature-count deltas
    seen_layers_a, seen_layers_b = set(), set()
    for z, x, y in chosen:
        in_a, in_b = (z, x, y) in a_ids, (z, x, y) in b_ids
        if not (in_a and in_b):
            a_only += in_a and not in_b
            b_only += in_b and not in_a
            continue
        pa, pb = ra.get(z, x, y), rb.get(z, x, y)
        if mvt:
            da, db = _decode_mvt(pa, compressed_a), _decode_mvt(pb, compressed_b)
            seen_layers_a.update(da)
            seen_layers_b.update(db)
            for layer in set(da) | set(db):
                ca = len(da.get(layer, {}).get("features", []))
                cb = len(db.get(layer, {}).get("features", []))
                layer_rel.setdefault(layer, []).append(abs(cb - ca) / max(ca, 1))
        else:
            size_rel.append(abs(len(pb) - len(pa)) / max(len(pa), 1))

    if mvt:
        absent = sorted(seen_layers_a - seen_layers_b)
    else:
        absent = sorted(layers_a - layers_b)
    return {
        "mvt": mvt,
        "n_pool": len(pool),
        "n_compared": len(chosen) - a_only - b_only,
        "a_only": a_only,
        "b_only": b_only,
        "layers_a": sorted(layers_a),
        "layers_b": sorted(layers_b),
        "size_rel": size_rel,
        "layer_rel": layer_rel,
        "absent_in_b": absent,
        "gate_fail": bool(absent),
    }


def _print_vector(res):
    mode = "MVT feature counts" if res["mvt"] else "STRUCTURAL (mapbox_vector_tile absent)"
    print(f"vector A/B [{mode}]: {res['n_compared']} tiles compared (pool {res['n_pool']})")
    print(f"  declared layers A: {res['layers_a']}")
    print(f"  declared layers B: {res['layers_b']}")
    if res["mvt"]:
        for layer in sorted(res["layer_rel"]):
            rel = res["layer_rel"][layer]
            print(f"  layer {layer}: median rel Δ {np.median(rel):.3f}  p95 {np.percentile(rel, 95):.3f}  "
                  f"({len(rel)} tiles)")
    elif res["size_rel"]:
        rel = res["size_rel"]
        print(f"  payload size: median rel Δ {np.median(rel):.3f}  p95 {np.percentile(rel, 95):.3f}  "
              "(byte sizes only — no per-layer feature counts without mapbox_vector_tile)")
    print(f"  A-only tiles: {res['a_only']}   B-only tiles: {res['b_only']}")
    if res["absent_in_b"]:
        print(f"  GATE: FAIL — layer(s) present in A entirely absent from B: {res['absent_in_b']}")
    else:
        print("  GATE: PASS (informational)")


# ── self-test ─────────────────────────────────────────────────────────────────────


def _write_planet(path, tiles, zmin, zmax):
    """A real planet.pmtiles from {(z, x, y): elevation array}, encoded exactly as the pipeline does."""
    import imagecodecs
    from pmtiles.tile import TileType, zxy_to_tileid
    from pmtiles.writer import Writer

    with open(path, "wb") as f:
        writer = Writer(f)
        for (z, x, y), elev in sorted(tiles.items(), key=lambda kv: zxy_to_tileid(*kv[0])):
            rgb = encode.encode(elev, z)
            writer.write_tile(zxy_to_tileid(z, x, y), imagecodecs.webp_encode(rgb, lossless=True))
        writer.finalize(
            {"tile_type": TileType.WEBP, "tile_compression": Compression.NONE,
             "min_zoom": zmin, "max_zoom": zmax,
             "min_lon_e7": int(-180e7), "min_lat_e7": int(-85e7),
             "max_lon_e7": int(180e7), "max_lat_e7": int(85e7),
             "center_zoom": zmin, "center_lon_e7": 0, "center_lat_e7": 0},
            {"attribution": ""})


def _write_build(dirpath, tiles, zmin, zmax):
    os.makedirs(dirpath, exist_ok=True)
    _write_planet(os.path.join(dirpath, "planet.pmtiles"), tiles, zmin, zmax)
    manifest = {
        "planet": {"file": "planet.pmtiles", "min_zoom": zmin, "max_zoom": zmax,
                   "bbox": [-180.0, -85.0, 180.0, 85.0]},
        "overlay": {"split_z": 5, "cells": {}},
        "source_ids": [], "attribution": "",
    }
    with open(os.path.join(dirpath, "manifest.json"), "w") as f:
        json.dump(manifest, f)


def _check():
    """Two synthetic build pairs drive the terrain gate end to end. Tiles sit at z8, deep (-3000 m),
    where a 5 m shift SURVIVES the vertical quantization (z8 step 8 m) — at coarse zooms a 5 m deep
    shift is correctly erased, which is why the gate operates on decoded content."""
    import shutil
    import tempfile

    d = tempfile.mkdtemp()
    try:
        # A 2x2 block of z8 tiles so the shifted-tile pair's deep p95 clears the 2 m threshold.
        coords = [(8, 128 + i, 96 + j) for i in range(2) for j in range(2)]
        base = {c: np.full((512, 512), -3000.0, dtype="float64") for c in coords}

        _write_build(f"{d}/id_a", base, 8, 8)
        _write_build(f"{d}/id_b", base, 8, 8)
        same = compare_terrain(f"{d}/id_a", f"{d}/id_b", samples=16, seed=1)
        assert same["n_compared"] == 4, same
        assert same["a_only"] == 0 and same["b_only"] == 0, same
        assert same["deep_n"] == 4 and same["deep_p95"] < 1e-6, same
        assert not same["gate_fail"], same

        shifted = {c: base[c].copy() for c in coords}
        shifted[coords[0]] = np.full((512, 512), -2995.0, dtype="float64")  # one tile, +5 m
        _write_build(f"{d}/sh_a", base, 8, 8)
        _write_build(f"{d}/sh_b", shifted, 8, 8)
        res = compare_terrain(f"{d}/sh_a", f"{d}/sh_b", samples=16, seed=1)
        flagged = [t for t in res["tiles"] if t.get("mean_abs", 0) > 2.0]
        assert flagged, res
        assert all(t["deep"] for t in flagged), res
        assert res["deep_p95"] > DEEP_OCEAN_P95_THRESHOLD_M, res
        assert res["gate_fail"], res

        print(f"identical pair: deep p95 {same['deep_p95']:.3f} m -> PASS")
        print(f"shifted pair:   worst tile mean|Δ| {flagged[0]['mean_abs']:.3f} m, "
              f"deep p95 {res['deep_p95']:.3f} m -> FAIL")
        print("ab_check.py self-check ok")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _parse_opts(args):
    samples, seed, rest = DEFAULT_SAMPLES, DEFAULT_SEED, []
    it = iter(args)
    for a in it:
        if a == "--samples":
            samples = int(next(it))
        elif a == "--seed":
            seed = int(next(it))
        else:
            rest.append(a)
    return rest, samples, seed


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv == ["--check"]:
        _check()
    elif argv[:1] == ["terrain"]:
        pos, n, s = _parse_opts(argv[1:])
        if len(pos) != 2:
            sys.exit("usage: ab_check.py terrain <baseA> <baseB> [--samples N] [--seed S]")
        res = compare_terrain(pos[0], pos[1], n, s)
        _print_terrain(res)
        sys.exit(1 if res["gate_fail"] else 0)
    elif argv[:1] == ["vector"]:
        pos, n, s = _parse_opts(argv[1:])
        if len(pos) != 2:
            sys.exit("usage: ab_check.py vector <baseA> <baseB> [--samples N] [--seed S]")
        res = compare_vector(pos[0], pos[1], n, s)
        _print_vector(res)
        sys.exit(1 if res["gate_fail"] else 0)
    else:
        sys.exit("usage: ab_check.py  terrain <baseA> <baseB>  |  vector <baseA> <baseB>  |  --check")
