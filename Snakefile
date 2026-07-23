# The Snakemake build — ONE DAG, ONE entry (this file). See docs/plans/2026-07-14-snakemake-build.md.
# Per-source knobs (crs/nodata/negate/datum_offset_m/clamp_positive/unpack)
# live in sources/<id>/metadata.json. Run from the repo root:
#   uv run snakemake sources [--config source=<id>] [-n]
#   uv run snakemake catalogs                     # + masks, covering, coverage
#   uv run snakemake check --config source=<id>
#   uv run snakemake publish [--config source=<id>]   # per-source R2 push
#   uv run snakemake bundles [publish_mosaic]     # stage 2/3: mosaic → forks → bundles
#   ./docker.sh snakemake sources                 # same, in the toolchain container
# Pipeline-code edits don't invalidate outputs; force explicitly (-R prep_source / -F).
#
# `cover` is a CHECKPOINT: the covering (the stem inventory the whole build scopes from) is a
# runtime product, so stage 2/3 targets can't enumerate their per-stem jobs at parse. Their
# input functions derive STEMS through the checkpoint (see pipelines/build.smk), so ONE
# invocation walks sources → covering → mosaic → forks → bundles with no second `-s` entry.

include: "pipelines/common.smk"

ALL_SOURCES = pipeline_config.sources()
# raw: true ⇒ register remote objects 1:1 (header reads + object mirror); everything else preps locally
RAW = [s for s in ALL_SOURCES if pipeline_config.load_metadata(s).get("raw")]
PROCESSED = [s for s in ALL_SOURCES if s not in RAW]
# listed ⇒ the enumerate checkpoint lists a bucket/urllist (vs echoing a committed file_list);
# these re-enumerate on the weekly cron. Orthogonal to processed/raw: nz_coastal is listed
# AND processed, the raw collections are listed AND raw, the rest are static.
LISTED = [s for s in ALL_SOURCES if "filter" in pipeline_config.load_metadata(s)]

# ── streaming ("source access resolves per source, local wins") ──────────────────────
# --config stream=1 (laptop preview): a source with no local prep evidence
# (store/source/<id>/raw/) fetches its published catalog.json (the whole registration) from
# R2 instead of prepping, and its COGs stream via SOURCE_VSI_BASE.
# A locally-prepped source keeps its prep rules — local wins. Off (the box, the
# sources workflow): STREAMED is empty and nothing changes.
STREAM_BASE = config.get("stream_base", "https://data.openwaters.io/bathymetry/source")
_wd = Path(config.get("workdir", str(SCRIPTS)))
STREAMED = ([s for s in ALL_SOURCES if not (_wd / "store/source" / s / "raw").is_dir()]
            if config.get("stream") else [])
LOCAL_PROCESSED = [s for s in PROCESSED if s not in STREAMED]
LOCAL_RAW = [s for s in RAW if s not in STREAMED]
LOCAL_LISTED = [s for s in LISTED if s not in STREAMED]

ONLY = config.get("source")
if ONLY and ONLY not in PROCESSED + RAW:
    raise WorkflowError(f"unknown source {ONLY!r} — known: {PROCESSED + RAW}")
TARGETS = [ONLY] if ONLY else PROCESSED + RAW

include: "pipelines/publish.smk"  # needs the source lists above


rule sources:
    input:
        expand("store/source/{source}/catalog.json", source=TARGETS),
        expand("store/polygon/{source}.gpkg", source=[s for s in TARGETS if s in PROCESSED]),


# Enumerate every source's fetchable items through ONE code path (source_enumerate). A
# CHECKPOINT: items.txt is a runtime product (a listed source's items come from a live bucket
# listing), so the per-item fetch jobs can't be enumerated at parse — the DAG re-evaluates
# once items.txt lands (raw_assets reads it via checkpoints.enumerate.get). Static sources
# echo their committed file_list; listed sources list + filter + dedupe. write-if-changed, so
# an unchanged enumeration doesn't cascade.
checkpoint enumerate:
    input:
        str(SOURCES_DIR / "{source}/file_list.txt"),
        metadata=str(SOURCES_DIR / "{source}/metadata.json"),
    output:
        "store/source/{source}/items.txt"
    wildcard_constraints:
        source=pat(LOCAL_PROCESSED + LOCAL_RAW)
    log:
        f"{TMP}/logs/enumerate/{{source}}.log"
    shell:
        "{PY}/source_enumerate.py {wildcards.source} 2> {log}"


# One download per enumerated item; the raw name is the URL hash (source_fetch), so inserting
# an item can't re-key the others and a warm legacy raw/<index> self-migrates without refetch.
# items.txt is `ancient` — it must exist (built by enumerate), but a re-enumeration must not
# invalidate raws whose URL is unchanged.
rule fetch_item:
    input:
        ancient("store/source/{source}/items.txt")
    output:
        "store/source/{source}/raw/{hash}"
    wildcard_constraints:
        source=pat(LOCAL_PROCESSED), hash=r"[0-9a-f]{16}"
    retries: 2
    benchmark:
        f"{TMP}/bench/fetch/{{source}}-{{hash}}.tsv"
    # stderr per job for forensics (a failed job's diagnostics stay isolated in its own log);
    # stdout keeps flowing to the run log so monitors and Actions heartbeats parse progress.
    log:
        f"{TMP}/logs/fetch/{{source}}-{{hash}}.log"
    shell:
        "{PY}/source_fetch.py {wildcards.source} {wildcards.hash} 2> {log}"


# Stage → datum → normalize → register, one job per source. source_catalog scans the
# normalized COGs into the item's seascape:files, so catalog.json is the SINGLE registration
# artifact (the separate bounds/catalog steps collapsed into this one).
rule prep_source:
    input:
        raw_assets,
        metadata=str(SOURCES_DIR / "{source}/metadata.json"),
        recipe=recipe_files,  # everything --hash-recipe hashes, so any recipe edit restamps
    output:
        # staged tif names aren't knowable at parse; catalog.json is the declared artifact
        "store/source/{source}/catalog.json"
    wildcard_constraints:
        source=pat(LOCAL_PROCESSED)
    priority: source_priority
    resources:
        mem_gb=8  # asc-mosaic / archive-extract jobs hold whole rasters in flight
    benchmark:
        f"{TMP}/bench/prep/{{source}}.tsv"
    log:
        f"{TMP}/logs/prep/{{source}}.log"
    shell:
        "( {PY}/source_prep.py {wildcards.source} && "
        "{PY}/source_catalog.py {wildcards.source} --hash-recipe ) 2> {log}"


# The weekly forced source refresh — run as its OWN invocation before catalogs/publish:
# a forced producer schedules all dependents at plan time, but across an invocation
# boundary the engine's checksums cure unchanged registrations, so the main invocation
# cascades only on real upstream drift. Scoped to the LISTED sources: they are the ones whose
# enumeration can drift week to week (a bucket/urllist listing); static sources change only
# when their committed file_list.txt does. The workflow runs `refresh -R enumerate`, so a
# re-listing that produces an unchanged items.txt cures downstream at the boundary.
# The objects push rides here too: mirror_objects is on the mirror.txt branch, NOT the
# catalog cascade the boundary suppresses, so pulling it into this invocation overlaps each
# source's ~190 GB copy with the next source's re-listing instead of stranding it behind the
# barrier. Objects stay additive and land before catalog.json, so cross-boundary atomicity holds.
rule refresh:
    input:
        expand("store/source/{source}/catalog.json", source=LOCAL_LISTED),
        expand("store/meta/publish/{source}.objects",
               source=[s for s in LOCAL_LISTED if s in RAW]),


# Raw collections register objects/<key> rows off the public bucket, reading the
# enumerate checkpoint's items.txt. source_mirror header-reads (with carry-forward) into the
# .rows.csv handoff, then source_catalog folds it into catalog.json — the SINGLE registration
# artifact. Re-listing on cadence is the enumerate checkpoint's job (the weekly workflow runs
# `refresh -R enumerate`); this step just materializes the registration from items.txt.
rule register:
    input:
        "store/source/{source}/items.txt",
        recipe=recipe_files,  # any recipe edit restamps the item's hash
    output:
        # `update` keeps the previous catalog.json in place so source_mirror can carry forward
        # its seascape:files without a full re-read. mirror.txt/mirror-bucket.txt keep their
        # names — they're the object-copy store contract, out of scope to rename.
        catalog=update("store/source/{source}/catalog.json"),
        mirror="store/source/{source}/mirror.txt",
        bucket="store/source/{source}/mirror-bucket.txt",
    wildcard_constraints:
        source=pat(LOCAL_RAW)
    # Priority BANDS, separated by orders of magnitude so no byte-weighted prep (raw MB,
    # realistically <1,000,000) can cross into a higher band, and so the values dominate
    # the scheduler's packing objective (which otherwise favors count-maximizing selection of
    # light jobs): masks 10M > registrations 5M > preps (raw MB).
    priority: 5_000_000  # long serial job with thousands of network header reads; start early
    retries: 2
    resources:
        mem_gb=2  # header reads + list bookkeeping, no raster in memory
    benchmark:
        f"{TMP}/bench/register/{{source}}.tsv"
    log:
        f"{TMP}/logs/register/{{source}}.log"
    shell:
        "( {PY}/source_mirror.py {wildcards.source} && "
        "{PY}/source_catalog.py {wildcards.source} --hash-recipe ) 2> {log}"


rule polygon:
    input:
        "store/source/{source}/catalog.json",
    output:
        "store/polygon/{source}.gpkg"
    wildcard_constraints:
        source=pat(LOCAL_PROCESSED)
    threads: 4
    log:
        f"{TMP}/logs/polygon/{{source}}.log"
    shell:
        "{PY}/source_polygonize.py {wildcards.source} {threads} 2> {log}"


# The streamed half of the catalogs invocation (--config stream=1): fetch a not-locally-
# prepped source's published catalog.json — the whole registration. Absent-only (engine:
# outputs exist ⇒ done); -R fetch_catalog refreshes. tmp+mv so a 404 never leaves a truncated
# file. Fallback: a published item from before phase 4 has no seascape:files, so ALSO fetch
# the sibling bounds.csv that config.source_files falls back to (drop when every source has
# re-registered — mirrors the config.source_files fallback, self-checked there).
rule fetch_catalog:
    output:
        catalog="store/source/{source}/catalog.json",
    wildcard_constraints:
        source=pat(STREAMED)
    retries: 2
    log:
        f"{TMP}/logs/fetch_catalog/{{source}}.log"
    shell:
        "( set -e; c={output.catalog}; b=store/source/{wildcards.source}/bounds.csv; "
        "curl -fsS {STREAM_BASE}/{wildcards.source}/catalog.json -o $c.tmp && mv $c.tmp $c; "
        "if ! grep -q '\"seascape:files\"' $c; then "
        "  curl -fsS {STREAM_BASE}/{wildcards.source}/bounds.csv -o $b.tmp && mv $b.tmp $b; "
        "fi ) 2> {log} || {{ rm -f {output.catalog}.tmp store/source/{wildcards.source}/bounds.csv.tmp; exit 1; }}"


# Masks rebuild only when forced (-R landmask): pinned snapshot/release ⇒ no data drift.
rule landmask:
    output:
        "store/landmask/land.fgb"
    priority: 10_000_000  # top band (see register): long single-threaded, ready at t=0 — overlap, don't tail
    retries: 2
    resources:
        mem_gb=4
    benchmark:
        f"{TMP}/bench/landmask.tsv"
    log:
        f"{TMP}/logs/landmask.log"
    shell:
        "{PY}/landmask.py prep 2> {log}"


rule watermask:
    output:
        "store/landmask/water.fgb"
    priority: 10_000_000  # see landmask
    retries: 2
    threads: 8  # the planet read is tiled + parallel (landmask._water_tile); IO-bound S3 reads
    resources:
        mem_gb=8  # the planet Overture-water reproject; refine from the benchmark
    benchmark:
        f"{TMP}/bench/watermask.tsv"
    log:
        f"{TMP}/logs/watermask.log"
    shell:
        "{PY}/landmask.py prep-water {threads} 2> {log}"


# The covering — the seam between stage 1 and stage 2/3, as a CHECKPOINT: it writes the stem
# inventory the build scopes from, so the engine re-evaluates the DAG (per-stem mosaic/fork/
# terrain jobs) AFTER it runs. BBOX rides as a param so a changed window reruns it (write-if-
# changed prunes in-window stale tiles, keeps out-of-window). Body/inputs/outputs are the plain
# rule's — only the `checkpoint` keyword differs.
checkpoint cover:
    input:
        expand("store/source/{source}/catalog.json", source=PROCESSED + RAW),
    output:
        "store/aggregation/covering.txt"
    params:
        bbox=os.environ.get("BBOX", "")
    log:
        f"{TMP}/logs/cover.log"
    shell:
        "{PY}/aggregation_covering.py --stable 2> {log}"


# Source-coverage provenance tileset.
rule coverage:
    input:
        expand("store/polygon/{source}.gpkg", source=PROCESSED),
        expand("store/source/{source}/catalog.json", source=PROCESSED + RAW),
        "store/aggregation/covering.txt",  # sequencing only; coverage reads footprints + catalogs
    output:
        "store/bundle/coverage.pmtiles"
    log:
        f"{TMP}/logs/coverage.log"
    shell:
        "{PY}/contour_run.py coverage 2> {log}"


# The complete stage-1 product set (what the stage-2/3 invocation parses from).
rule catalogs:
    input:
        expand("store/source/{source}/catalog.json", source=PROCESSED + RAW),
        "store/landmask/land.fgb",
        "store/landmask/water.fgb",
        "store/aggregation/covering.txt",
        "store/bundle/coverage.pmtiles",


# Validate one source's contract (requires --config source=<id>).
def check_input(wc):
    if not ONLY:
        raise WorkflowError("check requires --config source=<id>")
    return f"store/source/{ONLY}/catalog.json"


rule check:
    input:
        check_input,
    params:
        source=lambda wc: ONLY,
    log:
        f"{TMP}/logs/check.log"
    shell:
        "{PY}/source_check.py {params.source} 2> {log}"


# Stage 2/3 (mosaic → cartographic forks → bundles → publish), gated on the `cover` checkpoint
# above. Kept in its own file for grouping — but it is INCLUDED here, so there is one entry.
include: "pipelines/build.smk"
