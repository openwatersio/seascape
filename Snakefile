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
        source=pat(LOCAL_PREPPED)
    priority: source_priority
    resources:
        mem_gb=8  # asc-mosaic / archive-extract jobs hold whole rasters in flight
    benchmark:
        "store/bench/prep/{source}.tsv"
    shell:
        "{PY}/source_prep.py {wildcards.source} && {PY}/source_bounds.py {wildcards.source}"


# The weekly forced source refresh — run as its OWN invocation before catalogs/publish:
# a forced producer schedules all dependents at plan time, but across an invocation
# boundary the engine's checksums cure unchanged registrations, so the main invocation
# cascades only on real upstream drift.
rule refresh:
    input:
        expand("store/source/{source}/bounds.csv", source=MIRRORED),


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
    # the scheduler's packing objective (the planet run showed small priorities losing to
    # count-maximizing selection): masks 10M > mirrors 5M > preps (raw MB).
    priority: 5_000_000  # long serial job with thousands of network header reads; start early
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
        source=pat(LOCAL_PREPPED)
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
        source=pat(LOCAL_PREPPED + LOCAL_MIRRORED)
    shell:
        "{PY}/source_catalog.py {wildcards.source} --hash-recipe"


# The streamed half of the catalogs invocation (--config stream=1): fetch a not-locally-
# prepped source's published registration. Absent-only like the legacy preview's fetch
# (engine: outputs exist ⇒ done); -R fetch_catalog refreshes. tmp+mv so a 404 never
# leaves a truncated file.
rule fetch_catalog:
    output:
        bounds="store/source/{source}/bounds.csv",
        catalog="store/source/{source}/catalog.json",
    wildcard_constraints:
        source=pat(STREAMED)
    retries: 2
    shell:
        "curl -fsS {STREAM_BASE}/{wildcards.source}/bounds.csv -o {output.bounds}.tmp && "
        "mv {output.bounds}.tmp {output.bounds} && "
        "curl -fsS {STREAM_BASE}/{wildcards.source}/catalog.json -o {output.catalog}.tmp && "
        "mv {output.catalog}.tmp {output.catalog}"


# Masks rebuild only when forced (-R landmask): pinned snapshot/release ⇒ no data drift.
rule landmask:
    output:
        "store/landmask/land.fgb"
    priority: 10_000_000  # top band (see mirror_source): long single-threaded, ready at t=0 — overlap, don't tail
    retries: 2
    resources:
        mem_gb=4
    benchmark:
        "store/bench/landmask.tsv"
    shell:
        "{PY}/landmask.py prep"


rule watermask:
    output:
        "store/landmask/water.fgb"
    priority: 10_000_000  # see landmask
    retries: 2
    resources:
        mem_gb=8  # the planet Overture-water reproject; refine from the benchmark
    benchmark:
        "store/bench/watermask.tsv"
    shell:
        "{PY}/landmask.py prep-water"


# The covering — stage 1's downstream interface. BBOX rides as a param so a changed
# window reruns it (write-if-changed prunes in-window stale tiles, keeps out-of-window).
rule cover:
    input:
        expand("store/source/{source}/bounds.csv", source=PREPPED + MIRRORED),
        expand("store/source/{source}/catalog.json", source=PREPPED + MIRRORED),
    output:
        "store/aggregation/covering.txt"
    params:
        bbox=os.environ.get("BBOX", "")
    shell:
        "{PY}/aggregation_covering.py --stable"


# Source-coverage provenance tileset.
rule coverage:
    input:
        expand("store/polygon/{source}.gpkg", source=PREPPED),
        expand("store/source/{source}/bounds.csv", source=PREPPED + MIRRORED),
        expand("store/source/{source}/catalog.json", source=PREPPED + MIRRORED),
        "store/aggregation/covering.txt",  # sequencing only; coverage reads footprints + bounds
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
