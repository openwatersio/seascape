# The build's entry point — stage 1 (sources) of the Snakemake lane; see
# docs/plans/2026-07-14-snakemake-build.md. Additive: the legacy Justfile chain
# stays authoritative until cutover A. Per-source knobs (crs/nodata/negate/
# datum_offset_m/clamp_positive/archive_members) live in metadata.json;
# source_prep.py reads them.
#
# Run from the repo root:
#   uv run snakemake sources -j4 [--config source=<id>] [-n]
#   uv run snakemake catalogs -j4            # every catalog + masks + the covering
#   uv run snakemake cover -j1               # store/aggregation/covering.txt only
#   uv run snakemake check --config source=<id>   # validate one source's contract
#   uv run snakemake publish [--config source=<id>]   # → r2 bathymetry/ (the one mirror)
#   ./docker.sh snakemake sources -j4        # same, inside the toolchain container
#
# Pipeline-code changes do NOT invalidate per-source outputs — matching the legacy
# sources lane, where currency is hashFiles(sources/<id>/**) and code changes need a
# force=true dispatch. After editing pipeline modules, force explicitly:
#   uv run snakemake sources -R prep_source      # re-prep every source
#   (workflow dispatch: force=true → -F)
# The masks and coverage DO keep code inputs — the legacy gates hash those modules,
# and rebuilding them is cheap.

include: "pipelines/common.smk"

# Sources are discovered from sources/ and routed by their metadata: `volatile: true`
# marks the mirrored registrations (remote objects enumerated off a public bucket —
# today volatility and mirroring coincide; if they ever split, that's a new metadata
# key). Everything else is prepped locally — per-asset fetch → content-keyed stage →
# datum/normalize; a new sources/<id>/ directory joins the lane with no code change.
ALL_SOURCES = pipeline_config.sources()
MIRRORED = [s for s in ALL_SOURCES if pipeline_config.load_metadata(s).get("volatile")]
PREPPED = [s for s in ALL_SOURCES if s not in MIRRORED]

ONLY = config.get("source")
if ONLY and ONLY not in PREPPED + MIRRORED:
    raise WorkflowError(f"unknown source {ONLY!r} — known: {PREPPED + MIRRORED}")
TARGETS = [ONLY] if ONLY else PREPPED + MIRRORED

# Per-source R2 publish choreography — included after the source lists it constrains on.
include: "pipelines/publish.smk"


rule sources:
    input:
        expand("store/source/{source}/catalog.json", source=TARGETS),
        expand("store/polygon/{source}.gpkg", source=[s for s in TARGETS if s in PREPPED]),


# One download per file_list.txt entry — per-file incrementality, retries, and
# parallelism. Raw names are the bare list index; extensions aren't trusted.
# The URL rides as a param, NOT file_list.txt as an input: params are a per-job rerun
# trigger, so editing one list line refetches exactly that index instead of all ~1300
# raws of a big source. source_fetch.py still resolves the URL from the list at runtime.
rule fetch_asset:
    output:
        "store/source/{source}/raw/{index}"
    params:
        url=lambda wc: pipeline_config.file_list(wc.source)[int(wc.index)],
    wildcard_constraints:
        source=pat(PREPPED), index=r"\d+"
    retries: 2
    benchmark:
        "store/bench/fetch/{source}-{index}.tsv"
    shell:
        "{PY}/source_fetch.py {wildcards.source} {wildcards.index}"


# Stage → datum → normalize → bounds, one job per source. Staging is content-keyed, so
# the staged tif names aren't knowable at parse — bounds.csv (one row per staged file) is
# the job's declared artifact, and bounds could never rerun without a re-prep anyway.
# The raw assets + metadata carry the provenance (module changes are force-only — header).
rule prep_source:
    input:
        raw_assets,
        metadata=str(SOURCES_DIR / "{source}/metadata.json"),
    output:
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
        bounds="store/source/{source}/bounds.csv",
        mirror="store/source/{source}/mirror.txt",
        bucket="store/source/{source}/mirror-bucket.txt",
    wildcard_constraints:
        source=pat(MIRRORED)
    priority: 5000  # thousands of network header reads — a long single job; start early
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


# The machine-facing contract item, last in every source's chain — the definitive
# "this source is prepared and current" signal, exactly as in the legacy lane.
# --hash-recipe: this lane computes the recipe hash itself (staleness here is engine
# provenance; the hash feeds the legacy build's tile keys until cutover B). recipe_files
# declares everything the hash covers, so a Justfile/harvest.py/file_list edit restamps
# the catalog without re-prepping the source.
rule catalog_item:
    input:
        "store/source/{source}/bounds.csv",
        recipe=recipe_files,
    output:
        "store/source/{source}/catalog.json"
    wildcard_constraints:
        source=pat(PREPPED + MIRRORED)
    shell:
        "{PY}/source_catalog.py {wildcards.source} --hash-recipe"


# The two masks (stage 1.5): the OSM land polygon and the Overture inland-water layer.
# One job each, code-only inputs — they download from upstream, so the DAG has no fetch
# input to declare; rebuilding only when landmask.py changes matches the legacy
# recipe-hash gating exactly (the pinned snapshot URL + Overture release live in the
# module, so the data can't drift under an unchanged recipe). Flagged coarse sources
# clamp against land.fgb and keep depths inside water.fgb during aggregation.
# priority 10000 — above every prep/mirror: these are the longest single-threaded jobs
# (watermask ~60 min), code-only inputs so ready at t=0, and rebuild only on a landmask.py
# change. Starting them first overlaps their runtime with the fetch/prep/mirror work
# instead of tailing the run; without it the byte-weighted prep priority front-loads big
# fetches ahead of them and pushes their finish past everything else.
rule landmask:
    input:
        code=code("landmask.py"),
    output:
        "store/landmask/land.fgb"
    priority: 10000
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
    priority: 10000
    retries: 2
    resources:
        mem_gb=8  # the planet Overture-water reproject; refine from the benchmark
    benchmark:
        "store/bench/watermask.tsv"
    shell:
        "{PY}/landmask.py prep-water"


# The covering — stage 1's downstream interface. aggregation_covering reads every
# source's bounds.csv + catalog.json (config.source_property → the max_zoom cap), NOT
# the masks. --stable writes per-tile CSVs at stable paths + covering.txt, write-if-
# changed so mtimes don't churn.
rule cover:
    input:
        expand("store/source/{source}/bounds.csv", source=PREPPED + MIRRORED),
        expand("store/source/{source}/catalog.json", source=PREPPED + MIRRORED),
        code=code("aggregation_covering.py"),
    output:
        "store/aggregation/covering.txt"
    shell:
        "{PY}/aggregation_covering.py --stable"


# Source-coverage provenance tileset — a stage-1 product (was sources.yml's own `coverage`
# job). contour_run.py coverage reads only the footprints + each source's resolved maxzoom
# (bounds.csv/catalog via source_maxzooms); it does NOT parse the covering or call
# get_aggregation_ids, so the --stable covering can't be mis-read here. covering.txt rides
# as a conservative sequencing input (build coverage against a settled store), not a read.
rule coverage:
    input:
        expand("store/polygon/{source}.gpkg", source=PREPPED),
        expand("store/source/{source}/bounds.csv", source=PREPPED + MIRRORED),
        expand("store/source/{source}/catalog.json", source=PREPPED + MIRRORED),
        "store/aggregation/covering.txt",
        code=code("contour_run.py", "aggregation_covering.py", "cache_versions.py"),
    output:
        "store/bundle/coverage.pmtiles"
    shell:
        "{PY}/contour_run.py coverage"


# The plan's "catalogs invocation": every converted source's catalog item, both masks,
# the covering, and the coverage tileset — the complete stage-1 product the stage-2/3
# invocation parses from.
rule catalogs:
    input:
        expand("store/source/{source}/catalog.json", source=PREPPED + MIRRORED),
        "store/landmask/land.fgb",
        "store/landmask/water.fgb",
        "store/aggregation/covering.txt",
        "store/bundle/coverage.pmtiles",


# Validate one source's published contract after its catalog exists. Requires
# --config source=<id>; source_check.py hard-errors on any contract violation.
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
