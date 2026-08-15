# Parse-time support for the repo-root Snakefile: paths, shell prefix, input helpers.

import os
import sys
from pathlib import Path

SCRIPTS = Path(workflow.snakefile).parent.resolve()  # inside an include, snakefile = THIS file
sys.path.insert(0, str(SCRIPTS))
os.environ.setdefault("SOURCES_DIR", str(SCRIPTS.parent / "sources"))
import config as pipeline_config

import snakemake_patches
snakemake_patches.apply()  # snakemake 9.23.1 benchmark timers leak monitor threads → scheduler stall

SOURCES_DIR = Path(os.environ["SOURCES_DIR"])
PY = f"uv run --project {SCRIPTS.parent} python {SCRIPTS}"

# store/ paths are cwd-relative; the box relocates with --config workdir=<volume>
workdir: config.get("workdir", str(SCRIPTS))

# Per-run scratch for logs/benchmarks; defaults to ephemeral tmp/ (local) but the box points
# it at local disk (tmp=/app/tmp) so nothing crosses the network volume or persists.
TMP = config.get("tmp", "tmp")


def pat(ids):
    """Wildcard constraint matching exactly these source ids (or nothing)."""
    return "|".join(ids) or "^\\b$"


def raw_assets(wc):
    """One raw/<item-hash> input per enumerated item — gated on the enumerate checkpoint so
    the fetch jobs are known only once items.txt exists."""
    items_txt = checkpoints.enumerate.get(source=wc.source).output[0]
    with open(items_txt) as f:
        urls = [l.strip() for l in f if l.strip()]
    return [f"store/source/{wc.source}/raw/{pipeline_config.item_hash(u)}" for u in urls]


def offset_surface(wc):
    """The chart-datum reference this source's prep subtracts, if metadata declares one."""
    name = pipeline_config.load_metadata(wc.source).get("offset_surface")
    return [f"store/datum/{name}.tif"] if name else []


def recipe_files(wc):
    """Every file under sources/<id>/ — the exact set source_catalog.recipe_hash hashes."""
    root = SOURCES_DIR / wc.source
    return sorted(str(p) for p in root.rglob("*") if p.is_file())


def source_priority(wc, input=None, attempt=None):
    """Longest-first: real raw bytes (MB) while they are on disk, else a count+zoom+priority
    guess. Set on prep only — priorities propagate upstream, so fetches inherit it. Raws are
    temp(), so the guess is what an already-prepped source scores at plan time; the measured
    branch still fires mid-run, once its fetches land and before its prep is scheduled."""
    from glob import glob
    raws = glob(f"store/source/{wc.source}/raw/*")
    if raws:
        return int(sum(os.path.getsize(r) for r in raws) / 1e6)
    meta = pipeline_config.load_metadata(wc.source)
    return (len(pipeline_config.file_list(wc.source))
            + 10 * (meta.get("max_zoom") or 0) + 100 * meta.get("priority", 0))
