# Parse-time support for the repo-root Snakefile: paths, shell prefix, input helpers.

import os
import sys
from pathlib import Path

SCRIPTS = Path(workflow.snakefile).parent.resolve()  # inside an include, snakefile = THIS file
sys.path.insert(0, str(SCRIPTS))
os.environ.setdefault("SOURCES_DIR", str(SCRIPTS.parent / "sources"))
import config as pipeline_config

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


def recipe_files(wc):
    """Every file under sources/<id>/ — the exact set source_catalog.recipe_hash hashes."""
    root = SOURCES_DIR / wc.source
    return sorted(str(p) for p in root.rglob("*") if p.is_file())


def source_priority(wc, input=None, attempt=None):
    """Longest-first: real raw bytes (MB) once fetched, else a count+zoom+priority guess.
    Set on prep only — priorities propagate upstream, so fetches inherit it."""
    from glob import glob
    raws = glob(f"store/source/{wc.source}/raw/*")
    if raws:
        return int(sum(os.path.getsize(r) for r in raws) / 1e6)
    meta = pipeline_config.load_metadata(wc.source)
    return (len(pipeline_config.file_list(wc.source))
            + 10 * (meta.get("max_zoom") or 0) + 100 * meta.get("priority", 0))
