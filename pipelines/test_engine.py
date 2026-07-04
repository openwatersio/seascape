"""End-to-end self-check for the aggregation -> downsampling -> bundle engine.

Builds two synthetic sources in an isolated tmp dir — a coarse broad ``base``
(-100 m, native ~z10) and a fine small ``fine`` (-50 m, native ~z13 but capped to
z11 in metadata) inside it — then runs the whole engine and asserts:

  - the per-source max_zoom CAP binds (fine renders at z11, not its native z13);
  - PRIORITY: at the fine source's zoom the merged value is the fine value (-50),
    not the base (-100) — highest-maxzoom source wins in overlap;
  - the base shows through where fine is absent (-100 present);
  - the bundle split: planet.pmtiles spans z0..macrotile_z and the deeper fine
    tiles land in one overlay-{cell}.pmtiles per populated OVERLAY_SPLIT_Z grid
    cell (so the engine covers z0..11 across bundles).

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
    planet (z0..PLANET_MAX_ZOOM) plus the grid-cell overlays above it (bundle
    routes child_z > PLANET_MAX_ZOOM into overlay-{cell}.pmtiles, not planet)."""
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


def check_shard_partition():
    """The frozen work list must give a complete, disjoint shard partition that does NOT drift
    when the set of already-built tiles changes. The bug: each shard recomputed the dirty list
    itself, so as sibling shards filled in the store the missing set — hence the [i::n] stride —
    differed per shard, and a self-heal (missing) tile could land at an index matching no
    shard's `i`. It was then never built, and downsample aborted 'pyramid incomplete'. A single
    frozen list makes every shard partition the identical work."""
    import aggregation_run
    env_force = os.environ.pop("FORCE_REBUILD", None)  # this check is about the incremental path
    tmp = tempfile.mkdtemp()
    cwd = os.getcwd()
    try:
        os.chdir(tmp)
        prev, cur = "01AAAAAAAAAAAAAAAAAAAAAAAA", "01BBBBBBBBBBBBBBBBBBBBBBBB"
        tiles = [f"8-{x}-{y}-12" for x in range(10) for y in range(10)]  # 100 same-cost tiles
        for aid in (prev, cur):
            os.makedirs(f"store/aggregation/{aid}")
            for t in tiles:
                open(f"store/aggregation/{aid}/{t}-aggregation.csv", "w").close()
        # No coverage change (identical empty CSVs) → dirty is purely the self-heal set: tiles
        # whose pmtiles is absent from the listing. Mark every other tile present.
        present = tiles[::2]
        keys = "".join(f"bathymetry/pmtiles/7-0-0/{t}.pmtiles\n" for t in present)
        with open("store/pmtiles-keys.txt", "w") as f:
            f.write(keys)
        aggregation_run.freeze()
        frozen = aggregation_run.work_list()
        missing = {f"store/aggregation/{cur}/{t}-aggregation.csv" for t in tiles if t not in present}
        assert set(frozen) == missing, f"frozen list != self-heal set (Δ {set(frozen) ^ missing})"
        n = 7
        slices = [aggregation_run.work_list()[i::n] for i in range(n)]
        flat = [fp for s in slices for fp in s]
        assert sorted(flat) == sorted(frozen), f"partition drops/dupes tiles: {set(frozen) ^ set(flat)}"
        assert len(flat) == len(set(flat)), "shards overlap"
        # Regression: the store filling in later (everything now present) must still yield the
        # SAME work — the frozen list is authoritative, so the partition cannot drift.
        with open("store/pmtiles-keys.txt", "w") as f:
            f.write("".join(f"bathymetry/pmtiles/7-0-0/{t}.pmtiles\n" for t in tiles))
        assert aggregation_run.work_list() == frozen, "work_list drifted after the live listing changed"
        print(f"shard-partition ok — {len(missing)} self-heal tiles, complete+disjoint across {n} shards, frozen vs live")
    finally:
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)
        if env_force is not None:
            os.environ["FORCE_REBUILD"] = env_force


def check_stale_overview():
    """An overview must rebuild when a child it averages is newer than it (or about to be
    self-healed), and that staleness must cascade up the pyramid. The bug: a child rebuilt by a
    later run/self-heal (no source-set change, own pmtiles present) never re-dirtied its coarse
    parent, so the parent kept averaging the old child and went stale forever (observed: a z6
    tile 4 days older than the z7 it averages)."""
    import downsampling
    env_force = os.environ.pop("FORCE_REBUILD", None)  # the incremental path
    tmp = tempfile.mkdtemp()
    cwd = os.getcwd()
    try:
        os.chdir(tmp)
        prev, cur = "01AAAAAAAAAAAAAAAAAAAAAAAA", "01BBBBBBBBBBBBBBBBBBBBBBBB"
        # An overview chain: z7 (4-4-5-7) averages a z8 base; z6 (3-2-2-6) averages the z7;
        # z5 (2-1-1-5) averages the z6. Identical in both coverings => no source-change dirt.
        chain = {  # downsampling.csv stem -> child pmtiles it references
            "4-4-5-7": "8-77-95-8.pmtiles",
            "3-2-2-6": "4-4-5-7.pmtiles",
            "2-1-1-5": "3-2-2-6.pmtiles",
        }
        for aid in (prev, cur):
            os.makedirs(f"store/aggregation/{aid}")
            open(f"store/aggregation/{aid}/8-0-0-8-aggregation.csv", "w").close()  # >=2 ids, no diff
            for stem, child in chain.items():
                with open(f"store/aggregation/{aid}/{stem}-downsampling.csv", "w") as f:
                    f.write(f"filename\n{child}\n")
        owns = [f"{stem}.pmtiles" for stem in chain] + ["8-77-95-8.pmtiles"]

        def write_listing(present, mtimes):
            with open("store/pmtiles-keys.txt", "w") as f:
                f.write("".join(f"bathymetry/pmtiles/{n}\n" for n in present))
            with open("store/pmtiles-mtimes.txt", "w") as f:
                f.write("".join(f"{ts}\tbathymetry/pmtiles/{n}\n" for n, ts in mtimes.items()))

        # All present; z7 is NEWER than the z6/z5 above it (z7 self-healed last run, parents not).
        write_listing(owns, {
            "8-77-95-8.pmtiles": "2026-06-10 00:00:00",
            "4-4-5-7.pmtiles":   "2026-06-22 00:00:00",  # fresh child
            "3-2-2-6.pmtiles":   "2026-06-18 00:00:00",  # stale: older than its z7 child
            "2-1-1-5.pmtiles":   "2026-06-18 00:00:00",  # only stale once z6 rebuilds (cascade)
        })
        dirty = {fp.split("/")[-1].replace("-downsampling.csv", "") for fp in downsampling.dirty_filepaths()}
        assert dirty == {"3-2-2-6", "2-1-1-5"}, f"stale cascade wrong: {dirty}"
        assert "4-4-5-7" not in dirty, "the fresh child must not rebuild (it is newer than its own child)"

        # Missing child => parent self-heals this run, cascading up the whole chain.
        write_listing([n for n in owns if n != "8-77-95-8.pmtiles"], {
            "4-4-5-7.pmtiles": "2026-06-22 00:00:00",
            "3-2-2-6.pmtiles": "2026-06-22 00:00:00",
            "2-1-1-5.pmtiles": "2026-06-22 00:00:00",
        })
        dirty = {fp.split("/")[-1].replace("-downsampling.csv", "") for fp in downsampling.dirty_filepaths()}
        assert dirty == {"4-4-5-7", "3-2-2-6", "2-1-1-5"}, f"missing-child cascade wrong: {dirty}"
        print("stale-overview ok — older-than-child + missing-child both rebuild and cascade up")
    finally:
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)
        if env_force is not None:
            os.environ["FORCE_REBUILD"] = env_force


def check_grid_split():
    """The bundle grid: stem_groups routes base zooms to planet and deep stems to their
    OVERLAY_SPLIT_Z cell; a coarse parent (z < split, a broad downsample extent) fans out
    to its descendant cells, and create_archive keeps only each cell's own tiles — so the
    sibling cells' archives partition the shared file exactly (no leaks, no dupes)."""
    import bundle
    import mercantile
    from pmtiles.reader import Reader, MmapSource, all_tiles
    from pmtiles.writer import Writer
    from pmtiles.tile import zxy_to_tileid, TileType, Compression

    assert bundle.stem_groups("8-77-95-8") == ["planet"], "base zoom must route to planet"
    assert bundle.stem_groups("8-77-95-14") == ["5-9-11"], "deep macrotile → its z5 cell"
    assert bundle.stem_groups("6-11-13-9") == ["5-5-6"], "overview parent → its z5 cell"
    fan = bundle.stem_groups("4-4-5-9")
    assert sorted(fan) == sorted(f"5-{x}-{y}" for x in (8, 9) for y in (10, 11)), \
        f"coarse parent must fan out to its 4 descendant cells: {fan}"

    tmp = tempfile.mkdtemp()
    cwd = os.getcwd()
    try:
        os.chdir(tmp)
        os.makedirs("store/pmtiles")
        # One single-zoom archive under the coarse parent (4,4,5) at z9 — 1024 tiles
        # spanning the 4 cells; payload = the tile id, so reads are verifiable.
        children = list(mercantile.children(mercantile.Tile(x=4, y=5, z=4), zoom=9))
        with open("store/pmtiles/4-4-5-9.pmtiles", "wb") as f:
            w = Writer(f)
            for tid in sorted(zxy_to_tileid(t.z, t.x, t.y) for t in children):
                w.write_tile(tid, str(tid).encode())
            w.finalize({"tile_type": TileType.WEBP, "tile_compression": Compression.NONE,
                        "min_zoom": 9, "max_zoom": 9, "min_lon_e7": 0, "min_lat_e7": 0,
                        "max_lon_e7": 1, "max_lat_e7": 1, "center_zoom": 9,
                        "center_lon_e7": 0, "center_lat_e7": 0}, {})
        seen = set()
        for cell in fan:
            meta = bundle.create_archive(["store/pmtiles/4-4-5-9.pmtiles"], cell)
            with open(f"store/bundle/{meta['file']}", "r+b") as f:
                for (z, x, y), data in all_tiles(Reader(MmapSource(f)).get_bytes):
                    assert bundle.cell_of(z, x, y) == cell, f"tile {(z, x, y)} leaked into {cell}"
                    assert data == str(zxy_to_tileid(z, x, y)).encode(), "payload mismatch"
                    assert (z, x, y) not in seen, f"tile {(z, x, y)} bundled twice"
                    seen.add((z, x, y))
        assert len(seen) == len(children), \
            f"cells must partition the coarse parent: {len(seen)} of {len(children)} tiles"
        print(f"grid-split ok — {len(children)} tiles partitioned across {len(fan)} cells, no leaks/dupes")
    finally:
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)


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
        run(tmp, "downsampling.py", "freeze")  # the CI path: shards + tail read this frozen list
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

        # bundle matrix + group-keys (the CI pull filter) must partition EVERY terrain
        # pmtiles across the groups: a tile in no group is missing from the bundles (a
        # hole the Worker overzooms GEBCO into); a tile in two is double-bundled. The
        # groups are planet + one overlay per z5 grid cell holding a deep (z11) tile.
        agg_dir = f"{tmp}/store/aggregation"
        agg_id = sorted(os.listdir(agg_dir))[-1]
        agg_stems = [n.replace("-aggregation.csv", "").split("-")
                     for n in os.listdir(f"{agg_dir}/{agg_id}") if n.endswith("-aggregation.csv")]
        expected = {"planet"} | {f"5-{int(x) >> (int(z) - 5)}-{int(y) >> (int(z) - 5)}"
                                 for z, x, y, cz in agg_stems if int(cz) > 10}
        cli_env = {**os.environ, "SOURCES_DIR": "sources", "PYTHONPATH": PIPE,
                   "MACROTILE_Z": "10", "NUM_OVERVIEWS": "2", "SKIP_CONTOURS": "1", "SKIP_SMOOTH": "1"}
        chunks = json.loads(subprocess.run(
            [sys.executable, os.path.join(PIPE, "bundle.py"), "matrix", "2"],
            cwd=tmp, env=cli_env, check=True, capture_output=True, text=True).stdout.splitlines()[-1])
        assert len(chunks) <= 2, f"matrix must respect the shard cap: {chunks}"
        names = [n for c in chunks for n in c["cells"].split(",") if n]
        assert sorted(names) == sorted(expected), f"unexpected groups: {sorted(names)} != {sorted(expected)}"
        gsel = {}
        for name in names:
            run(tmp, "bundle.py", "group-keys", name)
            with open(f"{tmp}/store/keys.txt") as f:
                gsel[name] = {os.path.basename(l.strip()) for l in f if l.strip()}
        gunion = set().union(*gsel.values())
        assert gunion == set(pmtiles), f"group-keys miscovers; missing {set(pmtiles) - gunion}, extra {gunion - set(pmtiles)}"
        assert sum(len(s) for s in gsel.values()) == len(gunion), "groups overlap"
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

        # covering wrote the cap into child_z: the deepest aggregation tile is z11, not z13.
        child_zs = [int(cz) for _, _, _, cz in agg_stems]
        assert max(child_zs) == 11, f"cap not applied (want 11): child_z={sorted(set(child_zs))}"

        # Bundle split: planet caps at macrotile_z (10); the z11 fine tiles route to
        # their grid cells' overlay archives, and the manifest records the cells.
        assert os.path.exists(f"{tmp}/store/bundle/planet.pmtiles"), "missing planet.pmtiles"
        for cell in expected - {"planet"}:
            assert os.path.exists(f"{tmp}/store/bundle/overlay-{cell}.pmtiles"), f"missing overlay {cell}"
        mf = json.load(open(f"{tmp}/store/bundle/manifest.json"))
        assert mf["planet"]["max_zoom"] == 10, f"planet cap wrong: {mf['planet']}"
        assert set(mf["overlay"]["cells"]) == expected - {"planet"}, f"manifest cells: {mf['overlay']}"
        assert all(v == 11 for v in mf["overlay"]["cells"].values()), f"cell max_zoom: {mf['overlay']}"
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
    check_shard_partition()
    check_stale_overview()
    check_grid_split()
    main()
