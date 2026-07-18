"""Parse/dry-run self-check for build.smk — the planet/preview invocation.

Asserts the two-invocation seam offline, against a synthetic store:

  - no covering -> a clear refusal naming `snakemake catalogs` (never a silent empty build);
  - with a covering -> the dry run schedules exactly one mosaic_tile per stem + the index,
    and reaches no stage-1 rule (fetch/prep/catalog/mask rules don't exist in this graph);
  - the dry run is PURE: it writes nothing under store/.

Run from pipelines/:  uv run python test_build.py
"""

import os
import subprocess
import sys
import tempfile
import re
import shutil

PIPE = os.path.dirname(os.path.abspath(__file__))
STEMS = ["8-75-96-10", "8-76-96-10"]


def snakemake(workdir, sources_dir, *args, bbox=None):
    env = {**os.environ, "SOURCES_DIR": sources_dir}
    env.pop("BBOX", None)  # scope is under test — never inherit the shell's window
    if bbox:
        env["BBOX"] = bbox
    return subprocess.run(
        ["uv", "run", "snakemake", "-s", os.path.join(PIPE, "build.smk"),
         "--config", f"workdir={workdir}", *args],
        cwd=PIPE, env=env, capture_output=True, text=True)


def store_tree(store):
    out = {}
    for root, _dirs, files in os.walk(store):
        for f in files:
            p = os.path.join(root, f)
            out[p] = os.stat(p).st_mtime_ns
    return out


def main():
    d = tempfile.mkdtemp()
    try:
        store = os.path.join(d, "store")
        sources_dir = os.path.join(d, "sources")

        # no covering -> refuse, pointing at the catalogs invocation
        p = snakemake(d, sources_dir, "-n", "mosaic")
        assert p.returncode != 0, "must refuse to parse without a covering"
        assert "snakemake catalogs" in p.stderr + p.stdout, (p.stdout, p.stderr)

        # a two-tile covering over one synthetic source, catalogs + masks as leaf files
        os.makedirs(f"{store}/aggregation")
        os.makedirs(f"{store}/source/synth")
        os.makedirs(f"{store}/landmask")
        os.makedirs(f"{sources_dir}/synth")
        with open(f"{sources_dir}/synth/metadata.json", "w") as f:
            f.write('{"name": "Synth", "priority": 1, "max_zoom": 10}')
        with open(f"{store}/source/synth/catalog.json", "w") as f:
            f.write('{"properties": {}}')
        for m in ("land.fgb", "water.fgb"):
            open(f"{store}/landmask/{m}", "w").close()
        with open(f"{store}/aggregation/covering.txt", "w") as f:
            f.write("".join(s + "\n" for s in STEMS))
        for s in STEMS:
            with open(f"{store}/aggregation/{s}-aggregation.csv", "w") as f:
                f.write("source,filename,maxzoom\nsynth,synth_0.tif,10\n")

        before = store_tree(store)
        p = snakemake(d, sources_dir, "-n", "mosaic")
        assert p.returncode == 0, p.stderr
        out = p.stdout
        assert re.search(r"mosaic_tile\s+2", out), f"expected 2 mosaic_tile jobs:\n{out}"
        assert re.search(r"mosaic_index\s+1", out), f"expected 1 mosaic_index job:\n{out}"
        for stage1 in ("fetch_asset", "prep_source", "mirror_source", "catalog_item",
                       "landmask", "watermask", "cover"):
            # scheduled rules appear as job-table rows / "rule <name>:" blocks — input paths
            # containing e.g. "landmask" are fine, a scheduled stage-1 JOB is not
            assert not re.search(rf"(?m)^(rule )?{stage1}\s*[:\d]", out), \
                f"stage-1 rule {stage1} reachable:\n{out}"
        assert store_tree(store) == before, "a dry run must not touch the store"

        # BBOX scopes STEMS at parse — the covering is the full inventory, the window is
        # the build's. A bbox over tile 8/75/96 only must schedule exactly one merge; a
        # window over open ocean refuses at parse instead of silently building nothing.
        p = snakemake(d, sources_dir, "-n", "mosaic", bbox="-74.3,40.5,-73.5,41.0")
        assert p.returncode == 0, p.stderr
        assert re.search(r"mosaic_tile\s+1", p.stdout), f"bbox must scope to 1 tile:\n{p.stdout}"
        p = snakemake(d, sources_dir, "-n", "mosaic", bbox="10.0,-40.0,11.0,-39.0")
        assert p.returncode != 0 and "no tiles in BBOX" in p.stderr + p.stdout

        print("test_build.py ok")
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    main()
