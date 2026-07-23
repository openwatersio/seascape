# The Snakemake build — ONE DAG, ONE entry (this file). See docs/plans/2026-07-14-snakemake-build.md.
# Per-source knobs (crs/nodata/negate/datum_offset_m/clamp_positive/archive_members)
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
# volatile: true ⇒ mirrored registration (remote objects); everything else preps locally
MIRRORED = [s for s in ALL_SOURCES if pipeline_config.load_metadata(s).get("volatile")]
PREPPED = [s for s in ALL_SOURCES if s not in MIRRORED]

# ── streaming ("source access resolves per source, local wins") ──────────────────────
# --config stream=1 (laptop preview): a source with no local prep evidence
# (store/source/<id>/raw/) fetches its published registration (bounds.csv +
# catalog.json) from R2 instead of prepping, and its COGs stream via SOURCE_VSI_BASE.
# A locally-prepped source keeps its prep rules — local wins. Off (the box, the
# sources workflow): STREAMED is empty and nothing changes.
STREAM_BASE = config.get("stream_base", "https://data.openwaters.io/bathymetry/source")
_wd = Path(config.get("workdir", str(SCRIPTS)))
STREAMED = ([s for s in ALL_SOURCES if not (_wd / "store/source" / s / "raw").is_dir()]
            if config.get("stream") else [])
LOCAL_PREPPED = [s for s in PREPPED if s not in STREAMED]
LOCAL_MIRRORED = [s for s in MIRRORED if s not in STREAMED]

ONLY = config.get("source")
if ONLY and ONLY not in PREPPED + MIRRORED:
    raise WorkflowError(f"unknown source {ONLY!r} — known: {PREPPED + MIRRORED}")
TARGETS = [ONLY] if ONLY else PREPPED + MIRRORED

include: "pipelines/publish.smk"  # needs the source lists above


rule sources:
    input:
        expand("store/source/{source}/catalog.json", source=TARGETS),
        expand("store/polygon/{source}.gpkg", source=[s for s in TARGETS if s in PREPPED]),


# One download per file_list.txt entry; raw names are the bare index (bytes get sniffed).
rule fetch_asset:
    output:
        "store/source/{source}/raw/{index}"
    params:
        # the URL as a param (not file_list.txt as input): editing one line refetches one index
        url=lambda wc: pipeline_config.file_list(wc.source)[int(wc.index)],
    wildcard_constraints:
        source=pat(PREPPED), index=r"\d+"
    retries: 2
    benchmark:
        f"{TMP}/bench/fetch/{{source}}-{{index}}.tsv"
    # stderr per job for forensics (a failed job's diagnostics stay isolated in its own log);
    # stdout keeps flowing to the run log so monitors and Actions heartbeats parse progress.
    log:
        f"{TMP}/logs/fetch/{{source}}-{{index}}.log"
    shell:
        "{PY}/source_fetch.py {wildcards.source} {wildcards.index} 2> {log}"


# Stage → datum → normalize → bounds, one job per source.
rule prep_source:
    input:
        raw_assets,
        metadata=str(SOURCES_DIR / "{source}/metadata.json"),
    output:
        # staged tif names aren't knowable at parse; bounds.csv is the declared artifact
        "store/source/{source}/bounds.csv"
    wildcard_constraints:
        source=pat(LOCAL_PREPPED)
    priority: source_priority
    resources:
        mem_gb=8  # asc-mosaic / archive-extract jobs hold whole rasters in flight
    benchmark:
        f"{TMP}/bench/prep/{{source}}.tsv"
    log:
        f"{TMP}/logs/prep/{{source}}.log"
    shell:
        "( {PY}/source_prep.py {wildcards.source} && {PY}/source_bounds.py {wildcards.source} ) 2> {log}"


# The weekly forced source refresh — run as its OWN invocation before catalogs/publish:
# a forced producer schedules all dependents at plan time, but across an invocation
# boundary the engine's checksums cure unchanged registrations, so the main invocation
# cascades only on real upstream drift.
# The objects push rides here too: mirror_objects is on the mirror.txt branch, NOT the
# catalog cascade the boundary suppresses, so pulling it into this invocation overlaps each
# source's ~190 GB copy with the next source's re-listing instead of stranding it behind the
# barrier. Objects stay additive and land before catalog.json, so cross-boundary atomicity holds.
rule refresh:
    input:
        expand("store/source/{source}/bounds.csv", source=MIRRORED),
        expand("store/meta/publish/{source}.objects", source=MIRRORED),


# Volatile mirrored collections register objects/<key> rows off the public bucket.
# Re-listing on cadence: the weekly workflow runs `refresh -R mirror_source` separately.
rule mirror_source:
    input:
        str(SOURCES_DIR / "{source}/file_list.txt"),
        metadata=str(SOURCES_DIR / "{source}/metadata.json"),
    output:
        # Use `update` to keep the previous bounds in place.
        bounds=update("store/source/{source}/bounds.csv"),
        mirror="store/source/{source}/mirror.txt",
        bucket="store/source/{source}/mirror-bucket.txt",
    wildcard_constraints:
        source=pat(LOCAL_MIRRORED)
    # Priority BANDS, separated by orders of magnitude so no byte-weighted prep (raw MB,
    # realistically <1,000,000) can cross into a higher band, and so the values dominate
    # the scheduler's packing objective (which otherwise favors count-maximizing selection of
    # light jobs): masks 10M > mirrors 5M > preps (raw MB).
    priority: 5_000_000  # long serial job with thousands of network header reads; start early
    retries: 2
    resources:
        mem_gb=2  # header reads + list bookkeeping, no raster in memory
    benchmark:
        f"{TMP}/bench/mirror/{{source}}.tsv"
    log:
        f"{TMP}/logs/mirror/{{source}}.log"
    shell:
        "{PY}/source_mirror.py {wildcards.source} 2> {log}"


rule polygon:
    input:
        "store/source/{source}/bounds.csv",
    output:
        "store/polygon/{source}.gpkg"
    wildcard_constraints:
        source=pat(LOCAL_PREPPED)
    threads: 4
    log:
        f"{TMP}/logs/polygon/{{source}}.log"
    shell:
        "{PY}/source_polygonize.py {wildcards.source} {threads} 2> {log}"


# catalog.json: the per-source currency signal, last in every source's chain.
rule catalog_item:
    input:
        "store/source/{source}/bounds.csv",
        recipe=recipe_files,  # everything --hash-recipe hashes, so any recipe edit restamps
    output:
        "store/source/{source}/catalog.json"
    wildcard_constraints:
        source=pat(LOCAL_PREPPED + LOCAL_MIRRORED)
    log:
        f"{TMP}/logs/catalog_item/{{source}}.log"
    shell:
        "{PY}/source_catalog.py {wildcards.source} --hash-recipe 2> {log}"


# The streamed half of the catalogs invocation (--config stream=1): fetch a not-locally-
# prepped source's published registration. Absent-only (engine: outputs exist ⇒ done);
# -R fetch_catalog refreshes. tmp+mv so a 404 never leaves a truncated file.
rule fetch_catalog:
    output:
        bounds="store/source/{source}/bounds.csv",
        catalog="store/source/{source}/catalog.json",
    wildcard_constraints:
        source=pat(STREAMED)
    retries: 2
    log:
        f"{TMP}/logs/fetch_catalog/{{source}}.log"
    shell:
        "((curl -fsS {STREAM_BASE}/{wildcards.source}/bounds.csv -o {output.bounds}.tmp && "
        "mv {output.bounds}.tmp {output.bounds} && "
        "curl -fsS {STREAM_BASE}/{wildcards.source}/catalog.json -o {output.catalog}.tmp && "
        "mv {output.catalog}.tmp {output.catalog}) || "
        "{{ rm -f {output.bounds}.tmp {output.catalog}.tmp; exit 1; }}) 2> {log}"


# Masks rebuild only when forced (-R landmask): pinned snapshot/release ⇒ no data drift.
rule landmask:
    output:
        "store/landmask/land.fgb"
    priority: 10_000_000  # top band (see mirror_source): long single-threaded, ready at t=0 — overlap, don't tail
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
        expand("store/source/{source}/bounds.csv", source=PREPPED + MIRRORED),
        expand("store/source/{source}/catalog.json", source=PREPPED + MIRRORED),
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
        expand("store/polygon/{source}.gpkg", source=PREPPED),
        expand("store/source/{source}/bounds.csv", source=PREPPED + MIRRORED),
        expand("store/source/{source}/catalog.json", source=PREPPED + MIRRORED),
        "store/aggregation/covering.txt",  # sequencing only; coverage reads footprints + bounds
    output:
        "store/bundle/coverage.pmtiles"
    log:
        f"{TMP}/logs/coverage.log"
    shell:
        "{PY}/contour_run.py coverage 2> {log}"


# The complete stage-1 product set (what the stage-2/3 invocation parses from).
rule catalogs:
    input:
        expand("store/source/{source}/catalog.json", source=PREPPED + MIRRORED),
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
