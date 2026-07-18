# Stage 1 (sources) of the Snakemake build — see docs/plans/2026-07-14-snakemake-build.md.
# Per-source knobs (crs/nodata/negate/datum_offset_m/clamp_positive/archive_members)
# live in sources/<id>/metadata.json. Run from the repo root:
#   uv run snakemake sources [--config source=<id>] [-n]
#   uv run snakemake catalogs                     # + masks, covering, coverage
#   uv run snakemake check --config source=<id>
#   uv run snakemake publish [--config source=<id>]
#   ./docker.sh snakemake sources                 # same, in the toolchain container
# Pipeline-code edits don't invalidate outputs; force explicitly (-R prep_source / -F).

include: "pipelines/common.smk"

ALL_SOURCES = pipeline_config.sources()
# volatile: true ⇒ mirrored registration (remote objects); everything else preps locally
MIRRORED = [s for s in ALL_SOURCES if pipeline_config.load_metadata(s).get("volatile")]
PREPPED = [s for s in ALL_SOURCES if s not in MIRRORED]

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
        "store/bench/fetch/{source}-{index}.tsv"
    shell:
        "{PY}/source_fetch.py {wildcards.source} {wildcards.index}"


# Stage → datum → normalize → bounds, one job per source.
rule prep_source:
    input:
        raw_assets,
        metadata=str(SOURCES_DIR / "{source}/metadata.json"),
    output:
        # staged tif names aren't knowable at parse; bounds.csv is the declared artifact
        "store/source/{source}/bounds.csv"
    wildcard_constraints:
        source=pat(PREPPED)
    priority: source_priority
    resources:
        mem_gb=8  # asc-mosaic / archive-extract jobs hold whole rasters in flight
    benchmark:
        "store/bench/prep/{source}.tsv"
    shell:
        "{PY}/source_prep.py {wildcards.source} && {PY}/source_bounds.py {wildcards.source}"


# Volatile mirrored collections register objects/<key> rows off the public bucket.
# Re-listing on cadence is the caller's job: the weekly workflow passes -R mirror_source.
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
        source=pat(MIRRORED)
    priority: 5000  # long serial job with thousands of network header reads; start early
    retries: 2
    resources:
        mem_gb=2  # header reads + list bookkeeping, no raster in memory
    benchmark:
        "store/bench/mirror/{source}.tsv"
    shell:
        "{PY}/source_mirror.py {wildcards.source}"


rule polygon:
    input:
        "store/source/{source}/bounds.csv",
    output:
        "store/polygon/{source}.gpkg"
    wildcard_constraints:
        source=pat(PREPPED)
    threads: 4
    shell:
        "{PY}/source_polygonize.py {wildcards.source} {threads}"


# catalog.json: the per-source currency signal, last in every source's chain.
rule catalog_item:
    input:
        "store/source/{source}/bounds.csv",
        recipe=recipe_files,  # everything --hash-recipe hashes, so any recipe edit restamps
    output:
        "store/source/{source}/catalog.json"
    wildcard_constraints:
        source=pat(PREPPED + MIRRORED)
    shell:
        "{PY}/source_catalog.py {wildcards.source} --hash-recipe"


# The masks rebuild only on a landmask.py change (pinned snapshot/release ⇒ no data drift).
rule landmask:
    input:
        code=code("landmask.py"),
    output:
        "store/landmask/land.fgb"
    priority: 10000  # long single-threaded jobs, ready at t=0: overlap, don't tail
    retries: 2
    resources:
        mem_gb=4
    benchmark:
        "store/bench/landmask.tsv"
    shell:
        "{PY}/landmask.py prep"


rule watermask:
    input:
        code=code("landmask.py"),
    output:
        "store/landmask/water.fgb"
    priority: 10000  # see landmask
    retries: 2
    resources:
        mem_gb=8  # the planet Overture-water reproject; refine from the benchmark
    benchmark:
        "store/bench/watermask.tsv"
    shell:
        "{PY}/landmask.py prep-water"


# The covering — stage 1's downstream interface.
rule cover:
    input:
        expand("store/source/{source}/bounds.csv", source=PREPPED + MIRRORED),
        expand("store/source/{source}/catalog.json", source=PREPPED + MIRRORED),
        code=code("aggregation_covering.py"),
    output:
        "store/aggregation/covering.txt"
    shell:
        "{PY}/aggregation_covering.py --stable"


# Source-coverage provenance tileset.
rule coverage:
    input:
        expand("store/polygon/{source}.gpkg", source=PREPPED),
        expand("store/source/{source}/bounds.csv", source=PREPPED + MIRRORED),
        expand("store/source/{source}/catalog.json", source=PREPPED + MIRRORED),
        "store/aggregation/covering.txt",  # sequencing only; coverage reads footprints + bounds
        code=code("contour_run.py", "aggregation_covering.py", "cache_versions.py"),
    output:
        "store/bundle/coverage.pmtiles"
    shell:
        "{PY}/contour_run.py coverage"


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
    shell:
        "{PY}/source_check.py {params.source}"
