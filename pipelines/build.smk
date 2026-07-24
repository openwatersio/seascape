# Stage 2/3 — the mosaic → cartographic forks → bundles → publish half of the ONE DAG. Included
# from the repo-root Snakefile (never run with `-s`), so cover/masks/catalogs and these rules
# share one graph and one invocation.
#
# Freshness here is ENGINE provenance (inputs + params). CODE is deliberately not an input — an
# innocuous merge-module edit must not re-merge the planet. To force a rule when its logic DID
# change, bump its `version` param (declarative, lives in the rule); `-R <rule>` is the ad-hoc
# override. This relies on the default rerun-triggers (params on) — never pin `--rerun-triggers mtime`.
#
# STEMS are a RUNTIME product: the `cover` checkpoint (repo-root Snakefile) writes the covering
# this build scopes from, so nothing per-stem is known at parse. Every aggregator derives its
# stem set through covering_stems() below, which forces the checkpoint and re-triggers DAG
# evaluation once the covering lands. The per-stem wildcard rules (mosaic_tile, contour_tile,
# soundings_tile, depare_tile, terrain_render, overlay_bundle) keep their wildcards and per-tile
# input functions — those only run when a job is instantiated, which is after the checkpoint.

import json

import aggregation_reproject
import bundle
import contour_run
import depare_run
import landmask
import mosaic as mosaic_mod
import smooth
import soundings_run
import terrain as terrain_mod
import utils

# Mask inputs only when they are local files — a /vsicurl mask (streamed preview) has no
# file to track; its identity rides in the mask content, tracked via its input file.
MASKS = [p for p in (landmask.path(), landmask.water_path()) if not p.startswith("/vsi")]

# depare rides only when SKIP_DEPARE is unset — an env gate, known at parse, so it decides which
# depare rules even EXIST (an empty input list would break the bundle rules). The stem set behind
# it is still checkpoint-derived (depare_stems()).
DEPARE = not os.environ.get("SKIP_DEPARE")


# ── checkpoint-gated stem derivation ──────────────────────────────────────────────────────
# covering_stems() forces the `cover` checkpoint (checkpoints.cover.get()) and returns the
# BBOX-scoped covering — the covering is the full on-disk inventory, so the window filter lives
# here (mosaic_mod.covering_stems), not in the file's extent. Cached per DAG evaluation, keyed on
# the covering's path+mtime+BBOX so a re-derivation after cover reruns picks up the new file. The
# empty-BBOX refusal (a window over open ocean) moved here from parse: it raises in the input
# function, still before any job runs.
_STEMS = {}
_CELLS = {}


def _covering_key():
    path = checkpoints.cover.get().output[0]
    return path, (path, os.path.getmtime(path), os.environ.get("BBOX", ""))


def covering_stems(wc=None):
    path, key = _covering_key()
    if key not in _STEMS:
        stems = mosaic_mod.covering_stems(path)
        if not stems:
            raise WorkflowError(
                f"covering has no tiles in BBOX={os.environ.get('BBOX', '')!r} — check the window")
        _STEMS[key] = stems
    return _STEMS[key]


def depare_stems(wc=None):
    return covering_stems() if DEPARE else []


_RENDER_STEMS = {}


def render_stems(wc=None):
    # Memoized like covering_stems: several input functions call this per DAG evaluation,
    # and the cascade over a planet covering is minutes of pure Python if recomputed.
    _, key = _covering_key()
    if key not in _RENDER_STEMS:
        _RENDER_STEMS[key] = terrain_mod.render_stems(covering_stems())
    return _RENDER_STEMS[key]


def cell_stems():
    """cell -> [render stems] for the overlay bundles, built once per DAG evaluation: deriving it
    per input-function call costs O(cells x render_stems), minutes at planet scale."""
    _, key = _covering_key()
    if key not in _CELLS:
        m = {}
        for s in render_stems():
            for c in bundle.overlay_cells([s]):
                m.setdefault(c, []).append(s)
        _CELLS[key] = m
    return _CELLS[key]


wildcard_constraints:
    stem=r"\d+-\d+-\d+-\d+"


_TILE_SOURCES = {}


def tile_sources(stem):
    """The source ids intersecting one covering tile — job-instantiation time (after the
    checkpoint), from its stable CSV beside the covering."""
    if stem not in _TILE_SOURCES:
        rows = Path(f"store/aggregation/{stem}-aggregation.csv").read_text().splitlines()[1:]
        _TILE_SOURCES[stem] = sorted({r.split(",")[0] for r in rows if r.strip()})
    return _TILE_SOURCES[stem]


def merge_inputs(wc):
    """A tile's staleness inputs: its covering CSV, each intersecting source's catalog item
    (the registration marker — a re-prepped or re-registered source restamps it, so exactly the
    intersecting tiles re-merge), and the masks (their content enters every merged tile)."""
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
        version=1, # increment to force a rebuild
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
        tiles=lambda wc: expand("store/mosaic/tiles/{stem}.tif", stem=covering_stems()),
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
# written from here; promotion is out of scope. Named publish_mosaic (not publish) — `publish`
# is the per-source R2 push in publish.smk; one DAG, so the two can't share a name.
rule publish_mosaic:
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
# depare peaks at BOTH ends: dense z14 (5.2 GB measured) and the coarse continent-window cz8/cz9
# stems (5.3 GB measured on 5-9-9-9, run 30025132613 — the whole-window OSM land/water GEOS load).
# cz8/cz9 = 4 is a deliberate under-reserve (light hedge): most coarse stems are cheap deep-ocean,
# so it keeps concurrency high and leans on the box's 64 GB NVMe swap + `retries` for the rare
# coastal-coarse peak. The first planet run measures cz10-12 / z4-anchored coarse to set these honestly.
DEPARE_GB = {14: 6, 13: 4, 9: 4, 8: 4}


def _fork_gb(table, default):
    return lambda wc, attempt: table.get(int(wc.stem.split("-")[3]), default) * attempt


def fork_inputs(wc):
    """A vector fork's inputs: the intersecting mosaic tiles (the buffered window's sources)
    — never the global index, so fork jobs run the moment their neighborhood of merges lands."""
    return [f"store/mosaic/tiles/{s}.tif" for s in mosaic_mod.intersecting_tiles(wc.stem)]


rule contour_tile:
    input:
        fork_inputs,
        masks=MASKS,
    output:
        "store/contour/{stem}.fgb"
    params:
        version=1, # increment to force a rebuild
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
        fork_inputs,
        masks=MASKS,
    output:
        "store/soundings/{stem}.geojson"
    params:
        version=1, # increment to force a rebuild
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


# On by default (the build.yml `depare` input); SKIP_DEPARE=1 opts out. The nodata-pass GEOS
# tail is bounded — STRtree + subdivision + snap-round difference (docs/plans/2026-07-21-depare-perf.md).
rule depare_tile:
    input:
        fork_inputs,
        masks=MASKS,
    output:
        "store/depare/{stem}.fgb"
    params:
        version=1, # increment to force a rebuild
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
    moment their neighborhood merges; cz<8 needs the GTI's planet-z8-COG fall-through. The masks
    ride too: the render rasterizes the land mask to nudge land-side exact-0 pixels to land."""
    if int(wc.stem.split("-")[3]) >= 8:
        return [f"store/mosaic/tiles/{s}.tif" for s in terrain_mod.window_tiles(wc.stem)] + MASKS
    return list(rules.mosaic_index.output) + MASKS


rule terrain_render:
    input:
        terrain_inputs
    output:
        "store/pmtiles/{stem}.pmtiles"
    priority: tile_weight  # heavy-first; greedy backfills lighter ready jobs into the rest of the budget
    params:
        version=1, # increment to force a rebuild
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


# Product-inventory aggregates: each family buildable alone against a warm mosaic. Every stem
# set is checkpoint-derived, so the input is a function (not a parse-time expand()).
rule contours:
    input:
        lambda wc: expand("store/contour/{stem}.fgb", stem=covering_stems())


rule soundings:
    input:
        lambda wc: expand("store/soundings/{stem}.geojson", stem=covering_stems())


rule depare:
    input:
        lambda wc: expand("store/depare/{stem}.fgb", stem=depare_stems())


rule terrain:
    input:
        lambda wc: expand("store/pmtiles/{stem}.pmtiles", stem=render_stems())


def tile_inputs(wc):
    """Everything cartographic per stem — the union the `tiles` target gates on (DEPARE rides
    only when enabled)."""
    return (expand("store/contour/{stem}.fgb", stem=covering_stems())
            + expand("store/soundings/{stem}.geojson", stem=covering_stems())
            + expand("store/depare/{stem}.fgb", stem=depare_stems())
            + expand("store/pmtiles/{stem}.pmtiles", stem=render_stems()))


rule tiles:
    input:
        tile_inputs


# ── bundles — the three vector layers tile-join into one vector.pmtiles ──
# Each bundler consumes the PLAIN per-stem outputs (0-byte = an empty tile, kept by size),
# asserts the covering is hole-free, and always rebuilds — Snakemake owns freshness. depare
# rides only when SKIP_DEPARE is unset (DEPARE).

rule soundings_bundle:
    input:
        lambda wc: expand("store/soundings/{stem}.geojson", stem=covering_stems())
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


# Guarded out entirely when SKIP_DEPARE is set: the input list would be empty.
if DEPARE:
    rule depare_bundle:
        input:
            lambda wc: expand("store/depare/{stem}.fgb", stem=depare_stems())
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
rule vector_bundle:
    input:
        contours=lambda wc: expand("store/contour/{stem}.fgb", stem=covering_stems()),
        soundings="store/bundle/soundings.pmtiles",
        depare=(["store/bundle/depare.pmtiles"] if DEPARE else []),
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
        lambda wc: expand("store/pmtiles/{stem}.pmtiles", stem=render_stems())
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


rule overlay_bundle:
    input:
        lambda wc: [f"store/pmtiles/{s}.pmtiles" for s in cell_stems().get(wc.cell, [])]
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


def bundle_inputs(wc):
    """The finished archive set: the vector layers + the raster planet/overlay archives. Both the
    `bundles` inventory target and `stage_build` gate on it, so neither references the other's
    input list (which, being a function, doesn't resolve cleanly through `rules`)."""
    return (["store/bundle/soundings.pmtiles"]
            + (["store/bundle/depare.pmtiles"] if DEPARE else [])
            + ["store/bundle/vector.pmtiles", "store/bundle/planet.pmtiles"]
            + expand("store/bundle/overlay-{cell}.pmtiles", cell=bundle.overlay_cells(render_stems())))


# The bundle-inventory target: the vector layers + the raster planet/overlay archives,
# buildable alone against a warm terrain render.
rule bundles:
    input:
        bundle_inputs


# Upload the finished archives + manifest.json to bathymetry/build/<sha>/ (manifest LAST,
# marking a complete build; release.yml promotes it). Publishing is remote, so there is no
# on-disk output — a plain always-runnable target gated on the finished bundles. coverage.pmtiles
# rides from disk when the `coverage` rule left it; stage_build never writes it. Dispatch-only
# (SHA from the env) — deliberately absent from the workflow's default target list.
rule stage_build:
    input:
        bundle_inputs
    benchmark:
        f"{TMP}/bench/stage-build.tsv"
    log:
        f"{TMP}/logs/stage-build.log"
    shell:
        "{PY}/bundle.py stage-build --stable 2> {log}"
