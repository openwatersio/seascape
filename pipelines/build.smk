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
import math

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
_VCELLS = {}


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


def vector_cells():
    """cell -> [covering stems] for the sharded vector bundle, built once per DAG evaluation. The
    vector cells group the covering stems by the VECTOR_SPLIT_Z grid (default: the macrotile grid,
    one covering stem per cell); each populated cell is one variable-depth vector_cell job."""
    _, key = _covering_key()
    if key not in _VCELLS:
        _VCELLS[key] = contour_run.vector_covering_cells(covering_stems())
    return _VCELLS[key]


wildcard_constraints:
    stem=r"\d+-\d+-\d+-\d+",
    cell=r"\d+-\d+-\d+"


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


def _fork_gb(table, default):
    return lambda wc, attempt: table.get(int(wc.stem.split("-")[3]), default) * attempt


# The merge streams block-wise, so its footprint never scales with window size the way
# weight() assumes: ceil(measured max) per child_z over the 2026-07-28 corpora (z15 8.4 GB
# vs the 27 GB weight-based reserve that admitted 5 merges where 17 fit). disk_mb stays
# weight-based — scratch (the -tmp reprojected tiffs) does scale with the window.
MOSAIC_GB = {15: 9, 14: 7, 13: 3, 12: 2, 11: 2, 10: 2}
MERGE_FACTOR = 1.5


# Schedule order: heaviest-first, diluted so cumulative weight stays linear — each slot takes
# from the heavy end of the weight-sorted covering while on/below the even-workload line, else
# from the light end. The heavy:light interleave self-tunes to the covering's actual weight mix,
# instead of an all-heavy memory-bound start and an all-light cpu-bound rest.
_ORDER = {}


def tile_priority(wc, input=None, attempt=None):
    _, key = _covering_key()
    if key not in _ORDER:
        # render_stems ⊇ covering_stems: terrain_render prioritizes overview stems too
        stems = sorted(render_stems(), key=utils.weight, reverse=True)
        n, avg = len(stems), sum(utils.weight(s) for s in stems) / len(stems)
        order, hi, lo, cum = [], 0, n - 1, 0.0
        for i in range(n):
            if cum <= i * avg:
                stem, hi = stems[hi], hi + 1
            else:
                stem, lo = stems[lo], lo - 1
            cum += utils.weight(stem)
            order.append(stem)
        _ORDER[key] = {s: n - i for i, s in enumerate(order)}
    return _ORDER[key][wc.stem]


# Priority bands, highest first: the mosaic index and the merges it gates, then the vector
# layers, then terrain. Each band sits an order of magnitude above the per-stem interleave
# range so no stem's rank can cross a band, and the interleave still orders jobs WITHIN a
# band. Every rule carries its band explicitly because rule priority does NOT propagate
# upstream (snakemake's dag.update_priority elevates dependency chains only for --prioritize
# targets) — and `--prioritize mosaic_index`, the alternative, is actively harmful: it
# flattens the whole merge chain to ONE priority, and the scheduler then maximizes the sum of
# priorities admitted under the memory budget, so swarms of small coarse merges pack ahead of
# the few 9 GB z15 merges. Measured (run 30417069133): the last z15 merge landed 2.5 h in,
# its fork chain trailed behind it, and the build ended with a ~2.5 h low-load z15 tail.
INDEX_BAND = 3_000_000
MOSAIC_BAND = 2_000_000
VECTOR_BAND = 1_000_000


def mosaic_tile_priority(wc, input=None, attempt=None):
    return MOSAIC_BAND + tile_priority(wc, input, attempt)


def vector_tile_priority(wc, input=None, attempt=None):
    return VECTOR_BAND + tile_priority(wc, input, attempt)


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
    priority: mosaic_tile_priority  # mosaic band, interleaved heavy-first within it
    retries: 2
    resources:
        mem_gb=_fork_gb(MOSAIC_GB, 3),
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
    priority: INDEX_BAND  # it gates every GTI-reading terrain render: run the moment it is ready
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

# The one shared f(depth, zoom) — a knob change reruns stage 3 only, never a merge. Every dial
# prepare_window applies must appear here, coarsening included: a dial missing from this hash
# leaves the window artifact fresh, so a sweep silently re-measures the previous surface.
SMOOTH_CFG = json.dumps({} if os.environ.get("SKIP_SMOOTH") else {
    "sigma": smooth.DEM_SIGMA, "sigma_deep": smooth.DEM_SIGMA_DEEP,
    "mask_sigma": smooth.MASK_SIGMA, "slope_low": smooth.SLOPE_LOW,
    "slope_high": smooth.SLOPE_HIGH, "depth_full": smooth.DEPTH_FULL,
    "depth_smooth": smooth.DEPTH_SMOOTH, "block": smooth.BLOCK,
    "deep_coarsen_threshold_m": smooth.DEEP_COARSEN_THRESHOLD_M,
    "deep_coarsen_factor": smooth.DEEP_COARSEN_FACTOR,
    "deep_coarsen_min_child_z": smooth.DEEP_COARSEN_MIN_CHILD_Z,
    "pond_fill_mm2": smooth.POND_FILL_MM2,
    "pond_fill_extent_m": smooth.POND_FILL_EXTENT_M,
    "pond_fill_max_depth_m": smooth.POND_FILL_MAX_DEPTH_M,
    "pond_fill_min_child_z": smooth.POND_FILL_MIN_CHILD_Z}, sort_keys=True)


# Fork reservations by child_z: ceil(measured max RSS) over the runs 30311420659 /
# 30320876479 / 30348364325 benchmark corpus, no pad — footprints are window-geometry-
# deterministic (p50 == max within a class), and the rare over-peak is covered by
# `attempt` escalation on retry plus the box's 64 GB swap.
# contour refines in feature batches (contour_run.STREAM_BATCH); run 30360226622 measured
# 4.7 GB at z14 (batch + gdal_contour child). z15 provisional: features carry more vertices,
# so the same batch count weighs more.
CONTOUR_GB = {15: 10, 14: 5}
SOUND_GB = {15: 12, 14: 8, 13: 3}
# depare reads partition buckets one at a time and writes rows incrementally, so its peak
# is the biggest band + coverage parts, not the window's whole set.
# cz8/cz9 = 4 is a deliberate under-reserve (light hedge): the class max (6.5 GB, a
# continent window) is a single outlier over a cheap deep-ocean majority, so reserving it
# for all would starve concurrency; the hedge leans on swap + `retries` instead.
DEPARE_GB = {15: 36, 14: 7, 13: 3, 12: 4, 10: 4, 9: 4, 8: 4}

# Per-stem depare reservation from the stem's own mosaic tile size — a constant per child_z
# reserves the class's worst case for every member (36 GB held four cz15 jobs to a 161 GB
# budget while their live RSS summed to ~12). Fit over run 30634360224's 2,170 rows:
# rss_GB = 0.28 + 1.805 x tile_GB (p99 residual 0.40 GB; only 7 cz15 anchors, hence the
# 4 GB pad and the `attempt` escalation carrying the tail). The rows are pre-pond-fill
# code, which only shrinks depare, so the fit is an upper bound. The tile is absent on a
# fresh store (DAG evaluation precedes the merges) — fall back to the DEPARE_GB constants.
def depare_gb(wc, attempt):
    try:
        gb = 0.28 + 1.805 * os.path.getsize(f"store/mosaic/tiles/{wc.stem}.tif") / 1e9 + 4
    except OSError:
        gb = DEPARE_GB.get(int(wc.stem.split("-")[3]), 3)
    return max(3, math.ceil(gb)) * attempt


def fork_inputs(wc):
    """A vector fork's inputs: the intersecting mosaic tiles (the buffered window's sources)
    — never the global index, so fork jobs run the moment their neighborhood of merges lands."""
    return [f"store/mosaic/tiles/{s}.tif" for s in mosaic_mod.intersecting_tiles(wc.stem)]


# The forks' shared read surface, built once per stem instead of three times: the buffered
# window materialized, smoothed, and deep-coarsened. temp() — a z15 window is 4.3 GB and
# only in-flight stems need theirs on disk. Consumers treat it as read-only.
rule fork_window:
    input:
        fork_inputs,
    output:
        temp("store/window/{stem}.tif")
    params:
        version=1, # increment to force a rebuild
        smooth=SMOOTH_CFG,
    priority: vector_tile_priority
    retries: 2
    resources:
        mem_gb=4
    benchmark:
        f"{TMP}/bench/window/{{stem}}.tsv"
    log:
        f"{TMP}/logs/window/{{stem}}.log"
    shell:
        "{PY}/smooth.py prepare-window {wildcards.stem} {output} 2> {log}"


rule contour_tile:
    input:
        window="store/window/{stem}.tif",
        masks=MASKS,
    output:
        "store/contour/{stem}.fgb"
    params:
        version=2, # increment to force a rebuild
        levels=json.dumps({"m": pipeline_config.CONTOUR_LEVELS, "ft": pipeline_config.CONTOUR_LEVELS_FT}),
        nav=contour_run.NAV_SMOOTH_MAX_M, deep=contour_run.DEEP_CUTOFF_M,
        ring=contour_run.MIN_RING_AREA_M2,
    priority: vector_tile_priority  # vector band: drain before terrain so the bundle overlaps it
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
        window="store/window/{stem}.tif",
        masks=MASKS,
    output:
        "store/soundings/{stem}.geojsons"
    params:
        version=1, # increment to force a rebuild
        cell=soundings_run.SOUND_CELL_PX, min_depth=soundings_run.SOUND_MIN_DEPTH_M,
        thin=soundings_run.SOUND_THIN_TIERS,
    priority: vector_tile_priority  # vector band: drain before terrain so the bundle overlaps it
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
        window="store/window/{stem}.tif",
        masks=MASKS,
    output:
        "store/depare/{stem}.fgb"
    params:
        version=2, # increment to force a rebuild
        levels=json.dumps({"m": pipeline_config.DEPARE_LEVELS, "ft": pipeline_config.DEPARE_LEVELS_FT}),
        drying=pipeline_config.DRYING_CAP, sliver=depare_run.SLIVER_MIN_PX,
        simplify_mm=depare_run.SIMPLIFY_MM,
    priority: vector_tile_priority  # vector band: drain before terrain so the bundle overlaps it
    retries: 2
    resources:
        mem_gb=depare_gb
    benchmark:
        f"{TMP}/bench/depare/{{stem}}.tsv"
    log:
        f"{TMP}/logs/depare/{{stem}}.log"
    shell:
        "{PY}/depare_run.py tile {wildcards.stem} 2> {log}"


# Weight like the merge: a native z14 window is the same array size; overview stems are tiny.
# 1.3 = 20% over the measured z15 ceiling (n=4 Solent renders, 18.3-18.7 GB, a 2% spread —
# pixel-count-dominated, weight()'s 17.3 GB base estimate + 8%); `attempt` covers the tail.
TERRAIN_FACTOR = 1.3


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
    priority: tile_priority  # interleaved heavy-first: evens the memory load over the build
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
        lambda wc: expand("store/soundings/{stem}.geojsons", stem=covering_stems())


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
            + expand("store/soundings/{stem}.geojsons", stem=covering_stems())
            + expand("store/depare/{stem}.fgb", stem=depare_stems())
            + expand("store/pmtiles/{stem}.pmtiles", stem=render_stems()))


rule tiles:
    input:
        tile_inputs


# ── vector bundle — cell-subtree sharded variable-depth tippecanoe + pmtiles merge ──
# The one joint variable-depth run is split three ways so the serial variable-depth tiler runs once
# per cell concurrently, not once over all planet content (docs/plans/2026-07-14-native-resolution.md):
#   vector_shallow — plain dense -Z0 -z(VECTOR_SPLIT_Z-1) over all three layers, features filtered
#     to minzoom <= VECTOR_SPLIT_Z-1; owns every z < VECTOR_SPLIT_Z tile.
#   vector_cell    — one variable-depth run per populated VECTOR_SPLIT_Z cell (default: the
#     macrotile grid — one covering stem per cell, so the worst cell job is one stem's content),
#     -Z VECTOR_SPLIT_Z -z(cell child_z), then rewritten to keep only its z >= VECTOR_SPLIT_Z owned
#     subtree (fringe tiles dropped). These are the long poles, so they ride VECTOR_BAND.
#   vector_join    — `pmtiles merge` the shallow + all cell archives into the served vector.pmtiles;
#     ownership is structurally disjoint after the fringe filter, so the merge is a pure concat
#     (go-pmtiles refuses overlapping inputs).
# Layers stay joint within every run (separately-tiled layers leaf at different depths and vanish the
# shallower one). All zoom gating rides as per-feature tippecanoe.minzoom (contour tiers, depare's z6
# floor, sounding pyramid levels). 0-byte per-tile inputs are empty tiles (kept by size); an
# all-empty cell writes a 0-byte archive the join skips. depare rides only when SKIP_DEPARE is unset
# (DEPARE). Always rebuilds — Snakemake owns freshness.
rule vector_shallow:
    input:
        contours=lambda wc: expand("store/contour/{stem}.fgb", stem=covering_stems()),
        soundings=lambda wc: expand("store/soundings/{stem}.geojsons", stem=covering_stems()),
        depare=lambda wc: expand("store/depare/{stem}.fgb", stem=depare_stems()),
    output:
        "store/bundle/vector-shallow.pmtiles"
    priority: VECTOR_BAND  # above every terrain render, so the shallow run starts as the layers drain
    resources:
        mem_gb=20  # UNMEASURED singleton running amid the terrain flood; protective, costs one slot
    params:
        bbox=os.environ.get("BBOX", ""),  # scope stamp — see mosaic_index
    benchmark:
        f"{TMP}/bench/vector-shallow.tsv"
    log:
        f"{TMP}/logs/vector-shallow.log"
    shell:
        "{PY}/contour_run.py bundle-shallow --stable 2> {log}"


rule vector_cell:
    input:
        contours=lambda wc: expand("store/contour/{stem}.fgb", stem=vector_cells().get(wc.cell, [])),
        soundings=lambda wc: expand("store/soundings/{stem}.geojsons", stem=vector_cells().get(wc.cell, [])),
        depare=lambda wc: expand("store/depare/{stem}.fgb",
                                 stem=(vector_cells().get(wc.cell, []) if DEPARE else [])),
    output:
        archive="store/bundle/vector-cell-{cell}.pmtiles",
        # the completeness evidence the join consumes; declared so a lost sidecar reruns the cell
        sidecar="store/bundle/vector-cell-{cell}.ids.json",
    priority: VECTOR_BAND  # a long pole in the band, so it overlaps the terrain fleet
    # No threads/mem reservation: the box deliberately oversubscribes CPU (--cores 2x vCPUs) and
    # binds on RAM, and a cell run has no honest single thread count (serial walk, parallel
    # read/write). Set mem_gb from the per-cell benchmarks once the first sharded run measures them.
    params:
        bbox=os.environ.get("BBOX", ""),  # scope stamp — see mosaic_index
    benchmark:
        f"{TMP}/bench/vector-cell-{{cell}}.tsv"
    log:
        f"{TMP}/logs/vector-cell-{{cell}}.log"
    shell:
        "{PY}/contour_run.py bundle-cell {wildcards.cell} --stable 2> {log}"


rule vector_join:
    input:
        shallow="store/bundle/vector-shallow.pmtiles",
        cells=lambda wc: expand("store/bundle/vector-cell-{cell}.pmtiles", cell=sorted(vector_cells())),
        sidecars=lambda wc: expand("store/bundle/vector-cell-{cell}.ids.json", cell=sorted(vector_cells())),
    output:
        "store/bundle/vector.pmtiles"
    priority: VECTOR_BAND  # the join finishes the vector band before terrain bundling
    resources:
        mem_gb=20  # UNMEASURED singleton running amid the terrain flood; protective, costs one slot
    params:
        bbox=os.environ.get("BBOX", ""),  # scope stamp — see mosaic_index
    benchmark:
        f"{TMP}/bench/vector-join.tsv"
    log:
        f"{TMP}/logs/vector-join.log"
    shell:
        "{PY}/contour_run.py bundle-join --stable 2> {log}"


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
    """The finished archive set: the one vector.pmtiles + the raster planet/overlay archives. Both
    the `bundles` inventory target and `stage_build` gate on it, so neither references the other's
    input list (which, being a function, doesn't resolve cleanly through `rules`). soundings/depare
    are no longer separate archives — the vector bundle folds them into vector.pmtiles in one run."""
    return (["store/bundle/vector.pmtiles", "store/bundle/planet.pmtiles"]
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
