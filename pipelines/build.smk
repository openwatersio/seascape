# Stage 2+ — the planet/preview invocation: `snakemake -s pipelines/build.smk mosaic`.
#
# A SEPARATE entry Snakefile is the plan's two-invocation split made structural: this
# graph parses PURELY from disk (the --stable covering `snakemake catalogs` wrote) and
# defines no rule that writes catalogs, masks, or coverings — and no fetch/mirror rule,
# so builds can never contact upstream, by graph construction rather than discipline.
#
# Freshness here is ENGINE provenance (inputs + params). CODE is deliberately not an
# input (force-only: `-R mosaic_tile`) — an innocuous merge-module edit must not
# re-merge the planet by default; see the plan's Identity section.

include: "common.smk"

import json

import aggregation_reproject
import keys
import landmask
import mosaic as mosaic_mod
import scheduler
import utils

# Mask inputs only when they are local files — a /vsicurl mask (streamed preview) has no
# file to track; its identity rides in the landmask cache version, as in the legacy keys.
MASKS = [p for p in (landmask.path(), landmask.water_path()) if not p.startswith("/vsi")]

# STEMS — parse-time, purely from disk, scoped to the BBOX env. The covering is the full
# on-disk inventory (write-if-changed keeps out-of-window tiles — on the box it is the
# PLANET), so the bbox filter lives here, not in the file's extent. Refusing to run without
# a covering (instead of silently building nothing) is the seam.
_covering = Path(config.get("workdir", str(SCRIPTS))) / "store" / "aggregation" / "covering.txt"
if not _covering.is_file():
    raise WorkflowError(f"no covering at {_covering} — run `snakemake catalogs` first")
STEMS = mosaic_mod.covering_stems(str(_covering))
if not STEMS:
    raise WorkflowError(f"covering has no tiles in BBOX={os.environ.get('BBOX', '')!r} — "
                        "check the window, or run `snakemake catalogs` first")


wildcard_constraints:
    stem=r"\d+-\d+-\d+-\d+"


_TILE_SOURCES = {}


def tile_sources(stem):
    """The source ids intersecting one covering tile — DAG-build time, from its stable CSV."""
    if stem not in _TILE_SOURCES:
        rows = (_covering.parent / f"{stem}-aggregation.csv").read_text().splitlines()[1:]
        _TILE_SOURCES[stem] = sorted({r.split(",")[0] for r in rows if r.strip()})
    return _TILE_SOURCES[stem]


def merge_inputs(wc):
    """A tile's staleness inputs: its covering CSV, each intersecting source's catalog item
    (the registration marker — a re-prepped or re-mirrored source restamps it, so exactly the
    intersecting tiles re-merge), and the masks (their content enters every merged tile, as in
    the legacy keys). All LEAF files here — their producing rules live only in the Snakefile."""
    return ([f"store/aggregation/{wc.stem}-aggregation.csv"]
            + [f"store/source/{s}/catalog.json" for s in tile_sources(wc.stem)]
            + MASKS)


def source_props(stem):
    """The resolved per-source build props the reproject/merge read — the same set the legacy
    mosaic key hashed (mosaic._PROPS), as a sorted-JSON param so any change reruns the tile."""
    return json.dumps(
        {s: {k: pipeline_config.source_property(s, k) for k in mosaic_mod._PROPS}
         for s in tile_sources(stem)}, sort_keys=True)


# The resolved merge config — _inputs_config's cfg minus the recipe hashes (those enter as
# the catalog INPUTS above).
MERGE_CFG = json.dumps({
    "resample": aggregation_reproject.RESAMPLE,
    "macrotile_z": utils.macrotile_z,
    "macrotile_buffer_3857": utils.macrotile_buffer_3857,
    "num_overviews": utils.num_overviews,
}, sort_keys=True)


def tile_weight(wc, input=None, attempt=None):
    return scheduler.weight(wc.stem)


# One covering tile's merge, alone — the planet's memory hot spot, isolated in its own job.
# scheduler.weight seeds the reservation (the geometric estimate the first planet run's
# benchmarks will re-fit); retries escalate it. The kernel cgroup cap (docker run --memory)
# arrives with the box workflow — on a laptop the reservation is scheduling only.
rule mosaic_tile:
    input:
        merge_inputs
    output:
        "store/mosaic/tiles/{stem}.tif"
    params:
        sources=lambda wc: source_props(wc.stem),
        merge=MERGE_CFG,
        toolchain=keys.toolchain(),
    priority: tile_weight  # heavy-first: shortens the tail; coastal tiles free stage-3 work first
    retries: 2
    resources:
        mem_gb=lambda wc, attempt: scheduler.weight(wc.stem) * attempt
    benchmark:
        "store/bench/mosaic/{stem}.tsv"
    shell:
        "{PY}/mosaic.py tile store/aggregation/{wildcards.stem}-aggregation.csv"


# The GTI + planet z8 + pointer — the interface for publish and streamed preview ONLY:
# stage-3 rules input their intersecting tiles directly (a throwaway VRT per job), never
# this index, so it can't become a planet-wide barrier at the DAG's widest point.
rule mosaic_index:
    input:
        tiles=expand("store/mosaic/tiles/{stem}.tif", stem=STEMS),
        covering="store/aggregation/covering.txt",
    output:
        index="store/mosaic/index/covering.parquet",
        planet="store/mosaic/planet-z8.tif",
        gti="store/mosaic/mosaic.gti",
    benchmark:
        "store/bench/mosaic-index.tsv"
    shell:
        "{PY}/mosaic.py index --stable"


# The product-inventory target: the mosaic, buildable alone.
rule mosaic:
    input:
        rules.mosaic_index.output
