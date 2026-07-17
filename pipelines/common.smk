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
