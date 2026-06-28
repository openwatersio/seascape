"""End-to-end self-check for the aggregation -> downsampling -> bundle engine.

Builds two synthetic sources in an isolated tmp dir — a coarse broad ``base``
(-100 m, native ~z10) and a fine small ``fine`` (-50 m, native ~z13 but capped to
z11 in metadata) inside it — then runs the whole engine and asserts:

  - the per-source max_zoom CAP binds (fine renders at z11, not its native z13);
  - PRIORITY: at the fine source's zoom the merged value is the fine value (-50),
    not the base (-100) — highest-maxzoom source wins in overlap;
  - the base shows through where fine is absent (-100 present);
  - the E1 split: planet.pmtiles spans z0..macrotile_z and the deeper fine tiles
    land in the fine.pmtiles overlay (so the engine covers z0..11 across bundles).

Run from pipelines/:  uv run python test_engine.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np
import rasterio
from rasterio.transform import from_origin

PIPE = os.path.dirname(os.path.abspath(__file__))


def run(tmp, *args):
    # Small macrotile_z / num_overviews keep the synthetic rasters tiny.
    # SKIP_CONTOURS/SKIP_SMOOTH: this is the raster priority test (both have/need none).
    env = {**os.environ, "SOURCES_DIR": "sources", "PYTHONPATH": PIPE,
           "MACROTILE_Z": "10", "NUM_OVERVIEWS": "2", "SKIP_CONTOURS": "1", "SKIP_SMOOTH": "1"}
    subprocess.run([sys.executable, os.path.join(PIPE, args[0]), *args[1:]],
                   cwd=tmp, env=env, check=True)


def make_source(tmp, sid, west, north, deg, px, value, max_zoom):
    os.makedirs(f"{tmp}/sources/{sid}", exist_ok=True)
    os.makedirs(f"{tmp}/store/source/{sid}", exist_ok=True)
    arr = np.full((px, px), value, dtype="float32")
    res = deg / px
    with rasterio.open(f"{tmp}/store/source/{sid}/{sid}_0.tif", "w", driver="GTiff",
                       height=px, width=px, count=1, dtype="float32", nodata=-9999,
                       crs="EPSG:4326", transform=from_origin(west, north, res, res)) as d:
        d.write(arr, 1)
    with open(f"{tmp}/sources/{sid}/metadata.json", "w") as f:
        json.dump({"name": sid, "max_zoom": max_zoom}, f)


def decode_bundles(tmp):
    """Return {zoom: [median elevation per tile]} across ALL bundle pmtiles —
    planet (z0..PLANET_MAX_ZOOM) plus the per-source overlays above it (E1
    routes child_z > PLANET_MAX_ZOOM into <source>.pmtiles, not planet)."""
    import glob
    import imagecodecs
    from pmtiles.reader import Reader, MmapSource, all_tiles
    import encode

    by_zoom = {}
    for path in sorted(glob.glob(f"{tmp}/store/bundle/*.pmtiles")):
        with open(path, "r+b") as f:
            reader = Reader(MmapSource(f))
            for (z, x, y), tile_bytes in all_tiles(reader.get_bytes):
                elev = encode.decode(imagecodecs.webp_decode(tile_bytes).astype("float32"))
                by_zoom.setdefault(z, []).append(float(np.median(elev)))
    return by_zoom


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


def main():
    tmp = tempfile.mkdtemp()
    try:
        # base: 1°x1° near the equator (-100, native ~z10). fine: 0.4°x0.4° inside
        # it (-50, native ~z14, well above the z11 cap so the cap binds on any GDAL
        # version). 0.4° spans more than a z11 tile so a z11 tile is fully fine.
        make_source(tmp, "base", west=-0.5, north=0.5, deg=1.0, px=1024, value=-100, max_zoom=10)
        make_source(tmp, "fine", west=-0.2, north=0.2, deg=0.4, px=4096, value=-50, max_zoom=11)

        run(tmp, "source_bounds.py", "base")
        run(tmp, "source_bounds.py", "fine")
        run(tmp, "aggregation_covering.py")
        run(tmp, "aggregation_run.py")
        run(tmp, "downsampling.py", "cover")
        # Exercise the CI fan-out (deep shards + coarse tail), not just the single
        # run() — both shards then the tail must reproduce a correct full pyramid.
        run(tmp, "downsampling.py", "run", "shard", "0", "2")
        run(tmp, "downsampling.py", "run", "shard", "1", "2")
        run(tmp, "downsampling.py", "run", "tail")
        run(tmp, "bundle.py")

        # shard-keys (the CI per-shard pull filter) must select every deep tile (extent
        # z >= SHARD_ROOT_Z) for exactly one shard and nothing below — a miss would pull
        # the wrong tiles in CI and leave silent holes.
        import glob as _glob
        shard_root_z = 10 - 2  # MACROTILE_Z - NUM_OVERVIEWS, the run() env above
        pmtiles = [os.path.basename(p) for p in _glob.glob(f"{tmp}/store/pmtiles/**/*.pmtiles", recursive=True)]
        with open(f"{tmp}/store/pmtiles-keys.txt", "w") as f:
            f.write("".join(f"pmtiles/{n}\n" for n in pmtiles))
        deep = {n for n in pmtiles if int(n.split("-")[0]) >= shard_root_z}
        selected = []
        for i in range(2):
            run(tmp, "downsampling.py", "shard-keys", str(i), "2")
            with open(f"{tmp}/store/shard-keys.txt") as f:
                selected.append({os.path.basename(l.strip()) for l in f if l.strip()})
        union = selected[0] | selected[1]
        assert union == deep, f"shard-keys miscovers deep tiles; missing {deep - union}, extra {union - deep}"
        assert not (selected[0] & selected[1]), f"shards must be disjoint: {selected[0] & selected[1]}"
        print(f"shard-keys ok — {len(deep)} deep pmtiles partitioned across 2 shards, none below z{shard_root_z}")

        # bundle group-keys (the CI per-group pull filter) must partition EVERY terrain
        # pmtiles across the groups: a tile in no group is missing from the bundles (a
        # hole the Worker overzooms GEBCO into); a tile in two is double-bundled.
        cli_env = {**os.environ, "SOURCES_DIR": "sources", "PYTHONPATH": PIPE,
                   "MACROTILE_Z": "10", "NUM_OVERVIEWS": "2", "SKIP_CONTOURS": "1", "SKIP_SMOOTH": "1"}
        names = json.loads(subprocess.run(
            [sys.executable, os.path.join(PIPE, "bundle.py"), "groups"],
            cwd=tmp, env=cli_env, check=True, capture_output=True, text=True).stdout.splitlines()[-1])
        assert set(names) == {"planet", "fine"}, f"unexpected groups: {names}"
        gsel = []
        for name in names:
            run(tmp, "bundle.py", "group-keys", name)
            with open(f"{tmp}/store/keys.txt") as f:
                gsel.append({os.path.basename(l.strip()) for l in f if l.strip()})
        gunion = set().union(*gsel)
        assert gunion == set(pmtiles), f"group-keys miscovers; missing {set(pmtiles) - gunion}, extra {gunion - set(pmtiles)}"
        assert not (gsel[0] & gsel[1]), f"groups overlap: {gsel[0] & gsel[1]}"
        print(f"group-keys ok — {len(pmtiles)} pmtiles partitioned across {len(names)} group(s)")

        # orphan exclusion: a pmtiles left from a re-tiled covering (stem not in the
        # current covering) must land in NO group, else it double-bundles a stale tile
        # over the live tiling (the raster twin of the contour-overlap bug).
        orphan = "0-0-0-99.pmtiles"
        with open(f"{tmp}/store/pmtiles-keys.txt", "a") as f:
            f.write(f"pmtiles/{orphan}\n")
        for name in names:
            run(tmp, "bundle.py", "group-keys", name)
            with open(f"{tmp}/store/keys.txt") as f:
                got = {os.path.basename(l.strip()) for l in f if l.strip()}
            assert orphan not in got, f"orphan {orphan} leaked into group {name}"
        print(f"orphan-exclusion ok — {orphan} excluded from every group")

        # covering wrote the cap into child_z: the deepest aggregation tile is z9, not z11.
        agg_dir = f"{tmp}/store/aggregation"
        agg_id = sorted(os.listdir(agg_dir))[-1]
        child_zs = [int(n.replace("-aggregation.csv", "").split("-")[3])
                    for n in os.listdir(f"{agg_dir}/{agg_id}") if n.endswith("-aggregation.csv")]
        assert max(child_zs) == 11, f"cap not applied (want 11): child_z={sorted(set(child_zs))}"

        # E1 split: planet caps at macrotile_z (10); the z11 fine tiles route to
        # the fine.pmtiles overlay. Both must exist.
        assert os.path.exists(f"{tmp}/store/bundle/planet.pmtiles"), "missing planet.pmtiles"
        assert os.path.exists(f"{tmp}/store/bundle/fine.pmtiles"), "missing fine overlay"
        by_zoom = decode_bundles(tmp)
        assert by_zoom, "no tiles in any bundle"
        max_z = max(by_zoom)
        assert max_z == 11, f"expected max zoom 11, got {max_z}"

        # PRIORITY: z11 only exists where fine is present; the fine-dominated z11
        # tile must read ~-50 (fine wins over base in overlap), not -100.
        z11_shallowest = max(by_zoom[11])  # -50 is shallower than base's -100
        assert z11_shallowest > -55, f"fine should win at z11 (shallowest z11 tile {z11_shallowest:.1f})"
        # base shows through somewhere: some tile reads ~-100.
        all_meds = [m for meds in by_zoom.values() for m in meds]
        assert min(all_meds) < -90, f"base (-100) should appear (min median {min(all_meds):.1f})"
        print(f"engine e2e ok — zooms {min(by_zoom)}..{max_z}, fine wins at z11 "
              f"({z11_shallowest:.1f}), base present (min {min(all_meds):.1f})")

        # FAIL-CLOSED: a covering whose pmtiles is gone (a dropped/unsynced shard) must
        # fail the bundle, not silently publish a hole the Worker fills with overzoomed
        # GEBCO. Delete one tile and assert bundle.py now exits non-zero.
        victim = sorted(_glob.glob(f"{tmp}/store/pmtiles/**/*.pmtiles", recursive=True))[0]
        os.remove(victim)
        try:
            run(tmp, "bundle.py")
        except subprocess.CalledProcessError:
            print(f"completeness gate ok — bundle failed on missing {os.path.basename(victim)}")
        else:
            raise AssertionError("bundle.py must fail when a covering has no pmtiles")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    check_priority()
    main()
