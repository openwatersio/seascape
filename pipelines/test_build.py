"""Parse/dry-run self-check for the unified DAG — the `cover` CHECKPOINT seam.

Since the two entry files merged into one Snakefile, there is no parse-time refusal to test.
Instead this asserts the checkpoint behaves, offline, against a synthetic store:

  - COLD (no covering) -> `mosaic -n` schedules the `cover` checkpoint and the pre-checkpoint
    skeleton (mosaic_index/mosaic), and NAMES the DAG as incomplete-until-checkpoint; the
    per-stem mosaic_tile jobs are NOT yet known (they appear only after cover materializes);
  - WARM (covering present + up to date) -> `mosaic -n` re-evaluates past the checkpoint and
    schedules exactly one mosaic_tile per stem + the index, with ZERO cover and no stage-1 job;
  - BBOX scopes the WARM stem set read-side (the covering is the full inventory); a window over
    open ocean refuses in the input function (still before any job runs);
  - the dry run is PURE: it writes nothing under store/.

Run from pipelines/:  uv run python test_build.py
"""

import os
import re
import shutil
import subprocess
import tempfile

PIPE = os.path.dirname(os.path.abspath(__file__))
SNAKEFILE = os.path.join(PIPE, "..", "Snakefile")  # ONE entry — the checkpoint lives here
STEMS = ["8-75-96-10", "8-76-96-10"]


def snakemake(workdir, sources_dir, *args, bbox=None):
    env = {**os.environ, "SOURCES_DIR": sources_dir}
    env.pop("BBOX", None)  # scope is under test — never inherit the shell's window
    if bbox:
        env["BBOX"] = bbox
    return subprocess.run(
        ["uv", "run", "snakemake", "-s", SNAKEFILE, "--config", f"workdir={workdir}", *args],
        cwd=PIPE, env=env, capture_output=True, text=True)


def job_counts(out):
    """Snakemake's dry-run job table -> {rule: count} (the scheduled jobs only, not the
    'missing provenance' advisory lines, which don't match ``rule   N``)."""
    counts = {}
    for line in out.splitlines():
        m = re.match(r"^([a-z_]+)\s+(\d+)\s*$", line.strip())
        if m and m.group(1) not in ("total", "job", "count"):
            counts[m.group(1)] = int(m.group(2))
    return counts


def store_tree(store):
    out = {}
    for root, _dirs, files in os.walk(store):
        for f in files:
            p = os.path.join(root, f)
            out[p] = os.stat(p).st_mtime_ns
    return out


def registration(store, sources_dir):
    """A synthetic source registered through `cover`'s inputs (bounds + catalog) plus masks, so
    the checkpoint is SCHEDULABLE cold and considered up-to-date once the covering is present."""
    os.makedirs(f"{store}/aggregation")
    os.makedirs(f"{store}/source/synth")
    os.makedirs(f"{store}/landmask")
    os.makedirs(f"{sources_dir}/synth")
    with open(f"{sources_dir}/synth/metadata.json", "w") as f:
        f.write('{"name": "Synth", "priority": 1, "max_zoom": 10}')
    with open(f"{store}/source/synth/bounds.csv", "w") as f:
        f.write("x\n")
    with open(f"{store}/source/synth/catalog.json", "w") as f:
        f.write('{"properties": {}}')
    for m in ("land.fgb", "water.fgb"):
        open(f"{store}/landmask/{m}", "w").close()


def covering(store):
    """The covering the checkpoint would write — created LAST so it is newer than its inputs
    (cover then reads as up-to-date, no rerun)."""
    for s in STEMS:
        with open(f"{store}/aggregation/{s}-aggregation.csv", "w") as f:
            f.write("source,filename,maxzoom\nsynth,synth_0.tif,10\n")
    with open(f"{store}/aggregation/covering.txt", "w") as f:
        f.write("".join(s + "\n" for s in STEMS))


def main():
    # ── COLD: no covering -> the checkpoint is scheduled, the DAG is incomplete past it ──
    d = tempfile.mkdtemp()
    try:
        store = os.path.join(d, "store")
        sources_dir = os.path.join(d, "sources")
        registration(store, sources_dir)  # no covering yet

        before = store_tree(store)
        p = snakemake(d, sources_dir, "-n", "mosaic")
        assert p.returncode == 0, p.stderr
        out = p.stdout
        counts = job_counts(out)
        assert counts.get("cover") == 1, f"cold store must schedule the cover checkpoint: {counts}"
        assert "mosaic_tile" not in counts, \
            f"per-stem jobs are unknown until the checkpoint runs: {counts}"
        assert "checkpoint" in out.lower(), \
            f"cold dry run must flag the DAG as incomplete-until-checkpoint:\n{out}"
        assert store_tree(store) == before, "a dry run must not touch the store"
    finally:
        shutil.rmtree(d, ignore_errors=True)

    # ── WARM: covering present + up to date -> re-evaluate fully past the checkpoint ──
    d = tempfile.mkdtemp()
    try:
        store = os.path.join(d, "store")
        sources_dir = os.path.join(d, "sources")
        registration(store, sources_dir)
        covering(store)

        before = store_tree(store)
        p = snakemake(d, sources_dir, "-n", "mosaic")
        assert p.returncode == 0, p.stderr
        counts = job_counts(p.stdout)
        assert counts.get("mosaic_tile") == 2, f"expected 2 mosaic_tile jobs: {counts}\n{p.stdout}"
        assert counts.get("mosaic_index") == 1, f"expected 1 mosaic_index job: {counts}"
        assert counts.get("cover", 0) == 0, f"a warm covering must NOT reschedule cover: {counts}"
        for stage1 in ("fetch_asset", "prep_source", "mirror_source", "catalog_item",
                       "fetch_catalog", "landmask", "watermask", "coverage"):
            assert counts.get(stage1, 0) == 0, f"stage-1 rule {stage1} scheduled: {counts}"
        assert store_tree(store) == before, "a dry run must not touch the store"

        # BBOX scopes the stem set read-side (covering is the full inventory, no cover rerun):
        # a window over tile 8/75/96 only schedules one merge.
        p = snakemake(d, sources_dir, "-n", "mosaic", bbox="-74.3,40.5,-73.5,41.0")
        assert p.returncode == 0, p.stderr
        assert job_counts(p.stdout).get("mosaic_tile") == 1, f"bbox must scope to 1 tile:\n{p.stdout}"

        # a window over open ocean refuses in the input function, before any job runs.
        p = snakemake(d, sources_dir, "-n", "mosaic", bbox="10.0,-40.0,11.0,-39.0")
        assert p.returncode != 0 and "no tiles in BBOX" in p.stdout + p.stderr, \
            f"open-ocean bbox must refuse:\n{p.stdout}\n{p.stderr}"

        # stage_build (build/<sha>/ publish) resolves to exactly one job over the bundles.
        p = snakemake(d, sources_dir, "-n", "stage_build")
        assert p.returncode == 0, p.stderr
        assert job_counts(p.stdout).get("stage_build") == 1, \
            f"expected 1 stage_build job:\n{p.stdout}"
        assert store_tree(store) == before, "a dry run must not touch the store"
    finally:
        shutil.rmtree(d, ignore_errors=True)

    print("test_build.py ok")


if __name__ == "__main__":
    main()
