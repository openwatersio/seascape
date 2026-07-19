"""End-to-end self-check for the ENGINE (Snakemake) build lane.

Builds two synthetic sources in an isolated tmp dir — a coarse broad ``base``
(-101 m, native ~z10) and a fine small ``fine`` (-51 m, native ~z13 but capped to
z11 in metadata) inside it — registers them through the real stage-1 CLIs (bounds
→ catalog → covering), then drives the real stage-2/3 engine DAG
(``snakemake -s build.smk bundles``) and asserts:

  - the per-source max_zoom CAP binds (fine renders at z11, not its native z13);
  - PRIORITY: at the fine source's zoom the merged value is the fine value (-51),
    not the base (-101) — highest-maxzoom source wins in overlap;
  - the base shows through where fine is absent (-101 present);
  - the bundle split: planet.pmtiles caps at macrotile_z and the deeper fine tiles
    land in one overlay-{cell}.pmtiles per populated OVERLAY_SPLIT_Z grid cell;
  - a no-op rerun schedules ZERO jobs (engine provenance, no covering diff);
  - a CONTOUR_LEVELS change reruns contour_tile + the vector bundle but ZERO
    mosaic_tile and ZERO terrain_render (the stage split's payoff — no re-merge,
    no re-render — proven by a dry run's rerun table);
  - the hole-free gate fires: a missing per-tile fork output makes the --stable
    bundle refuse rather than publish a hole.

Run from pipelines/:  uv run python test_engine.py
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from glob import glob

import numpy as np
import rasterio
from rasterio.transform import from_origin

PIPE = os.path.dirname(os.path.abspath(__file__))

# Small macrotile_z / num_overviews keep the synthetic rasters tiny. SKIP_SMOOTH: the raster
# priority test needs no smoothing (one fewer moving part in the dry-run rerun table). The source
# values sit just off the contour levels (-101/-51) so gdal_contour sees clean crossings only at
# the feathered seam. CONTOUR_LEVELS is stripped so a dev shell can't poison the provenance
# assertions.
def _base_env(tmp, extra=None):
    env = {**os.environ, "SOURCES_DIR": os.path.join(tmp, "sources"),
           "MACROTILE_Z": "10", "NUM_OVERVIEWS": "2", "SKIP_SMOOTH": "1"}
    env.pop("CONTOUR_LEVELS", None)
    env.pop("BBOX", None)
    env.update(extra or {})
    return env


def cli(tmp, script, *args, env=None):
    """Run a stage-1 per-item CLI (cwd=tmp, so store/ + sources/ are tmp-relative)."""
    e = _base_env(tmp, env)
    e["SOURCES_DIR"] = "sources"  # relative to cwd=tmp
    proc = subprocess.run([sys.executable, os.path.join(PIPE, script), *args],
                          cwd=tmp, env=e, check=True, capture_output=True, text=True)
    return proc


def snake(tmp, *args, env=None, check=True):
    """Run the engine DAG against build.smk (workdir=tmp, sources from tmp/sources)."""
    e = _base_env(tmp, env)
    proc = subprocess.run(
        ["uv", "run", "snakemake", "-s", os.path.join(PIPE, "build.smk"),
         "--config", f"workdir={tmp}", *args],
        cwd=PIPE, env=e, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"snakemake {args} failed:\n{proc.stdout}\n{proc.stderr}")
    return proc


def make_source(tmp, sid, west, north, deg, px, value, max_zoom, extra_meta=None):
    """A constant-value EPSG:4326 source COG (already 'prepped') + sources/<id>/metadata.json."""
    os.makedirs(f"{tmp}/sources/{sid}", exist_ok=True)
    os.makedirs(f"{tmp}/store/source/{sid}", exist_ok=True)
    arr = np.full((px, px), value, dtype="float32")
    res = deg / px
    with rasterio.open(f"{tmp}/store/source/{sid}/{sid}_0.tif", "w", driver="GTiff",
                       height=px, width=px, count=1, dtype="float32", nodata=-9999,
                       crs="EPSG:4326", transform=from_origin(west, north, res, res)) as d:
        d.write(arr, 1)
    meta = {"name": sid, "max_zoom": max_zoom, **(extra_meta or {})}
    with open(f"{tmp}/sources/{sid}/metadata.json", "w") as f:
        json.dump(meta, f)


def make_masks(tmp):
    """Valid but far-away land + water FlatGeobufs (EPSG:3857): the depare fork requires the masks
    as build.smk inputs, but the synthetic tiles near the equator read zero features, so drying /
    nodata degrade to land-only and the depth bands still exercise the fork."""
    import geopandas as gpd
    from shapely.geometry import box
    os.makedirs(f"{tmp}/store/landmask", exist_ok=True)
    far = gpd.GeoDataFrame({"geometry": [box(2.0e7, 2.0e7, 2.001e7, 2.001e7)]}, crs="EPSG:3857")
    for name in ("land.fgb", "water.fgb"):
        far.to_file(f"{tmp}/store/landmask/{name}", driver="FlatGeobuf")


def decode_bundles(tmp):
    """{zoom: [median elevation per tile]} across every bundle pmtiles — planet
    (z0..PLANET_MAX_ZOOM) plus the grid-cell overlays above it."""
    import imagecodecs
    from pmtiles.reader import Reader, MmapSource, all_tiles
    sys.path.insert(0, PIPE)
    import encode

    by_zoom = {}
    for path in sorted(glob(f"{tmp}/store/bundle/*.pmtiles")):
        if os.path.basename(path) in ("vector.pmtiles", "soundings.pmtiles", "depare.pmtiles",
                                      "coverage.pmtiles"):
            continue  # vector layers, not terrain rasters
        with open(path, "r+b") as f:
            for (z, x, y), tile_bytes in all_tiles(Reader(MmapSource(f)).get_bytes):
                elev = encode.decode(imagecodecs.webp_decode(tile_bytes).astype("float32"))
                by_zoom.setdefault(z, []).append(float(np.median(elev)))
    return by_zoom


def _job_counts(dry_stdout):
    """Parse snakemake's dry-run job table into {rule: count}. Rows look like ``rule_name   N``
    under a ``job      count`` header."""
    counts = {}
    for line in dry_stdout.splitlines():
        m = re.match(r"^([a-z_]+)\s+(\d+)\s*$", line.strip())
        if m and m.group(1) not in ("total", "job", "count"):
            counts[m.group(1)] = int(m.group(2))
    return counts


def check_priority():
    """get_grouped_source_items merge order: a metadata `priority` source wins overlap even
    over a finer (higher-maxzoom) source; without priority, native resolution decides."""
    import utils
    import config
    orig = config.load_metadata
    d = tempfile.mkdtemp()
    csv = os.path.join(d, "x.csv")
    with open(csv, "w") as f:
        f.write("source,filename,maxzoom\nB,b.tif,13\nA,a.tif,11\n")  # B finer, A coarser
    try:
        config.load_metadata = lambda s: {"priority": 1} if s == "A" else {}
        order = [g[0]["source"] for g in utils.get_grouped_source_items(csv)]
        assert order[0] == "A", f"priority should win merge order: {order}"
        config.load_metadata = lambda s: {}  # no priority anywhere
        order = [g[0]["source"] for g in utils.get_grouped_source_items(csv)]
        assert order[0] == "B", f"without priority, finer (maxzoom 13) wins: {order}"
        print("priority ok — datum-authoritative source wins merge order; else maxzoom")
    finally:
        config.load_metadata = orig
        shutil.rmtree(d, ignore_errors=True)


def check_pmtiles_reproducible():
    """The pmtiles Writer gzips its root+leaf directories; stock gzip stamps a wall-clock mtime
    into each header, so two builds of byte-identical tiles used to differ in those 4 bytes.
    utils.py pins mtime=0 by rebinding the pmtiles gzip binding; guard it: two archives built from
    the same tiles under different clocks must be byte-for-byte identical."""
    import time as _time
    sys.path.insert(0, PIPE)
    import utils  # applies the deterministic-gzip monkey-patch at import time
    from pmtiles.writer import Writer
    from pmtiles.tile import zxy_to_tileid, TileType, Compression

    def build(path):
        tiles = {zxy_to_tileid(14, x, y): bytes([(x + y) % 256]) * 16
                 for x in range(48) for y in range(48)}
        with open(path, "wb") as f:
            w = Writer(f)
            for tid in sorted(tiles):
                w.write_tile(tid, tiles[tid])
            w.finalize(
                {"tile_type": TileType.WEBP, "tile_compression": Compression.NONE,
                 "min_zoom": 14, "max_zoom": 14,
                 "min_lon_e7": 0, "min_lat_e7": 0, "max_lon_e7": 10, "max_lat_e7": 10,
                 "center_zoom": 14, "center_lon_e7": 5, "center_lat_e7": 5},
                {"attribution": utils.ATTRIBUTION})

    tmp = tempfile.mkdtemp()
    orig_time = _time.time
    try:
        a, b = f"{tmp}/a.pmtiles", f"{tmp}/b.pmtiles"
        _time.time = lambda: 1_000_000_000.0
        build(a)
        _time.time = lambda: 2_000_000_000.0
        build(b)
        _time.time = orig_time
        ba, bb = open(a, "rb").read(), open(b, "rb").read()
        assert ba == bb, (f"pmtiles NOT reproducible ({len(ba)} vs {len(bb)} bytes) — the "
                          "deterministic-gzip patch in utils.py likely broke on a pmtiles upgrade")
        print(f"pmtiles-reproducible ok — byte-identical {len(ba)}-byte archive across two clocks")
    finally:
        _time.time = orig_time
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    tmp = tempfile.mkdtemp()
    try:
        # base: 1°x1° near the equator (-101, native ~z10). fine: 0.4°x0.4° inside it (-51, native
        # ~z14, well above the z11 cap so the cap binds on any GDAL version).
        make_source(tmp, "base", west=-0.5, north=0.5, deg=1.0, px=1024, value=-101, max_zoom=10)
        make_source(tmp, "fine", west=-0.2, north=0.2, deg=0.4, px=4096, value=-51, max_zoom=11)
        make_masks(tmp)

        # ── stage 1 (per-item CLIs, exactly what the Snakefile rules shell out to) ──
        for sid in ("base", "fine"):
            cli(tmp, "source_bounds.py", sid)
            cli(tmp, "source_catalog.py", sid)
        cli(tmp, "aggregation_covering.py", "--stable")   # the `cover` rule: covering.txt + CSVs

        stems = [s for s in open(f"{tmp}/store/aggregation/covering.txt").read().split() if s]
        n_stems = len(stems)
        assert n_stems, "the covering must have tiles"

        # covering wrote the cap into child_z: the deepest aggregation tile is z11, not z13.
        child_zs = [int(s.rsplit("-", 1)[1]) for s in stems]
        assert max(child_zs) == 11, f"cap not applied (want 11): child_z={sorted(set(child_zs))}"

        # ── stage 2/3: the real engine DAG, one command ──
        snake(tmp, "-c", "4", "bundles")

        # planet caps at macrotile_z (10); the deeper fine tiles route to their overlay cells.
        assert os.path.exists(f"{tmp}/store/bundle/planet.pmtiles"), "missing planet.pmtiles"
        overlays = glob(f"{tmp}/store/bundle/overlay-*.pmtiles")
        assert overlays, "the z11 fine tiles must produce at least one overlay archive"
        assert os.path.exists(f"{tmp}/store/bundle/vector.pmtiles"), "missing vector.pmtiles"

        by_zoom = decode_bundles(tmp)
        assert by_zoom, "no tiles in any terrain bundle"
        max_z = max(by_zoom)
        assert max_z == 11, f"expected max zoom 11, got {max_z}"
        assert max(z for z in by_zoom if by_zoom[z]) == 11

        # PRIORITY: z11 only exists where fine is present; the fine-dominated z11 tile reads ~-51.
        z11_shallowest = max(by_zoom[11])  # -51 is shallower than base's -101
        assert z11_shallowest > -55, f"fine should win at z11 (shallowest z11 tile {z11_shallowest:.1f})"
        # base shows through somewhere: some tile reads ~-101.
        all_meds = [m for meds in by_zoom.values() for m in meds]
        assert min(all_meds) < -90, f"base (-101) should appear (min median {min(all_meds):.1f})"
        print(f"engine e2e ok — {n_stems} stems, zooms {min(by_zoom)}..{max_z}, fine wins at z11 "
              f"({z11_shallowest:.1f}), base present (min {min(all_meds):.1f})")

        # ── no-op rerun schedules ZERO jobs (engine provenance) ──
        dry = snake(tmp, "-c", "4", "-n", "bundles").stdout
        counts = _job_counts(dry)
        assert not counts or sum(counts.values()) == 0 or "Nothing to be done" in dry, \
            f"a no-op rerun must schedule 0 jobs, got {counts}"
        print(f"no-op rerun ok — 0 jobs scheduled")

        # ── a CONTOUR_LEVELS change reruns contour + vector bundle, NOT mosaic/terrain ──
        levels = ("-10000 -8000 -6000 -5000 -4000 -3000 -2000 -1000 -500 -300 -200 "
                  "-100 -50 -30 -20 -10 -5")  # default ladder minus -2
        dry = snake(tmp, "-c", "4", "-n", "bundles", env={"CONTOUR_LEVELS": levels}).stdout
        counts = _job_counts(dry)
        assert counts.get("mosaic_tile", 0) == 0, f"a contour-config change must NOT re-merge: {counts}"
        assert counts.get("terrain_render", 0) == 0, f"a contour-config change must NOT re-render: {counts}"
        assert counts.get("contour_tile", 0) == n_stems, \
            f"a contour-config change must rerun every contour tile: {counts}"
        assert counts.get("vector_bundle", 0) == 1, f"the vector bundle must rerun: {counts}"
        print(f"stage-split ok — CONTOUR_LEVELS reruns {counts.get('contour_tile')} contour_tile "
              f"+ vector_bundle, 0 mosaic_tile, 0 terrain_render")

        # ── the hole-free gate: a missing per-tile fork output makes --stable bundle refuse ──
        victim = sorted(glob(f"{tmp}/store/contour/*.fgb"))[0]
        os.remove(victim)
        proc = subprocess.run(
            [sys.executable, os.path.join(PIPE, "contour_run.py"), "bundle", "--stable"],
            cwd=tmp, env=_base_env(tmp, {"SOURCES_DIR": "sources"}),
            capture_output=True, text=True)
        assert proc.returncode != 0 and "contour incomplete" in (proc.stderr + proc.stdout), \
            f"the hole-free gate must refuse a missing per-tile file:\n{proc.stdout}\n{proc.stderr}"
        print(f"hole-free gate ok — bundle --stable refused the missing {os.path.basename(victim)}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.path.insert(0, PIPE)
    import depare_run
    import landmask
    landmask._check()
    depare_run._check()
    check_priority()
    check_pmtiles_reproducible()
    main()
