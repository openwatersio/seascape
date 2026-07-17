# Snakemake build — planning doc

_Written 2026-07-14, revised through 2026-07-17: (1) for PR #84 (persistent volume store), (2) to full scope — the **entire pipeline is one Snakemake DAG** and `just` is retired, (3) restructured around the build's three actual stages, which the DAG must make structural, not implicit, (4) hardened per external review — pure parse, mandatory per-job memory caps, gated deletions, the source output contract, and typed sources, (5) sequenced as a complete shadow build alongside the unchanged legacy build, sources first, with cutover only after end-to-end evidence, (6) two requirements made explicit: every product is a named, independently-buildable target, and the DAG has no stage-2/3 barrier — a full build keeps the box saturated, and (7) staged cutover — each stage's shadow lane replaces its legacy stage as soon as it is verified (sources first), rather than one end-to-end cutover. Companion to [2026-07-09-production-build.md](2026-07-09-production-build.md), whose stage model and invariants this keeps — including finishing its deferred phase 5c. Assumes the #84 store model: the build store is a long-lived Hetzner volume; R2 keeps stage-1 mirrors (for streaming preview), the published mosaic, and `build/<sha>/` outputs._

## Problem

Every failed planet build on the box (5/5 as of 2026-07-14) died the same way: OOM during `just aggregate` — exit 137, box at 183/184 GB with 64 GB swap saturated, sometimes killing the concurrent rclone push too. The failures are not hash-key or consistency failures; the two successful runs were bbox builds that never stress memory.

The root problem has layers:

1. **Model-based admission with no enforcement.** `scheduler.py` is a cooperative GB-budget semaphore driven by a geometric weight estimate. The estimate is wrong — its own design comment predicts ~90 GB real usage where the box observes 184+64 — and when the model is wrong there is no second line of defense: all workers share one process pool, so the kernel's OOM kill takes the pool, the stage, and the build. The instrumentation (`ru_maxrss`) can't see where the extra memory goes, so every "tune the factor" iteration is a guess followed by an overnight failure.
2. **The plan's own tripwire fired.** The production-build plan said: _"adopt Snakemake/DVC only if the helper's DAG bookkeeping grows past a couple hundred lines."_ The substrate is now `keys.py` (388) + `scheduler.py` (262) + the dirty-list/pool halves of `aggregation_run.py` and `terrain.py` (~250) + build.yml's per-stage sequencing — a hand-rolled workflow engine, minus the parts (per-job isolation, retries, keep-going, resource accounting, dry-run) that make workflow engines robust.
3. **The pipeline is described in four places that must agree by hand.** The dependency structure lives simultaneously in 24 per-source Justfiles (~470 lines of identical download→normalize chains varying only in flags), the root Justfile's stage sequence, sources.yml's matrix + publish ordering, and build.yml's stage-by-stage docker runs — with `keys.py` dirty lists underneath deciding what's stale. `preview` is a fifth description: ~70 lines of bespoke shell wrapped around `just planet`.
4. **The build's own stage boundaries are implicit where they matter most.** The build is really three builds with different products, cadences, and costs — **(1) sources**: fetch → normalize → catalog → covering; **(2) mosaic**: the merged Float32 truth DEM; **(3) tiles**: everything cartographic, raster and vector. The production plan drew exactly this line, but the code only half-honors it: `aggregation_run.py` produces the mosaic _and_ runs the vector forks off the same transient smoothed merge in one body (the deferred 5c), so a contour-levels change still pays for the planet's most expensive computation — the multi-source feather-merge — to regenerate vectors that never needed it, and the OOM-prone merge and the cheap vector work share one memory profile, one process, one failure domain.

## The shape of the fix

**Everything that produces a file becomes a Snakemake rule, and the three stages become the DAG's visible structure** — three rule families with two narrow interfaces between them (the source catalog + covering; the mosaic GTI). One graph, entered at different targets on different cadences:

- `snakemake sources` — weekly cron: fetch/mirror upstreams, normalize, publish source mirrors to R2 (sources.yml, re-platformed onto the box + volume).
- `snakemake planet` — dispatch: covering → mosaic → derived tiles → bundles → publish. Its DAG roots at the volume's normalized sources and **contains no download rule** — builds never contact upstream, enforced by graph reachability instead of workflow discipline.
- `snakemake preview --config bbox=…` — laptop: same graph, sources streamed from R2; once the mosaic is published, stage 3 alone against `/vsicurl` mosaic windows — the minutes-long cartographic iteration loop the production plan promised.

Snakemake is a Python-native build system: rules declare inputs → outputs → a command; the engine derives the DAG, schedules under declared resource budgets, runs **each job as its own process**, retries with escalating resources, keeps going past isolated failures, records per-job max RSS (`benchmark:`), and explains what would rerun and why on a dry run. One pure-Python `uv` dependency — no conda, no server, no daemon. **Pin the exact release** in `uv.lock` and the toolchain image (the current 9.x line), and treat every semantic this plan leans on — dry-run rerun-reason output, `retries` + `attempt`-scaled resources, benchmark fields, rerun-trigger behavior with missing metadata — as _verified against the pinned release during shadow-mosaic phase 8_, not assumed from docs of mixed vintage; CLI details like the old `--reason` flag have already changed across major versions.

| Concern              | Today                                                               | Under Snakemake                                                                                                                |
| -------------------- | ------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| Pipeline description | Justfiles ×25 + sources.yml + build.yml + dirty lists               | one DAG (Snakefile + generic source rules), three stage families                                                               |
| Stage 2/3 boundary   | implicit; forks ride inside the merge job (5c deferred)             | structural: stage 3 reads only mosaic windows                                                                                  |
| Staleness            | `keys.py` content names + per-stage dirty lists + recipe-hash gates | engine provenance: input mtimes + `params:` + code, persisted on the volume                                                    |
| Memory               | `scheduler.py` cooperative budget inside one shared pool            | `resources: mem_gb=` per job under global `--resources`, per-job process isolation, `retries` with attempt-scaled reservations |
| Failure blast radius | one OOM kills the pool → stage → build                              | one job; `--keep-going` finishes the rest, `--rerun-incomplete` resumes after a hard kill                                      |
| Measurement          | hand-rolled `ru_maxrss` logging                                     | `benchmark:` per job — max RSS incl. children, to TSV                                                                          |
| Observability        | read the logs                                                       | dry run with rerun reasons: exactly what rebuilds and why                                                                      |

Two properties are requirements of this design, not side effects, and both are verified in Validation: **every product is a named target** — each source, the source list + covering, the mosaic, each stage-3 family, the bundles, the release — buildable alone against whatever upstream products already exist (see the product inventory below); and **no global barrier between stage 2 and stage 3** — a stem's tile jobs run as soon as its intersecting mosaic tiles exist, so cheap windowed stage-3 work backfills the cores the memory budget denies to additional merges (see Saturation). A long single-threaded stall in a full build is a defect with a name, not an accepted cost.

**What stays untouched while the shadow build is proved:** the legacy workflows and commands; the domain modules (the `source_*.py` chain, reproject/merge/smooth/encode, the fork generators, mosaic, bundle); the #84 volume-store model; the production R2 prefixes and release promotion; the published-mosaic contract (immutable content-addressed COGs + GTI, pointer last); `build/<sha>/`; the toolchain image; the Worker; and all chart invariants — bias-shallow, deterministic merge order, buffer-input/restrict-output, one smoothing function, nodata discipline. The Snakemake lane is additive until each stage's own cutover: no old workflow, Justfile, scheduler, key machinery, or serving pointer is removed or repointed merely because its Snakemake replacement exists — it is removed once that replacement has been verified and has taken production (see Migration).

## Design

### The product inventory: every product is a target

Each horizontal slice of the graph is a named target — a one-line aggregator rule — so any product builds independently against whatever upstreams are already cached, and `snakemake <product> -n` explains its staleness in isolation:

| Product                             | Target                              | Output                                              | Downstream interface                       |
| ----------------------------------- | ----------------------------------- | --------------------------------------------------- | ------------------------------------------ |
| one source                          | `source_<id>`                       | `store/source/<id>/…` + `catalog.json`              | the catalog item                           |
| the source list                     | `catalogs`                          | every catalog item + masks + covering CSVs + `coverage.pmtiles` | catalog items + the covering               |
| the mosaic                          | `mosaic`                            | `store/mosaic/tiles/*.tif` + GTI                    | tiles (stage 3); GTI (publish + preview)   |
| terrain                             | `terrain`                           | render tiles → terrain archives                     | pmtiles                                    |
| contours / depth areas / soundings  | `contours` / `depare` / `soundings` | `store/<fork>/*.fgb`                                | FGBs → bundles                             |
| bundles + coverage                  | `bundles`                           | pmtiles archives                                    | `build/<sha>/` staging                     |
| the release                         | `planet`                            | `build/<sha>/` + manifests                          | release promotion                          |

`planet` is simply the topmost aggregator; `preview` is the same targets under a bbox config. This adds no machinery — each row is `rule <name>: input: …` — but the rows are the contract: a product missing from this table has no independent existence, and a change that couples two rows needs a stated reason.

### Identity: engine provenance on the volume; hashes only at publish boundaries

The first draft kept content-hash keys in artifact filenames and had Snakemake target those names. That design existed for the R2-as-store protocol — delta pushes, manifest hydrate, GC by manifest — which needed self-certifying names to survive disposable boxes. **PR #84 deletes that protocol.** The store is a persistent volume: "what survived the last run" and "what the next run sees" are the same filesystem — precisely the environment Snakemake's provenance model was built for. Keeping keys-in-filenames on top of it would mean running two freshness systems forever.

So: **plain, stable artifact names** (`store/contour/{stem}.fgb`), with the provenance database living **on the volume** beside the artifacts it describes:

- **Inputs** (covering CSV, source files, catalog items, masks, mosaic tiles) rerun dependents when they change — rclone syncs preserve mtimes, and a re-registered source is exactly a changed input.
- **Config enters as `params:`** — the same resolved values the key functions serialize today, declared on the stage that reads them: merge/feather params on mosaic rules, `CONTOUR_LEVELS` and smooth knobs on stage-3 rules.
- **Code is NOT an input on stage-1 per-source rules** — matching the legacy sources lane, where currency is `hashFiles(sources/<id>/**)` and pipeline-code changes require a `force=true` dispatch. Declaring code invalidated every source (and every downloaded raw) on any module edit — adding a format converter for one new source must not re-download the planet. Forcing is explicit and native: `snakemake sources -R prep_source`, or the workflow's `force` input (`-F`). The masks and coverage keep code inputs (the legacy gates hash exactly those modules, and rebuilding them is cheap). **Stages 2–3 keep code as declared inputs** — the tile keys hash per-stage code today, so parity there points the other way; the toolchain image tag rides as a param.
- **`FORCE_REBUILD` → `--forceall` / `-R <rule>`.** Native.

Content-addressing survives only where an artifact leaves the box for concurrent readers: the published mosaic keeps its immutable `tiles/<stem>-<key12>.tif` + GTI + pointer-last contract, with the key computed by **hashing the finished COG's bytes at publish time**. This is not incrementality machinery — it is the atomic-publish mechanism for object storage with no rename (a COG reader holding an old header must never range-read a silently replaced file), and it costs ~20 lines, not `keys.py`. GC re-roots on mosaic indexes + build manifests (already #84's stated follow-up).

Two consequences, accepted and named: **`.snakemake/` must live on the volume** (symlink or `--directory`), and provenance-shaped guarantees are only as durable as the volume, which #84 already accepts. **The metadata-loss behavior is an experiment, not an assumption**: outputs whose mtimes look current with _missing_ provenance records may or may not rerun depending on release semantics, so the provenance-loss drill (Validation) must show, on the pinned version, that deleting `.snakemake/` with the store intact schedules a conservative rebuild — and it gates the deletion of keyed freshness (Migration). If the pinned release doesn't guarantee it, the mitigation is a **provenance-generation token**: one tiny file declared as an input to every rule, written **only when an empty `.snakemake/` is initialized** (by the run wrapper, before parse — never during it, so dry runs stay pure). A lost metadata store mints a new token, which makes "no metadata" indistinguishable from "everything changed" — and an intact store never touches it.

### Stage 1 — sources: an output contract, not a recipe language

**A source is defined by what it emits, not how it's prepared.** Every source produces exactly one machine-facing object plus its normalized assets:

```jsonc
// store/source/<id>/catalog.json — the contract, STAC-Item-shaped
{
  "id": "dgm_w",
  "kind": "elevation",              // typed producer — see Typed sources below
  "license": "…", "attribution": "…",
  "recipe_hash": "…",
  "properties": { "priority": …, "max_zoom": …, "offset": …, "land_clamp": …, "negate": …, "band": …, "mixed_crs": … },
  "assets": [ { "href": "…tif", "bbox": [...], "maxzoom": 14 }, … ]
}
```

Everything downstream — the covering, the merge params, publishing, GC — reads only this contract. `bounds.csv` retires into the `assets` array (bbox + maxzoom + size per file; even CUDEM's ~8k entries is a few-hundred-KB JSON — if a mega-source ever makes the single file awkward, the named escape hatch is splitting assets into a sibling `assets.ndjson` with `catalog.json` still the pointer, not a redesign). **Not GTI, deliberately**: GTI's value is being GDAL-openable, and the catalog has no GDAL consumer — it's contract metadata read by Python and HTTP; `mixed_crs` sources (dgm_w, S-102) can't form a valid GTI without the warp that is the merge's job, and the `features` kind has no raster tiles at all. They compose instead: a QA GTI/VRT view of any elevation source is a one-liner generated *from* the catalog's assets when wanted. The two-step publish ordering collapses: **publishing is a rule** that pushes artifacts, then `catalog.json` last — one marker vouching for everything.

Behind the contract, exactly **two preparation paths**:

1. **The common path** — every prepped source, uniform: `metadata.json` + `file_list.txt`, handled by shared per-asset rules — **per-asset fetch → content-keyed stage → datum → normalize**, then catalog/footprint as reduction jobs. `file_list.txt` is the single input manifest (one upstream URL per line, any format); `fetch_asset` downloads each entry to an extensionless `raw/<index>`, and `source_prep.stage()` routes each raw by **sniffing its magic bytes**, never by source id: a zip of GeoTIFFs → extract; a zip of ESRI ASCII `.asc` grids → mosaic to one tif; a 7z → extract via py7zr; a gzip → decompress and re-sniff (`.e00` ARC/INFO → `convert_e00`; tar → extract members; bare GeoTIFF → keep); a netCDF → `gdal_translate`. **Selective extraction is declarative**: a top-level `archive_members` metadata key (an fnmatch glob over member paths, all archive kinds) picks which members stage — `african_great_lakes` selects its four `*_Analytical_ras.tif` from one `.7z`, `great_lakes` its one `*_lld.tif` per tarball (tar + `archive_members` keeps the legacy exactly-one-per-tarball guard); no key = every raster member. The former "bespoke-downloader" lakes (Tahoe, the Swiss lakes, the Great and African Great Lakes, the NOAA estuaries) are just common-path sources once staging learned their formats — no per-source download code in the build. The per-source arguments (`--crs`, `--offset`, band/negate/mixed_crs) move into `metadata.json`, closing the production plan's "datum lives only in a recipe arg" complaint at the root. _Deviations from the original draft:_ this replaces the "source-owned `prepare.py`" escape hatch for these sources with content-keyed converters; the `.e00` GRD parser was **moved into `pipelines/convert_e00.py`** (a format converter, not a source script) and is a **bounded duplicate** of the parser in `source_download_tahoe.py` until cutover A retires the legacy downloaders; and archive members stage under their **archive basenames** (no rename step), so `african_great_lakes`' files are `Lake_*_Analytical_ras.tif` rather than the legacy downloader's renamed `Lake_*.tif` — content byte-identical, names more faithful to upstream.
2. **List generation** (existing convention, not a build step): a source-owned `sources/<id>/harvest.py` that **regenerates `file_list.txt`** from an upstream index — human-run and committed, not part of the DAG (`batnas`, `uk_surfzone`). It produces the manifest; acquisition + conversion are then uniform. With `archive_members` covering selective extraction, every current source is on the common path; the **whole-source `prepare.py` escape hatch** remains named only for a genuinely inexpressible future source (`dgm_w`'s computed reference surface is the nearest real case). No per-source `rules.smk`.

The contributor experience: `sources/my_source/` contains `metadata.json` + `file_list.txt` (+ a human-run `harvest.py` only when the manifest is machine-generated from an index). Two supporting commands earn their keep: **`snakemake check --config source=<id>`** (`source_check.py`) validates the contract (metadata name/license/producer/website, a datum note or explicit `unknown`, CRS + nodata on every local raster, bbox/catalog consistency against a `bounds.csv` recompute) — a new source fails registration, not aggregation, extending the existing catalog self-check; and a **`new-source` scaffold** that stamps the template so nobody copies a historically-unusual source as their starting point.

Volatile sources keep their model inside the same contract: a cheap always-run **listing rule** writes the enumerated inventory _if changed_; the mirror rule (rclone R2→R2, internally idempotent, never touching the volume) depends on it. **The masks (stage 1.5) are two more rules** in this family, and the **source-coverage tileset** (`store/bundle/coverage.pmtiles`, `contour_run.py coverage`) becomes a stage-1 rule too — it moves out of sources.yml's dedicated `coverage` job into the sources stage (reached via the `publish`/`catalogs` aggregate), keeping its inputs (footprints + each source's resolved maxzoom) but no longer needing its own workflow job. A new, initially dispatch-only **sources-snakemake.yml** runs alongside sources.yml (boot → `snakemake sources publish --keep-going` → destroy; €1–3/week): `--keep-going` replaces the GH matrix's isolation — one red source fails its jobs, everything else completes, the run goes red as the alert channel while last-good published state keeps builds green. The old scheduled workflow remains authoritative until cutover.

**Stage 1 ends at the covering, and parse time is read-only.** `aggregation_covering.py` currently mints a fresh ULID per run, which would force checkpoints (dynamic DAG discovery) everywhere. Fix the cause: **`cover` becomes an idempotent, write-if-changed rule** — per-tile CSVs at stable paths (`store/aggregation/{stem}-aggregation.csv`, no ULID directories), rewritten only when content differs so mtimes don't churn. Aggregation tiles are global-grid-aligned, so a bbox covering is a subset of the planet's stems with identical rows. A source-bounds change touches only intersecting tiles' CSVs, so spatial change detection falls out of write-if-changed + input tracking.

**The two-invocation structure (the price of no checkpoints), stated plainly.** The covering — and therefore STEMS and the whole stage-2/3 DAG — is derived at parse time from files on disk, so an invocation must never regenerate its own parse inputs, and **parse must never fetch or write anything** (a dry run that does network I/O or mutates the store is not a dry run). So the graph splits into two invocations, chained by thin wrappers so the user still types one command:

1. `snakemake catalogs` — ordinary **rules** fetch each unprepared source's catalog item (write-if-changed; a locally-prepared source's catalog wins and its fetch rule doesn't exist), resolve masks, and run `cover`. Its own DAG needs no covering, so nothing is parse-derived from what it writes.
2. `snakemake planet|preview` — parses **purely from disk** (milliseconds against local catalog items), derives STEMS, runs stages 2–3. Its DAG contains no rule that writes catalogs, masks, or coverings — the Snakefile asserts this at parse and refuses a target mix that violates it.

`docker.sh preview` (and build.yml) run both in sequence; `snakemake preview -n` is pure and instant. `sources` remains its own third entry point on its own cadence, and the new-source dev loop stays two commands (`snakemake source_<id>`, then preview). If a genuinely same-invocation coupling ever becomes necessary, Snakemake's `checkpoint` is the documented mechanism for execution-time DAG membership — adopted then, with its re-evaluation cost, not smuggled in now.

The interface stage 1 presents downstream is exactly two things: **catalog items** (per-source properties + recipe hash + per-file bounds assets — `bounds.csv` retired) and **the covering** (which tiles exist, which source files each reads).

### Stage 2 — mosaic: the merge, alone

One rule family, one job per aggregation tile, doing **only** the expensive thing: reproject each intersecting source by `(priority, maxzoom, id)`, feather seams, apply datum offsets and the land clamp, write the unsmoothed Float32 COG with nodata-aware `average` overviews. No smoothing, no forks, no display conditioning — the truth layer, exactly as the production plan defines it:

```python
rule mosaic_tile:
    input:
        csv     = "store/aggregation/{stem}-aggregation.csv",
        sources = source_inputs,          # hydrate: local COGs; stream: catalog.json refs only
        masks   = MASKS,
        code    = MERGE_MODULE_FILES,     # reproject/merge/landmask — NOT smooth.py
    output: "store/mosaic/tiles/{stem}.tif"
    params: sources=lambda wc: source_props(wc.stem), feather=FEATHER_CFG, toolchain=IMAGE_TAG
    resources: mem_gb=lambda wc, attempt: weight(wc.stem) * attempt
    retries: 2
    benchmark: "store/bench/mosaic/{stem}.tsv"
    shell: "python mosaic.py tile {input.csv}"

rule mosaic_index:
    input: expand("store/mosaic/tiles/{stem}.tif", stem=STEMS)
    output: GTI_PARQUET, PLANET_Z8, LOCAL_GTI
    shell: "python mosaic.py index"
```

This isolation is not cosmetic. The feather-merge is the planet's memory hot spot — the thing every OOM died in — and now it is the _only_ thing inside the heavy jobs: the memory model has one job shape to get right, benchmarks measure it in isolation, and no cheap vector work shares its blast radius. Smoothing config is deliberately absent from this rule's params: a smoothing change cannot re-merge anything.

Stage 2's interface downstream is **the mosaic tiles, named by the GTI** — but the two consumers split: stage-3 jobs read tiles directly (each assembles a throwaway VRT over its intersecting tiles), while publish and streamed preview consume the GTI. Stage 3 must never depend on the global index — see below.

### Stage 3 — tiles: every consumer reads mosaic windows

This finishes the production plan's deferred 5c. Today the vector forks ride inside the merge job, reading the transient smoothed merge; here they become independent rule families that read **buffered mosaic windows** through the GTI, smooth per-consumer with the one shared `f(depth, zoom)`, and restrict output to the unbuffered tile:

```python
rule contour_tile:
    input:
        tiles = lambda wc: intersecting_mosaic_tiles(wc.stem),   # parse-time, from the covering
        code  = ["contour_run.py", "smooth.py"],
    output: "store/contour/{stem}.fgb"
    params: levels=CONTOUR_CFG, smooth=SMOOTH_CFG
    resources: mem_gb=window_weight       # a windowed read, not a source-stack merge
    benchmark: "store/bench/contour/{stem}.tsv"
    shell: "python contour_run.py tile {wildcards.stem}"   # opens a per-job VRT over input.tiles — not the global GTI

# soundings_tile, depare_tile: same shape (depare also inputs the land + water masks);
# terrain renders fan out per render stem at per-zoom resolution (already mosaic-fed, 5b);
# overlay-cell bundles, vector-layer bundles, and coverage are independent jobs downstream.
```

What this buys, concretely:

- **Stages 2 and 3 pipeline; the index is not a barrier.** Stage-3 jobs input only their intersecting mosaic tiles — never `LOCAL_GTI`, which depends on every tile and would re-create a planet-wide barrier at the DAG's widest point. Each job assembles a throwaway VRT over its handful of tiles (an identical windowed read; the GTI remains the interface for publish and streamed preview). So a stem's contour/depare/soundings/terrain work is runnable the moment its neighborhood of merges lands — while deep-ocean merges are still running — and `mosaic_index` gates only publish and preview.
- **Per-fork independence returns, better than before.** A `CONTOUR_LEVELS` change reruns contour jobs — against a fully cached mosaic, with **no re-merge at all** — where today it re-runs the feather-merge for every affected tile, and where this plan's earlier revision accepted rerunning all forks. The cheap-iteration loop the mosaic split was designed for becomes the DAG's default behavior, and a dry run proves it before any compute is spent.
- **Stage 3's memory is a different, smaller problem.** A windowed read at target resolution through the mosaic's overviews (never full-res-then-decimate) is bounded by the window, not by the source stack — the heavy and light job shapes stop sharing a scheduler.
- **Seam discipline gets simpler and must be verified.** Buffer-input/restrict-output survives, but its hard part — reproducing byte-identical overlapping merges per consumer — is gone: every consumer windows one continuous mosaic. This is the piece 5c was deferred for (multi-mosaic-tile seam coverage); the seam check in Validation is the gate.
- **A deliberate behavior change rides along**, the same one the production plan already accepted for terrain: vectors are cut from the persisted mosaic (smoothed at read time) rather than the transient merge, so outputs are no longer bit-identical to today's — equivalent, not equal. The A/B in Validation compares decoded content, not bytes.

Stage 3 is also where the preview fast path lands: with a published mosaic, `snakemake preview` against `/vsicurl` mosaic windows runs stage 3 only — no sources, no merge, no masks — the minutes-long laptop loop for contour/smoothing/style iteration.

**Concurrency has one owner.** With the engine scheduling, the modules' internal `Pool`s go: every per-item CLI is single-job, single-process (tippecanoe's internal threading is declared via `threads:` so the scheduler accounts for it). A module that secretly fans out under a one-job reservation is the memory model lying to itself — the same defect as the scheduler it replaces, one layer down. `bundle.py`'s overlay-cell group loop becomes one job per cell instead of an internal pool.

### Typed sources — specified now, scheduled never (yet)

There's no immediate plan to ship vector feature sources (reefs, rocks, wrecks, obstructions, restricted areas), but the contract above is designed so adding them later is an extension, not a retrofit. The concept: **sources are typed producers.** Every source emits the same catalog + assets contract; the `kind` field routes its assets into the right downstream branch:

| Kind        | Normalized assets                        | Downstream path                                            |
| ----------- | ---------------------------------------- | ---------------------------------------------------------- |
| `elevation` | Float32 COGs, negative-down              | covering → mosaic → terrain / contours / depare / soundings |
| `features`  | FlatGeobuf/GeoParquet per semantic layer | attribute-normalize → tile per layer → publish per layer   |

Acquisition, catalog, attribution, licensing, `check_source` validation, and publishing are shared; only the normalized-asset contract differs. A feature asset adds a few fields (`layer`, geometry types, a small per-layer schema name like `seascape-obstruction-v1`, min/maxzoom); `prepare.py` normalizes source-specific attributes into small **per-semantic-layer schemas** (obstruction, reef, waterway, shoreline, quality/provenance areas — no universal nautical-feature schema up front), preserving original feature ids for provenance. Feature sources never touch the mosaic: their branch is normalize → partition → tippecanoe per layer, fully parallel to and independent of the bathymetry branch.

What this implies **now** is only: the catalog carries `kind: elevation` (one field), the covering/mosaic rules select on it, and stage 3's bundling shouldn't deepen its coupling to one global archive — which it wants anyway:

- **Per-layer archives are the follow-on, not the migration.** `vector.pmtiles` as a single global tile-join is the last serial reduction in the build, and folding every future layer into it makes one central bundler grow forever. The end state is independent `contours.pmtiles` / `depth-areas.pmtiles` / `soundings.pmtiles` (+ per-feature-layer archives later): each builds concurrently, a reef change never rebundles global contours, failures and releases isolate, and styles load only what they need — at the cost of a few boring extra Worker/TileJSON endpoints. It stays out of the migration on the same blast-radius discipline as everything else here (the migration's A/B depends on the current `build/<sha>/` contract), and lands as the first post-migration change, before any feature source. If a single layer's global tippecanoe ever becomes the long pole, the same overlay-cell sharding the raster side already uses (and the Worker already routes) is the named escape hatch.
- **Publish manifests stay per-product** — each archive/product family writes its own small completion manifest and the release manifest points at them, so no publish step waits on an "everything finished" reduction.

### Memory: what actually changes, honestly

Be precise about what `resources: mem_gb=` is: **scheduler accounting, not enforcement**. Snakemake admits jobs against the declared budget and never monitors them — exactly the trust model that just failed five times in a row. So the design is estimates for _scheduling_, kernel for _guarantees_:

1. **Per-job memory caps are mandatory on the box, not belt-and-suspenders.** Every job runs under a kernel-enforced limit equal to its reservation — Snakemake on the host with each job as `docker run --memory={resources.mem_gb}g` against the existing toolchain image (or `systemd-run --scope -p MemoryMax=` where systemd owns the box; same cgroup either way). A job that exceeds its estimate is killed **by its own cgroup** at the reserved size — the host, Snakemake, the publish rclone, and sibling jobs are structurally unreachable. `retries` re-runs it with cap _and_ reservation `attempt`-scaled. Estimates being wrong now costs one job one retry, not a build. Laptops run unenforced; dev-scale bboxes never approached the failure mode.
2. **The global budget carries an explicit reserve**: `--resources mem_gb = RAM − reserve(OS + Snakemake + publish + dirty page cache)`, stated as numbers in the workflow, not implied.
3. **The missing ~90 GB gets measured, not guessed.** `benchmark:` records every job's true max RSS (children included) — with the merge isolated in its own rule family, the number being fit is the number that matters. The first planet attempt doubles as the instrumented run the current design never managed.
4. **The stage split shrinks the hard problem.** Only `mosaic_tile` holds a source stack; stage-3 jobs are windowed reads with their own (smaller, separately benchmarked) weights.
5. **Residual risk stays named:** if the unaccounted memory is _not_ per-job anonymous memory (e.g. dirty page-cache from store writes to a ~300 MB/s Ceph volume), cgroup caps and orchestrators alike won't fix it — though cgroup v2 memory limits do account a job's own page cache, which is itself diagnostic. The benchmark-vs-meminfo gap on run one tells us; the fix is vm/writeback tuning or #84's NVMe-scratch split.

### Saturation: where idle time comes from, and what closes it

A full build should keep the box busy end to end. The idle-time sources, each with an owner:

1. **Memory admission, not job count, is why CPUs idle.** When the budget admits three 50 GB merges, the remaining cores need work that isn't a merge. The stage-2/3 pipelining above is the fix: windowed stage-3 jobs are memory-cheap and runnable as soon as their tiles exist, so the scheduler backfills cores with vector/terrain work under the same global budget. This works only because stage 3 does not wait on `mosaic_index`.
2. **Heavy-first ordering.** `priority: weight(stem)` on `mosaic_tile` starts the expensive multi-source coastal merges first — which both shortens the makespan tail (no 60 GB tile discovered last) and releases earliest exactly the tiles stage 3's vector work wants (coastal stems are where the contours are). Cheap deep-ocean GEBCO-only merges backfill anywhere.
3. **Honest `threads:`.** Every rule declares its true parallelism (tippecanoe threads, GDAL warp threads) so CPU accounting is as real as memory accounting. Named knob, off by default: when the memory budget admits fewer merges than cores, `mosaic_tile` may scale `GDAL_NUM_THREADS` with its granted threads instead of leaving cores idle.
4. **The serial tail, inventoried.** `mosaic_index` (a parquet write; the z8 planet render splits into its own rule if the benchmark says it's not cheap), the global `vector.pmtiles` join (the known last serial reduction — per-layer archives, already the first post-migration change, are also the saturation fix), and publish rclone (network-bound; overlaps compute as per-product rules). Nothing else may be serial; a new reduction rule needs a stated reason.

### One path, and `just` retires

- `bbox` (empty = planet) flows into the covering. **Source access resolves per source, local wins**: the `catalogs` invocation fetches a catalog item only for sources with no locally-produced one; a locally-prepared source is read from local disk, every other source's COGs stream via `/vsicurl`. On the build box, a `hydrate` flag turns streamed sources into fetch-rule outputs instead (**the dirty-set source sync becomes the DAG itself**, eventually retiring `sources-manifest`). At phase 12 this permits deleting the preview shell ([Justfile:157-224](../../Justfile#L157-L224)) — including its footgun of `rm -rf`-ing a locally-prepared source that isn't published yet ([Justfile:168-169](../../Justfile#L168-L169)) and the fetched-catalog-shadows-local-metadata gotcha; stable stem paths + write-if-changed coverings make the derived-store `rm -rf` unnecessary too.
- **Developing a new source** is the same graph: `snakemake source-<id>` runs its stage-1 chain locally (download → datum → normalize → bounds → polygon → catalog), and the next `snakemake preview` mixes that local source with the streamed rest — no mode switch, no all-or-nothing `preview-local`. Publishing it is the identical rule chain run by the weekly `sources` target; the dev loop and production share one definition.
- A new, initially dispatch-only `build-snakemake.yml` collapses its Build step to mount volume → `docker run … snakemake planet` → shadow publish. BBOX-never-writes-planet-pointers stays a workflow guard. The old build.yml and release path remain authoritative until cutover.
- With sources, masks, mosaic, tiles, and publishes all rules, nothing load-bearing remains in any Justfile. `dev` (two dev servers) becomes an npm workspace script; tests stay plain `uv run`/`actionlint` invocations in ci.yml (they produce no artifacts — rules would abuse the tool); `docker.sh` fronts `snakemake`. CONTRIBUTING's command table shrinks to `snakemake preview|planet|sources`, `npm run dev`, `npm test`.

### What gets deleted

| Deleted                                                                                                                        | ~lines |
| ------------------------------------------------------------------------------------------------------------------------------ | ------ |
| 24 per-source Justfiles + root Justfile                                                                                        | ~740   |
| `scheduler.py`                                                                                                                 | 262    |
| `keys.py` — all but `file_hash` for publish-time naming                                                                        | ~340   |
| pool/dirty-list/sources-manifest halves of `aggregation_run.py`, `terrain.py`; the fork-inside-merge body and per-fork key fns | ~350   |
| sources.yml matrix + publish choreography; build.yml per-stage orchestration (atop #84's removals)                             | ~250   |

Net: ~1,900 lines of orchestration, freshness, and recipe boilerplate for one pinned dependency plus a Snakefile + generic source rules (~300 lines, written once). The claim is checkable, and the Validation section is where it either survives or doesn't.

## Migration: shadow, verify, cut over — one stage at a time

Each stage runs the same cycle: build its Snakemake lane in shadow, verify it against that stage's gates, then **cut over that stage** — the Snakemake lane becomes the production producer, the legacy machinery for that stage retires after a rollback window, and the next stage builds on the now-canonical output. There is no end-of-project big-bang cutover: once sources is verified, Snakemake sources _is_ the sources build while stages 2–3 are still being written.

Two seams bound the cycles:

- **Cycle 1 — sources (phases 1–4, then cutover A).** The seam is the published source contract — `source/<id>/` COGs + `bounds.csv` + `catalog.json` on R2. The legacy planet build already consumes exactly that and doesn't care who produced it, so flipping the producer is invisible downstream.
- **Cycle 2 — the build (phases 6–10, then cutover B).** Mosaic and tiles verify in sequence — the mosaic's memory and A/B gates pass before tile work starts — but they cut over together, because the legacy build has no stage-2/3 seam to hold: the vector forks ride inside the merge (problem 4), so no legacy stage 3 exists that could keep running against a Snakemake mosaic.

Migration adds two workflows — `sources-snakemake.yml` and `build-snakemake.yml` — rather than editing sources.yml or build.yml in place. While a stage is in shadow, its workflow is dispatch-only and must not mutate production state:

- **Separate local state:** legacy uses `/mnt/seascape-store/store`; Snakemake uses `/mnt/seascape-store/snakemake-store`, including its own `.snakemake/`. Both may live on the same volume and therefore keep the existing `r2-store` concurrency group; use a second volume only if running the lanes concurrently becomes valuable.
- **Separate object-store state:** legacy publishes below `bathymetry/`; Snakemake publishes below `bathymetry-next/` (`source/`, `mosaic/`, and `build/<sha>/`). A workflow guard rejects the legacy prefix in shadow mode. Nothing in release.yml or the Worker reads the shadow prefix.
- **Share only immutable bulk inputs:** volatile CUDEM/S-102 objects may be read from the existing mirror instead of copied, but shadow rules never `sync --delete` or publish a marker into a shared prefix. Catalogs, normalized outputs, footprints, provenance, and completion markers are isolated.
- **No transitional wrapper around Just:** a source counts as converted only when generic Snakemake rules or its `prepare.py` invoke the existing Python modules directly. Calling `just source` from a rule would test plumbing while preserving the duplicate recipe system.
- **Deletion is per-cycle, after that cycle's rollback window** — not deferred to the end of the whole migration. A stage's legacy machinery (recipes, workflow choreography) is deleted once its Snakemake replacement has run in production through the window; machinery shared with a not-yet-cut-over stage stays.

Each phase below is independently shippable and sequenced after #84:

1. **Shadow foundation.** Pin Snakemake in `uv.lock` and the toolchain image; add the Snakefile, namespace config, prefix guard, persistent shadow `.snakemake/`, and one offline rule/test proving the container invocation and provenance location. Add no schedule and publish nothing to production.
2. **One ordinary source, end to end.** Convert a representative non-volatile source through metadata/file list → per-asset download/stage/datum/normalize → footprint/catalog validation → catalog-last shadow publish. Use the real generic source rules, not its Justfile. Gate on output equivalence, a zero-job no-op rerun, one-file invalidation, interrupted-run recovery, and absence of writes below `bathymetry/`.
3. **Shadow sources workflow.** Copy only the infrastructure shell of sources.yml (image, runner, mount, teardown, leak checks) into `sources-snakemake.yml`; replace GitHub discovery, the matrix, currentness checks, and publish choreography with one `snakemake sources --keep-going` job. Preserve the `source` and `force` dispatch inputs. It remains dispatch-only and writes only shadow state.
4. **Convert the source families.** Move ordinary sources in increasing complexity: simple HTTP GeoTIFF; archive; negate/offset/band variants; multi-file; mixed-CRS; bespoke `prepare.py`; volatile mirrors last. Convert land and inland-water masks with the volatile sources. Reuse immutable mirrored objects without granting the shadow lane delete or marker writes in their prefix. Gate the full sources target on: all sources complete; its second run is a no-op except intentional volatile listings; one broken source lets unrelated work finish but leaves the workflow red; shrink guards and catalog-last recovery survive an interrupted publish.
5. **Cutover A — sources.** Once phase 4's gates pass: `sources-snakemake.yml` takes the weekly schedule and publishes to the production `bathymetry/source/` prefix (the prefix guard lifts for stage-1 outputs only); sources.yml is paused but dispatchable through a rollback window. After the window, delete sources.yml, the per-source Justfiles, and the `just source`/`sources` recipes. One source-recipe system remains; the legacy planet build keeps consuming the identical published contract and doesn't notice.
6. **Shadow consumer boundary.** Make covering write-if-changed at stable paths (no ULID directories), implement the `catalogs` invocation against the (now-canonical) catalogs, and compare its covering with the legacy build. Gate on pure parse/dry-run, no upstream download rule reachable from `planet`, and spatially precise invalidation from one catalog change.
7. **Per-item stage 2/3 CLIs.** Add `mosaic.py tile <csv>` (merge-only body), `terrain.py render <stem>`, and `contour_run.py|soundings_run.py|depare_run.py tile <stem>` reading buffered mosaic windows. The 5c re-pointing happens here as plain Python, testable per tile while every legacy entry point remains intact.
8. **Shadow mosaic.** Add stage-2 mosaic tile + index rules, per-job memory caps, retries, and benchmarks; add dispatch-only `build-snakemake.yml` targeting `bathymetry-next`. The first planet attempt is the measurement run — merge-only RSS data persists whether or not the build completes. Verify the pinned release's CLI and provenance semantics here. The mosaic's gates (memory model, decoded A/B vs the legacy merge) pass before tile work starts.
9. **Shadow tiles, products, and preview.** Add terrain, the re-pointed vector rules, bundles, coverage, hash-at-publish mosaic, refs/masks/seed rules, `--config bbox=`, and the stage-3-only `/vsicurl` preview. Gate stage 3 on contour/depare continuity across mosaic tiles. Produce a complete release-shaped artifact below `bathymetry-next/build/<sha>/`, but do not promote it.
10. **Build evidence gate.** Run the complete build lane: one full planet; decoded A/B comparison; seam checks; preview; a zero-job no-op rerun; a green incremental planet; the config-change matrix; provenance-loss drill; enforced-OOM isolation/retry; interrupted-run recovery; and assembly of a releasable shadow manifest. Failure leaves the legacy build untouched and authoritative.
11. **Cutover B — the build.** Point release promotion at the Snakemake product prefix; replace build.yml's dispatch with `snakemake planet`; run and verify one production build through the Worker. Keep legacy build.yml manually dispatchable through an explicit rollback window. Cutover changes pointers and schedules, not pipeline behavior.
12. **Retire the legacy build lane.** Only after cutover B's rollback window: delete build.yml orchestration, the root Justfile, `scheduler.py`, keyed freshness except publish-time `file_hash`, internal pools/dirty lists, fork-inside-merge, and the preview shell; re-point docker.sh and documentation; move `dev` to npm. This commit deletes code whose replacements have already run in production—it introduces no new build behavior.

The first production slice is therefore sources, not mosaic — and it doesn't just ship first, it **cuts over first** (phase 5): the generic source contract, parallel per-asset jobs, keep-going failure isolation, persistent provenance, atomic publishing, and the new workflow/runner boundary all reach production while the OOM-prone planet path is untouched, and stages 2–3 build against canonical Snakemake sources instead of a long-lived shadow copy. Coordinate with the in-flight lineage: this stacks on #84 (`volume-store`) → #80; `native-resolution` (PR #83) continues to own the legacy `scheduler.py` until phase 12.

## Validation

- **A/B on a bbox**: Snakemake path vs current path produce equivalent artifacts — decoded-tile/content comparison, not bytes (the 5c re-pointing is a named behavior change; names differ by design).
- **Seam check (the 5c gate)**: contour/depare continuity across mosaic tile edges over a bbox spanning ≥4 mosaic tiles, including a hi-res/GEBCO boundary. This is the risk 5c was deferred for; it gets a fixture, not a hope.
- **Stage independence, now a one-command assertion**: no-op rerun schedules 0 jobs; `CONTOUR_LEVELS` edit → only contour + vector-bundle jobs, zero `mosaic_tile` jobs (dry-run rerun reasons); a smoothing change reruns stage 3 only; a priority/offset change reruns exactly the intersecting mosaic tiles and their dependents. Dry runs are pure — no network, no writes.
- **Product independence**: every row of the product inventory builds alone against cached upstreams — `snakemake contours` with a warm mosaic runs zero stage-1/2 jobs; `snakemake source_<id>` touches nothing outside its source directory.
- **Saturation evidence**: per-job wall-clock (`benchmark:` + the run log timeline) from the first planet run must show stage-3 jobs starting before the last `mosaic_tile` finishes — the barrier is really gone — and an idle tail no wider than the named serial reductions (index, global join, publish).
- **Kill resilience (enforced-OOM drill)**: give one heavy merge a deliberately underestimated reservation _with the per-job cgroup cap active_ — lowering the global `--resources` budget alone only reduces concurrency and tests nothing. Expected: that job is OOM-killed by its own cgroup, sibling jobs and the scheduler survive, the retry runs with an escalated cap + reservation, exit status reports honestly, and a re-dispatch schedules only what's missing.
- **Provenance-loss drill (with stated expected output)**: delete `.snakemake/` with the store intact → `snakemake planet -n` on the pinned release must schedule a conservative rebuild (never 0 jobs, never silently-stale output). If the pinned release shows 0 jobs — mtime-current outputs with no metadata to check params/code against — the provenance-generation token described under Identity becomes mandatory. This drill gates phase 12's deletion of keyed freshness.
- **Source-equivalence** (phases 2–4): each converted source reproduces the Justfile chain's outputs byte-identically (same Python, same args — the conversion moves flags, not logic); one changed `file_list.txt` entry re-normalizes exactly one file.
- `snakemake preview` (NY harbor) matches current preview; laptop with no R2 credentials works end-to-end; `snakemake planet -n` reaching no download rule is the builds-never-fetch invariant, now testable.
- Benchmark TSVs from the first planet run either validate `weight()` or hand us real per-tile merge peaks to fit it — the open memory question closes with data either way.

## Non-goals

- **HTTP conditional fetch (ETag/If-None-Match, Range resume) — named for later, not built.** A stored validator per raw asset would make `-R fetch_asset` a cheap revalidation pass (304s, no bytes), which both defangs the force-only code-change policy's big hammer and closes a staleness gap the legacy lane shares: a non-volatile upstream that republishes under an unchanged URL is currently never detected. Range resume additionally rescues multi-GB downloads on flaky links. Costs: a sidecar validator file per raw + fallbacks for servers with weak or absent ETags. Adopt when either the weekly forced-refetch cost or silent upstream drift actually bites.

- **Distribution.** Snakemake has cluster/cloud executor plugins (slurm, kubernetes, aws-batch); we don't use them. One box holds; if it ever doesn't, the same Snakefile gains an executor flag instead of a rewrite.
- **Plugins, generally.** Zero plugins in the migration — the built-in local executor, default scheduler, and plain-file I/O suffice. Named for later: a logger plugin (`github-status` / panoptes-style) if watching planet builds through Actions logs gets painful. Named rejection: the `s3` storage plugin as an rclone replacement — a second R2 client with its own quirks versus the pinned rclone that already survived R2's version-id breakage, and publish ordering is rule dependencies either way; `http` storage plugins structurally can't serve the catalog fetches (they act on rule I/O, and catalogs are parse inputs of the next invocation). No cgroup-enforcement plugin exists — the per-job `docker run --memory` wrapper stays custom, by design.
- **Replacing Python or GDAL.** The evidence (all failures are memory management, zero are wrong results) doesn't indict the pipeline code; a rewrite would keep GDAL's allocation behavior while re-deriving the same scheduling problem.
- **A recipe DSL — and per-source Snakemake.** Per-source variation is arguments in `metadata.json` consumed by generic rules, with a source-owned `prepare.py` emitting the same output contract as the only escape hatch. No YAML step-language, and no per-source `rules.smk` exposing engine internals to source authors.
- **Changing the stage-3 tiling grid.** Stage 3 inherits the aggregation grid for now; the mosaic boundary makes decoupling possible later. What the migration *does* owe the future: stage-3 CLIs and filenames treat their tile id as a stage-3 window id that happens to equal an aggregation stem — never as an aggregation identity — so a finer stage-3 grid is a parameter change, not an interface break.
- **Feature (vector) sources.** Specified above so the catalog schema doesn't need a retrofit; not built until a real source demands it, and per-layer archives land first.
- **Reviving the R2 store protocol.** #84's volume is the store; R2 keeps inputs, the published mosaic, and outputs. If the volume model is ever reversed, the keyed-filename design (this doc's first draft, in git history) is the fallback identity model.
