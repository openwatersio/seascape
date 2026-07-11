"""Content-hash cache keys: skip a stage output whose inputs, code, config, and toolchain
are all unchanged.

A stage computes ``stage_key(inputs, modules, config)`` — a short stable hash of the resolved
config values, the bytes of each pipeline module it depends on, each input's own hash, and the
toolchain identity. That 12-hex key then rides IN the artifact's filename
(``store/pmtiles/<stem>-<key>.pmtiles``, ``content_path``): the artifact is CONTENT-ADDRESSED, so
freshness is simply "the content-named file exists" (``fork_fresh``) — no sidecar to match. A
re-keyed rebuild (a config/input change) writes a NEW name and the old file becomes unreferenced
garbage the GC collects; nothing is ever mutated in place, so a crash can't tear a live artifact
and a partial store reads stale, never falsely fresh. ``publish`` renames a temp file into the
content name atomically so the name only appears complete; a fork that legitimately produces
nothing writes an ``empty_marker`` instead. ``FORCE_REBUILD`` ignores freshness — the escape
hatch, not a correctness requirement.

The per-build STORE MANIFEST (a walk of the store, ``store_manifest.py``) records every fork's
content name / empty marker; the workflow publishes it and flips a pointer LAST, so a reader (the
next build's hydrate, the GC) sees the complete old world or the complete new one. Manifests +
GC live outside Python — this module knows only the local store.

The ``sidecar`` / ``read_key`` / ``write_key`` / ``is_fresh`` helpers below are the ONE remaining
sidecar user: the per-sha bundle outputs (``store/bundle/*``), which are never hydrated and are
rebuilt every box build, so a local ``.key`` sidecar is the cheapest laptop skip. The
content-addressed STORE (pmtiles / contour / soundings / depare / overviews) carries no sidecars.

Module hashing is per-module and COARSE on purpose: a stage lists the ``pipelines/*.py`` it
depends on and the key hashes those whole files, so a comment edit over-invalidates the stage.
Over-invalidation is accepted; under-invalidation is the bug this exists to kill — a smoothing /
contour / encode change the old covering diff couldn't see, silently serving stale tiles. Config
enters as the RESOLVED VALUES a stage read (a level list, a sigma), never env-var names.

R2-agnostic like the rest of ``pipelines/``: the toolchain identity comes from a ``TOOLCHAIN``
env var (the workflow passes the GHCR image tag; docker.sh passes it too) and falls back to the
local GDAL version so a laptop's keys stay honest about GDAL skew. No bucket names here.

  python keys.py --check   self-check
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from functools import lru_cache
from glob import glob

# This directory — listed modules are resolved against it.
PIPELINES_DIR = os.path.dirname(os.path.abspath(__file__))


@lru_cache(maxsize=None)
def _module_bytes(name):
    """Bytes of a listed pipeline module (a bare name like ``smooth`` or ``smooth.py``). Cached,
    so a module is read+hashed once per process, not once per tile."""
    fn = name if name.endswith(".py") else f"{name}.py"
    with open(os.path.join(PIPELINES_DIR, fn), "rb") as f:
        return f.read()


@lru_cache(maxsize=1)
def toolchain():
    """The toolchain identity that belongs in every key: ``TOOLCHAIN`` (the workflow passes the
    GHCR image tag, which pins one GDAL/tippecanoe) when set, else the local GDAL version so a
    laptop's keys are still honest about GDAL skew. A GDAL bump correctly invalidates the world.
    Caveat: the fallback sees GDAL only — a local tippecanoe upgrade goes unnoticed (acceptable:
    laptops don't feed the shared store, and the box always has TOOLCHAIN set)."""
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


def _canonical(config):
    """Deterministic JSON of a config dict — sorted keys, no whitespace jitter — so the same
    resolved values always hash the same (``default=str`` lets a stray tuple/float serialize)."""
    return json.dumps(config, sort_keys=True, separators=(",", ":"), default=str).encode()


def stage_key(inputs, modules, config):
    """``H(canonical(config) ‖ each module's bytes ‖ each input hash ‖ toolchain)`` → 12 hex.

    inputs: already-resolved hashes/identities (``str``) or raw ``bytes`` — a source recipe hash,
      a parent artifact's key, a covering-row string. ``None`` entries are tolerated (an
      unregistered source with no recipe hash) and contribute a fixed separator only, so an input
      appearing/vanishing still moves the key.
    modules: pipeline module names whose whole-file bytes are the stage's code dependency.
    config: the resolved config VALUES the stage read (never env-var names).
    """
    h = hashlib.sha256()
    h.update(_canonical(config))
    for name in modules:
        h.update(b"\x00M\x00")
        h.update(_module_bytes(name))
    for inp in inputs:
        h.update(b"\x00I\x00")
        if inp is None:
            continue
        h.update(inp.encode() if isinstance(inp, str) else inp)
    h.update(b"\x00T\x00")
    h.update(toolchain().encode())
    return h.hexdigest()[:12]


def sidecar(artifact):
    """The key sidecar path for an artifact (lives right next to it, so the store's rclone
    prefixes carry it with zero workflow changes)."""
    return artifact + ".key"


def read_key(artifact):
    p = sidecar(artifact)
    if not os.path.isfile(p):
        return None
    with open(p) as f:
        return f.read().strip()


def write_key(artifact, key):
    """Atomic: the sidecar only ever exists complete (a torn one would merely read stale, but
    atomicity costs one rename)."""
    p = sidecar(artifact)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        f.write(key)
    os.replace(tmp, p)


def forced():
    """The escape hatch: ``FORCE_REBUILD`` rebuilds every keyed artifact regardless of match."""
    return bool(os.environ.get("FORCE_REBUILD"))


def is_fresh(artifact, key, require_artifact=True):
    """The skip rule: the sidecar matches ``key`` — unless ``FORCE_REBUILD``, which ignores it.

    ``require_artifact`` (default): the artifact must also exist, so a dropped artifact self-heals
    (missing → stale → rebuilt). A fork that may legitimately produce nothing (an all-land tile's
    contours) passes ``require_artifact=False`` and relies on the sidecar alone, matching the old
    covering diff, which only ever self-healed the always-present terrain pmtiles."""
    if forced():
        return False
    if require_artifact and not os.path.isfile(artifact):
        return False
    return read_key(artifact) == key


# ── content-addressed store artifacts ────────────────────────────────────────
# The store's pmtiles / FGB / geojson / overview artifacts carry their key in the filename
# (below), so freshness is existence and there is no sidecar to match. `artifact` here is always
# the LOGICAL base path ({z}-{x}-{y}-{child_z}.<ext>) — content_path splices the key in.

_KEY_RE = re.compile(r"[0-9a-f]{12}")


def content_path(artifact, key):
    """The content-addressed path of a logical artifact: the 12-hex key spliced in before the
    extension. ``store/contour/8-1-1-12.fgb`` + key -> ``store/contour/8-1-1-12-<key>.fgb``."""
    root, ext = os.path.splitext(artifact)
    return f"{root}-{key}{ext}"


def empty_marker(artifact, key):
    """The marker path for a fork that legitimately produced NO artifact under this key (an
    all-land tile's contours). Same content-addressed name, ``.empty`` extension: its existence
    alone means "this fork ran at this key and was empty" (the key is in the name, so the bytes
    are irrelevant). A distinct extension keeps it out of every ``*.fgb`` / ``*.pmtiles`` glob and
    makes the empty case explicit in a store listing — cleaner than a bytes-meaningless ``.key``
    sidecar, and it can't be confused with a legacy sidecar the GC sweeps."""
    root, _ = os.path.splitext(artifact)
    return f"{root}-{key}.empty"


def is_content_name(name):
    """True iff ``name`` (a basename or path) is a content-addressed artifact/marker — its stem's
    trailing ``-``-field is a 12-hex key. Filters legacy mutable names and torn logical writes out
    of a store glob before ``stem_of`` / ``stem_and_key`` parse them."""
    root = os.path.splitext(os.path.basename(name))[0]
    return bool(_KEY_RE.fullmatch(root.rpartition("-")[2]))


def stem_and_key(name):
    """Split a content-addressed basename ``<stem>-<key12>.<ext>`` -> ``(stem, key)``. The stem is
    the logical tile id ({z}-{x}-{y}-{child_z}); the key is the trailing 12-hex field. Raises on a
    non-content-addressed name — callers glob-filter with ``is_content_name`` first."""
    root = os.path.splitext(os.path.basename(name))[0]
    stem, _, key = root.rpartition("-")
    if not _KEY_RE.fullmatch(key):
        raise ValueError(f"{name!r} is not a content-addressed artifact")
    return stem, key


def stem_of(name):
    """The logical stem of a content-addressed name (everything before the ``-<key12>``)."""
    return stem_and_key(name)[0]


def fork_fresh(artifact, key):
    """A fork is fresh iff its content-addressed artifact OR its empty marker exists under this
    exact key (unless ``FORCE_REBUILD``). Existence IS the match — a dropped artifact self-heals
    (gone -> stale -> rebuilt), a legitimately-empty fork is marked done by its ``.empty`` marker,
    and a partial/torn write never reads fresh (publish renames atomically). Folds phase-3's
    sidecar-match + the terrain-vs-vector ``require_artifact`` split into one existence check."""
    if forced():
        return False
    return os.path.isfile(content_path(artifact, key)) or os.path.isfile(empty_marker(artifact, key))


def publish(tmp_path, final_path):
    """Atomically move a fully-written temp file to its content-addressed name (``os.replace``
    within one filesystem is atomic). The final name only ever appears complete — a crash mid-write
    leaves the temp, which no freshness check looks at, and the fork reads stale next run. The
    content-addressed analog of phase-3's sidecar-last rule."""
    os.makedirs(os.path.dirname(final_path), exist_ok=True)
    os.replace(tmp_path, final_path)


def write_empty(artifact, key):
    """Atomically create the empty marker for a fork that produced no artifact under this key
    (tmp + rename, so a crash before completion leaves no marker -> reads stale -> re-runs, never
    a marker vouching for an empty result the fork never reached)."""
    final = empty_marker(artifact, key)
    os.makedirs(os.path.dirname(final), exist_ok=True)
    tmp = final + ".tmp"
    with open(tmp, "w"):
        pass
    os.replace(tmp, final)


def supersede(artifact):
    """Remove every content-addressed sibling (ALL keys) of an artifact's logical stem in its
    store dir — content files AND empty markers — so exactly the current key survives locally. A
    re-keyed rebuild would otherwise leave last build's file beside the new one under the same
    logical stem, and the bundle globs by stem: two files, a doubled tile. Called before a fork
    (re)writes; the crash-safety anchor (a crash after this, before publish, reads stale). In R2
    the superseded object just lingers unreferenced for the GC (pushes never ``--delete``)."""
    root, ext = os.path.splitext(artifact)
    for p in glob(f"{root}-*{ext}") + glob(f"{root}-*.empty"):
        if os.path.isfile(p):
            os.remove(p)


@lru_cache(maxsize=None)
def _file_hash_cached(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def file_hash(path):
    """A content hash of a local file (its bytes) to feed as a key input — a hand-swapped mask,
    say. ``None`` for a missing or ``/vsi`` (remote) path: a remote input carries its identity
    another way (a recipe-hash marker, or the module that pins its URL/release). Cached per
    absolute path — inputs don't change under a running build."""
    if path is None or path.startswith("/vsi") or not os.path.isfile(path):
        return None
    return _file_hash_cached(os.path.abspath(path))


def _check():
    """Key stable across a no-op recompute; moves on a config-value change, an input change, and a
    LISTED module's byte change; ignores an UNLISTED module's byte change; the toolchain enters the
    key; the sidecar skip rule + the FORCE bypass."""
    import shutil
    import tempfile

    global PIPELINES_DIR
    saved_dir, saved_force = PIPELINES_DIR, os.environ.pop("FORCE_REBUILD", None)
    saved_tc = os.environ.pop("TOOLCHAIN", None)
    d = tempfile.mkdtemp()
    try:
        PIPELINES_DIR = d
        _module_bytes.cache_clear()
        toolchain.cache_clear()
        with open(f"{d}/mod_a.py", "w") as f:
            f.write("A = 1\n")
        with open(f"{d}/mod_b.py", "w") as f:
            f.write("B = 1\n")

        cfg = {"levels": [1, 2], "cap": 3}
        base = stage_key(["in1", "in2"], ["mod_a"], cfg)
        assert stage_key(["in1", "in2"], ["mod_a"], dict(cfg)) == base, "key not stable on a no-op recompute"
        assert stage_key(["in1", "in2"], ["mod_a"], {"levels": [1, 3], "cap": 3}) != base, "config-value change must move the key"
        assert stage_key(["in1", "in9"], ["mod_a"], cfg) != base, "input change must move the key"
        assert stage_key([None, "in2"], ["mod_a"], cfg) != base, "a vanished input must move the key"

        # an UNLISTED module's byte change is ignored (mod_b changes; a mod_a-only key holds)
        with open(f"{d}/mod_b.py", "w") as f:
            f.write("B = 999\n")
        _module_bytes.cache_clear()
        assert stage_key(["in1", "in2"], ["mod_a"], cfg) == base, "unlisted module change must NOT move the key"

        # a LISTED module's byte change moves the key
        with open(f"{d}/mod_a.py", "w") as f:
            f.write("A = 2\n")
        _module_bytes.cache_clear()
        assert stage_key(["in1", "in2"], ["mod_a"], cfg) != base, "listed module byte change must move the key"

        # the toolchain enters the key
        os.environ["TOOLCHAIN"] = "img:v1"
        toolchain.cache_clear()
        k1 = stage_key([], ["mod_b"], {})
        os.environ["TOOLCHAIN"] = "img:v2"
        toolchain.cache_clear()
        assert stage_key([], ["mod_b"], {}) != k1, "toolchain must enter the key"
        os.environ.pop("TOOLCHAIN")
        toolchain.cache_clear()

        # sidecar skip rule + FORCE bypass
        art = f"{d}/art.pmtiles"
        open(art, "w").close()
        write_key(art, base)
        assert is_fresh(art, base), "artifact + matching sidecar must be fresh"
        assert not is_fresh(art, "other"), "a sidecar mismatch must be stale"
        os.remove(art)
        assert not is_fresh(art, base), "a missing artifact must be stale (self-heal)"
        # require_artifact=False: sidecar alone decides (the legitimately-empty fork)
        assert is_fresh(art, base, require_artifact=False), "sidecar-only freshness must ignore a missing artifact"
        open(art, "w").close()
        os.environ["FORCE_REBUILD"] = "1"
        assert not is_fresh(art, base), "FORCE must ignore a match"
        assert not is_fresh(art, base, require_artifact=False), "FORCE must ignore a sidecar-only match too"
        os.environ.pop("FORCE_REBUILD")

        # ── content-addressed store: name splicing, the content-name parse, fresh/publish/empty ──
        k = "0123456789ab"  # a well-formed 12-hex key
        logical = f"{d}/store/x/8-1-1-12.fgb"
        cpath = content_path(logical, k)
        assert cpath == f"{d}/store/x/8-1-1-12-{k}.fgb", cpath
        assert empty_marker(logical, k) == f"{d}/store/x/8-1-1-12-{k}.empty"
        assert is_content_name(os.path.basename(cpath)) and not is_content_name("8-1-1-12.fgb"), \
            "the 12-hex trailing field distinguishes a content name from a legacy/logical one"
        assert stem_and_key(os.path.basename(cpath)) == ("8-1-1-12", k)
        assert stem_of("8-1-1-12-abcdef012345.pmtiles") == "8-1-1-12"
        # a nested overview stem still round-trips (5-field logical stems don't occur, but the
        # trailing-key parse is field-count agnostic — it strips exactly the last field)
        assert stem_of("2-1-1-5-0011aabbccdd.pmtiles") == "2-1-1-5"

        # fresh iff the content file OR the empty marker exists; publish is atomic
        assert not fork_fresh(logical, k), "no artifact, no marker -> stale"
        tmp = f"{d}/scratch.fgb"
        open(tmp, "w").close()
        publish(tmp, cpath)
        assert os.path.isfile(cpath) and not os.path.isfile(tmp), "publish moves atomically"
        assert fork_fresh(logical, k), "content file present -> fresh"
        os.remove(cpath)
        write_empty(logical, k)
        assert os.path.isfile(empty_marker(logical, k)) and fork_fresh(logical, k), \
            "an empty marker alone marks the fork fresh (the legitimately-empty fork)"
        os.environ["FORCE_REBUILD"] = "1"
        assert not fork_fresh(logical, k), "FORCE ignores an existing content name / empty marker"
        os.environ.pop("FORCE_REBUILD")

        # supersede clears ALL keys' siblings of a stem (content + empty), leaving only a fresh write
        for other in ("aaaaaaaaaaaa", "bbbbbbbbbbbb"):
            open(content_path(logical, other), "w").close()
        open(content_path(f"{d}/store/x/8-1-1-13.fgb", k), "w").close()  # a DIFFERENT stem — untouched
        supersede(logical)
        assert not glob(f"{d}/store/x/8-1-1-12-*.fgb") and not glob(f"{d}/store/x/8-1-1-12-*.empty"), \
            "supersede removes every sibling of the stem"
        assert os.path.isfile(content_path(f"{d}/store/x/8-1-1-13.fgb", k)), \
            "supersede must not touch a different logical stem"
        print("keys.py self-check ok")
    finally:
        PIPELINES_DIR = saved_dir
        _module_bytes.cache_clear()
        toolchain.cache_clear()
        if saved_force is not None:
            os.environ["FORCE_REBUILD"] = saved_force
        if saved_tc is not None:
            os.environ["TOOLCHAIN"] = saved_tc
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        _check()
    else:
        sys.exit("keys.py is a library; run with --check for the self-check")
