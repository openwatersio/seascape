"""Concatenate the single-zoom PMTiles into a planet base + fixed-grid overlays.

Everything at ``child_z <= PLANET_MAX_ZOOM`` (the GEBCO-native base cap, default =
macrotile_z) goes into a complete ``planet.pmtiles`` (z0..cap). Higher-res tiles go
into one ``overlay-{z}-{x}-{y}.pmtiles`` per populated cell of a fixed
``OVERLAY_SPLIT_Z`` grid — each holding every z(cap+1)..zmax tile under that cell,
from whatever sources are there.

The grid is deliberately source-agnostic. Grouping overlays by source made each
archive's size a function of a source's footprint: one deep source whose bbox
swallowed the continent's interior became a catch-all that outgrew the build box's
disk, and every new source made it worse. A grid cell is a fixed fraction of the
globe, so new sources add *cells*, never bytes-per-cell; if a cell ever outgrows the
box, raise OVERLAY_SPLIT_Z. It also gives the serving Worker an O(1) route: the
owning cell is computed from the tile address (no footprint search).

``manifest.json`` records planet metadata + ``overlay`` ({split_z, cells: {cell:
max_zoom}}) + the configured source ids (the viewer's provenance palette).

Pure concat in tile-id order. The engine schedules one job per archive; Snakemake owns freshness.

Usage (from pipelines/):
  bundle.py planet --stable        the planet base archive (z0..PLANET_MAX_ZOOM)
  bundle.py cell <cell> --stable   one overlay grid cell's deeper tiles
  bundle.py stage-build --stable   assemble + publish build/<sha>/ (manifest.json last)
"""

import json
import math
import os
import shutil
import sys
from glob import glob

import mercantile
from pmtiles.tile import zxy_to_tileid, TileType, Compression
from pmtiles.reader import Reader, MmapSource, all_tiles
from pmtiles.writer import Writer

import config
import contour_run
import utils

# Base cap = the GEBCO-native zoom (the planet is complete + overzoomable below it).
PLANET_MAX_ZOOM = int(os.environ.get("PLANET_MAX_ZOOM", str(utils.macrotile_z)))
# The overlay grid zoom. Every overlay tile (child_z > PLANET_MAX_ZOOM) belongs to
# its ancestor cell at this zoom; one archive per populated cell.
SPLIT_Z = int(os.environ.get("OVERLAY_SPLIT_Z", "5"))
# cell_of shifts by (z - SPLIT_Z), so the grid must sit at or above the shallowest
# overlay content zoom — fail fast here, not as a ValueError deep in the bundle.
if SPLIT_Z > PLANET_MAX_ZOOM + 1:
    sys.exit(f"OVERLAY_SPLIT_Z ({SPLIT_Z}) must be <= PLANET_MAX_ZOOM + 1 ({PLANET_MAX_ZOOM + 1})")


def cell_of(z, x, y):
    """The SPLIT_Z grid cell name owning tile (z,x,y), z >= SPLIT_Z."""
    return f"{SPLIT_Z}-{x >> (z - SPLIT_Z)}-{y >> (z - SPLIT_Z)}"


def stem_groups(stem):
    """Group name(s) a pmtiles stem {z}-{x}-{y}-{child_z} contributes to: 'planet'
    for base zooms, else the overlay cell(s) its content tiles live under. Overlay
    parents normally sit at z >= SPLIT_Z (exactly one cell — overview render extents
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


def read_full_archive(filepath):
    out = {}
    with open(filepath, "r+b") as f:
        reader = Reader(MmapSource(f))
        for tile_tuple, tile_bytes in all_tiles(reader.get_bytes):
            out[zxy_to_tileid(*tile_tuple)] = tile_bytes
    return out


def create_archive(filepaths, name, stem_of):
    """Concat the single-zoom pmtiles into store/bundle/<archive_filename(name)>.
    For an overlay cell, tiles outside the cell (a coarse parent spanning cells)
    are left to the sibling cells' archives. ``stem_of`` parses a member's logical
    {z}-{x}-{y}-{child_z} — ``_plain_stem``, where the flat basename IS the stem."""
    utils.create_folder("store/bundle")
    out_filepath = f"store/bundle/{archive_filename(name)}"
    min_z, max_z = math.inf, 0
    min_lon, min_lat, max_lon, max_lat = math.inf, math.inf, -math.inf, -math.inf

    with open(out_filepath, "wb") as f1:
        hash_writer = utils.HashWriter(f1)
        writer = Writer(hash_writer)

        tile_ids_and_filepaths = []
        for filepath in filepaths:
            z, x, y, child_z = (int(a) for a in stem_of(filepath).split("-"))
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
    """The linked page carries the full per-source attribution and modification notice."""
    return utils.ATTRIBUTION


def bundle_group(name, filepaths, stem_of):
    print(f"bundling {archive_filename(name)} ({len(filepaths)} pmtiles)...")
    return create_archive(filepaths, name, stem_of)


# ── the --stable inventory ─────────────────────────────────────────────────────
# Plain flat inventory (store/pmtiles/<stem>.pmtiles), one archive per invocation. Snakemake
# owns freshness, so no manifest and no internal cell pool — the engine schedules the cells as
# jobs. Same grid partition (stem_groups / cell_of) for every archive.


def _plain_stem(path):
    """The logical stem of a plain name (store/pmtiles/<stem>.pmtiles): the basename minus
    extension IS the stem."""
    return os.path.splitext(os.path.basename(path))[0]


def _stable_inventory():
    """{stem: plain path} over every render stem of the BBOX-scoped covering
    (terrain.render_stems of mosaic.covering_stems). The render-stem set IS the inventory."""
    import mosaic
    import terrain
    stems = terrain.render_stems(mosaic.covering_stems())
    return {s: f"store/pmtiles/{s}.pmtiles" for s in stems}


def overlay_cells(stems):
    """The populated overlay cell ids for a render-stem list — every non-planet group stem_groups
    routes a stem into. build.smk derives the per-cell overlay outputs (and each cell's member
    stems) from this."""
    cells = set()
    for stem in stems:
        cells.update(n for n in stem_groups(stem) if n != "planet")
    return sorted(cells)


def bundle_planet_stable():
    """Build store/bundle/planet.pmtiles (z0..PLANET_MAX_ZOOM) from the plain inventory — the base
    stems (stem_groups == ['planet']). Asserts every render stem's pmtiles is on disk first (a
    MISSING one is an interrupted build) — the only bundle-time gate."""
    inv = _stable_inventory()
    stems = sorted(inv)
    contour_run.require_stable_complete("terrain", stems, [inv[s] for s in stems])
    filepaths = [inv[s] for s in stems if stem_groups(s) == ["planet"]]
    bundle_group("planet", filepaths, stem_of=_plain_stem)
    print(f"planet bundle (stable): store/bundle/planet.pmtiles ({len(filepaths)} pmtiles)")


def bundle_cell_stable(cell):
    """Build ONE store/bundle/overlay-<cell>.pmtiles from the plain inventory: every stem whose
    stem_groups routes it into this cell (create_archive keeps only the cell's own tiles). One cell
    per invocation — no internal pool; the engine schedules the cells. Asserts this cell's member
    pmtiles are on disk first (a MISSING one is an interrupted build)."""
    inv = _stable_inventory()
    filepaths = [inv[s] for s in sorted(inv) if cell in stem_groups(s)]
    stems = [_plain_stem(f) for f in filepaths]
    contour_run.require_stable_complete("terrain", stems, filepaths)
    bundle_group(cell, filepaths, stem_of=_plain_stem)
    print(f"overlay bundle (stable): store/bundle/{archive_filename(cell)} ({len(filepaths)} pmtiles)")


# ── the per-commit release bundle (build/<sha>/) ────────────────────────────────
# The finished archives the serving Worker fetches (planet + overlays + vector, plus
# coverage when the catalogs invocation left it) and a manifest.json it reads: planet
# BundleMeta, the overlay grid, source_ids, attribution — the Worker's Manifest shape and
# no more (worker/src/index.ts). Uploaded to bathymetry/build/<sha>/ with manifest.json
# LAST: its presence marks a complete build (release.yml refuses a sha without one).

_BUILD_DEST = "r2:$DATA_BUCKET/bathymetry/build"  # $DATA_BUCKET + $SHA stay shell env vars


def _archive_meta(path):
    """One archive's Worker BundleMeta {file, min_zoom, max_zoom, bbox}, read from its pmtiles
    header (bbox from the e7 bounds create_archive wrote)."""
    with open(path, "rb") as f:
        h = Reader(MmapSource(f)).header()
    return {"file": os.path.basename(path), "min_zoom": h["min_zoom"], "max_zoom": h["max_zoom"],
            "bbox": [h["min_lon_e7"] / 1e7, h["min_lat_e7"] / 1e7,
                     h["max_lon_e7"] / 1e7, h["max_lat_e7"] / 1e7]}


def stage_build(bundle_dir="store/bundle"):
    """Assemble build/<sha>/ LOCALLY — no network, so the test drives it directly. Reads the
    finished archives, writes manifest.json from their headers (planet BundleMeta + overlay
    {split_z, cells} + source_ids + attribution), and returns (upload_files, manifest_path) with
    manifest.json deliberately ABSENT from upload_files — the publisher sends it LAST.

    planet + vector are REQUIRED (a missing one is an interrupted build). Overlay cells ride
    when populated. soundings/depare are NOT shipped — they tile-join INTO vector.pmtiles, the
    only vector archive the Worker fetches. coverage.pmtiles is a catalogs product (this graph
    never writes it); it ships when the warm volume has it, and the Worker tolerates its absence."""
    planet = f"{bundle_dir}/planet.pmtiles"
    vector = f"{bundle_dir}/vector.pmtiles"
    for req in (planet, vector):
        if not os.path.isfile(req):
            sys.exit(f"stage-build: missing {req} — run the bundles first")
    # The COMPUTED cell inventory, never a glob: the shared volume can hold overlay files from
    # earlier coverings (shrunk coverage, bbox leftovers) that must not ship or be advertised.
    import mosaic
    import terrain
    cells = overlay_cells(terrain.render_stems(mosaic.covering_stems()))
    overlays = [f"{bundle_dir}/overlay-{c}.pmtiles" for c in cells]
    missing = [o for o in overlays if not os.path.isfile(o)]
    if missing:
        sys.exit(f"stage-build: {len(missing)} overlay cell(s) missing, e.g. {missing[:3]} — "
                 "run the bundles first")
    stale = sorted(set(glob(f"{bundle_dir}/overlay-*.pmtiles")) - set(overlays))
    if stale:
        print(f"stage-build: ignoring {len(stale)} stale overlay file(s) not in the current "
              f"inventory, e.g. {[os.path.basename(s) for s in stale[:3]]}")
    manifest = {
        "planet": _archive_meta(planet),
        "overlay": {"split_z": SPLIT_Z, "cells": {
            os.path.basename(o)[len("overlay-"):-len(".pmtiles")]: _archive_meta(o)["max_zoom"]
            for o in overlays}},
        "source_ids": config.sources(),  # the viewer's provenance palette
        "attribution": attribution(),
    }
    uploads = [planet, vector, *overlays]
    coverage = f"{bundle_dir}/coverage.pmtiles"
    if os.path.isfile(coverage):
        uploads.append(coverage)
    else:
        print("stage-build: no coverage.pmtiles — shipping without the provenance layer "
              "(the catalogs invocation produces it)")
    manifest_path = f"{bundle_dir}/manifest.json"
    tmp = manifest_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
    os.replace(tmp, manifest_path)  # only ever appears complete
    return uploads, manifest_path


def _stage_build_guard():
    """SHA + rclone/R2 creds are required — stage-build publishes from the box only."""
    if not os.environ.get("SHA"):
        sys.exit("stage-build: SHA unset — the build/<sha>/ prefix is github.sha")
    if shutil.which("rclone") is None:
        sys.exit("stage-build: rclone not found — publishing runs on the box only")
    if not os.environ.get("RCLONE_CONFIG_R2_TYPE"):
        sys.exit("stage-build: RCLONE_CONFIG_R2_TYPE unset (no R2 creds) — runs on the box only")


def stage_build_publish():
    """Upload build/<sha>/: every archive, then manifest.json LAST. One `rclone copyto` per file;
    $DATA_BUCKET + $SHA stay shell env vars (rclone's env-var remote is `r2`)."""
    _stage_build_guard()
    uploads, manifest_path = stage_build()
    dest = f"{_BUILD_DEST}/$SHA"
    for fp in uploads:
        utils.run_command(f"rclone copyto {fp} {dest}/{os.path.basename(fp)} "
                          "--retries 5 --stats 30s --stats-one-line", silent=False)
    utils.run_command(f"rclone copyto {manifest_path} {dest}/manifest.json --retries 5", silent=False)
    print(f"stage-build: {len(uploads)} archive(s) + manifest.json → {dest}", flush=True)


def _check():
    """Pure stage_build over a synthetic bundle dir — file set, manifest shape, manifest-last.
    No network (stage_build never touches R2). Builds real 1-tile pmtiles so the header read is
    exercised end to end."""
    import tempfile

    saved_dir, cwd = config.SOURCES_DIR, os.getcwd()
    d = tempfile.mkdtemp()

    def synth(path, min_zoom, max_zoom, bounds):
        w, s, e, n = bounds
        with open(path, "wb") as f:
            wr = Writer(f)
            # Writer derives min/max_zoom from the written tiles — anchor both ends of the range.
            for z in {min_zoom, max_zoom}:
                wr.write_tile(zxy_to_tileid(z, 0, 0), b"webp")
            wr.finalize(
                {"tile_type": TileType.WEBP, "tile_compression": Compression.NONE,
                 "min_zoom": min_zoom, "max_zoom": max_zoom,
                 "min_lon_e7": int(w * 1e7), "min_lat_e7": int(s * 1e7),
                 "max_lon_e7": int(e * 1e7), "max_lat_e7": int(n * 1e7),
                 "center_zoom": min_zoom, "center_lon_e7": 0, "center_lat_e7": 0},
                {"attribution": utils.ATTRIBUTION})
    try:
        os.chdir(d)
        config.SOURCES_DIR = "sources"
        os.makedirs("sources/src")
        with open("sources/src/metadata.json", "w") as f:
            json.dump({"name": "src", "max_zoom": 12}, f)
        os.makedirs("store/bundle")
        synth("store/bundle/planet.pmtiles", 0, 8, [-180, -85, 180, 85])
        synth("store/bundle/overlay-5-1-1.pmtiles", 9, 14, [-10, -10, 10, 10])
        synth("store/bundle/vector.pmtiles", 0, 14, [-180, -85, 180, 85])
        synth("store/bundle/coverage.pmtiles", 0, 14, [-180, -85, 180, 85])
        # soundings/depare exist on disk but must NEVER ship — they fold into vector.pmtiles.
        synth("store/bundle/soundings.pmtiles", 0, 14, [-180, -85, 180, 85])
        synth("store/bundle/depare.pmtiles", 0, 14, [-180, -85, 180, 85])

        uploads, manifest_path = stage_build()
        names = {os.path.basename(u) for u in uploads}
        assert names == {"planet.pmtiles", "vector.pmtiles", "overlay-5-1-1.pmtiles",
                         "coverage.pmtiles"}, f"upload set {names}"
        assert not any("soundings" in n or "depare" in n for n in names), \
            "soundings/depare tile-join into vector.pmtiles — never shipped separately"
        assert "manifest.json" not in names and manifest_path.endswith("/manifest.json"), \
            "manifest.json is returned separately so the publisher sends it LAST"

        m = json.load(open(manifest_path))
        assert set(m["planet"]) == {"file", "min_zoom", "max_zoom", "bbox"}, \
            f"planet is the Worker BundleMeta, no more: {sorted(m['planet'])}"
        assert m["planet"]["file"] == "planet.pmtiles" and m["planet"]["max_zoom"] == 8
        assert m["planet"]["bbox"] == [-180.0, -85.0, 180.0, 85.0], m["planet"]["bbox"]
        assert m["overlay"] == {"split_z": SPLIT_Z, "cells": {"5-1-1": 14}}, m["overlay"]
        assert m["source_ids"] == ["src"] and m["attribution"] == utils.ATTRIBUTION
        assert json.load(open(stage_build()[1])) == m, "manifest not deterministic across a re-walk"

        # coverage rides only when present; planet/vector are required.
        os.remove("store/bundle/coverage.pmtiles")
        assert not any("coverage" in os.path.basename(u) for u in stage_build()[0]), \
            "coverage must drop out of the upload set when absent"
        os.remove("store/bundle/planet.pmtiles")
        try:
            stage_build()
        except SystemExit:
            pass
        else:
            raise AssertionError("a missing planet.pmtiles must fail stage-build")
        print("bundle stage-build self-check ok")
    finally:
        config.SOURCES_DIR = saved_dir
        os.chdir(cwd)
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    a = sys.argv[1:]
    if a == ["planet", "--stable"]:
        bundle_planet_stable()
    elif a[:1] == ["cell"] and len(a) == 3 and a[2] == "--stable":
        bundle_cell_stable(a[1])
    elif a == ["stage-build", "--stable"]:
        stage_build_publish()
    elif a == ["--check"]:
        _check()
    else:
        sys.exit("usage: bundle.py  planet --stable  |  cell <cell> --stable  |  "
                 "stage-build --stable  |  --check")
