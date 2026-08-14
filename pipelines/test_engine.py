"""End-to-end self-check for the ENGINE (Snakemake) build lane.

Builds two synthetic sources in an isolated tmp dir — a coarse broad ``base``
(-101 m, native ~z10) and a fine small ``fine`` (-51 m, native ~z13 but capped to
z11 in metadata) inside it — registers them through the real stage-1 CLIs (bounds
→ catalog → covering), then drives the real stage-2/3 engine DAG through the ONE Snakefile — past the `cover`
checkpoint that gates stage 2/3 (``snakemake bundles``) — and asserts:

  - the per-source max_zoom CAP binds (fine renders at z11, not its native z13);
  - PRIORITY: at the fine source's zoom the merged value is the fine value (-51),
    not the base (-101) — highest-maxzoom source wins in overlap;
  - the base shows through where fine is absent (-101 present);
  - the bundle split: planet.pmtiles caps at macrotile_z and the deeper fine tiles
    land in one overlay-{cell}.pmtiles per populated OVERLAY_SPLIT_Z grid cell;
  - a no-op rerun schedules ZERO jobs (engine provenance, no covering diff);
  - a CONTOUR_LEVELS change reruns contour_tile + the vector shallow/cell/join rules
    but ZERO mosaic_tile and ZERO terrain_render (the stage split's payoff — no
    re-merge, no re-render — proven by a dry run's rerun table);
  - the sharded vector bundle: a global shallow run (z < SPLIT_Z, minzoom-filtered) +
    one variable-depth run per SPLIT_Z cell (each fringe-filtered to its owned subtree),
    `pmtiles merge`d into a single sparse vector.pmtiles (no separate soundings/depare
    archives); tile ownership is STRICTLY disjoint (shallow below SPLIT_Z, cells at/above
    it with zero cross-cell overlap), and the merged archive's self-check
    invariants hold (no depare below z6, no contour below its tier minzoom, depare
    m-bands disjoint, soundings present, every input id survives); the manifest carries
    vector.max_zoom = the covering's max child_z (the Worker's serving depth);
  - scope transitions: a bbox build re-merges nothing, and the next planet build
    heals the bbox-truncated forks (input-set trigger) and rebuilds every
    scope-stamped aggregate (params trigger) — regional artifacts never survive;
  - staging inventory: a stale overlay-*.pmtiles on disk is excluded from
    stage_build's uploads and manifest;
  - the hole-free gate fires: a missing per-tile fork output makes the --stable
    bundle refuse rather than publish a hole;
  - raws are transient: a real fetch → prep → catalog run over a localhost server
    leaves the staged COG and discards the raw archive, a clean rerun re-downloads
    nothing, and a forced re-prep schedules the fetch again.

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
    """Run the engine DAG against the ONE Snakefile (workdir=tmp, sources from tmp/sources). The
    covering already exists (the CLI wrote it above), so the `cover` checkpoint is up to date and
    the DAG re-evaluates straight past it into the per-stem jobs."""
    e = _base_env(tmp, env)
    proc = subprocess.run(
        ["uv", "run", "snakemake", "-s", os.path.join(PIPE, "..", "Snakefile"),
         "--config", f"workdir={tmp}", *args],
        cwd=PIPE, env=e, capture_output=True, text=True)
    if check and proc.returncode != 0:
        # The tmp workdir vanishes with the test, so surface per-job stderr logs now.
        logs = ""
        for path in sorted(glob(os.path.join(tmp, "tmp", "logs", "**", "*.log"), recursive=True)):
            with open(path) as f:
                tail = f.read()[-2000:]
            if tail.strip():
                logs += f"\n--- {os.path.relpath(path, tmp)} ---\n{tail}"
        raise AssertionError(f"snakemake {args} failed:\n{proc.stdout}\n{proc.stderr}{logs}")
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
    # The COG is dropped in already 'prepped', so the enumerate checkpoint is a no-op — but its
    # output must be present + up-to-date, else re-evaluating catalog.json's staleness would
    # schedule an enumerate that has no file_list to read. Empty enumeration ⇒ zero fetch jobs.
    with open(f"{tmp}/sources/{sid}/file_list.txt", "w") as f:
        f.write("")
    with open(f"{tmp}/store/source/{sid}/items.txt", "w") as f:
        f.write("")


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
        base = os.path.basename(path)
        if base.startswith("vector") or base in ("soundings.pmtiles", "depare.pmtiles",
                                                 "coverage.pmtiles"):
            continue  # vector layers (vector.pmtiles + its shallow/cell shards), not terrain rasters
        with open(path, "r+b") as f:
            for (z, x, y), tile_bytes in all_tiles(Reader(MmapSource(f)).get_bytes):
                elev = encode.decode(imagecodecs.webp_decode(tile_bytes).astype("float32"))
                by_zoom.setdefault(z, []).append(float(np.median(elev)))
    return by_zoom


def _pm_tiles(path):
    """{zoom: [(x, y), ...]} present in a pmtiles archive (its directory IS the availability index —
    a sparse variable-depth archive has gaps by design)."""
    from pmtiles.reader import Reader, MmapSource, all_tiles
    byz = {}
    with open(path, "r+b") as f:
        for (z, x, y), _ in all_tiles(Reader(MmapSource(f)).get_bytes):
            byz.setdefault(z, []).append((x, y))
    return byz


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
    over a finer (higher-maxzoom) source; without priority, native resolution decides — and
    that ordering is per FILE, so a source carrying two resolutions (CUDEM's 1/9" and 1/3"
    bands) still merges its finer files first."""
    import utils
    import config
    orig = config.load_metadata
    d = tempfile.mkdtemp()
    csv = os.path.join(d, "x.csv")
    with open(csv, "w") as f:
        f.write("source,filename,maxzoom\nB,b.tif,13\nA,a.tif,11\n")  # B finer, A coarser
    mixed = os.path.join(d, "mixed.csv")
    with open(mixed, "w") as f:  # ONE source, both bands, as a merged CUDEM tile registers
        f.write("source,filename,maxzoom\ncudem,ncei13_third.tif,12\n"
                "cudem,ncei19_ninth.tif,13\n")
    try:
        config.load_metadata = lambda s: {"priority": 1} if s == "A" else {}
        order = [g[0]["source"] for g in utils.get_grouped_source_items(csv)]
        assert order[0] == "A", f"priority should win merge order: {order}"
        config.load_metadata = lambda s: {}  # no priority anywhere
        order = [g[0]["source"] for g in utils.get_grouped_source_items(csv)]
        assert order[0] == "B", f"without priority, finer (maxzoom 13) wins: {order}"
        groups = utils.get_grouped_source_items(mixed)
        assert [(g[0]["maxzoom"], g[0]["filename"]) for g in groups] == \
            [(13, "ncei19_ninth.tif"), (12, "ncei13_third.tif")], groups
        print("priority ok — datum-authoritative source wins merge order; else maxzoom, "
              "per file within a source")
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


def check_vector_selfcheck():
    """_vector_selfcheck is the completeness guard the sparse variable-depth bundle relies on to
    prove no chart content was dropped. PROVE it can actually detect drops: build tiny archives with
    the (patched) tippecanoe and assert it RAISES on each failure mode — a dropped contour, a dropped
    sounding, a depare gap (missing polygon leaving a partition hole), an overlapping depare pair, and
    specifically a dropped drval1==0 band (guards the `drval1 or -1` bug from regressing) — and that a
    correct archive passes. Each negative RED-fails without the fix and GREEN-passes with it."""
    import mercantile
    sys.path.insert(0, PIPE)
    import contour_run as cr
    tmp = tempfile.mkdtemp()

    # Plain tiling (every zoom materialized) makes these fixtures deterministic — the completeness
    # mechanism (id survival) is agnostic to how a tile subdivided, and the real sharded
    # variable-depth run is exercised end-to-end by main(). (A sparse synthetic archive leafs above
    # a depare feature's minzoom and never emits it — realistic only at production density.)
    def build(layers, maxz):
        vec = f"{tmp}/v.pmtiles"
        cmd = ["tippecanoe", "-o", vec, "-f", "-Z", "0", "-z", str(maxz), "-P", "-q",
               "--no-tile-size-limit", "--detect-shared-borders"]
        for name, feats in layers:
            seq = f"{tmp}/{name}.geojsons"
            with open(seq, "w") as f:
                f.write("\n".join(json.dumps(x) for x in feats))
            cmd += ["-L", f"{name}:{seq}"]
        subprocess.run(cmd, check=True, capture_output=True)
        return vec

    def feat(fid, geom, props, mn=0, mx=None):
        tc = {"minzoom": mn}
        if mx is not None:
            tc["maxzoom"] = mx
        return {"type": "Feature", "id": fid, "tippecanoe": tc, "properties": props, "geometry": geom}

    def line(cs):
        return {"type": "LineString", "coordinates": cs}

    def pt(xy):
        return {"type": "Point", "coordinates": xy}

    def square(x0, y0, s):
        return {"type": "Polygon", "coordinates": [[[x0, y0], [x0 + s, y0], [x0 + s, y0 + s],
                                                    [x0, y0 + s], [x0, y0]]]}

    def must_raise(vec, maxz, expected, needle, label):
        try:
            cr._vector_selfcheck(vec, maxz, expected)
        except SystemExit as e:
            assert needle in str(e), f"{label}: raised but not for '{needle}':\n{e}"
            return
        raise AssertionError(f"{label}: _vector_selfcheck did NOT raise (expected '{needle}')")

    try:
        # ── base 3-layer archive: big features well above the leaf-pixel floor, all present ──
        contours = [feat(1, line([[0.0, 0.0], [0.1, 0.05]]), {"depth_m": -4000, "sys": "m"}),
                    feat(2, line([[0.0, 0.1], [0.1, 0.15]]), {"depth_m": -4000, "sys": "m"})]
        soundings = [feat(3, pt([0.02, 0.02]), {"depth_m": 5}),
                     feat(4, pt([0.05, 0.12]), {"depth_m": 9})]
        depare = [feat(5, square(0.0, 0.0, 0.05), {"sys": "m", "drval1": 5}, mn=6),
                  feat(6, square(0.2, 0.2, 0.05), {"sys": "m", "drval1": 10}, mn=6)]
        vec = build([("contours", contours), ("soundings", soundings), ("depare", depare)], 8)
        expected = {"contours": {1: "contour sys=m depth_m=-4000", 2: "contour sys=m depth_m=-4000"},
                    "soundings": {3: "sounding depth=5", 4: "sounding depth=9"},
                    "depare": {5: "depare drval1=5", 6: "depare drval1=10"}}
        cr._vector_selfcheck(vec, 8, expected)  # POSITIVE: a correct archive must pass
        print("  positive: correct 3-layer archive passes")

        # (a) dropped contour — an expected id present in no tile
        must_raise(vec, 8, {**expected, "contours": {**expected["contours"], 9991: "contour sys=m depth_m=-30"}},
                   "contours", "dropped contour")
        # (b) dropped sounding
        must_raise(vec, 8, {**expected, "soundings": {**expected["soundings"], 9992: "sounding depth=13"}},
                   "soundings", "dropped sounding")
        print("  (a) dropped contour and (b) dropped sounding both caught by completeness")

        # (d) overlapping depare pair (two m-bands, ~50% overlap in one tile)
        snd = [feat(3, pt([0.5, 0.5]), {"depth_m": 5})]
        over = [feat(1, square(0.4, 0.4, 0.1), {"sys": "m", "drval1": 5}, mn=6),
                feat(2, square(0.45, 0.45, 0.1), {"sys": "m", "drval1": 10}, mn=6)]
        vov = build([("soundings", snd), ("depare", over)], 8)
        must_raise(vov, 8, None, "overlap", "overlapping depare pair")
        # (e) dropped drval1==0 band — the 0-band must participate in the partition check
        # (with the `drval1 or -1` bug it was excluded, so this overlap went undetected → no raise)
        zero = [feat(1, square(0.4, 0.4, 0.1), {"sys": "m", "drval1": 0}, mn=6),
                feat(2, square(0.45, 0.45, 0.1), {"sys": "m", "drval1": 5}, mn=6)]
        vz = build([("soundings", snd), ("depare", zero)], 8)
        must_raise(vz, 8, None, "overlap", "drval1==0 band in partition check")
        print("  (d) overlapping depare pair and (e) drval1==0 band both caught by the overlap check")

        # (c) missing depare polygon leaving a gap: a right-half band capped at z6 (present at the
        # parent, gone at its 4 z7 children) shrinks the partition deeper → the gap check fires.
        T = mercantile.tile(1.0, 1.0, 6)
        b = mercantile.bounds(T)
        eps = (b.east - b.west) * 0.02
        midx = (b.west + b.east) / 2

        def half(x0, x1):
            return {"type": "Polygon", "coordinates": [[[x0 + eps, b.south + eps], [x1 - eps, b.south + eps],
                    [x1 - eps, b.north - eps], [x0 + eps, b.north - eps], [x0 + eps, b.south + eps]]]}
        gdepare = [feat(1, half(b.west, midx), {"sys": "m", "drval1": 5}, mn=6),
                   feat(2, half(midx, b.east), {"sys": "m", "drval1": 10}, mn=6, mx=6)]
        gsnd = [feat(10 + i, pt([(mercantile.bounds(ch).west + mercantile.bounds(ch).east) / 2,
                                 (mercantile.bounds(ch).south + mercantile.bounds(ch).north) / 2]),
                     {"depth_m": 5})
                for i, ch in enumerate(mercantile.children(T))]
        vgap = build([("soundings", gsnd), ("depare", gdepare)], 7)
        must_raise(vgap, 7, None, "gap", "depare partition gap")
        print("  (c) depare partition gap caught by the down-zoom area check")
        print("vector self-check negative tests ok — every drop mode raises; a correct archive passes")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_shallow_minzoom_filter():
    """The shallow vector run drops features whose tippecanoe.minzoom exceeds its -z (SPLIT_Z-1) at
    SEQ-WRITE time — an explicit filter, not a trust in tippecanoe's clamp. Prove _fgb_to_seq and
    _soundings_to_seq honor max_minzoom: only the below-cut features are written, ids and all."""
    import geopandas as gpd
    from shapely.geometry import LineString
    sys.path.insert(0, PIPE)
    import contour_run as cr
    tmp = tempfile.mkdtemp()
    try:
        deep, shoal, cut = -4000, -2, 3  # minzoom("m",-4000)==0 <= cut < native+ minzoom("m",-2)
        assert cr.contour_minzoom("m", deep) <= cut < cr.contour_minzoom("m", shoal)
        fgb = f"{tmp}/c.fgb"
        gpd.GeoDataFrame(
            {"depth_m": [deep, shoal], "sys": ["m", "m"]},
            geometry=[LineString([(0, 0), (0.1, 0.1)]), LineString([(0, 0.2), (0.1, 0.3)])],
            crs="EPSG:4326").to_file(fgb, driver="FlatGeobuf")
        seq = f"{tmp}/c.geojsons"
        cr._fgb_to_seq([fgb], ("depth_m", "sys"),
                       lambda p: cr.contour_minzoom(p["sys"], float(p["depth_m"])),
                       seq, "contour", lambda p: str(p.get("depth_m")), 0, 10, max_minzoom=cut)
        written = [json.loads(l) for l in open(seq)]
        assert len(written) == 1 and written[0]["properties"]["depth_m"] == deep, \
            f"only the minzoom<=cut contour survives the shallow filter: {written}"

        gj = f"{tmp}/s-in.geojsons"  # line-delimited features, the per-stem soundings format
        with open(gj, "w") as f:
            for feat in [
                {"type": "Feature", "tippecanoe": {"minzoom": 0}, "properties": {"depth_m": 5},
                 "geometry": {"type": "Point", "coordinates": [0, 0]}},
                {"type": "Feature", "tippecanoe": {"minzoom": cut + 5}, "properties": {"depth_m": 9},
                 "geometry": {"type": "Point", "coordinates": [0.1, 0.1]}}]:
                f.write(json.dumps(feat) + "\n")
        cr._soundings_to_seq([gj], f"{tmp}/s.geojsons", 0, max_minzoom=cut)
        swritten = [json.loads(l) for l in open(f"{tmp}/s.geojsons")]
        assert len(swritten) == 1 and swritten[0]["properties"]["depth_m"] == 5, \
            f"only minzoom<=cut soundings survive the shallow filter: {swritten}"
        print("shallow minzoom-filter ok — seq builders drop features above the shallow -z")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_raw_discard():
    """Raws are temp(): staged, then discarded — and NOT refetched until something needs them.

    Runs the real fetch → prep → catalog chain against a GeoTIFF served from a throwaway
    localhost HTTP server (requests has no file:// transport, and the point is to exercise
    source_fetch as it actually runs). Three properties, each a separate way to get this wrong:
    the raw is gone after prep, a clean rerun does NOT re-download it, and a forced re-prep
    schedules the fetch again rather than failing on the absent input.
    """
    import functools
    import http.server
    import threading

    tmp = tempfile.mkdtemp()
    served = os.path.join(tmp, "upstream")
    os.makedirs(served)
    px, res = 64, 0.01
    with rasterio.open(os.path.join(served, "tile.tif"), "w", driver="GTiff", height=px,
                       width=px, count=1, dtype="float32", nodata=-9999, crs="EPSG:4326",
                       transform=from_origin(0.0, 0.0, res, res)) as d:
        d.write(np.full((px, px), -30.0, dtype="float32"), 1)

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=served)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        sid = "discard"
        os.makedirs(f"{tmp}/sources/{sid}")
        with open(f"{tmp}/sources/{sid}/metadata.json", "w") as f:
            json.dump({"name": sid, "max_zoom": 10, "crs": "EPSG:4326"}, f)
        with open(f"{tmp}/sources/{sid}/file_list.txt", "w") as f:
            f.write(f"http://127.0.0.1:{httpd.server_address[1]}/tile.tif\n")

        catalog = f"store/source/{sid}/catalog.json"
        snake(tmp, "-c", "1", catalog)
        assert os.path.isfile(f"{tmp}/{catalog}"), "the source must register"
        assert sorted(glob(f"{tmp}/store/source/{sid}/*.tif")), "the staged COG must survive"
        left = glob(f"{tmp}/store/source/{sid}/raw/*")
        assert not left, f"prep must discard its raws, found {left}"

        counts = _job_counts(snake(tmp, "-c", "1", "-n", catalog).stdout)
        assert not counts, f"a clean rerun must schedule nothing, got {counts}"

        forced = _job_counts(snake(tmp, "-c", "1", "-n", "-R", "prep_source", catalog).stdout)
        assert forced.get("fetch_item") == 1, \
            f"a forced re-prep must refetch the discarded raw, got {forced}"
        print("raw discard ok — staged then dropped, no refetch until a force needs it")
    finally:
        httpd.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


def check_covering_scope_independent():
    """The covering (the per-stem aggregation CSVs) must be BBOX-INDEPENDENT: BBOX selects WHICH
    stems a build processes, never a stem's CSV CONTENT. Build the covering at planet scope, then
    under a bbox that CUTS ACROSS the broad `base` source (base extends past the window) while
    EXCLUDING the finer `fine` source (fine ends at x=0.2, the window starts at x=0.25), and assert
    every stem the two coverings share is BYTE-IDENTICAL — the invariant that keeps a scope change
    from restamping a CSV and re-merging. Under the clip-clamp bug the bbox clamps source bounds to
    the window, dropping fine from the boundary tiles that reach into it (here that also coarsens
    the whole window to two z8 stems), so the coverings share nothing / differ — the check RED-fails
    without the fix, GREEN-passes with it."""
    tmp = tempfile.mkdtemp()
    try:
        make_source(tmp, "base", west=-0.5, north=0.5, deg=1.0, px=1024, value=-101, max_zoom=10)
        make_source(tmp, "fine", west=-0.2, north=0.2, deg=0.4, px=4096, value=-51, max_zoom=11)
        for sid in ("base", "fine"):
            cli(tmp, "source_catalog.py", sid)

        aggdir = f"{tmp}/store/aggregation"

        def build_covering(bbox=None):
            cli(tmp, "aggregation_covering.py", "--stable", env=({"BBOX": bbox} if bbox else None))
            return {n: open(f"{aggdir}/{n}", "rb").read()
                    for n in os.listdir(aggdir) if n.endswith("-aggregation.csv")}

        planet = build_covering()
        shutil.rmtree(aggdir)
        bbox = build_covering("0.25,-0.5,0.5,0.5")  # cuts across base, excludes fine

        shared = set(planet) & set(bbox)
        assert shared, f"a base-cutting bbox must share stems with the planet covering: {sorted(bbox)}"
        assert set(bbox) < set(planet), \
            f"the bbox covering must be a strict subset of the planet covering: {sorted(set(bbox) - set(planet))}"
        differing = [n for n in sorted(shared) if planet[n] != bbox[n]]
        assert not differing, f"BBOX changed shared-stem CSV content — covering is scope-dependent: {differing}"
        # the cut must actually exercise a boundary tile whose window part is base-only but reaches
        # out into fine: with a scope-independent covering it keeps fine (child_z 11).
        assert any(n.endswith("-11-aggregation.csv") for n in bbox), \
            f"a boundary tile reaching into fine must keep child_z 11 in the bbox covering: {sorted(bbox)}"
        print(f"covering scope-independence ok — {len(shared)} shared stems byte-identical across "
              "planet and a base-cutting bbox")
    finally:
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
        # source_catalog scans the prepped COGs into the item's seascape:files (the retired
        # source_bounds folded in), so one CLI is the whole registration.
        for sid in ("base", "fine"):
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
        vec = f"{tmp}/store/bundle/vector.pmtiles"
        assert os.path.exists(vec), "missing vector.pmtiles"
        # soundings/depare are NOT separate archives — the sharded run folds them into vector.pmtiles.
        for gone in ("soundings.pmtiles", "depare.pmtiles"):
            assert not os.path.exists(f"{tmp}/store/bundle/{gone}"), \
                f"{gone} must not exist as a separate archive (folded into vector.pmtiles)"

        sys.path.insert(0, PIPE)
        import contour_run
        # the vector split the SUBPROCESSES used: VECTOR_SPLIT_Z defaults to the macrotile grid,
        # and the fixtures pin MACROTILE_Z in _base_env (this process's own import saw the ambient one)
        env = _base_env(tmp)
        SPLIT_Z = int(env.get("VECTOR_SPLIT_Z", "0")) or int(env["MACROTILE_Z"])
        maxz = contour_run._stems_maxz(stems)

        # ── tile ownership (Verification): the shallow archive owns EVERY z < SPLIT_Z tile and each
        # cell archive owns EXACTLY its z >= SPLIT_Z subtree. The owned-subtree filter (bundle_cell_stable)
        # dropped every fringe tile a neighbour's edge buffer bled in, so cross-cell overlap is now ZERO
        # by construction — the tiles are STRICTLY disjoint, which is what lets `pmtiles merge` (refuses
        # overlapping inputs) concatenate them. Assert the exact zoom split, that every cell tile sits
        # in its OWN home subtree (a mis-sharded stem would land far from home), and that no tile is
        # emitted by two cells. ──
        import collections as _c
        shallow_arch = f"{tmp}/store/bundle/vector-shallow.pmtiles"
        assert os.path.exists(shallow_arch), "missing vector-shallow.pmtiles"
        cell_archs = sorted(glob(f"{tmp}/store/bundle/vector-cell-*.pmtiles"))
        assert cell_archs, "the covering must produce at least one vector cell archive"
        if os.path.getsize(shallow_arch) > 0:
            for z in _pm_tiles(shallow_arch):
                assert z < SPLIT_Z, f"shallow archive emitted z{z} >= SPLIT_Z ({SPLIT_Z})"
        emitters = _c.defaultdict(set)  # (z,x,y) -> {cell (cz,cx,cy)} for z >= SPLIT_Z
        for ca in cell_archs:
            if os.path.getsize(ca) == 0:
                continue  # 0-byte = an empty cell; the join skips it
            # the cell id's own zoom cz, NOT SPLIT_Z: covering cells root as coarse as seed_z
            cz, cx, cy = (int(v) for v in os.path.basename(ca)[len("vector-cell-"):-len(".pmtiles")].split("-"))
            for z, xys in _pm_tiles(ca).items():
                assert z >= SPLIT_Z, f"cell archive emitted z{z} < SPLIT_Z ({SPLIT_Z})"
                for x, y in xys:
                    # after the fringe filter, every tile a cell emits must sit in its OWN subtree
                    assert z >= cz and (x >> (z - cz), y >> (z - cz)) == (cx, cy), \
                        f"z{z} {x}/{y} emitted by cell {cz}-{cx}-{cy} is outside its own subtree — fringe not filtered"
                    emitters[(z, x, y)].add((cz, cx, cy))
        dups = {k: sorted(v) for k, v in emitters.items() if len(v) > 1}
        assert not dups, \
            f"cell archives must be STRICTLY disjoint after the owned-subtree filter, got overlaps: {dict(list(dups.items())[:5])}"
        print(f"vector tile-ownership ok — shallow z<{SPLIT_Z}, {len(cell_archs)} cell archive(s) "
              f"z>={SPLIT_Z}; {len(emitters)} tiles, 0 cross-cell overlaps (strictly disjoint, pmtiles merge)")

        # ── shallow minzoom filter on the built archive: no feature whose minzoom exceeds SPLIT_Z-1
        # may ride a shallow tile — depare (floor DEPARE_MINZOOM) appears iff its floor fits the
        # shallow band. (The seq-level filter is proven directly by check_shallow_minzoom_filter;
        # this guards the end-to-end archive.) ──
        if os.path.getsize(shallow_arch) > 0:
            depare_fits_shallow = contour_run.DEPARE_MINZOOM <= SPLIT_Z - 1
            for z, xys in _pm_tiles(shallow_arch).items():
                for x, y in xys[:4]:
                    L = contour_run._decode_tile(shallow_arch, z, x, y)
                    if not depare_fits_shallow:
                        assert not L.get("depare"), \
                            f"depare (minzoom {contour_run.DEPARE_MINZOOM}) must not ride shallow z{z}"
                    elif z < contour_run.DEPARE_MINZOOM:
                        assert not L.get("depare"), \
                            f"depare (minzoom {contour_run.DEPARE_MINZOOM}) rode shallow z{z} below its floor"
                    for feat in L.get("contours", []):
                        p = feat["properties"]
                        d, s = p.get("depth_m"), p.get("sys")
                        if d is not None and s:
                            assert contour_run.contour_minzoom(s, float(d)) <= SPLIT_Z - 1, \
                                f"contour depth_m={d} minzoom exceeds shallow -z at z{z}"

        # ── the joined archive's safety invariants (Verification 3,4,5): one served archive, three
        # layers, all zoom gating per-feature. _vector_selfcheck asserts no depare below z6, no
        # contour below its tier minzoom, depare m-bands disjoint, soundings present, every input id
        # survives — it ran in-build; re-run it here so the test OWNS the invariant, not just the
        # build. ──
        layers = set()
        for z in (max(child_zs), 6, 5):  # leaf zoom, the depare floor, at SPLIT_Z
            for x, y in _pm_tiles(vec).get(z, [])[:4]:
                layers |= set(contour_run._decode_tile(vec, z, x, y))
                if z < 6:
                    assert "depare" not in contour_run._decode_tile(vec, z, x, y), \
                        f"depare must not appear below z6 (z{z} {x}/{y})"
        assert {"contours", "soundings", "depare"} <= layers, \
            f"the joined archive must carry all three layers: {sorted(layers)}"
        contour_run._vector_selfcheck(vec, maxz)  # raises SystemExit on any invariant violation
        snake(tmp, "-c", "4", "vector_selfcheck")  # the rule wiring: runs beside staging in prod
        print(f"vector joined-run ok — one sparse archive, layers {sorted(layers)}, self-check passed")

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
        # group at the SUBPROCESS split (this process's contour_run imported the ambient macrotile)
        n_cells = len({(int(s.split("-")[1]) >> (int(s.split("-")[0]) - SPLIT_Z),
                        int(s.split("-")[2]) >> (int(s.split("-")[0]) - SPLIT_Z)) for s in stems})
        dry = snake(tmp, "-c", "4", "-n", "bundles", env={"CONTOUR_LEVELS": levels}).stdout
        counts = _job_counts(dry)
        assert counts.get("mosaic_tile", 0) == 0, f"a contour-config change must NOT re-merge: {counts}"
        assert counts.get("terrain_render", 0) == 0, f"a contour-config change must NOT re-render: {counts}"
        assert counts.get("contour_tile", 0) == n_stems, \
            f"a contour-config change must rerun every contour tile: {counts}"
        assert counts.get("vector_shallow", 0) == 1, f"the shallow vector run must rerun: {counts}"
        assert counts.get("vector_cell", 0) == n_cells, \
            f"every vector cell ({n_cells}) must rerun: {counts}"
        assert counts.get("vector_join", 0) == 1, f"the vector join must rerun: {counts}"
        print(f"stage-split ok — CONTOUR_LEVELS reruns {counts.get('contour_tile')} contour_tile "
              f"+ vector_shallow + {n_cells} vector_cell + vector_join, 0 mosaic_tile, 0 terrain_render")

        # ── scope transitions: a bbox build then a planet dry-run. The bbox CUTS ACROSS the base
        # source (base extends past every window edge) and EXCLUDES the fine source (fine ends at
        # x=0.2, the window starts at x=0.25) — the shape that made the covering-clip bug visible
        # (byte-stability of the shared CSVs is proven directly by check_covering_scope_independent).
        # Here the payoff is asserted end-to-end: the planet dry-run re-merges NOTHING (the mosaic
        # tiles the first planet build wrote stay current), only the bbox-truncated fork
        # neighborhoods heal, and every scope-stamped aggregate rebuilds.
        import mosaic
        bbox = "0.25,-0.5,0.5,0.5"  # right strip of base; fine (x<=0.2) is out of window
        covering_path = f"{tmp}/store/aggregation/covering.txt"
        os.environ["BBOX"] = bbox
        try:
            inwin = mosaic.covering_stems(covering_path)  # BBOX-scoped covering — the in-window stems
        finally:
            del os.environ["BBOX"]
        n_inwin = len(inwin)
        assert 0 < n_inwin < n_stems, f"bbox must select a strict subset of the covering: {n_inwin}/{n_stems}"
        # a boundary stem reaching into fine must still carry fine (child_z 11), proving the covering
        # kept the out-of-window source rather than clamping them away
        assert any(s.endswith("-11") for s in inwin), \
            f"a boundary stem reaching into fine must keep child_z 11: {inwin}"

        # the heal set: in-window stems whose fork neighborhood reaches a stem OUTSIDE the window.
        # The bbox build forks them with a bbox-truncated neighborhood; the planet build re-forks
        # exactly these (input-set trigger). Interior in-window stems whose whole neighborhood is
        # in-window do NOT change and stay current — so the heal count is < n_inwin here.
        import aggregation_reproject
        import mercantile
        inwin_set = set(inwin)

        def _neighbors(stem, pool):
            z, x, y, _cz = (int(a) for a in stem.split("-"))
            l, b, r, t = aggregation_reproject.buffered_bounds(
                mercantile.Tile(x=x, y=y, z=z), mosaic.window_buffer_3857(stem))
            hit = []
            for s in pool:
                sz, sx, sy, _c = (int(a) for a in s.split("-"))
                sb = mercantile.xy_bounds(mercantile.Tile(x=sx, y=sy, z=sz))
                if sb.left < r and sb.right > l and sb.bottom < t and sb.top > b:
                    hit.append(s)
            return hit

        n_heal = sum(1 for s in inwin if any(n not in inwin_set for n in _neighbors(s, stems)))
        assert 0 < n_heal < n_inwin, f"heal set must be a nonempty strict subset: {n_heal}/{n_inwin}"

        snake(tmp, "-c", "4", "bundles", env={"BBOX": bbox})   # real regional build

        dry = snake(tmp, "-c", "4", "-n", "bundles").stdout    # then a planet dry-run
        counts = _job_counts(dry)
        # THE fix: byte-identical CSVs across scopes, so the planet mosaic tiles never look stale.
        assert counts.get("mosaic_tile", 0) == 0, f"scope change must never re-merge: {counts}"
        # the bbox run re-forked its in-window stems with TRUNCATED neighborhoods (input set is
        # bbox-scoped); the planet build heals exactly the boundary stems via the input-set trigger.
        assert counts.get("contour_tile", 0) == n_heal, f"planet must re-fork the {n_heal} bbox-truncated stems: {counts}"
        assert counts.get("soundings_tile", 0) == n_heal, f"soundings heal too: {counts}"
        assert counts.get("depare_tile", 0) == n_heal, f"depare heals too: {counts}"
        for agg in ("mosaic_index", "vector_shallow", "vector_join", "terrain_planet_bundle"):
            assert counts.get(agg, 0) == 1, f"{agg} must rebuild after a scope change: {counts}"
        # one bbox-stamped vector cell per in-window stem (stem-grid split); the rest kept planet stamps
        assert counts.get("vector_cell", 0) == n_inwin, \
            f"the {n_inwin} bbox-stamped vector cells must rebuild after a scope change: {counts}"
        snake(tmp, "-c", "4", "bundles")  # restore planet-scoped products for the checks below
        print(f"scope-transition ok — bbox cutting across base re-merges nothing, heals {n_heal} of "
              f"{n_inwin} in-window stems, rebuilds every aggregate")

        # ── staging inventory: a stale overlay file on disk must not ship or be advertised ──
        real = sorted(glob(f"{tmp}/store/bundle/overlay-*.pmtiles"))[0]
        stale_path = f"{tmp}/store/bundle/overlay-9-99-99.pmtiles"
        shutil.copyfile(real, stale_path)
        proc = subprocess.run(
            [sys.executable, "-c",
             f"import sys, json; sys.path.insert(0, {PIPE!r}); import bundle\n"
             "up, mp = bundle.stage_build()\n"
             "m = json.load(open(mp))\n"
             "print(json.dumps({'uploads': up, 'cells': sorted(m['overlay']['cells']), "
             "'vector_max_zoom': m['vector']['max_zoom']}))"],
            cwd=tmp, env=_base_env(tmp, {"SOURCES_DIR": "sources"}), capture_output=True, text=True)
        assert proc.returncode == 0, f"stage_build failed:\n{proc.stdout}\n{proc.stderr}"
        staged = json.loads(proc.stdout.splitlines()[-1])
        assert "9-99-99" not in staged["cells"], f"stale cell advertised: {staged['cells']}"
        assert not any("9-99-99" in u for u in staged["uploads"]), "stale overlay staged for upload"
        assert "stale" in proc.stdout, "staging must report the ignored stale file"
        # the manifest carries the Worker's vector serving depth = the covering's max child_z (11)
        assert staged["vector_max_zoom"] == max(child_zs), \
            f"manifest vector.max_zoom must be the covering max child_z {max(child_zs)}: {staged['vector_max_zoom']}"
        os.remove(stale_path)
        print("staging-inventory ok — stale overlay excluded; manifest carries "
              f"vector.max_zoom={staged['vector_max_zoom']}")

        # ── the hole-free gate: a missing per-tile fork output makes --stable bundle refuse ──
        victim = sorted(glob(f"{tmp}/store/contour/*.fgb"))[0]
        os.remove(victim)
        proc = subprocess.run(
            [sys.executable, os.path.join(PIPE, "contour_run.py"), "bundle-shallow", "--stable"],
            cwd=tmp, env=_base_env(tmp, {"SOURCES_DIR": "sources"}),
            capture_output=True, text=True)
        assert proc.returncode != 0 and "contour incomplete" in (proc.stderr + proc.stdout), \
            f"the hole-free gate must refuse a missing per-tile file:\n{proc.stdout}\n{proc.stderr}"
        print(f"hole-free gate ok — bundle-shallow --stable refused the missing {os.path.basename(victim)}")
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
    check_vector_selfcheck()
    check_shallow_minzoom_filter()
    check_raw_discard()
    check_covering_scope_independent()
    main()
