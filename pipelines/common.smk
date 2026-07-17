# Parse-time support for the repo-root Snakefile: path anchors, the shell prefix,
# and the input/constraint helpers. Included first; the Snakefile itself holds only
# the converted-source lists and the rules.

import os
import sys
from pathlib import Path

# workflow.snakefile is the file being parsed — inside an include that's THIS
# file, and common.smk lives in pipelines/, so its parent is the scripts dir.
SCRIPTS = Path(workflow.snakefile).parent.resolve()
sys.path.insert(0, str(SCRIPTS))
os.environ.setdefault("SOURCES_DIR", str(SCRIPTS.parent / "sources"))
import config as pipeline_config

SOURCES_DIR = Path(os.environ["SOURCES_DIR"])
PY = f"uv run --project {SCRIPTS.parent} python {SCRIPTS}"

# Jobs execute in pipelines/ (store/ is cwd-relative there, like every pipeline
# module) and .snakemake/ lives beside store/ — on the build box both relocate to
# the persistent volume with --config workdir=<volume>/snakemake-store.
workdir: config.get("workdir", str(SCRIPTS))


def pat(ids):
    """Wildcard constraint matching exactly these source ids (or nothing)."""
    return "|".join(ids) or "^\\b$"


def code(*names):
    """A rule's code inputs: the named modules + the shared config/utils, absolute."""
    return [str(SCRIPTS / n) for n in (*names, "config.py", "utils.py")]


def raw_assets(wc):
    """One store/source/<id>/raw/<index> input per file_list.txt entry — every prepped
    source acquires bytes the same way; stage() routes each raw by content."""
    return [f"store/source/{wc.source}/raw/{i}"
            for i in range(len(pipeline_config.file_list(wc.source)))]


def recipe_files(wc):
    """Every file under sources/<id>/ (recursive, sorted) — exactly the set
    source_catalog.recipe_hash hashes, so any recipe edit (Justfile, harvest.py,
    file_list.txt, metadata.json) restamps the catalog's seascape:recipe_hash."""
    root = SOURCES_DIR / wc.source
    return sorted(str(p) for p in root.rglob("*") if p.is_file())


def source_priority(wc, input=None, attempt=None):
    """Heavier sources first — the scheduler knows nothing about duration, so a big
    source scheduled last runs alone at the end (longest-first shortens the makespan).
    Weight = real raw bytes (MB) once the source has ever been fetched (a stat is
    parse-pure, and actual size beats any proxy); a never-fetched source falls back to
    file_list length + max_zoom + chart priority — finer/prioritized sources plausibly
    carry more data, which catches the one-huge-file cases (infomar) that count alone
    misses. Set on prep only: priorities propagate upstream through the DAG, so the
    source's fetch jobs inherit it."""
    from glob import glob
    raws = glob(f"store/source/{wc.source}/raw/*")
    if raws:
        return int(sum(os.path.getsize(r) for r in raws) / 1e6)
    meta = pipeline_config.load_metadata(wc.source)
    return (len(pipeline_config.file_list(wc.source))
            + 10 * (meta.get("max_zoom") or 0) + 100 * meta.get("priority", 0))
