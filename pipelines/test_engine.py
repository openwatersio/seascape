"""End-to-end self-check for the aggregation -> downsampling -> bundle engine.

Builds two synthetic sources in an isolated tmp dir — a coarse broad ``base``
(-101 m, native ~z10) and a fine small ``fine`` (-51 m, native ~z13 but capped to
z11 in metadata) inside it — then runs the whole engine and asserts:

  - the per-source max_zoom CAP binds (fine renders at z11, not its native z13);
  - PRIORITY: at the fine source's zoom the merged value is the fine value (-51),
    not the base (-101) — highest-maxzoom source wins in overlap;
  - the base shows through where fine is absent (-101 present);
  - the bundle split: planet.pmtiles spans z0..macrotile_z and the deeper fine
    tiles land in one overlay-{cell}.pmtiles per populated OVERLAY_SPLIT_Z grid
    cell (so the engine covers z0..11 across bundles);
  - the KEY model (phase 3 acceptance): a no-change rerun re-runs zero tiles and
    skips downsample + bundle; a CONTOUR_LEVELS change re-runs every tile's merge
    (the contour fork is stale) but leaves the terrain keys untouched, so
    downsample + bundle still skip; FORCE_REBUILD re-bundles regardless.

Run from pipelines/:  uv run python test_engine.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from glob import glob

import numpy as np
import rasterio
from rasterio.transform import from_origin

PIPE = os.path.dirname(os.path.abspath(__file__))


def run(tmp, *args, env=None):
    # Small macrotile_z / num_overviews keep the synthetic rasters tiny. SKIP_SMOOTH: the raster
    # priority test needs no smoothing (and the key checks want one fewer moving part). Contours,
    # soundings, and depare all fork — the source values sit just off the contour levels (-101/-51)
    # so gdal_contour sees clean crossings only at the feathered seam, never a constant==level
    # plateau. FORCE_REBUILD/CONTOUR_LEVELS are stripped from the inherited env so a dev shell
    # can't poison the incremental-path assertions; `env` adds per-call overrides.
    base = {**os.environ, "SOURCES_DIR": "sources", "PYTHONPATH": PIPE,
            "MACROTILE_Z": "10", "NUM_OVERVIEWS": "2", "SKIP_SMOOTH": "1"}
    base.pop("FORCE_REBUILD", None)
    base.pop("CONTOUR_LEVELS", None)
    base.update(env or {})
    proc = subprocess.run([sys.executable, os.path.join(PIPE, args[0]), *args[1:]],
                          cwd=tmp, env=base, check=True, capture_output=True, text=True)
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    return proc


def content_names(tmp, *patterns):
    """{logical stem: basename} for every content-addressed file matching the patterns (relative to
    tmp, recursive). The key rides in the name, so a moved key shows as a changed basename for the
    stem — this is the before/after snapshot the key-stability assertions compare (replacing the
    phase-3 .key sidecar snapshot). Both a fork's artifact and its .empty marker are content-named,
    so passing both globs snapshots every tile whether or not it produced features."""
    import keys
    out = {}
    for pattern in patterns:
        for p in glob(f"{tmp}/{pattern}", recursive=True):
            base = os.path.basename(p)
            if keys.is_content_name(base):
                out[keys.stem_of(base)] = base
    return out


def store_manifest_body(tmp):
    """Assemble the store manifest in the SAME subprocess env the build used (so the keys match)
    and return its bytes. Writes store/manifests/<id>.json and reads it back."""
    build_id = run(tmp, "store_manifest.py", "write").stdout.strip()
    with open(f"{tmp}/store/manifests/{build_id}.json") as f:
        return f.read()


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


def _synthetic_covering(aid, tiles):
    """A cwd-relative covering + one synthetic source (each tile's CSV carries one source row so
    the fork keys resolve real source properties). Caller sets config.SOURCES_DIR = 'sources'."""
    os.makedirs("sources/src", exist_ok=True)
    with open("sources/src/metadata.json", "w") as f:
        json.dump({"name": "src", "max_zoom": 12}, f)
    os.makedirs(f"store/aggregation/{aid}", exist_ok=True)
    fps = []
    for t in tiles:
        fp = f"store/aggregation/{aid}/{t}-aggregation.csv"
        with open(fp, "w") as f:
            f.write("source,filename,maxzoom\nsrc,src_0.tif,12\n")
        fps.append(fp)
    return fps


def check_self_heal():
    """A tile whose terrain pmtiles is missing from the store is stale even when nothing changed
    (self-heal): its content-addressed name is simply absent. Same invariant the old covering diff
    enforced, now by construction. On the build box this is the resume mechanism: a re-dispatch
    hydrates whatever the last manifest referenced, and only the tiles whose content name never
    landed come back stale. Also asserts FORCE_REBUILD makes everything stale (the escape hatch)
    and that fresh keys mean ZERO work (the no-change contract at plan level).

    Also covers sources-manifest, which rides the same key-based dirty derivation: the manifest
    must be exactly the STALE tiles' (source, filename) union — per-tile rows for stale tiles
    only, shared rows deduped, an absolute /vsi row passed through verbatim (the build box
    filters those out of its hydrate list; source_path streams them untouched) — and the
    covering's FULL source set under FORCE."""
    import aggregation_run
    import config
    import keys
    import utils
    env_force = os.environ.pop("FORCE_REBUILD", None)  # this check is about the incremental path
    saved_land = os.environ.pop("LANDMASK", None)      # a dev mask's file hash would enter the keys
    saved_water = os.environ.pop("WATERMASK", None)
    tmp = tempfile.mkdtemp()
    cwd, saved_dir = os.getcwd(), config.SOURCES_DIR
    try:
        os.chdir(tmp)
        config.SOURCES_DIR = "sources"
        aid = "01BBBBBBBBBBBBBBBBBBBBBBBB"
        tiles = [f"8-{x}-{y}-12" for x in range(6) for y in range(6)]  # 36 same-cost tiles
        legacy = "/vsicurl/https://example.com/old.tif"
        # Three sources per tile so the manifest facets are all exercised: a per-tile file
        # (mirrored-style path), one file shared by every tile (dedup), one absolute /vsi row
        # (verbatim passthrough). Each needs a metadata.json so the fork keys resolve props.
        for sid in ("sA", "shared", "legacy"):
            os.makedirs(f"sources/{sid}", exist_ok=True)
            with open(f"sources/{sid}/metadata.json", "w") as f:
                json.dump({"name": sid}, f)
        os.makedirs(f"store/aggregation/{aid}", exist_ok=True)
        fps = []
        for t in tiles:
            fp = f"store/aggregation/{aid}/{t}-aggregation.csv"
            with open(fp, "w") as f:
                f.write("source,filename,maxzoom\n"
                        f"sA,objects/dem/{t}.tif,12\n"  # per-tile file (mirrored-style path)
                        "shared,common.tif,10\n"        # shared across every tile → dedup
                        f"legacy,{legacy},9\n")         # absolute /vsi row → verbatim
            fps.append(fp)
        fp_by_tile = dict(zip(tiles, fps))
        # Mark every fork of every tile fresh by materializing its content-addressed file (existence
        # is the freshness marker — no sidecar). A vector fork could instead leave a .empty marker;
        # a content file works for all four here.
        for fp, t in zip(fps, tiles):
            for fork in aggregation_run.FORKS:
                cpath = keys.content_path(aggregation_run._artifact(fork, t),
                                          aggregation_run._KEYFN[fork](fp))
                utils.create_folder(os.path.dirname(cpath))
                open(cpath, "w").close()
        assert aggregation_run.dirty_filepaths() == [], "fresh keys must mean zero work"

        missing = set(tiles[1::2])
        for t in missing:
            os.remove(keys.content_path(aggregation_run._artifact("terrain", t),
                                        aggregation_run._KEYFN["terrain"](fp_by_tile[t])))
        dirty = {fp.split("/")[-1].replace("-aggregation.csv", "")
                 for fp in aggregation_run.dirty_filepaths()}
        assert dirty == missing, f"dirty != self-heal set (Δ {dirty ^ missing})"

        # sources-manifest rides the same dirty derivation: exactly the stale tiles' union.
        aggregation_run.sources_manifest()
        with open("store/source-manifest.txt") as f:
            got = {line.strip() for line in f if line.strip()}
        want = {f"sA/objects/dem/{t}.tif" for t in missing} | {"shared/common.tif", f"legacy/{legacy}"}
        assert got == want, f"manifest != stale tiles' source union (Δ {got ^ want})"
        print(f"sources-manifest ok — {len(got)} unique files: stale tiles only, shared deduped, /vsi verbatim")

        os.environ["FORCE_REBUILD"] = "1"  # the escape hatch ignores every key match
        assert len(aggregation_run.dirty_filepaths()) == len(tiles), "FORCE must re-run every tile"
        aggregation_run.sources_manifest()  # FORCE ⇒ every tile stale ⇒ the FULL source set
        with open("store/source-manifest.txt") as f:
            got = {line.strip() for line in f if line.strip()}
        assert got == {f"sA/objects/dem/{t}.tif" for t in tiles} | {"shared/common.tif", f"legacy/{legacy}"}, \
            "under FORCE the manifest must be the covering's full source set"
        os.environ.pop("FORCE_REBUILD")
        print(f"self-heal ok — {len(missing)} tiles with missing pmtiles stale, rest fresh; FORCE re-runs all")
    finally:
        config.SOURCES_DIR = saved_dir
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)
        if env_force is not None:
            os.environ["FORCE_REBUILD"] = env_force
        if saved_land is not None:
            os.environ["LANDMASK"] = saved_land
        if saved_water is not None:
            os.environ["WATERMASK"] = saved_water


def check_stale_overview():
    """An overview must rebuild when a child it averages changed (its content name moved) or is
    missing (about to self-heal), and the staleness must cascade up the pyramid — the invariants
    the old mtime cascade enforced, now as a content-name cascade: an overview's key hashes its
    children's keys (read off their content filenames) and rides in its own name, so a rebuilt
    child yields a new overview name that isn't present -> stale, and dirty stems carry the cascade
    up within the finest-first plan."""
    import downsampling
    import keys
    import utils
    env_force = os.environ.pop("FORCE_REBUILD", None)  # the incremental path
    tmp = tempfile.mkdtemp()
    cwd = os.getcwd()
    try:
        os.chdir(tmp)
        aid = "01BBBBBBBBBBBBBBBBBBBBBBBB"
        # An overview chain: z7 (4-4-5-7) averages a z8 base; z6 (3-2-2-6) averages the z7;
        # z5 (2-1-1-5) averages the z6.
        chain = {  # downsampling.csv stem -> child pmtiles it references
            "4-4-5-7": "8-77-95-8.pmtiles",
            "3-2-2-6": "4-4-5-7.pmtiles",
            "2-1-1-5": "3-2-2-6.pmtiles",
        }
        os.makedirs(f"store/aggregation/{aid}")
        open(f"store/aggregation/{aid}/8-77-95-8-aggregation.csv", "w").close()
        for stem, child in chain.items():
            with open(f"store/aggregation/{aid}/{stem}-downsampling.csv", "w") as f:
                f.write(f"filename\n{child}\n")

        def touch(path):
            utils.create_folder(os.path.dirname(path))
            open(path, "w").close()

        def base_content(key):  # the aggregate child's content-addressed terrain pmtiles
            return keys.content_path(downsampling._child_logical("8-77-95-8.pmtiles"), key)

        def settle_overview(stem):  # write the overview's content file at its current key
            fp = f"store/aggregation/{aid}/{stem}-downsampling.csv"
            touch(keys.content_path(downsampling._overview_artifact(fp), downsampling._overview_key(fp)))

        def dirty_stems():
            return {fp.split("/")[-1].replace("-downsampling.csv", "")
                    for fp in downsampling.dirty_filepaths()}

        K1, K2 = "aaaaaaaaaaaa", "bbbbbbbbbbbb"  # two content keys = a child rebuilt
        # The aggregate child's terrain artifact (key K1), then each overview settled finest-first
        # off its child's content name — exactly the state a completed run leaves behind.
        touch(base_content(K1))
        for stem in chain:  # 4-4-5-7, 3-2-2-6, 2-1-1-5 — finest first
            settle_overview(stem)
        assert dirty_stems() == set(), "a settled pyramid must be fully fresh"

        # 1) the aggregate child was rebuilt (new terrain key K2) -> the whole chain above is stale.
        os.remove(base_content(K1))
        touch(base_content(K2))
        assert dirty_stems() == {"4-4-5-7", "3-2-2-6", "2-1-1-5"}, f"child-key cascade wrong: {dirty_stems()}"
        os.remove(base_content(K2))
        touch(base_content(K1))
        assert dirty_stems() == set(), "restoring the child key returns the pyramid to fresh"

        # 2) a middle overview's pmtiles vanished -> itself (self-heal) + everything above, but
        # NOT the fresh finer overview below it.
        mid = f"store/aggregation/{aid}/3-2-2-6-downsampling.csv"
        os.remove(keys.content_path(downsampling._overview_artifact(mid), downsampling._overview_key(mid)))
        assert dirty_stems() == {"3-2-2-6", "2-1-1-5"}, f"missing-artifact cascade wrong: {dirty_stems()}"
        assert "4-4-5-7" not in dirty_stems(), "the fresh finer overview must not rebuild"
        settle_overview("3-2-2-6")

        # 3) FORCE re-runs the whole pyramid regardless of key matches.
        os.environ["FORCE_REBUILD"] = "1"
        assert dirty_stems() == {"4-4-5-7", "3-2-2-6", "2-1-1-5"}, "FORCE must re-run every overview"
        os.environ.pop("FORCE_REBUILD")
        print("stale-overview ok — changed-child + missing-artifact both rebuild and cascade up; FORCE all")
    finally:
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)
        if env_force is not None:
            os.environ["FORCE_REBUILD"] = env_force


def check_key_invalidation():
    """The per-fork key wiring: a config-VALUE change (CONTOUR_LEVELS, the ring-drop knobs) moves
    exactly the contour(+depare) keys; a listed module's byte change moves exactly the forks that
    list it (keys.py's own --check covers the hashing primitive — this asserts each fork declares
    the right determinants); a merge-level source prop (band/mixed_crs — reproject reads them) and
    a mask presence change move every fork (all read the merged DEM they shape)."""
    import aggregation_run
    import config
    import contour_run
    import keys
    env_force = os.environ.pop("FORCE_REBUILD", None)
    saved_land = os.environ.pop("LANDMASK", None)
    saved_water = os.environ.pop("WATERMASK", None)
    tmp = tempfile.mkdtemp()
    cwd, saved_dir = os.getcwd(), config.SOURCES_DIR
    try:
        os.chdir(tmp)
        config.SOURCES_DIR = "sources"
        fp = _synthetic_covering("01CCCCCCCCCCCCCCCCCCCCCCCC", ["8-1-1-12"])[0]

        def all_keys():
            return {fork: aggregation_run._KEYFN[fork](fp) for fork in aggregation_run.FORKS}

        k0 = all_keys()
        assert all_keys() == k0, "keys must be stable across a no-op recompute"

        # config VALUE change: the contour ladder (and the depare levels derived from it)
        saved_levels = config.CONTOUR_LEVELS, config.DEPARE_LEVELS
        config.CONTOUR_LEVELS = config.CONTOUR_LEVELS[:-1]
        config.DEPARE_LEVELS = config.CONTOUR_LEVELS + [0]
        k1 = all_keys()
        assert k1["contour"] != k0["contour"] and k1["depare"] != k0["depare"], \
            "a CONTOUR_LEVELS change must move the contour + depare keys"
        assert k1["terrain"] == k0["terrain"] and k1["soundings"] == k0["soundings"], \
            "a CONTOUR_LEVELS change must NOT move the terrain/soundings keys"
        config.CONTOUR_LEVELS, config.DEPARE_LEVELS = saved_levels

        # listed-module byte change: touch soundings_run's bytes -> only the soundings key moves
        orig = keys._module_bytes
        keys._module_bytes = lambda name: (orig(name) + b"\n# x") if name == "soundings_run" else orig(name)
        try:
            k2 = all_keys()
        finally:
            keys._module_bytes = orig
        assert k2["soundings"] != k0["soundings"], "a listed module's byte change must move its fork's key"
        assert all(k2[f] == k0[f] for f in ("terrain", "contour", "depare")), \
            "a module byte change must not move forks that don't list it"

        # a local mask appearing enters every fork's key (all consume the masked merge)
        os.makedirs("store/landmask")
        with open("store/landmask/land.fgb", "wb") as f:
            f.write(b"mask-bytes-v1")
        os.environ["LANDMASK"] = "store/landmask/land.fgb"
        k3 = all_keys()
        assert all(k3[f] != k0[f] for f in aggregation_run.FORKS), \
            "a mask content hash must enter every fork's key"
        os.environ.pop("LANDMASK")
        os.remove("store/landmask/land.fgb")  # it also sits at the DEFAULT mask path — restore k0

        # the contour ring-drop knobs (env-tunable, gate the fork) move ONLY the contour key
        saved_ring = contour_run.MIN_RING_AREA_M2
        contour_run.MIN_RING_AREA_M2 = saved_ring * 2
        try:
            k4 = all_keys()
        finally:
            contour_run.MIN_RING_AREA_M2 = saved_ring
        assert k4["contour"] != k0["contour"], "a ring-drop knob change must move the contour key"
        assert all(k4[f] == k0[f] for f in ("terrain", "soundings", "depare")), \
            "a ring-drop knob change must not move the other forks"

        # band / mixed_crs are merge determinants (reproject reads them) -> every fork moves
        with open("sources/src/metadata.json", "w") as f:
            json.dump({"name": "src", "max_zoom": 12, "band": 2}, f)
        k5 = all_keys()
        assert all(k5[f] != k0[f] for f in aggregation_run.FORKS), \
            "a band change must move every fork's key"
        with open("sources/src/metadata.json", "w") as f:
            json.dump({"name": "src", "max_zoom": 12, "mixed_crs": True}, f)
        k6 = all_keys()
        assert all(k6[f] != k0[f] and k6[f] != k5[f] for f in aggregation_run.FORKS), \
            "a mixed_crs change must move every fork's key"
        print("key-invalidation ok — config values, listed modules, source props, and masks "
              "move exactly their forks")
    finally:
        config.SOURCES_DIR = saved_dir
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)
        if env_force is not None:
            os.environ["FORCE_REBUILD"] = env_force
        if saved_land is not None:
            os.environ["LANDMASK"] = saved_land
        if saved_water is not None:
            os.environ["WATERMASK"] = saved_water


def check_crash_leaves_stale():
    """A fork that crashes mid-run must leave the NEXT run stale — the crash-safety invariant
    content addressing preserves. run() SUPERSEDES a stem's content-named siblings (all keys, plus
    any .empty marker) BEFORE (re)generating, so a crash between the supersede and the atomic
    publish leaves nothing at the current key: no content file, no empty marker -> fork_fresh False.
    Without the supersede, last build's content file at the SAME key (a FORCE re-run, or unchanged
    inputs) would survive the crash and read fresh over a fork that never actually completed — a
    permanent silent hole in vector.pmtiles. A garbage DEM (exists, unreadable) crashes each fork;
    assert the stem carries neither artifact nor marker afterward and plans as stale."""
    import aggregation_run
    import config
    import contour_run
    import depare_run
    import keys
    import soundings_run
    import utils
    env_force = os.environ.pop("FORCE_REBUILD", None)
    saved_land = os.environ.pop("LANDMASK", None)
    saved_water = os.environ.pop("WATERMASK", None)
    tmp = tempfile.mkdtemp()
    cwd, saved_dir = os.getcwd(), config.SOURCES_DIR
    try:
        os.chdir(tmp)
        config.SOURCES_DIR = "sources"
        aid = "01EEEEEEEEEEEEEEEEEEEEEEEE"
        stem = "8-1-1-12"
        fp = _synthetic_covering(aid, [stem])[0]
        tdir = f"store/aggregation/{aid}/{stem}-tmp"
        os.makedirs(tdir)
        with open(f"{tdir}/0-3857.tiff", "w") as f:
            f.write("not a GeoTIFF")  # passes the exists() gate, crashes every fork mid-run
        for fork, mod in (("contour", contour_run), ("soundings", soundings_run), ("depare", depare_run)):
            art = aggregation_run._artifact(fork, stem)
            key = aggregation_run._KEYFN[fork](fp)
            cpath = keys.content_path(art, key)
            utils.create_folder(os.path.dirname(cpath))
            open(cpath, "w").close()  # the pre-crash state: a fresh content artifact at this key
            assert keys.fork_fresh(art, key), f"{fork}: setup must be fresh"
            # run()'s per-fork sequence: supersede (clears this key + all others) THEN (re)generate.
            keys.supersede(art)
            try:
                mod.generate(fp, cpath)
                raise AssertionError(f"{fork}: generate on a garbage DEM must raise")
            except AssertionError:
                raise
            except Exception:
                pass  # the simulated mid-run crash
            assert not os.path.isfile(cpath), f"{fork}: the crash must not leave a content artifact"
            assert not os.path.isfile(keys.empty_marker(art, key)), f"{fork}: nor an empty marker"
            assert not keys.fork_fresh(art, key), f"{fork}: the post-crash state must plan as stale"

        # Bundle level, same rule: the per-sha bundle outputs (store/bundle/*.pmtiles + manifest.json)
        # stay sidecar-keyed — they're never hydrated — so their invalidate-before-write discipline
        # (72c4034) survives content addressing. soundings.pmtiles' sidecar (and the stale archive)
        # must go BEFORE tippecanoe starts, so a FORCE + crash mid-bundle reads stale next run
        # instead of vouching for a torn archive. A CONTENT-named garbage member (so _live keeps it)
        # makes tippecanoe fail right after the invalidation.
        utils.create_folder("store/soundings")
        with open(f"store/soundings/{stem}-abcdef012345.geojson", "w") as f:
            f.write("not geojson")
        utils.create_folder("store/bundle")
        out = "store/bundle/soundings.pmtiles"
        open(out, "w").close()
        keys.write_key(out, "prev-bundle-key")
        os.environ["FORCE_REBUILD"] = "1"  # the FORCE case: proceed despite whatever the key says
        try:
            soundings_run.bundle()
            raise AssertionError("soundings bundle on a garbage member must raise")
        except AssertionError:
            raise
        except Exception:
            pass  # the simulated mid-bundle crash
        finally:
            os.environ.pop("FORCE_REBUILD", None)
        # tippecanoe may itself leave a torn NEW archive behind — that's the real crash shape;
        # what matters is the sidecar is gone, so nothing vouches for whatever file remains.
        assert keys.read_key(out) is None, "a bundle crash must not leave the old sidecar"
        assert not keys.is_fresh(out, "prev-bundle-key", require_artifact=False), \
            "the post-crash bundle state must plan as stale"

        # Empty input, same rule: a previously-built bundle whose inputs are now ZERO must not
        # survive the early return — _finalize_contours folds soundings/depare.pmtiles into
        # vector.pmtiles whenever they exist on disk, so a stale layer would ship as current.
        # Under content addressing "legitimately empty" means an .empty marker per covering stem;
        # the completeness gate demands one before the empty-input path is even reachable.
        os.remove(f"store/soundings/{stem}-abcdef012345.geojson")
        for fork in ("contour", "soundings"):
            keys.write_empty(aggregation_run._artifact(fork, stem),
                             aggregation_run._KEYFN[fork](fp))
        for stale_out, mod in (("store/bundle/soundings.pmtiles", soundings_run),
                               ("store/bundle/vector.pmtiles", contour_run)):
            open(stale_out, "w").close()
            keys.write_key(stale_out, "prev-bundle-key")
            mod.bundle()  # zero members (no geojson / no contour FGBs) -> the empty-input path
            assert not os.path.isfile(stale_out), \
                f"{stale_out}: an empty-input bundle must remove the previously-built output"
            assert keys.read_key(stale_out) is None, \
                f"{stale_out}: an empty-input bundle must remove the old sidecar"
        print("crash-leaves-stale ok — supersede + mid-fork/mid-bundle crashes and empty inputs "
              "all leave nothing vouching; next run re-runs")
    finally:
        config.SOURCES_DIR = saved_dir
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)
        if env_force is not None:
            os.environ["FORCE_REBUILD"] = env_force
        if saved_land is not None:
            os.environ["LANDMASK"] = saved_land
        if saved_water is not None:
            os.environ["WATERMASK"] = saved_water


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
        # One single-zoom archive under the coarse parent (4,4,5) at z9 — 1024 tiles spanning the
        # 4 cells; payload = the tile id, so reads are verifiable. Content-addressed name (a 12-hex
        # key), which create_archive parses back to the logical stem via keys.stem_of.
        pm = "store/pmtiles/4-4-5-9-abcdef012345.pmtiles"
        children = list(mercantile.children(mercantile.Tile(x=4, y=5, z=4), zoom=9))
        with open(pm, "wb") as f:
            w = Writer(f)
            for tid in sorted(zxy_to_tileid(t.z, t.x, t.y) for t in children):
                w.write_tile(tid, str(tid).encode())
            w.finalize({"tile_type": TileType.WEBP, "tile_compression": Compression.NONE,
                        "min_zoom": 9, "max_zoom": 9, "min_lon_e7": 0, "min_lat_e7": 0,
                        "max_lon_e7": 1, "max_lat_e7": 1, "center_zoom": 9,
                        "center_lon_e7": 0, "center_lat_e7": 0}, {})
        seen = set()
        for cell in fan:
            meta = bundle.create_archive([pm], cell)
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
    [0, DRYING_CAP] bucket, kept where it is effective WATER = (NOT land) OR inland water — i.e.
    cut by effective LAND = OSM land ∖ inland water (bucket ∖ land ∪ bucket ∩ water). A foreshore
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
            out = f"store/depare/{stem}.fgb"
            depare_run.generate(f"store/aggregation/{aid}/{stem}-aggregation.csv", out)
            return gpd.read_file(out)

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
            out = f"store/depare/{stem}.fgb"
            depare_run.generate(f"store/aggregation/{aid}/{stem}-aggregation.csv", out)
            row_lonlat = lambda r: to4326.transform(*(tr * (px // 2 + 0.5, r + 0.5)))
            return (gpd.read_file(out),
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
            path = f"store/depare/{stem}.fgb"
            depare_run.generate(f"store/aggregation/{aid}/{stem}-aggregation.csv", path)
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
        # base: 1°x1° near the equator (-101, native ~z10). fine: 0.4°x0.4° inside
        # it (-51, native ~z14, well above the z11 cap so the cap binds on any GDAL
        # version). 0.4° spans more than a z11 tile so a z11 tile is fully fine.
        # -101/-51 (not -100/-50) so no constant surface sits exactly ON a contour level.
        make_source(tmp, "base", west=-0.5, north=0.5, deg=1.0, px=1024, value=-101, max_zoom=10)
        make_source(tmp, "fine", west=-0.2, north=0.2, deg=0.4, px=4096, value=-51, max_zoom=11)

        run(tmp, "source_bounds.py", "base")
        run(tmp, "source_bounds.py", "fine")
        run(tmp, "aggregation_covering.py")
        run(tmp, "aggregation_run.py")
        run(tmp, "downsampling.py", "cover")
        run(tmp, "downsampling.py", "run")   # whole dirty pyramid on one machine
        run(tmp, "bundle.py")

        n_tiles = len(glob(f"{tmp}/store/aggregation/*/*-aggregation.csv"))

        # ── store manifest: complete (a name per fork per tile + one per overview), every name a
        # real store object, and the ``.empty`` flag matches the extension. Assembled in the same
        # subprocess env the build used so its recomputed keys match the content filenames.
        body_first = store_manifest_body(tmp)
        manifest = json.loads(body_first)
        entries = manifest["entries"]
        assert {"terrain", "contour", "soundings", "depare", "overview"} <= {e["fork"] for e in entries}, \
            f"manifest missing a fork: {sorted({e['fork'] for e in entries})}"
        for e in entries:
            assert os.path.isfile(f"{tmp}/store/{e['name']}"), f"manifest names a missing file: {e['name']}"
            assert e["name"].endswith(".empty") == e["empty"], f"empty flag/ext mismatch: {e}"
        assert store_manifest_body(tmp) == body_first, "store manifest must be byte-deterministic across a re-walk"
        print(f"store-manifest ok — {len(entries)} entries, complete, every name a live store object")

        # ── phase-3 acceptance (a): a NO-CHANGE rerun does zero work. A fresh covering (new
        # ULID, identical rows) leaves every fork's content name present, so aggregate re-runs
        # zero tiles, the overview names all match, and the bundle skips off the manifest key.
        run(tmp, "aggregation_covering.py")
        out = run(tmp, "aggregation_run.py").stdout
        assert "nothing to do." in out, f"no-change rerun must aggregate nothing: {out!r}"
        run(tmp, "downsampling.py", "cover")
        out = run(tmp, "downsampling.py", "run").stdout
        assert "parent(s)" not in out, f"no-change rerun must downsample nothing: {out!r}"
        out = run(tmp, "bundle.py").stdout
        assert "unchanged — skip" in out, f"no-change rerun must skip the bundle: {out!r}"
        # The manifest BODY is byte-stable across the no-op rerun even though the covering ULID
        # (the manifest FILENAME) changed — the pointer-last atomicity depends on this determinism.
        assert store_manifest_body(tmp) == body_first, \
            "store manifest body must be byte-stable across a no-op rerun (new covering ULID)"
        print(f"no-change rerun ok — 0 of {n_tiles} tiles re-ran; downsample + bundle skipped; manifest stable")

        # ── phase-3 acceptance (b): a CONTOUR_LEVELS change re-runs every tile (the contour +
        # depare forks are stale, and the shared merge is recomputed with them) but rewrites NO
        # terrain pmtiles — their keys don't include the contour config — so downsample and the
        # terrain bundle skip entirely. The old covering diff's blind spot, now a test.
        levels = ("-10000 -8000 -6000 -5000 -4000 -3000 -2000 -1000 -500 -300 -200 "
                  "-100 -50 -30 -20 -10 -5")  # default ladder minus the -2
        terrain_before = content_names(tmp, "store/pmtiles/**/*.pmtiles")
        contour_before = content_names(tmp, "store/contour/*.fgb", "store/contour/*.empty")
        assert terrain_before and len(contour_before) == n_tiles, \
            "first run must have written a contour artifact or empty marker per tile"
        proc = run(tmp, "aggregation_run.py", env={"CONTOUR_LEVELS": levels})
        out = proc.stdout
        assert "nothing to do." not in out, "a contour-config change must dirty the tiles"
        # The ladder-divergence guard: an overridden CONTOUR_LEVELS must warn that the tiles now
        # diverge from the style's hand-mirrored DEPARE_LADDER_M/FT.
        assert "CONTOUR_LEVELS overridden" in proc.stderr, \
            "the style-divergence warning must fire on an overridden ladder"
        assert content_names(tmp, "store/pmtiles/**/*.pmtiles") == terrain_before, \
            "terrain content names must be untouched by a contour-config change"
        # Every tile re-ran: its contour content name (artifact or empty marker) carries the new
        # key, and the old-key file was superseded. Deterministic, unlike counting Pool stdout.
        contour_after = content_names(tmp, "store/contour/*.fgb", "store/contour/*.empty")
        assert set(contour_after) == set(contour_before), "the same tiles must still be present"
        assert all(contour_after[s] != contour_before[s] for s in contour_before), \
            "every tile's contour content name (key) must move on a contour-config change"
        out = run(tmp, "downsampling.py", "run", env={"CONTOUR_LEVELS": levels}).stdout
        assert "parent(s)" not in out, f"downsample must skip when terrain keys are unchanged: {out!r}"
        out = run(tmp, "bundle.py", env={"CONTOUR_LEVELS": levels}).stdout
        assert "unchanged — skip" in out, f"bundle must skip when terrain keys are unchanged: {out!r}"
        print(f"contour-config change ok — {n_tiles} tiles re-merged, terrain keys stable, "
              "downsample + bundle skipped")

        import glob as _glob

        # The groups are planet + one overlay per z5 grid cell holding a deep (z11) tile.
        agg_dir = f"{tmp}/store/aggregation"
        agg_id = sorted(os.listdir(agg_dir))[-1]
        agg_stems = [n.replace("-aggregation.csv", "").split("-")
                     for n in os.listdir(f"{agg_dir}/{agg_id}") if n.endswith("-aggregation.csv")]
        expected = {"planet"} | {f"5-{int(x) >> (int(z) - 5)}-{int(y) >> (int(z) - 5)}"
                                 for z, x, y, cz in agg_stems if int(cz) > 10}

        # orphan exclusion: a CONTENT-named pmtiles left from a re-tiled covering (its stem is not
        # in the current covering) must land in NO bundle, else it double-bundles a stale tile over
        # the live tiling (the raster twin of the contour-overlap bug). Drop one in and re-bundle
        # under FORCE (phase-3 acceptance (d): the escape hatch re-runs a fully-fresh bundle): the
        # manifest's overlay cells must be unchanged (an orphan z99 stem would else mint a cell).
        orphan = f"{tmp}/store/pmtiles/0-0-0-99-abcdef012345.pmtiles"
        open(orphan, "w").close()
        out = run(tmp, "bundle.py", env={"FORCE_REBUILD": "1"}).stdout
        assert "unchanged — skip" not in out, "FORCE must re-run the bundle regardless of keys"
        mf_orphan = json.load(open(f"{tmp}/store/bundle/manifest.json"))
        assert set(mf_orphan["overlay"]["cells"]) == expected - {"planet"}, \
            f"orphan leaked into the bundle: {set(mf_orphan['overlay']['cells'])} != {expected - {'planet'}}"
        os.remove(orphan)
        print(f"orphan-exclusion ok — stale-covering pmtiles excluded from every bundle; FORCE re-bundles")

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
        # tile must read ~-51 (fine wins over base in overlap), not -101.
        z11_shallowest = max(by_zoom[11])  # -51 is shallower than base's -101
        assert z11_shallowest > -55, f"fine should win at z11 (shallowest z11 tile {z11_shallowest:.1f})"
        # base shows through somewhere: some tile reads ~-101.
        all_meds = [m for meds in by_zoom.values() for m in meds]
        assert min(all_meds) < -90, f"base (-101) should appear (min median {min(all_meds):.1f})"
        print(f"engine e2e ok — zooms {min(by_zoom)}..{max_z}, fine wins at z11 "
              f"({z11_shallowest:.1f}), base present (min {min(all_meds):.1f})")

        # FAIL-CLOSED: a covering whose pmtiles is gone (a failed/interrupted run) must
        # fail the bundle, not silently publish a hole the Worker fills with overzoomed
        # GEBCO. Delete one tile and assert bundle.py now exits non-zero (verify_complete
        # runs BEFORE the key skip, so a fresh manifest key can't paper over the hole).
        victim = sorted(_glob.glob(f"{tmp}/store/pmtiles/**/*.pmtiles", recursive=True))[0]
        os.remove(victim)
        try:
            run(tmp, "bundle.py")
        except subprocess.CalledProcessError:
            print(f"completeness gate ok — bundle failed on missing {os.path.basename(victim)}")
        else:
            raise AssertionError("bundle.py must fail when a covering has no pmtiles")

        # The VECTOR twin (contour_run.verify_vector_complete, self-enforcing rather than relying
        # on build.yml's store-manifest step running first): a covering tile whose contour fork
        # left neither artifact nor .empty marker must fail the contour bundle. The gate fires
        # before tippecanoe, so this needs no tippecanoe binary; it also runs before the
        # fresh-skip, so a fresh vector key can't paper over the gap.
        vfgb = sorted(_glob.glob(f"{tmp}/store/contour/*.fgb"))[0]
        os.remove(vfgb)
        try:
            run(tmp, "contour_run.py", "bundle")
        except subprocess.CalledProcessError as e:
            assert "contour incomplete" in (e.stderr or ""), \
                f"wrong failure (want the completeness gate): {e.stderr!r}"
            print(f"vector completeness gate ok — contour bundle failed on missing {os.path.basename(vfgb)}")
        else:
            raise AssertionError("contour_run.py bundle must fail when a covering tile has no contour artifact/marker")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    import depare_run
    import landmask
    landmask._check()
    depare_run._check()
    check_priority()
    check_self_heal()
    check_stale_overview()
    check_key_invalidation()
    check_crash_leaves_stale()
    check_grid_split()
    check_feather_guard()
    check_land_clamp()
    check_water_clamp()
    check_depare()
    check_depare_drying()
    check_depare_water()
    main()
