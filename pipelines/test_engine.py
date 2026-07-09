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
import math
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
    # depare still runs (all-negative sources -> depth bands only, no drying/nodata, no mask
    # needed); check_depare_drying / check_depare_water exercise those paths against real masks.
    env = {**os.environ, "SOURCES_DIR": "sources", "PYTHONPATH": PIPE,
           "MACROTILE_Z": "10", "NUM_OVERVIEWS": "2", "SKIP_CONTOURS": "1", "SKIP_SMOOTH": "1"}
    subprocess.run([sys.executable, os.path.join(PIPE, args[0]), *args[1:]],
                   cwd=tmp, env=env, check=True)


def make_source(tmp, sid, west, north, deg, px, value, max_zoom, extra_meta=None, deg_ns=None):
    """A constant-value EPSG:4326 source COG + metadata.json. Square (deg x deg) by default;
    pass deg_ns for a rectangle (north-south degrees). extra_meta merges extra metadata keys."""
    os.makedirs(f"{tmp}/sources/{sid}", exist_ok=True)
    os.makedirs(f"{tmp}/store/source/{sid}", exist_ok=True)
    arr = np.full((px, px), value, dtype="float32")
    res_x, res_y = deg / px, (deg if deg_ns is None else deg_ns) / px
    with rasterio.open(f"{tmp}/store/source/{sid}/{sid}_0.tif", "w", driver="GTiff",
                       height=px, width=px, count=1, dtype="float32", nodata=-9999,
                       crs="EPSG:4326", transform=from_origin(west, north, res_x, res_y)) as d:
        d.write(arr, 1)
    meta = {"name": sid, "max_zoom": max_zoom, **(extra_meta or {})}
    with open(f"{tmp}/sources/{sid}/metadata.json", "w") as f:
        json.dump(meta, f)


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


def check_weighted_shards():
    """Downsample deep shards bin-pack ancestors by work (parent-webp count), not
    ancestor count: the count-sized stride left a 77-minute straggler shard next to
    hundreds of spin-up-only ones. The partition must stay complete + disjoint (every
    consumer derives the same bins from the frozen list), n must self-size to
    total/heaviest, and no bin may exceed 2x the heaviest ancestor (the LPT bound)."""
    import downsampling
    import utils
    tmp = tempfile.mkdtemp()
    cwd = os.getcwd()
    try:
        os.chdir(tmp)
        aid = "01CCCCCCCCCCCCCCCCCCCCCCCC"
        os.makedirs(f"store/aggregation/{aid}")
        rz = downsampling.SHARD_ROOT_Z
        # 3 deep subtrees (extent -> extent+3: 4**3 = 64 parent webps each) among 40
        # couple-of-overview ancestors (weight 1) — the planet build's shape in miniature.
        heavies = [f"store/aggregation/{aid}/{rz}-{x}-0-{rz + 3}-downsampling.csv" for x in range(3)]
        lights = [f"store/aggregation/{aid}/{rz}-{x}-{y}-{rz}-downsampling.csv"
                  for x in range(8) for y in range(1, 6)]
        with open(f"store/aggregation/{aid}/{downsampling.FROZEN}", "w") as f:
            f.write("".join(fp + "\n" for fp in heavies + lights))
        w = downsampling.ancestor_weights()
        assert len(w) == 43 and sorted(w.values(), reverse=True)[:3] == [64, 64, 64], w
        # matrix sizing: enough bins that each ~= the heaviest subtree — not one per ancestor
        n = math.ceil(sum(w.values()) / max(w.values()))
        assert n == 4, f"expected 4 work-sized bins for 43 ancestors, got {n}"
        owned = [downsampling.owned_ancestors(i, n) for i in range(n)]
        flat = [a for s in owned for a in s]
        assert len(flat) == len(set(flat)) == len(w), "bins must partition the ancestors"
        loads = [sum(w[a] for a in s) for s in owned]
        assert max(loads) < 2 * max(w.values()), f"a bin exceeds the LPT bound: {loads}"
        # the pure packer (also chunks the bundle groups): heaviest first into lightest bin
        assert utils.lpt_bins({"a": 5, "b": 3, "c": 3, "d": 1}, 2) == [["a", "d"], ["b", "c"]]
        print(f"weighted-shards ok — {len(w)} ancestors -> {n} bins, loads {sorted(loads, reverse=True)}")
    finally:
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)


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


def check_land_clamp():
    """The flag-gated post-warp land clamp, through the REAL reproject(): a `land_clamp`
    coarse source's negatives under the land mask go to 0, an unflagged finer source's
    negatives survive (provenance by construction), and the mask rasterizes onto the exact
    buffered -te/-tr the warp uses (alignment → seam determinism; a mismatch would raise).

    Not covered here (holds by construction, so not worth standing up the vector stages):
    clamped land reads 0, so gdal_contour's negative levels and soundings' negative-only
    candidate search both find nothing on it — the terrain assertion is the load-bearing one.
    """
    import mercantile
    import config
    import aggregation_reproject

    tmp = tempfile.mkdtemp()
    cwd, saved_dir = os.getcwd(), config.SOURCES_DIR
    saved_landmask = os.environ.pop("LANDMASK", None)  # else an exported dev mask overrides the
    saved_watermask = os.environ.pop("WATERMASK", None)  # synthetic one → geography-dependent
    try:                                                 # failure; this tile has no inland water
        os.chdir(tmp)
        config.SOURCES_DIR = "sources"  # in-process reproject() reads config.SOURCES_DIR directly

        tile = mercantile.Tile(x=75, y=96, z=8)  # the plan's NY-harbor example tile
        w, s, e, n = mercantile.bounds(tile)
        mid_lon, mid_lat = (w + e) / 2, (s + n) / 2

        # coarse flagged (-5) fills the whole tile; fine unflagged (-8) covers only the west half.
        # (make_source builds each source COG + metadata on the shared source-store contract.)
        make_source(tmp, "cbase", w, n, e - w, 256, -5.0, 10, extra_meta={"land_clamp": True}, deg_ns=n - s)
        make_source(tmp, "cfine", w, n, mid_lon - w, 256, -8.0, 11, deg_ns=n - s)

        # land = the north half of the tile (+ generous halo), water = the south half.
        gj = "land.geojson"
        with open(gj, "w") as f:
            json.dump({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {},
                       "geometry": {"type": "Polygon", "coordinates": [[[w - 1, mid_lat], [e + 1, mid_lat],
                                    [e + 1, n + 1], [w - 1, n + 1], [w - 1, mid_lat]]]}}]}, f)
        os.makedirs("store/landmask", exist_ok=True)
        subprocess.run(["ogr2ogr", "-f", "FlatGeobuf", "-t_srs", "EPSG:3857", "-overwrite",
                        "store/landmask/land.fgb", gj], check=True)

        aid = "01CLAMPCLAMPCLAMPCLAMPCLAMP"
        os.makedirs(f"store/aggregation/{aid}")
        csv = f"store/aggregation/{aid}/8-75-96-11-aggregation.csv"
        with open(csv, "w") as f:
            f.write("source,filename,maxzoom\ncfine,cfine_0.tif,11\ncbase,cbase_0.tif,10\n")

        aggregation_reproject.reproject(csv)

        tdir = f"store/aggregation/{aid}/8-75-96-11-tmp"

        def sample(tiff, row_frac, col_frac):
            with rasterio.open(tiff) as r:
                arr = r.read(1)
            return float(arr[int(r.height * row_frac), int(r.width * col_frac)])

        # cbase (group 1, flagged): north interior clamped to 0, south interior still -5.
        assert sample(f"{tdir}/1-3857.tiff", 0.25, 0.5) == 0.0, "coarse land negative must clamp to 0"
        assert sample(f"{tdir}/1-3857.tiff", 0.75, 0.5) == -5.0, "coarse water must survive the clamp"
        # cfine (group 0, unflagged): its negatives survive even under land (provenance).
        assert sample(f"{tdir}/0-3857.tiff", 0.25, 0.25) == -8.0, "unflagged fine source must be untouched"
        print("land-clamp ok — flagged coarse land→0, unflagged fine survives, mask aligned to warp")
    finally:
        config.SOURCES_DIR = saved_dir
        if saved_landmask is not None:
            os.environ["LANDMASK"] = saved_landmask
        if saved_watermask is not None:
            os.environ["WATERMASK"] = saved_watermask
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)


def check_water_clamp():
    """Part 2.5 (#24) inverse clamp through the REAL reproject(): a `land_clamp` coarse source's
    fabricated POSITIVE land over mapped inland water is cleared to nodata (the merge then fills it
    to 0 and Part 3 renders a nodata depth-area), while the same source's positive elsewhere — land,
    and the surrounding ocean that has NO water polygon — survives, and an unflagged source is never
    touched. Keys on the water-only mask, so ocean (== lake in the combined mask) stays safe."""
    import mercantile
    import rasterio.warp
    import config
    import aggregation_reproject

    tmp = tempfile.mkdtemp()
    cwd, saved_dir = os.getcwd(), config.SOURCES_DIR
    saved_landmask = os.environ.pop("LANDMASK", None)
    saved_watermask = os.environ.pop("WATERMASK", None)
    try:
        os.chdir(tmp)
        config.SOURCES_DIR = "sources"
        tile = mercantile.Tile(x=75, y=96, z=8)
        w, s, e, n = mercantile.bounds(tile)
        # a lake box in the tile interior (0.4..0.6 of each span, well inside the halo)
        lw, ls = w + (e - w) * 0.4, s + (n - s) * 0.4
        le, ln = w + (e - w) * 0.6, s + (n - s) * 0.6

        # flagged coarse source: +50 across the whole tile (GEBCO's false land, incl. over the lake)
        make_source(tmp, "cbase", w, n, e - w, 256, 50.0, 10, extra_meta={"land_clamp": True}, deg_ns=n - s)
        # unflagged fine source: +50 over the west half — must survive even inside the lake
        make_source(tmp, "cfine", w, n, (w + e) / 2 - w, 256, 50.0, 11, deg_ns=n - s)

        os.makedirs("store/landmask", exist_ok=True)
        with open("land.geojson", "w") as f:  # land = whole tile + halo (the land clamp needs a mask)
            json.dump({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {},
                       "geometry": {"type": "Polygon", "coordinates": [[[w - 1, s - 1], [e + 1, s - 1],
                                    [e + 1, n + 1], [w - 1, n + 1], [w - 1, s - 1]]]}}]}, f)
        subprocess.run(["ogr2ogr", "-f", "FlatGeobuf", "-t_srs", "EPSG:3857", "-overwrite",
                        "store/landmask/land.fgb", "land.geojson"], check=True)
        with open("water.geojson", "w") as f:  # water = the lake box, with a kind like the real feed
            json.dump({"type": "FeatureCollection", "features": [{"type": "Feature",
                       "properties": {"kind": "lake"}, "geometry": {"type": "Polygon", "coordinates":
                       [[[lw, ls], [le, ls], [le, ln], [lw, ln], [lw, ls]]]}}]}, f)
        subprocess.run(["ogr2ogr", "-f", "FlatGeobuf", "-t_srs", "EPSG:3857", "-overwrite",
                        "store/landmask/water.fgb", "water.geojson"], check=True)
        os.environ["LANDMASK"] = "store/landmask/land.fgb"
        os.environ["WATERMASK"] = "store/landmask/water.fgb"

        aid = "01WATERWATERWATERWATERWATE"
        os.makedirs(f"store/aggregation/{aid}")
        csv = f"store/aggregation/{aid}/8-75-96-11-aggregation.csv"
        with open(csv, "w") as f:
            f.write("source,filename,maxzoom\ncfine,cfine_0.tif,11\ncbase,cbase_0.tif,10\n")
        aggregation_reproject.reproject(csv)
        tdir = f"store/aggregation/{aid}/8-75-96-11-tmp"

        def at(tiff, lon, lat):
            with rasterio.open(tiff) as r:
                xs, ys = rasterio.warp.transform("EPSG:4326", r.crs, [lon], [lat])
                row, col = r.index(xs[0], ys[0])
                return float(r.read(1)[row, col]), bool(r.read_masks(1)[row, col])

        lake = ((lw + le) / 2, (ls + ln) / 2)               # inside the lake
        west_lake = (lw + (le - lw) * 0.1, (ls + ln) / 2)   # inside the lake AND the west half (cfine)
        outside = (w + (e - w) * 0.05, s + (n - s) * 0.05)  # a land/ocean corner, no water polygon

        v_lake, ok_lake = at(f"{tdir}/1-3857.tiff", *lake)
        assert not ok_lake, "flagged positive over inland water must clear to nodata"
        v_out, ok_out = at(f"{tdir}/1-3857.tiff", *outside)
        assert ok_out and v_out == 50.0, "flagged positive over non-water (land/ocean) must survive"
        v_un, ok_un = at(f"{tdir}/0-3857.tiff", *west_lake)
        assert ok_un and v_un == 50.0, "unflagged source must be untouched, even inside the lake"
        print("water-clamp ok — flagged +land over water→nodata, land/ocean kept, unflagged survives")
    finally:
        config.SOURCES_DIR = saved_dir
        if saved_landmask is not None:
            os.environ["LANDMASK"] = saved_landmask
        if saved_watermask is not None:
            os.environ["WATERMASK"] = saved_watermask
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)


def check_feather_guard():
    """The merge seam feather must not manufacture water (Part 1's stage invariant, the reason
    the source-blind post-merge re-clamp could be deleted): a pixel entering the blend >= 0
    — clamped land, real land topo, or a 0-filled nodata hole — may not leave it < 0.

    Through the REAL aggregation_merge.merge(): a land stripe (0) and a negative water stripe
    (-5) from two sources meet at a feathered source seam, with an uncovered hole carved into
    the water so the nodata-origin path is exercised too (its pre-feather value is the 0-fill,
    which the guard snapshots AFTER the fill). Assert every pixel that entered >= 0 stays >= 0.

    Load-bearing: without the guard the 0-land pixels on the seam and the 0-filled hole pixels
    within feather reach of it blur toward -5 and go negative — the false water rim the guard
    exists to prevent.
    """
    import aggregation_merge

    tmp = tempfile.mkdtemp()
    cwd = os.getcwd()
    try:
        os.chdir(tmp)
        aid = "01FEATHERFEATHERFEATHERFE"
        stem = "8-0-0-8"
        tdir = f"store/aggregation/{aid}/{stem}-tmp"
        os.makedirs(tdir)

        h = w = 96
        # source 0 (higher priority): left half = 0 (clamped land); right half nodata.
        s0 = np.full((h, w), -9999.0, dtype="float32")
        s0[:, :48] = 0.0
        # source 1 (lower priority): right half = -5 (water), except a hole neither source fills
        # -> merge 0-fills it, and the feather (centred on the col-48 seam) can reach it.
        s1 = np.full((h, w), -9999.0, dtype="float32")
        s1[:, 48:] = -5.0
        s1[40:56, 50:56] = -9999.0
        tr = from_origin(0, h, 1, 1)
        for name, arr in (("0-3857.tiff", s0), ("1-3857.tiff", s1)):
            with rasterio.open(f"{tdir}/{name}", "w", driver="GTiff", height=h, width=w, count=1,
                               dtype="float32", nodata=-9999, crs="EPSG:3857", transform=tr) as dd:
                dd.write(arr, 1)
        with open(f"{tdir}/reprojection.json", "w") as f:
            json.dump({"buffer_pixels": 16}, f)  # -> a real feather (sigma ~ 3)

        aggregation_merge.merge(f"store/aggregation/{aid}/{stem}-aggregation.csv")
        with rasterio.open(f"{tdir}/2-3857.tiff") as r:  # merge writes {len(tiffs)}-3857.tiff
            out = r.read(1)

        # pre-feather value per pixel: source 0 where valid, else source 1 where valid, else the
        # 0-fill of a truly-uncovered pixel — exactly the set the guard must keep >= 0.
        pre = np.where(s0 != -9999.0, s0, np.where(s1 != -9999.0, s1, 0.0))
        nonneg = pre >= 0
        assert (out[nonneg] >= 0).all(), \
            f"feather manufactured water: {int((out[nonneg] < 0).sum())} of {int(nonneg.sum())} " \
            ">=0 pixels went negative"
        hole = np.zeros((h, w), bool)
        hole[40:56, 50:56] = True
        assert (out[hole] >= 0).all(), "a 0-filled nodata hole bordering water must not exit negative"
        # non-vacuous: the feather ran and real water survived it (else >=0-stays->=0 is trivial).
        assert (out[:, 60:] < 0).any(), "test is vacuous — no negative water survived the merge"
        print(f"feather-guard ok — {int(nonneg.sum())} >=0 pixels stayed >=0 across the seam feather")
    finally:
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)


def check_depare_drying():
    """Contour-derived drying through the REAL depare_run.generate(): the metre ladder's
    [0, DRYING_CAP] bucket, cut to EFFECTIVE water = OSM land ∖ inland water. A foreshore
    (+2) DEM meets the OSM land + inland-water masks, so the assertions cover the load-bearing
    cases: a foreshore seaward of the land line -> drying; the SAME height inside effective land
    (land, no water) -> cut, NOT drying; a foreshore inside a water polygon nested in the land
    coverage -> STILL drying (the ICW/tidal-channel case — cutting by RAW land would delete it);
    drying carries drval1 < 0, drval2 = 0, NO sys, the drying rank; bands ∪ drying ∪ nodata stay
    pairwise disjoint; and two adjacent tiles' drying meets at the shared seam (deterministic on
    the buffered grid, each clipped exactly to its bbox)."""
    import geopandas as gpd
    import mercantile
    from pyproj import Transformer
    from shapely.geometry import Point, box

    import config
    import depare_run
    from aggregation_reproject import get_resolution

    tmp = tempfile.mkdtemp()
    cwd = os.getcwd()
    saved_landmask = os.environ.pop("LANDMASK", None)    # else an exported dev mask overrides
    saved_watermask = os.environ.pop("WATERMASK", None)  # the synthetic ones
    cap = config.DRYING_CAP
    aid = "01DRYGEODRYGEODRYGEODRYGEO"
    try:
        os.chdir(tmp)
        to4326 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
        left = mercantile.Tile(x=301, y=384, z=10)
        right = mercantile.Tile(x=302, y=384, z=10)          # shares left's east edge
        bl, br = mercantile.xy_bounds(left), mercantile.xy_bounds(right)
        wL, wR = bl.right - bl.left, br.right - br.left
        m = 1000.0  # y margin (metres) so the mask boxes overhang the tile height

        os.makedirs("store/landmask", exist_ok=True)
        # land covers left [0.15..1.0] + right [0..0.30]; inland water (a channel) covers left
        # [0.55..1.0] + right [0..0.30] — nested INSIDE the land, the ICW geometry. The land box
        # reaches past the seam so both tiles' east/west foreshore is under land.
        land_box = box(bl.left + 0.15 * wL, bl.bottom - m, br.left + 0.30 * wR, bl.top + m)
        water_box = box(bl.left + 0.55 * wL, bl.bottom - m, br.left + 0.30 * wR, bl.top + m)
        gpd.GeoDataFrame(geometry=[land_box], crs="EPSG:3857").to_file(
            "store/landmask/land.fgb", driver="FlatGeobuf")
        gpd.GeoDataFrame({"kind": ["lake"]}, geometry=[water_box], crs="EPSG:3857").to_file(
            "store/landmask/water.fgb", driver="FlatGeobuf")
        os.environ["LANDMASK"] = "store/landmask/land.fgb"
        os.environ["WATERMASK"] = "store/landmask/water.fgb"

        NODATA = -9999.0

        def build(tile, cols):
            """Write a merged DEM (foreshore +2 with the given -25 band / NODATA column spans on a
            +2 baseline) for `tile` on its native 3857 grid, run generate()."""
            stem = f"{tile.z}-{tile.x}-{tile.y}-{tile.z}"  # child_z == z: a native 512 px tile
            tdir = f"store/aggregation/{aid}/{stem}-tmp"
            os.makedirs(tdir, exist_ok=True)
            b = mercantile.xy_bounds(tile)
            res = get_resolution(tile.z)
            px = round((b.right - b.left) / res)
            tr = from_origin(b.left, b.top, res, res)
            dem = np.full((px, px), 2.0, dtype="float32")  # [0, cap] foreshore baseline
            for lo, hi, v in cols:
                dem[:, int(lo * px):int(hi * px)] = v
            with rasterio.open(f"{tdir}/0-3857.tiff", "w", driver="GTiff", height=px, width=px,
                               count=1, dtype="float32", nodata=NODATA, crs="EPSG:3857",
                               transform=tr) as d:
                d.write(dem, 1)
            depare_run.generate(f"store/aggregation/{aid}/{stem}-aggregation.csv")
            return gpd.read_file(f"store/depare/{stem}.fgb")

        # left: open foreshore | cut foreshore (under land) | -25 band | NODATA | ICW foreshore
        # (under land AND water, reaching the east seam). right: ICW foreshore reaching the west
        # seam | -25 band. The +2 baseline fills the rest (open on the west, ICW on the east).
        gL = build(left, [(0.35, 0.55, -25.0), (0.55, 0.70, NODATA)])
        gR = build(right, [(0.30, 1.0, -25.0)])

        def frac_pt(b, fx, fy):
            return Point(*to4326.transform(b.left + (b.right - b.left) * fx,
                                           b.bottom + (b.top - b.bottom) * fy))
        openp = frac_pt(bl, 0.07, 0.5)    # foreshore seaward of the land line -> drying
        cut = frac_pt(bl, 0.25, 0.5)      # foreshore inside effective land (no water) -> NOT drying
        band = frac_pt(bl, 0.45, 0.5)     # -25 -> a depth band
        nod = frac_pt(bl, 0.62, 0.5)      # NODATA under the water polygon -> nodata
        icw = frac_pt(bl, 0.85, 0.5)      # foreshore inside water nested in land -> STILL drying

        assert gL is not None and len(gL), "left tile must produce depare features"
        dry = gL[gL["drval1"] < 0]
        assert len(dry), "the foreshore must produce drying features"
        assert dry.covers(openp).any(), "foreshore seaward of the land line must be drying"
        assert not gL.covers(cut).any(), "foreshore inside effective land (no water) must be cut"
        assert dry.covers(icw).any(), \
            "foreshore inside a water polygon nested in land must stay drying (the ICW case)"

        # drying schema: drval1 = -cap, drval2 = 0, NO sys, the drying rank.
        assert (dry["drval1"] == -cap).all() and (dry["drval2"] == 0.0).all(), "drying drval schema"
        assert dry["sys"].isna().all(), "drying ships once, with no sys"
        assert (dry["rank"] == depare_run.DRYING_RANK).all(), "drying rank"

        # The three kinds coexist and are pairwise disjoint (per-point, and by area).
        assert (gL[gL.covers(band)]["drval1"] == 20.0).any(), "-25 m -> the [20,30] band"
        nd = gL[gL.covers(nod)]
        assert len(nd) and nd["drval1"].isna().all() and (nd["kind"] == "lake").all(), \
            "NODATA under the water polygon -> nodata (kind, no drval1)"
        kinds = {"band": gL[gL["drval1"] >= 0], "drying": dry, "nodata": gL[gL["drval1"].isna()]}
        assert all(len(v) for v in kinds.values()), "left tile must carry all three depare kinds"
        from shapely.ops import unary_union
        merged = {k: unary_union(list(v.geometry)) for k, v in kinds.items()}
        for a, bb in (("band", "drying"), ("band", "nodata"), ("drying", "nodata")):
            inter = merged[a].intersection(merged[bb]).area
            assert inter < 1e-6 * gL.geometry.area.sum(), f"{a} ∩ {bb} not disjoint ({inter})"

        # Seam: left's ICW drying reaches its east edge, right's reaches its west edge, same
        # longitude — the drying fills meet across the boundary (the bands' seam contract).
        seam = mercantile.bounds(left).east
        dryR = gR[gR["drval1"] < 0]
        assert len(dryR), "right tile must carry drying at the shared seam"
        assert abs(dry.total_bounds[2] - seam) < 1e-4, f"left drying stops short of the seam: {dry.total_bounds[2]}"
        assert abs(dryR.total_bounds[0] - seam) < 1e-4, f"right drying stops short of the seam: {dryR.total_bounds[0]}"
        print("depare-drying ok — foreshore drying, effective-land cut, ICW kept, disjoint, seam meets")
    finally:
        if saved_landmask is not None:
            os.environ["LANDMASK"] = saved_landmask
        else:
            os.environ.pop("LANDMASK", None)
        if saved_watermask is not None:
            os.environ["WATERMASK"] = saved_watermask
        else:
            os.environ.pop("WATERMASK", None)
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)


def check_depare():
    """The depare fork through the REAL depare_run.generate(): off a merged DEM it buckets the
    water into depth-band partitions (drval1/drval2, both level ladders, tagged sys). Two
    adjacent tiles carry the same depth bands across their shared edge, so the assertions
    cover: exactly one m-partition with the right bucket over each band, land dropped, the
    fathom-curve set present, and — the seam contract — both tiles' partitions meet at the
    boundary (each clipped exactly to its bbox)."""
    import geopandas as gpd
    import mercantile
    from pyproj import Transformer
    from shapely.geometry import Point

    import config
    import depare_run
    from aggregation_reproject import get_resolution

    tmp = tempfile.mkdtemp()
    cwd = os.getcwd()
    aid = "01DEPAREDEPAREDEPAREDEPARE"
    try:
        os.chdir(tmp)
        to4326 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)

        def build(tile):
            """Write a merged DEM for `tile` on its native 3857 grid, run generate()."""
            stem = f"{tile.z}-{tile.x}-{tile.y}-{tile.z}"  # child_z == z: a native 512 px tile
            tdir = f"store/aggregation/{aid}/{stem}-tmp"
            os.makedirs(tdir, exist_ok=True)
            b = mercantile.xy_bounds(tile)
            res = get_resolution(tile.z)
            px = round((b.right - b.left) / res)
            tr = from_origin(b.left, b.top, res, res)
            q = px // 4
            dem = np.full((px, px), -150.0, dtype="float32")  # [100,200] bucket baseline
            dem[:q, :] = config.DRYING_CAP + 50  # land above the cap -> neither band nor drying
            dem[q:2 * q, :] = -1.0       # -> the [0,2] bucket
            dem[2 * q:3 * q, :] = -25.0  # -> the [20,30] bucket
            with rasterio.open(f"{tdir}/0-3857.tiff", "w", driver="GTiff", height=px, width=px,
                               count=1, dtype="float32", nodata=-9999, crs="EPSG:3857",
                               transform=tr) as d:
                d.write(dem, 1)
            depare_run.generate(f"store/aggregation/{aid}/{stem}-aggregation.csv")
            row_lonlat = lambda r: to4326.transform(*(tr * (px // 2 + 0.5, r + 0.5)))
            return (gpd.read_file(f"store/depare/{stem}.fgb"),
                    {"land": Point(row_lonlat(q // 2)),
                     "shoal": Point(row_lonlat(q + q // 2)),
                     "mid": Point(row_lonlat(2 * q + q // 2)),
                     "deep": Point(row_lonlat(3 * q + q // 2))})

        left = mercantile.Tile(x=301, y=384, z=10)
        right = mercantile.Tile(x=302, y=384, z=10)          # shares left's east edge
        gL, pts = build(left)
        gR, _ = build(right)
        m = gL[gL["sys"] == "m"]

        def bucket(pt):
            hit = m[m.covers(pt)]
            assert len(hit) == 1, f"exactly one m-partition must cover the point, got {len(hit)}"
            return (hit.iloc[0]["drval1"], hit.iloc[0]["drval2"])

        assert bucket(pts["shoal"]) == (0.0, 2.0), "-1 m must land in the [0,2] bucket"
        assert bucket(pts["mid"]) == (20.0, 30.0), "-25 m must land in the [20,30] bucket"
        assert bucket(pts["deep"]) == (100.0, 200.0), "-150 m must land in the [100,200] bucket"
        assert not gL.covers(pts["land"]).any(), "no partition may cover land"
        assert (gL["sys"] == "ft").any(), "the fathom-curve partition set must be present"

        # Seam: left's partitions reach its east edge, right's reach its west edge, and they are
        # the same longitude (each clipped exactly to its bbox) — the band fills meet across it.
        seam = mercantile.bounds(left).east
        assert abs(gL.total_bounds[2] - seam) < 1e-4, f"left depare stops short of the seam: {gL.total_bounds[2]}"
        assert abs(gR.total_bounds[0] - seam) < 1e-4, f"right depare stops short of the seam: {gR.total_bounds[0]}"
        print("depare ok — bands bucketed to their ladder levels, land dropped, tiles meet at the seam")
    finally:
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)


def check_depare_water():
    """Part 3 nodata through the REAL depare_run.generate(): with the inland-water polygons
    published (WATERMASK), an unsurveyed lake the merge left as NODATA yields the `nodata` kind
    (carries `kind`, NO drval1/drval2/sys, the nodata rank) while the surveyed part yields depth
    bands; nodata∩bands is empty (water MINUS the band coverage); neighbouring tiles' nodata meet
    at the seam; and a tile with no water polygon (ocean) gains no nodata. Drying's derivation is
    exercised in check_depare_drying."""
    import geopandas as gpd
    import mercantile
    import numpy as np
    from pyproj import Transformer
    from shapely.geometry import Point, box

    import depare_run
    from aggregation_reproject import get_resolution

    tmp = tempfile.mkdtemp()
    cwd = os.getcwd()
    saved_watermask = os.environ.get("WATERMASK")
    aid = "01DEPAREWATERDEPAREWATERDE"
    NODATA = -9999.0
    try:
        os.chdir(tmp)
        to4326 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
        left = mercantile.Tile(x=301, y=384, z=10)
        right = mercantile.Tile(x=302, y=384, z=10)          # shares left's east edge
        ocean = mercantile.Tile(x=350, y=384, z=10)          # no water polygon reaches it

        # One inland-water polygon covering left+right (not the ocean tile), with a `kind`.
        bl, br = mercantile.xy_bounds(left), mercantile.xy_bounds(right)
        wbox = box(min(bl.left, br.left), min(bl.bottom, br.bottom),
                   max(bl.right, br.right), max(bl.top, br.top))
        os.makedirs("store/landmask", exist_ok=True)
        gpd.GeoDataFrame({"kind": ["lake"]}, geometry=[wbox], crs="EPSG:3857").to_file(
            "store/landmask/water.fgb", driver="FlatGeobuf")
        os.environ["WATERMASK"] = "store/landmask/water.fgb"

        def build(tile, lake_side=None, deep=False):
            stem = f"{tile.z}-{tile.x}-{tile.y}-{tile.z}"  # child_z == z: a native 512 px tile
            tdir = f"store/aggregation/{aid}/{stem}-tmp"
            os.makedirs(tdir, exist_ok=True)
            b = mercantile.xy_bounds(tile)
            res = get_resolution(tile.z)
            px = round((b.right - b.left) / res)
            tr = from_origin(b.left, b.top, res, res)
            if deep:
                dem = np.full((px, px), -50.0, dtype="float32")   # ocean: all one band, no lake
            else:
                dem = np.full((px, px), -25.0, dtype="float32")   # surveyed baseline ([20,30] band)
                q = px // 2
                if lake_side == "east":
                    dem[:, q:] = NODATA  # unsurveyed lake (no depth -> nodata), reaching the east seam
                elif lake_side == "west":
                    dem[:, :q] = NODATA  # unsurveyed lake -> nodata, reaching the west seam
            with rasterio.open(f"{tdir}/0-3857.tiff", "w", driver="GTiff", height=px, width=px,
                               count=1, dtype="float32", nodata=NODATA, crs="EPSG:3857",
                               transform=tr) as d:
                d.write(dem, 1)
            depare_run.generate(f"store/aggregation/{aid}/{stem}-aggregation.csv")
            path = f"store/depare/{stem}.fgb"
            return gpd.read_file(path) if os.path.isfile(path) else None

        gL = build(left, lake_side="east")
        gR = build(right, lake_side="west")
        gO = build(ocean, deep=True)

        def frac_pt(b, fx, fy):
            return Point(*to4326.transform(b.left + (b.right - b.left) * fx,
                                           b.bottom + (b.top - b.bottom) * fy))
        surveyed = frac_pt(bl, 0.25, 0.5)     # west half, DEM -25 -> a band
        lake = frac_pt(bl, 0.75, 0.5)         # east half, NODATA -> nodata

        assert gL is not None and len(gL), "left tile must produce depare features"
        band = gL["drval1"] >= 0
        nodata = gL["drval1"].isna()
        assert band.any() and nodata.any(), "left tile must carry both depth bands and nodata"

        # nodata: carries kind, NO drval1/drval2/sys, the nodata rank
        nd = gL[nodata]
        assert (nd["kind"] == "lake").all(), "nodata must carry the water polygon's Overture kind"
        assert nd["drval2"].isna().all(), "nodata must carry no drval2"
        assert nd["sys"].isna().all(), "nodata must carry no sys (emitted once, unit-independent)"
        assert (nd["rank"] == depare_run.NODATA_RANK).all(), "nodata rank"
        assert (gL[band]["rank"] == depare_run.BAND_RANK).all(), "band rank"

        # nodata ∩ bands empty (nodata is the water MINUS the band coverage)
        assert (gL[gL.covers(surveyed)]["drval1"] >= 0).all(), "surveyed water -> a depth band"
        assert not gL[gL.covers(surveyed)]["drval1"].isna().any(), "surveyed water must not be nodata"
        lh = gL[gL.covers(lake)]
        assert len(lh) and lh["drval1"].isna().all(), "the unsurveyed lake -> nodata only (no band)"

        # Seam: left's lake nodata reaches its east edge, right's reaches its west edge — they meet.
        seam = mercantile.bounds(left).east
        ndL, ndR = gL[gL["drval1"].isna()], gR[gR["drval1"].isna()]
        assert len(ndL) and len(ndR), "both tiles must carry nodata at the shared seam"
        assert abs(ndL.total_bounds[2] - seam) < 1e-4, f"left nodata stops short of the seam: {ndL.total_bounds[2]}"
        assert abs(ndR.total_bounds[0] - seam) < 1e-4, f"right nodata stops short of the seam: {ndR.total_bounds[0]}"

        # Ocean: bands, but NO nodata (no water polygon reaches this tile).
        assert gO is not None and len(gO) and (gO["drval1"] >= 0).all(), \
            "ocean tile must be all depth bands — no water polygon, so no nodata"
        print("depare-water ok — unsurveyed lake→nodata(kind), nodata∩bands empty, seam meets, ocean none")
    finally:
        if saved_watermask is None:
            os.environ.pop("WATERMASK", None)
        else:
            os.environ["WATERMASK"] = saved_watermask
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
    import depare_run
    import landmask
    landmask._check()
    depare_run._check()
    check_priority()
    check_shard_partition()
    check_weighted_shards()
    check_stale_overview()
    check_grid_split()
    check_feather_guard()
    check_land_clamp()
    check_water_clamp()
    check_depare()
    check_depare_drying()
    check_depare_water()
    main()
