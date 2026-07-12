"""Content-hash cache keys: skip a stage output whose inputs, code, config, and toolchain
are all unchanged.

A stage computes ``stage_key(inputs, modules, config)`` — a short stable hash of the resolved
config values, the bytes of each pipeline module it depends on, each input's own hash, and the
toolchain identity — and writes it in a sidecar next to the artifact (``store/pmtiles/x.pmtiles``
+ ``x.pmtiles.key``). An artifact is fresh iff it exists AND its sidecar matches the recomputed
key; a stage skips a fresh artifact and rebuilds a stale/missing one. ``FORCE_REBUILD`` ignores
the match — the escape hatch, no longer a correctness requirement.

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
import subprocess
import sys
from functools import lru_cache

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
