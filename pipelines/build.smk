# Stage 2+ — the planet/preview invocation: `snakemake -s pipelines/build.smk mosaic`.
#
# A SEPARATE entry Snakefile keeps the two invocations structurally apart: this
# graph parses PURELY from disk (the --stable covering `snakemake catalogs` wrote) and
# defines no rule that writes catalogs, masks, or coverings — and no fetch/mirror rule,
# so builds can never contact upstream, by graph construction rather than discipline.
#
# Freshness here is ENGINE provenance (inputs + params). CODE is deliberately not an
# input (force-only: `-R mosaic_tile`) — an innocuous merge-module edit must not
# re-merge the planet by default.

include: "common.smk"

import json

import aggregation_reproject
import landmask
import mosaic as mosaic_mod
import utils

# Mask inputs only when they are local files — a /vsicurl mask (streamed preview) has no
# file to track; its identity rides in the mask content, tracked via its input file.
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
    intersecting tiles re-merge), and the masks (their content enters every merged tile). All LEAF
    files here — their producing rules live only in the Snakefile."""
    return ([f"store/aggregation/{wc.stem}-aggregation.csv"]
            + [f"store/source/{s}/catalog.json" for s in tile_sources(wc.stem)]
            + MASKS)


def source_props(stem):
    """The resolved per-source build props the reproject/merge read (mosaic._PROPS), as a
    sorted-JSON param so any change reruns the tile."""
    return json.dumps(
        {s: {k: pipeline_config.source_property(s, k) for k in mosaic_mod._PROPS}
         for s in tile_sources(stem)}, sort_keys=True)


# The resolved merge config as a rerun param; the recipe hashes ride in the catalog INPUTS
# above, not here.
MERGE_CFG = json.dumps({
    "resample": aggregation_reproject.RESAMPLE,
    "macrotile_z": utils.macrotile_z,
    "macrotile_buffer_3857": utils.macrotile_buffer_3857,
    "num_overviews": utils.num_overviews,
}, sort_keys=True)


# The merge job holds only the merged array + reprojected sources, not the vector forks:
# corpus max 6.5 GB on a 4.3 GB-weight z14, so 1.5x reserves 6.75 GB; retries escalate.
MERGE_FACTOR = 1.5


def tile_weight(wc, input=None, attempt=None):
    return utils.weight(wc.stem, factor=MERGE_FACTOR)


# One covering tile's merge, alone — the planet's memory hot spot, isolated in its own job.
# utils.weight seeds the reservation (a geometric estimate the benchmarks re-fit); retries
# escalate it. On a laptop the reservation is scheduling only (no kernel cap).
rule mosaic_tile:
    input:
        merge_inputs
    output:
        "store/mosaic/tiles/{stem}.tif"
    params:
        sources=lambda wc: source_props(wc.stem),
        merge=MERGE_CFG,
        toolchain=utils.toolchain(),
    priority: tile_weight  # heavy-first: shortens the tail; coastal tiles free stage-3 work first
    retries: 2
    resources:
        mem_gb=lambda wc, attempt: utils.weight(wc.stem, factor=MERGE_FACTOR) * attempt,
        # real scratch: the -tmp folder of per-source reprojected tiffs, ~tile-sized
        disk_mb=lambda wc: utils.weight(wc.stem, factor=MERGE_FACTOR) * 1024,
    benchmark:
        f"{TMP}/bench/mosaic/{{stem}}.tsv"
    log:
        f"{TMP}/logs/mosaic/{{stem}}.log"
    shell:
        "{PY}/mosaic.py tile store/aggregation/{wildcards.stem}-aggregation.csv 2> {log}"


# The GTI + planet z8 + pointer — the interface for publish and streamed preview ONLY:
# stage-3 rules input their intersecting tiles directly (a throwaway VRT per job), never
# this index, so it can't become a planet-wide barrier at the DAG's widest point.
rule mosaic_index:
    input:
        tiles=expand("store/mosaic/tiles/{stem}.tif", stem=STEMS),
        covering="store/aggregation/covering.txt",
    params:
        # scope stamp: a bbox build's regional artifact must not read as current in a later
        # planet build — the params trigger re-runs every aggregate when the scope changes
        bbox=os.environ.get("BBOX", ""),
    output:
        index="store/mosaic/index/covering.parquet",
        planet="store/mosaic/planet-z8.tif",
        gti="store/mosaic/mosaic.gti",
    benchmark:
        f"{TMP}/bench/mosaic-index.tsv"
    log:
        f"{TMP}/logs/mosaic-index.log"
    shell:
        "{PY}/mosaic.py index --stable 2> {log}"


# The product-inventory target: the mosaic, buildable alone.
rule mosaic:
    input:
        rules.mosaic_index.output


# Content-address the plain tiles + planet-z8 to R2 under hashed names and write a CANDIDATE
# pointer (index + gti). Publishing is remote, so there is no on-disk output — a plain
# always-runnable target gated on the finished index. The serving pointer mosaic.gti is never
# written from here; promotion is out of scope.
rule publish:
    input:
        rules.mosaic_index.output
    benchmark:
        f"{TMP}/bench/mosaic-publish.tsv"
    log:
        f"{TMP}/logs/mosaic-publish.log"
    shell:
        "{PY}/mosaic.py publish 2> {log}"


# ── stage 3 (cartographic products): every consumer reads windows of the persisted ──
# ── mosaic, as a separate job rather than riding inside the merge                    ──

import bundle
import contour_run
import depare_run
import smooth
import soundings_run
import terrain as terrain_mod

RENDER_STEMS = terrain_mod.render_stems(STEMS)
DEPARE_STEMS = [] if os.environ.get("SKIP_DEPARE") else STEMS

# The one shared f(depth, zoom) — a knob change reruns stage 3 only, never a merge.
SMOOTH_CFG = json.dumps({} if os.environ.get("SKIP_SMOOTH") else {
    "sigma": smooth.DEM_SIGMA, "sigma_deep": smooth.DEM_SIGMA_DEEP,
    "mask_sigma": smooth.MASK_SIGMA, "slope_low": smooth.SLOPE_LOW,
    "slope_high": smooth.SLOPE_HIGH, "depth_full": smooth.DEPTH_FULL,
    "depth_smooth": smooth.DEPTH_SMOOTH, "block": smooth.BLOCK}, sort_keys=True)


# Fork reservations by child_z, fitted to the benchmark corpus (11k rows): footprints are
# deterministic (p95 == max), so reserve measured max + ~10%; retries escalate via `attempt`.
CONTOUR_GB = {14: 10, 13: 4}
SOUND_GB = {14: 6, 13: 3}
DEPARE_GB = {14: 6, 13: 4}


def _fork_gb(table, default):
    return lambda wc, attempt: table.get(int(wc.stem.split("-")[3]), default) * attempt


def fork_inputs(wc):
    """A vector fork's inputs: the intersecting mosaic tiles (the buffered window's sources)
    — never the global index, so fork jobs run the moment their neighborhood of merges lands."""
    return [f"store/mosaic/tiles/{s}.tif" for s in mosaic_mod.intersecting_tiles(wc.stem)]


rule contour_tile:
    input:
        fork_inputs
    output:
        "store/contour/{stem}.fgb"
    params:
        levels=json.dumps({"m": pipeline_config.CONTOUR_LEVELS, "ft": pipeline_config.CONTOUR_LEVELS_FT}),
        nav=contour_run.NAV_SMOOTH_MAX_M, deep=contour_run.DEEP_CUTOFF_M,
        ring=contour_run.MIN_RING_AREA_M2, smooth=SMOOTH_CFG,
    priority: tile_weight  # heavy-first; greedy backfills lighter ready jobs into the rest of the budget
    retries: 2
    resources:
        mem_gb=_fork_gb(CONTOUR_GB, 3)
    benchmark:
        f"{TMP}/bench/contour/{{stem}}.tsv"
    log:
        f"{TMP}/logs/contour/{{stem}}.log"
    shell:
        "{PY}/contour_run.py tile {wildcards.stem} 2> {log}"


rule soundings_tile:
    input:
        fork_inputs
    output:
        "store/soundings/{stem}.geojson"
    params:
        cell=soundings_run.SOUND_CELL_PX, min_depth=soundings_run.SOUND_MIN_DEPTH_M,
        smooth=SMOOTH_CFG,
    priority: tile_weight  # heavy-first; greedy backfills lighter ready jobs into the rest of the budget
    retries: 2
    resources:
        mem_gb=_fork_gb(SOUND_GB, 2)
    benchmark:
        f"{TMP}/bench/soundings/{{stem}}.tsv"
    log:
        f"{TMP}/logs/soundings/{{stem}}.log"
    shell:
        "{PY}/soundings_run.py tile {wildcards.stem} 2> {log}"


# Behind SKIP_DEPARE until the perf backlog's bounding work: the dense-tile GEOS tail is
# unbounded (~65 min single-core measured on the densest stem).
rule depare_tile:
    input:
        fork_inputs,
        masks=MASKS,
    output:
        "store/depare/{stem}.fgb"
    params:
        levels=json.dumps({"m": pipeline_config.DEPARE_LEVELS, "ft": pipeline_config.DEPARE_LEVELS_FT}),
        drying=pipeline_config.DRYING_CAP, sliver=depare_run.SLIVER_MIN_PX, smooth=SMOOTH_CFG,
    priority: tile_weight  # heavy-first; greedy backfills lighter ready jobs into the rest of the budget
    retries: 2
    resources:
        mem_gb=_fork_gb(DEPARE_GB, 3)
    benchmark:
        f"{TMP}/bench/depare/{{stem}}.tsv"
    log:
        f"{TMP}/logs/depare/{{stem}}.log"
    shell:
        "{PY}/depare_run.py tile {wildcards.stem} 2> {log}"


# Weight like the merge: a native z14 window is the same array size; overview stems are tiny.
TERRAIN_FACTOR = 2.0  # native renders unproven at scale (corpus n=1); keep the wide margin


def terrain_inputs(wc):
    """cz>=8 renders read a per-stem VRT of their halo-buffered tile set, so they run the
    moment their neighborhood merges; cz<8 needs the GTI's planet-z8-COG fall-through."""
    if int(wc.stem.split("-")[3]) >= 8:
        return [f"store/mosaic/tiles/{s}.tif" for s in terrain_mod.window_tiles(wc.stem)]
    return rules.mosaic_index.output


rule terrain_render:
    input:
        terrain_inputs
    output:
        "store/pmtiles/{stem}.pmtiles"
    priority: tile_weight  # heavy-first; greedy backfills lighter ready jobs into the rest of the budget
    params:
        cfg=json.dumps(terrain_mod._config(), sort_keys=True),
    resources:
        mem_gb=lambda wc, attempt: utils.weight(wc.stem, factor=TERRAIN_FACTOR) * attempt,
        disk_mb=lambda wc: utils.weight(wc.stem, factor=TERRAIN_FACTOR) * 1024,
    benchmark:
        f"{TMP}/bench/terrain/{{stem}}.tsv"
    log:
        f"{TMP}/logs/terrain/{{stem}}.log"
    shell:
        "{PY}/terrain.py render {wildcards.stem} 2> {log}"


# Product-inventory aggregates: each family buildable alone against a warm mosaic.
rule contours:
    input:
        expand("store/contour/{stem}.fgb", stem=STEMS)


rule soundings:
    input:
        expand("store/soundings/{stem}.geojson", stem=STEMS)


rule depare:
    input:
        expand("store/depare/{stem}.fgb", stem=DEPARE_STEMS)


rule terrain:
    input:
        expand("store/pmtiles/{stem}.pmtiles", stem=RENDER_STEMS)


# Everything cartographic (bundles + publish arrive next; DEPARE rides only when enabled).
rule tiles:
    input:
        rules.contours.input,
        rules.soundings.input,
        rules.depare.input,
        rules.terrain.input,


# ── bundles — the three vector layers tile-join into one vector.pmtiles ──
# Each bundler consumes the PLAIN per-stem outputs (0-byte = an empty tile, kept by size),
# asserts the covering is hole-free, and always rebuilds — Snakemake owns freshness. depare
# rides only when SKIP_DEPARE is unset (DEPARE_STEMS).

rule soundings_bundle:
    input:
        expand("store/soundings/{stem}.geojson", stem=STEMS)
    output:
        "store/bundle/soundings.pmtiles"
    params:
        bbox=os.environ.get("BBOX", ""),  # scope stamp — see mosaic_index
    benchmark:
        f"{TMP}/bench/soundings-bundle.tsv"
    log:
        f"{TMP}/logs/soundings-bundle.log"
    shell:
        "{PY}/soundings_run.py bundle --stable 2> {log}"


# Guarded out entirely when DEPARE_STEMS is empty (SKIP_DEPARE): the input list would be empty.
if DEPARE_STEMS:
    rule depare_bundle:
        input:
            expand("store/depare/{stem}.fgb", stem=DEPARE_STEMS)
        output:
            "store/bundle/depare.pmtiles"
        params:
            bbox=os.environ.get("BBOX", ""),  # scope stamp — see mosaic_index
        benchmark:
            f"{TMP}/bench/depare-bundle.tsv"
        log:
            f"{TMP}/logs/depare-bundle.log"
        shell:
            "{PY}/depare_run.py bundle --stable 2> {log}"


# The contour tippecanoe + the single tile-join that folds soundings (+ depare when enabled)
# into vector.pmtiles — so both bundled layers are inputs, not just the contour FGBs.
_VECTOR_DEPARE = ["store/bundle/depare.pmtiles"] if DEPARE_STEMS else []

rule vector_bundle:
    input:
        expand("store/contour/{stem}.fgb", stem=STEMS),
        "store/bundle/soundings.pmtiles",
        _VECTOR_DEPARE,
    output:
        "store/bundle/vector.pmtiles"
    params:
        bbox=os.environ.get("BBOX", ""),  # scope stamp — see mosaic_index
    benchmark:
        f"{TMP}/bench/vector-bundle.tsv"
    log:
        f"{TMP}/logs/vector-bundle.log"
    shell:
        "{PY}/contour_run.py bundle --stable 2> {log}"


# ── terrain (raster) bundles — the planet base archive + one overlay per populated ──
# OVERLAY_SPLIT_Z grid cell, concatenated from the PLAIN per-stem terrain pmtiles. The
# planet holds z0..PLANET_MAX_ZOOM; each overlay cell holds its deeper tiles. Snakemake owns
# freshness; one cell per invocation (the engine schedules the cells).

rule terrain_planet_bundle:
    input:
        expand("store/pmtiles/{stem}.pmtiles", stem=RENDER_STEMS)
    output:
        "store/bundle/planet.pmtiles"
    params:
        bbox=os.environ.get("BBOX", ""),  # scope stamp — see mosaic_index
    benchmark:
        f"{TMP}/bench/planet-bundle.tsv"
    log:
        f"{TMP}/logs/planet-bundle.log"
    shell:
        "{PY}/bundle.py planet --stable 2> {log}"


wildcard_constraints:
    cell=r"\d+-\d+-\d+"


# One cell -> stems map, built once: deriving it inside the input function costs
# O(cells x render_stems) at DAG build, minutes at planet scale.
_CELL_STEMS = {}
for _s in RENDER_STEMS:
    for _c in bundle.overlay_cells([_s]):
        _CELL_STEMS.setdefault(_c, []).append(_s)


rule overlay_bundle:
    input:
        lambda wc: [f"store/pmtiles/{s}.pmtiles" for s in _CELL_STEMS.get(wc.cell, [])]
    output:
        "store/bundle/overlay-{cell}.pmtiles"
    params:
        bbox=os.environ.get("BBOX", ""),  # scope stamp — see mosaic_index
    benchmark:
        f"{TMP}/bench/overlay-{{cell}}.tsv"
    log:
        f"{TMP}/logs/overlay-{{cell}}.log"
    shell:
        "{PY}/bundle.py cell {wildcards.cell} --stable 2> {log}"


# The bundle-inventory target: the vector layers + the raster planet/overlay archives,
# buildable alone against a warm terrain render.
rule bundles:
    input:
        rules.soundings_bundle.output,
        (rules.depare_bundle.output if DEPARE_STEMS else []),
        rules.vector_bundle.output,
        rules.terrain_planet_bundle.output,
        expand("store/bundle/overlay-{cell}.pmtiles", cell=bundle.overlay_cells(RENDER_STEMS)),


# Upload the finished archives + manifest.json to bathymetry/build/<sha>/ (manifest LAST,
# marking a complete build; release.yml promotes it). Publishing is remote, so there is no
# on-disk output — a plain always-runnable target gated on the finished bundles. coverage.pmtiles
# rides from disk when the catalogs invocation left it; the graph never writes it. Dispatch-only
# (SHA from the env) — deliberately absent from the workflow's default target list.
rule stage_build:
    input:
        rules.bundles.input
    benchmark:
        f"{TMP}/bench/stage-build.tsv"
    log:
        f"{TMP}/logs/stage-build.log"
    shell:
        "{PY}/bundle.py stage-build --stable 2> {log}"
