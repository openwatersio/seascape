"""Assemble the per-build STORE MANIFEST: a deterministic map of every content-addressed store
artifact this build's covering produced, so the next build's hydrate pulls exactly them and the GC
knows what's still referenced.

It is a pure walk of the LOCAL store — no R2, no bucket names. For each tile in the current
covering it recomputes the stage-2 mosaic key + each vector fork's key (the same functions the
aggregate used) and, for each terrain render stem, the terrain key (the same terrain.py uses),
recording the content-addressed file it finds (or the ``.empty`` marker a legitimately-empty fork
left). A missing artifact/marker is a hard error: the manifest asserts completeness, exactly like
``bundle.verify_complete`` for the terrain pmtiles, so a half-built store can never get a manifest
that would then vouch for it.

The manifest BODY carries no build id (the id is the filename + the pointer), so a no-op rebuild —
same store, fresh covering ULID — produces byte-identical bytes. The workflow names the file
``manifests/<id>.json``, publishes it, and flips the ``manifest.json`` pointer LAST (the
pointer-last / bounds.csv-last atomicity generalized): a reader sees the whole old world or the
whole new one.

  store_manifest.py write   walk the store, write store/manifests/<covering-id>.json, print the id
  store_manifest.py --check  self-check
"""

import json
import os
import sys

import aggregation_run
import keys
import mosaic
import terrain
import utils

# store/<name> in R2 mirrors store/<name> locally, so a manifest name is just the local store path
# with this prefix stripped — a store-root-relative path the workflow feeds to `rclone --files-from`.
_STORE = "store/"


def _rel(path):
    return path[len(_STORE):] if path.startswith(_STORE) else path


def _entry(stem, fork, key, art):
    """One manifest entry for a fork: the content file if it exists, else the empty marker, else a
    hard completeness error (a fork the plan expected but neither artifact nor marker landed)."""
    cpath, epath = keys.content_path(art, key), keys.empty_marker(art, key)
    if os.path.isfile(cpath):
        return {"stem": stem, "fork": fork, "key": key, "name": _rel(cpath), "empty": False}
    if os.path.isfile(epath):
        return {"stem": stem, "fork": fork, "key": key, "name": _rel(epath), "empty": True}
    raise SystemExit(
        f"store manifest incomplete: {fork} {stem} has neither its content artifact nor an empty "
        f"marker under key {key} (a failed/interrupted build) — refusing to publish a manifest")


def entries():
    """Every content-addressed store artifact the current covering produced, as a deterministic
    (name-sorted) list of {stem, fork, key, name, empty}: per aggregation tile the stage-2 MOSAIC
    tile + the vector forks (contour/soundings/depare); per terrain render stem (native + coalesced
    overviews) the served terrain pmtiles. Empty at bootstrap (no covering) — the caller refuses to
    publish a planet manifest then."""
    from glob import glob

    ids = utils.get_aggregation_ids()
    if not ids:
        return []
    aggregation_id = ids[-1]
    out = []
    for fp in aggregation_run.covering_sorted():
        stem = fp.split("/")[-1].replace("-aggregation.csv", "")
        out.append(_entry(stem, "mosaic", mosaic.mosaic_key(fp), mosaic.tile_artifact(stem)))
        for fork, plan in aggregation_run.plan_forks(fp).items():
            if "key" not in plan:  # a SKIP_*'d fork produces nothing — not part of the store
                continue
            out.append(_entry(stem, fork, plan["key"], plan["art"]))
    agg_keys = terrain._aggregation_mosaic_keys(aggregation_id)
    for tstem in sorted(terrain._render_stems(aggregation_id)):
        out.append(_entry(tstem, "terrain", terrain.terrain_key(tstem, agg_keys),
                          terrain._artifact(tstem)))
    return sorted(out, key=lambda e: e["name"])


def serialize(entry_list):
    """The manifest bytes — sorted keys + the pre-sorted entry list, so identical store state gives
    byte-identical output (the determinism the byte-stability test relies on)."""
    return json.dumps({"entries": entry_list}, sort_keys=True, indent=2) + "\n"


def write():
    """Write store/manifests/<covering-id>.json and print the id (the workflow names the R2 object
    and the pointer off it). The covering ULID is this build's natural id — a fresh one per build,
    chronologically sortable — and stays OUT of the manifest body so the body is byte-stable."""
    ids = utils.get_aggregation_ids()
    if not ids:
        sys.exit("store manifest: no covering — run `just cover` first")
    build_id = ids[-1]
    body = serialize(entries())
    utils.create_folder("store/manifests")
    path = f"store/manifests/{build_id}.json"
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(body)
    os.replace(tmp, path)  # the manifest file only ever appears complete
    print(build_id)


def _check():
    """Assembly is complete (a missing artifact fails), byte-deterministic across a re-walk, and
    reflects the content name / empty marker each fork left. Builds a synthetic covering + store."""
    import shutil
    import tempfile

    import config
    saved_dir, cwd = config.SOURCES_DIR, os.getcwd()
    saved_land = os.environ.pop("LANDMASK", None)
    saved_water = os.environ.pop("WATERMASK", None)
    d = tempfile.mkdtemp()
    try:
        os.chdir(d)
        config.SOURCES_DIR = "sources"
        os.makedirs("sources/src", exist_ok=True)
        with open("sources/src/metadata.json", "w") as f:
            json.dump({"name": "src", "max_zoom": 12}, f)
        aid = "01MANIFESTMANIFESTMANIFEST"
        os.makedirs(f"store/aggregation/{aid}")
        # two aggregate tiles at z8 (native child_z 12); terrain.cover plans their native + overview
        # render stems from these.
        tiles = ["8-1-1-12", "8-2-2-12"]
        for t in tiles:
            with open(f"store/aggregation/{aid}/{t}-aggregation.csv", "w") as f:
                f.write("source,filename,maxzoom\nsrc,src_0.tif,12\n")
        terrain.cover()

        # Materialize each stored artifact/marker at its current key: per tile the mosaic COG + the
        # vector forks (tile B's contour legitimately EMPTY — a marker, not a file); per terrain
        # render stem its pmtiles.
        def keyof(t, fork):
            return aggregation_run._KEYFN[fork](f"store/aggregation/{aid}/{t}-aggregation.csv")

        def touch(cpath):
            utils.create_folder(os.path.dirname(cpath))
            open(cpath, "w").close()
        for t in tiles:
            mfp = f"store/aggregation/{aid}/{t}-aggregation.csv"
            touch(keys.content_path(mosaic.tile_artifact(t), mosaic.mosaic_key(mfp)))
            for fork in aggregation_run.FORKS:
                art, key = aggregation_run._artifact(fork, t), keyof(t, fork)
                if t == "8-2-2-12" and fork == "contour":
                    keys.write_empty(art, key)            # the legitimately-empty fork
                else:
                    touch(keys.content_path(art, key))
        agg_keys = terrain._aggregation_mosaic_keys(aid)
        render_stems = sorted(terrain._render_stems(aid))
        for tstem in render_stems:
            touch(keys.content_path(terrain._artifact(tstem), terrain.terrain_key(tstem, agg_keys)))

        e = entries()
        # per tile: 1 mosaic + len(FORKS) vector forks; plus one terrain entry per render stem.
        assert len(e) == len(tiles) * (1 + len(aggregation_run.FORKS)) + len(render_stems), \
            f"entry count {len(e)}"
        assert serialize(e) == serialize(entries()), "manifest not byte-deterministic across a re-walk"
        empties = [x for x in e if x["empty"]]
        assert len(empties) == 1 and empties[0]["stem"] == "8-2-2-12" and empties[0]["fork"] == "contour" \
            and empties[0]["name"].endswith(".empty"), "the empty fork must record its marker, not a file"
        assert all(not x["empty"] and keys.is_content_name(x["name"]) for x in e if not x["empty"]), \
            "non-empty entries name a content-addressed artifact"
        assert all(x["name"].startswith(("pmtiles/", "mosaic/", "contour/", "soundings/", "depare/"))
                   for x in e), "names are store-root-relative"
        assert {"mosaic", "terrain", "contour", "soundings", "depare"} == {x["fork"] for x in e}, \
            f"manifest missing a store kind: {sorted({x['fork'] for x in e})}"

        # completeness: drop one terrain artifact -> assembly fails (the gate bundle.verify_complete mirrors)
        victim = keys.content_path(terrain._artifact(render_stems[0]),
                                   terrain.terrain_key(render_stems[0], agg_keys))
        os.remove(victim)
        try:
            entries()
        except SystemExit:
            pass
        else:
            raise AssertionError("a missing artifact must fail manifest assembly")
        print(f"store_manifest self-check ok ({len(e)} entries, 1 empty marker)")
    finally:
        config.SOURCES_DIR = saved_dir
        os.chdir(cwd)
        shutil.rmtree(d, ignore_errors=True)
        if saved_land is not None:
            os.environ["LANDMASK"] = saved_land
        if saved_water is not None:
            os.environ["WATERMASK"] = saved_water


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["write"]:
        write()
    elif a[:1] == ["--check"]:
        _check()
    else:
        sys.exit("usage: store_manifest.py write | --check")
