# Build optimization backlog

The remaining actionable performance work under the Snakemake build
(docs/plans/2026-07-14-snakemake-build.md). Incident evidence, resolved items, and
discarded hypotheses live in [optimization-history.md](optimization-history.md).

## Remaining action items

Ranked by expected impact on the incremental (weekly-refresh) build first, cold planet second.

### Marsh-coast depare: coarsening landed, shoal-bias audit owed

`contour-p` (patched GDAL ring appender, 2026-07-29) removed the polygon-contour
pathology; the remaining cost on marsh stems is everything downstream of it. Measured on
Gulf-wetland z15 stems (run 30482927526, ccx33):

- `contour-p` full m-ladder on `8-64-105-15`: **206 s**, emitting a **730 MB** partition
  FGB (stock `gdal_contour -p` never finished this pass — it hit the 3600 s timeout, then
  the 4x-coarsened rescue, then failed).
- Whole `depare_tile`: ~3,150–3,770 s. So both ladders' contour work is ~7 min of ~63 min
  and **~89% is the post-contour pass**: bucket reads of a 730 MB partition set, band
  clipping, the drying algebra, the nodata differencing, and the FGB write — over geometry
  with 40k+ parts per band (vs. NY harbor's few thousand).
- Peak RSS 19.5 GB on `8-62-105-15` — inside the 24 GB reservation but nearly the whole
  28 GB budget of a ccx33; it is the argument for keeping `DEPARE_GB[15]` generous until
  this pass is bounded, and against re-fitting it from pre-fix corpora.

**The coarsening landed** ([../plans/2026-07-30-shallow-coarsening.md](../plans/2026-07-30-shallow-coarsening.md)): the drying bucket is coverage-unioned and bands coverage-simplify to the S-58 legibility floor (`003ebad`, 4.7× on the pathology), and enclosed sub-legible marsh ponds fill in the fork window (`7c77424`, 11.6×). Block-max coarsening was refuted by measurement — part count, not vertex count, drives the GEOS cost, and block-max left parts unchanged. `contour-p` ships via the Dockerfile from `patches/gdal-polygon-ring-appender-quadratic.patch` (upstream PR: OSGeo/gdal#14983). Standing practice: profile marsh changes locally on `pipelines/perf/` (z12 fixture stems + prod root) before any dispatch.

**Reservation consequence — the per-stem fit is in.** Marsh stems set the depare memory
ceiling: `8-63-105-15` peaked ~29 GB RSS + 3.8 GB swapped (pre-fix corpora never sampled
these stems — they died in `gdal_contour -p` before the GEOS phase). Reservations now come
from `depare_gb` (build.smk): a fit from the stem's own mosaic-tile size (run 30634360224,
2,170 rows), floored per child_z at the class MEDIAN, with `attempt` escalation carrying
the tail — run 30641774632 ran 21 stems past their reservations with zero failures and the
best utilization measured. `depare_run.tile` also logs the window size next to its polygon
count (a compressed window's size tracks the geometric detail that drives the GEOS peak;
the observed scaling is superlinear: 6.0 GB window -> 9.7 GB peak, 9.94 GB -> ~29 GB), so
each planet run adds ~110 paired z15 points to refine the fit — it had only 7 cz15
anchors. The named end state is still a per-job kernel cap (see "Memory reservation
upkeep"), which turns a wrong estimate into one retried job instead of a swapping box.

Remaining open:

- **Shipped shoal-bias violations.** The `-r average` whole-window retry rescue (`depare_run.py`) and the symmetric shallow Gaussian both chart deeper than truth in places, and `SMOOTH_CFG` doesn't reach the coarsen dials. The shallow-bias audit (intake item 1 below) is the measurement to run before choosing fixes.
- **Re-profile the split.** The "~89% is the post-contour pass" attribution above predates the coarsening dials; measure on the perf rig before investing further here.
- The GEOS pathologies the pass routes around (MakeValid MultiPolygon dispatch; CoverageValidator per-target index rebuilds) are documented with measurements and queued for upstream filing; no pipeline work rides on them.

### Stop false planet rebuilds — trigger hygiene

Run 29847332817 rebuilt the world off a metadata artifact, not data: `mosaic_index`,
`soundings_bundle`, and all 239 `overlay_bundle` jobs re-ran with reason _"Params have changed
since last execution: before: `<nothing exclusive>` now: `''`"_ — the params-provenance transition
from the single-DAG migration. Everything else cascaded by mtime: `mosaic_index` rewrote
`mosaic.gti`/`planet-z8.tif` → 344 terrain renders + a full `publish_mosaic`; `soundings_bundle` →
`vector_bundle` → `stage_build`. Total: 8 h 12 m of ccx63 for a no-op.

- **Per-rule `version` force tokens** (stage-2/3 tile rules, 2026-07-24): each rule carries a
  monotonic `version` int in its `params:`, bumped in the PR that changes the rule's logic. Code
  stays a non-input (an innocuous edit never re-merges the planet), but a deliberate bump forces
  exactly that rule via the params trigger — declarative force that lives in the diff, not a `-R`
  flag to remember. Verified: introducing or bumping the token reruns only that rule + its
  cascade, a no-op stays 0 jobs. This KEEPS Snakemake's default rerun-triggers (params on) — do
  **not** pin `--rerun-triggers mtime`, which disables it (this supersedes the earlier mtime-pin
  idea, which would have pushed every deliberate force back onto `-R` at dispatch). Extend the
  token to the bundle rules as they're modified; deliberately no CI guard (code can change without
  warranting a bump).
- **Landed — dispatch scope gate** (`8689962`): every dispatch states `max_jobs`; the workflow logs the `snakemake -n` plan and aborts when it exceeds the stated scope OR plans zero jobs for a scope-stating dispatch (the silent-no-op failure mode measured on a `--until` run). A `targets` input plus isolation aggregates (e.g. `soundings_all`) keep leaf rebuilds scoped without staleness overrides. The params-provenance transition confirmed one-time: later runs re-ran nothing on params except deliberate scope changes.
- **Resolved 2026-07-29 — fresh-box benchmark-missing reruns.** Snakemake counts a job's
  benchmark among its products, and benches are box-scratch (per 435c5f0), so every fresh box
  re-ran `mosaic_index` ("Missing output files: …/bench/mosaic-index.tsv") plus its
  planet-z8/GTI mtime cascade. Fixed in `snakemake_patches.py`: `Job.missing_output` ignores a
  missing benchmark for rules with real outputs (output-less rules — `publish_mosaic`,
  `stage_build` — keep it: the missing bench is what fires them each run, caught by
  test_build). Two hard lessons attached: benchmark paths are load-bearing scheduler state —
  relocating them to the store scheduled a **36,642-job full-planet rebuild** (measured live,
  cancelled) and would regress 435c5f0's per-run analysis; and the fix class must be validated
  with `test_build.py`, not `test_engine.py` alone.
- **Elevate the `mosaic_tile` content-hash guard (below, "noted 2026-07-27") to next-build
  work.** Run 30417069133's wave is its exact payoff case: the weekly S-102 catalog restamp
  re-merges every intersecting coastal tile, and each byte-identical output re-runs four forks,
  renders, and a cell for nothing. The guard kills the cascade at the merge.

Success: a build with no changed inputs finishes in minutes, and any planet-scale re-run is
deliberate. Effort: small. Risk: low.

### A bbox run must not add work to the next planet run

Requirement, not a preference: validating on a window should cost only that window. Today it
taxes the next planet build, and the tax is most of that build's job count.

Full accounting of run 30506340413 (planet, dispatched straight after the Gulf marsh bbox run
30482927526). Of the ~210 jobs it had started at the 200-step mark, **~197 are attributable to
the bbox->planet transition** and ~13 are genuine work:

| rule | jobs | reason snakemake gave |
| --- | --- | --- |
| `terrain_render` | 179 | Set of input files has changed |
| `contour_tile` | 7 | Input files updated by another job |
| `soundings_tile` | 5 | Input files updated by another job |
| `fork_window` | 4 | Set of input files has changed |
| `fork_window` | 4 | Missing output files (temp() consumed by the bbox run) |
| `depare_tile` | 3 | Missing output files (the 26 that never completed) |
| `depare_tile` | 1 | Input files updated by another job |
| `vector_cell` | 2 | Missing output files |
| `mosaic_index` | 1 | Set of input files has changed |
| `cover` | 1 | **Params have changed** — `'-94.0,28.5,-88.8,30.6'` -> `''` |

The chain, in order:

1. **Root**: `cover`'s bbox param differs, so the checkpoint re-runs and rewrites the covering.
2. **First order — scope-dependent input SETS.** Any rule whose inputs come from
   `covering_stems()` sees a different set at planet scope than under a window:
   `mosaic_index` (the whole covering — 5 stems under the bbox, 3,286 now, which also rewrites
   planet-z8 + the GTI), `terrain_render` (`terrain_inputs` -> `window_tiles(stem)`, the
   halo-buffered neighbourhood), `fork_window` (`fork_inputs` -> `intersecting_tiles(stem)`,
   the same neighbourhood).
3. **Second order — the fork cascade, previously undocumented.** `fork_window` has no content
   guard, so a scope-triggered window rebuild restamps `store/window/<stem>.tif` and every
   consumer of that window re-runs on mtime alone: `contour_tile` + `soundings_tile` +
   `depare_tile`, 13 jobs here. The window itself is ~38 min at z15; the forks it drags are
   the expensive part (contour ~9 min, soundings ~3 min, depare 20–60 min), so ONE spurious
   window rebuild costs roughly an hour of core time downstream.

Two fixes, independent:

- **Make the input sets scope-independent** (removes causes 1–2). The covering already went
  scope-independent per-stem (`store/aggregation/<stem>-aggregation.csv`); the remaining
  surfaces are the neighbourhood derivations, which could read the full on-disk covering
  rather than the scoped stem list. Note this is also a CORRECTNESS fix, not only a cost one:
  the bbox run wrote those terrain tiles into the SHARED store with a truncated halo, so the
  reruns are the pipeline healing itself. Any fix must either make the set scope-independent
  or keep scope-truncated artifacts out of the shared store — suppressing the rerun alone
  would ship the bad tiles.
- **Extend the `mosaic_tile` content-hash guard (below) to `fork_window`** (removes cause 3
  regardless of why a window rebuilds). A window rebuilt to byte-identical content must not
  restamp its mtime, or it cascades into three forks per stem. Arguably higher value here
  than on the merge, since depare is the build's most expensive rule.

Gate: a bbox run followed by a planet DRY RUN should schedule zero terrain and zero forks.
That belongs in `test_engine`'s scope-transition check, which already asserts the healing
half of this behaviour.

Until the structural fixes land, the dispatch scope gate (`max_jobs` + zero-plan abort, above) bounds the blast radius operationally: the tax surfaces in the logged plan and aborts loudly instead of running silently.

**Not implicated, verified from the same log.** None of the 2026-07-29 changes triggered
anything: `DEPARE_GB` is a `resources:` value, priority bands are `priority:`, the
`contour-p` selection is an env var, and code + the Dockerfile are deliberately not rule
inputs. No job re-ran with "Code has changed" and no rule re-ran on a params change except
`cover`. The provenance design behaved as intended; the reruns are all scope and mtime.

### Failed jobs must leave a readable error

A rule's `2> {log}` redirect TRUNCATES when snakemake starts the retry, so a failed attempt's
stderr is destroyed within a second of being written and the job looks like it failed in
silence. This cost real time on 2026-07-29/30: two depare failures (`8-63-105-15`,
`8-63-106-15`) were both read as "exit 1, empty log", and the empty log was taken as evidence
for a memory diagnosis that the second failure then contradicted (116 GB free, `oom_kill 0`,
and it failed in 4 minutes rather than 2.5 hours). The premise was a measurement artifact.

Fixed for depare in `depare_run.tile`: any exception also appends its traceback to
`store/depare/<stem>.err`, which retries cannot erase. **Deliberately NOT fixed by changing
the redirect to `2>>`** — `Persistence._code(rule)` returns `rule.shellcmd`, so editing a
rule's shell string is a CODE change and re-runs every job of that rule (3,286 depare tiles
for one character). Verified by reading snakemake's source, after an inconclusive dry-run
test.

Owed: the same treatment for the other long rules (contour, soundings, terrain, the vector
bundles) — same one-helper pattern, same reason. Any future change to a rule's `shell:`
string, however cosmetic, must be treated as a full-rule rebuild.

### Vector bundling — the dominant cost, now fully measured

Run 29847332817 measured the whole chain end-to-end on a ccx63:

- `soundings_bundle` **2 h 56 m** (16:16–19:12): ~80 min in the single-threaded tippecanoe feature
  read at 0.2 cores / 5 MiB/s over 2,531 inputs, then ~2 h of tiling at only 1.4–2 cores.
- `vector_bundle` **4 h 35 m** (19:12–23:47): contour tippecanoe 2 h 53 m at 1.6–4 cores over
  2,450 inputs, then the tile-join folding soundings in — **1 h 42 m at ~20 cores**.
- Serial **by data dependency** (soundings.pmtiles is a vector_bundle input), so the chain is
  7 h 31 m of wall regardless of core count; the box is >90% idle throughout.
- Any dirty tile re-pays the full chain: tippecanoe is all-or-nothing over the planet's inputs,
  so the weekly volatile refresh hits this every time. This is the steady-state cost, not a
  cold-build cost.

**Per-layer archives: rejected (2026-07-26).** Serving contours/depth-areas/soundings as
independent pmtiles was evaluated repeatedly and is NOT happening — multiple archives per style
is a bad consumer experience. `vector.pmtiles` stays the single served vector artifact; every
optimization below works within that contract (shard/cache at build time, merge at publish).

**Update 2026-07-28 — the serial chain is gone.** The bundle is now `vector_shallow` (dense
z0..SPLIT-1) + one `vector_cell` per covering root cell + `vector_join`, all Snakemake rules:
cells build as their stems finish (observed overlapping the fork band mid-run), cache in the
store, and only dirty cells rebuild on refresh — the "incremental bundling" end state below is
structurally in place. Measured: the old joint run took 14.5 h single-threaded; a dense NYC
harbor cell now builds in 2 m 47 s. The stitch is `pmtiles merge` (landed, see the workload-shape
section). The chain is validated end-to-end: the first complete planet build (2026-08-02)
census-verified every cell against its fork sidecars (94.1 M feature ids, fringe drops
accounted) and proved the concat join by tile-count identity — ~10 s at planet scale vs
tile-join's 4 m 46 s on the same inputs.

Open: marsh cells run with `VECTOR_CELL_TILE_BYTES` = 800 KB tile-byte headroom (`c52dbf4`)
over tippecanoe's 500 KB default — a stopgap. The structural fix is shrinking overview-zoom
tiles so the cap can retire.

**Wagyu exit-106 (shared-borders variant) — resolved 2026-07-23.** The regenerated fixture (Stockholm-archipelago stem FGB
`store/depare/6-35-18-10.fgb`, 265 MB, 124k polygons) crashes stock tippecanoe 2.79 AND
felt/tippecanoe main (v2.80.0 — the box's build) with `--detect-shared-borders`: a Wagyu
hole-placement bug ([mapbox/tippecanoe#761](https://github.com/mapbox/tippecanoe/issues/761),
unfixed upstream) on dense hole-heavy polygons; input is valid per ST_IsValid, so **no version
bump fixes it**. Fix: `depare_run.py` swaps `--detect-shared-borders` →
`--no-simplification-of-shared-nodes` (felt's own documented successor — keeps shared edges
exact/crack-free and builds clean, all 124k features retained), with an `assert` guarding
against reintroduction. Pre-clean was rejected (the collapse is inside tippecanoe at tile
quantization, downstream of source precision); feature-split-by-sys works but is fragile. A
minimized git-storable fixture is **impossible** (125 MB floor — the crash is emergent from a
large connected polygon mass, so spatial bisection stalls), so the assert is the regression
guard rather than a CI fixture; the full fixture (kept locally / R2 if ever wanted) is a
positive gate since it now builds clean.

**Wagyu exit-106 (coalesce variant) — patched 2026-07-28.** `--coalesce-smallest-as-needed`
drop passes stack identical tiny-polygon placeholder squares into multipolygons whose holes
wagyu cannot parent, and the vendored wagyu aborts the whole run. Cell 8-34-83 reproduced
deterministically; `patches/wagyu-drop-unplaceable-hole.patch` drops the orphan hole instead
(the fix the tippecanoe maintainer endorsed in mapbox/tippecanoe#761 but never implemented).
Only tiles that previously crashed change bytes. Upstream: pushed as
`bkeepers/tippecanoe:wagyu-drop-unplaceable-hole` with the PR body drafted
(`upstream-tippecanoe-wagyu-pr.md`); filing awaits approval.

Prior data points that still inform the work: unified invocation (contours + soundings in one
named-layer tippecanoe) saved 22.8% at planet scale, semantically exact on the 16-stem sample.
The vector bundle is now SHARDED variable-depth runs (one dense shallow run + one run per
stem-grid cell, all three layers joint per run — depare's no-drop partition policy became the
invocation-wide `--coalesce-smallest-as-needed`, contours coalesce cleanly, gate-verified),
`pmtiles merge`d into the single served archive; the old per-layer tile-join fold is gone. Legacy
baseline was 7 h 01 m / 62% of the 11 h 19 m build.

**Publish hash pass:** `_HashCache` (mtime_ns+size keyed, store/mosaic/hash-cache.json) was in
run 29847332817 but cold — `mosaic_publish` still read 376 GB in 30.6 min populating it. Expect
the warm-cache no-op publish to be near-instant; verify on the next run. **Coarse renders —
mostly resolved by the per-stem VRT** (measured run 29795677492 vs the GTI-era corpus): cz9
731→164 s, cz10 592→45 s, cz13 132→16 s. Remaining: the cz<8 planet-COG readers (z5 anchors up
to ~90 min) still read through the GTI, and cz8 regressed 83→257 s (n=9; now decimating z14
tiles 64× through the VRT — check whether the GTI fall-through was the better path for cz8).

Keep the operational lessons regardless: Tippecanoe tmp + output on NVMe (never Ceph), minute-level
heartbeats (elapsed, CPU, RSS, IO deltas, deleted-open tmp bytes, disk headroom) around every long
subprocess.

### Deep-contour seam tolerance in `check_contours`

Deep Chaikin-smoothed contours diverge at tile seams (measured on 6-19-18-9|6-19-19-9: the 20 fm / 50 m / 30 fm levels, all deeper than `NAV_SMOOTH_MAX` = 30 m). Shallow (unsmoothed) contours match exactly; the deep lines smooth away from the raw tile-edge per window, so the two windows' Chaikin passes disagree at the seam — the intentional shallow-bias smoothing the module docstring calls "fundamental," not a clip or window bug (the shapely-clip A/B proved the clip faithful). Decision owed: `seam_check.check_contours` should either exclude levels above `NAV_SMOOTH_MAX` or carry a wider tolerance for them — it currently flags them as MISMATCH. (Same class as the `check_depare` grazing-coverage false positive fixed in the depare re-enable — see optimization-history.md — but a distinct mechanism; contours keep `ON_EDGE_PIXELS` because a line crosses the seam at a point.)

### Publish overlap and large-object upload tuning

`stage_build` in run 29847332817 was a pure upload tail: **36 min** reading ~35 GB of bundles at
~16 MB/s effective, starting only after vector_bundle finished, at ~0.15 cores. Two fixes:

- Overlap: make bundle uploads per-product rules that run as each bundle lands (overlays were done
  7 h before their upload started), pointer-last ordering intact.
- Throughput: the large single objects (vector ~10 GB, soundings ~6 GB) crawl on one transfer —
  same knob as the planet-z8 BigTIFF (**25 MB/s** measured): tune `--s3-upload-concurrency` +
  explicit `--s3-chunk-size` (memory ≈ concurrency × chunk) for `copyto`/large objects only.
  (`--s3-chunk-size 64M` landed 2026-07-29 on the stage_build copyto — required anyway to lift
  rclone's default-chunking 48 GB object cap; concurrency tuning still open.) Target ≥2× without
  regressing tile copies, which already sustain ~201 MB/s (peak 542) with `--transfers 32`.
  Retain `--stats-log-level NOTICE`.

### Memory reservation upkeep

`MERGE_FACTOR` (build.smk) is fit from measured peaks — re-fit from the per-run `bench/mosaic/`
(the run's benchmark artifact) after each planet run. Benchmarks are per-run scratch now, so the
rows are already fresh — no cross-run stale rows to filter. Per-job kernel caps (cgroup /
`docker run --memory` per job) land once reservations are benchmark-backed — a cap equal to the
reservation turns a wrong estimate into one retried job instead of a box OOM. Reserve from the
class MEDIAN with small pads, not the tail: over-admission keeps the box busy, and `attempt`
escalation plus the box swapfile turn the rare breach into one retried job (run 30641774632 ran
21 stems past their reservations with zero failures and the best utilization measured). The old
reserve-the-ceiling rule assumed p50 == max within a fork class; the marsh classes broke that.
As of 2026-07-28 every fork class (window/contour/soundings/depare × child_z) is measured under
the streamed implementations. Depare reserves per stem from its mosaic-tile size (`depare_gb` in
build.smk, median-floored per child_z — see the marsh item above); re-fit its coefficients from
each planet run's bench rows, since the current fit has only 7 cz15 anchors.
Terrain z15 was re-fit 2026-07-29 (`TERRAIN_FACTOR` 2.0 → 1.3; n=4 at 18.3–18.7 GB, a 2%
spread).

Two hard-won invariants to keep enforcing:

- **GDAL writers: `BIGTIFF=IF_SAFER` on every writer whose output scales with tile size.**
  `IF_NEEDED` never engages on compressed output; five separate writers (reproject translate,
  merge, merged→COG, smooth rewrite + clamp interaction, rasterized masks) each hit the 4 GB
  classic-TIFF wall at z15, one dispatch at a time. A lint-style suite check that greps for
  gdal/rasterio writers lacking the option would make the sixth impossible — small, not yet
  written.
- **Long-lived rasterio processes: cap `GDAL_CACHEMAX`.** The in-process default is 5% of box
  RAM; any block-streamed loop silently balloons to ~file size without an `Env` cap. Audit
  other in-process raster loops (terrain render, encode) for the same latent term.

### Split mosaic-index subphases if the benchmark says so

`mosaic_index` builds the parquet index, planet-z8, and the GTI pointer in one rule. Legacy
numbers: ~17 min single-core index + a modestly parallel z8 build. If the rule's benchmark shows
the z8 warp dominating, split it into its own rule so index-only changes don't re-decimate the
planet. Effort: small.

### R2 garbage collection and multipart hygiene

Volume GC is largely obsolete: plain stable names overwrite in place, and the legacy-state
retirement freed ~475 GiB. R2 keeps accumulating content-addressed publishes — GC re-roots on
mosaic indexes + build manifests (#84's stated follow-up). Also abort stale R2 multipart uploads
after a safe age (four incomplete mosaic-tile uploads from 2026-07-14 were found dangling).

### Small correctness/robustness items

- Orphan-source guard: `aggregation_covering` skips sources missing `metadata.json`; landmask and
  coverage callers can still load them. Apply the same guard everywhere.
- Release-candidate closeout — **done for v0.3.0** (complete planet build → candidate QA → promote → smoke tests green); the process is documented in ../runbooks/build-dispatch.md and ../build-validation.md. Known quality follow-ups from the QA ride as issues #108–#110.
- NVMe for hot reads — **landed 2026-07-28/29**: the masks are served from NVMe via per-FILE
  `nsenter` bind mounts (build.yml; per-file, not a directory shadow — the dir also holds the
  landmask rule's inputs, and the runner service's private mount namespace hides binds made
  outside PID 1's). Paths and mtimes unchanged; three validated runs show zero DAG impact. The
  broader NVMe-hot-store idea is superseded by the plan's named metadata-hydration path
  (Non-goals: Distribution).

### Fallback, named but not scheduled

Finer aggregation tiles (descend dense cells below the z8 macrotile floor, ~8192 px working
rasters) remain the escape hatch if windowing ever proves insufficient. Changes mosaic tiling, GTI
overview registration, and the serving pyramid — last resort. The stage-3 CLIs already treat their
tile id as a window id, so a finer stage-3 grid is a parameter change.

## Workload shape (planet covering, 2026-07-28, MAX_CHILD_Z=15)

Kept for reservation fitting and bbox selection (regenerate via `aggregation_covering.py`):

| metric            | value                                                                          |
| ----------------- | ------------------------------------------------------------------------------ |
| tiles total       | 3286                                                                           |
| child_z histogram | z8:1138 · z9:753 · z10:758 · z11:81 · z12:159 · z13:195 · z14:92 · **z15:110** |

The former z14 tail split: S-102/CUDEM/nz_coastal stems promoted to native z15 (110 stems,
capped by `MAX_CHILD_Z`), leaving 92 at z14. The heavy tail remains the US NE/mid-Atlantic
coast plus the UK surf zone; weekly raw-source refreshes dirty these same tiles — the heavy
tail is steady-state, not a one-time cost. Fresh per-tile peaks come from the per-run
`bench/*/**.tsv` benchmark artifact on every run.

**Vector join: `pmtiles merge` instead of tile-join (landed 2026-07-29).** tile-join had to MERGE
boundary tiles (adjacent cells co-emit their shared fringe through tippecanoe's tile buffer), ran
the felt PMTiles writer with its known corruption bug (felt/tippecanoe#278), and decoded/re-encoded
where a byte copy would do. go-pmtiles `merge` is sequential I/O over clustered shards but requires
STRICT disjointness (it errors on any overlapping tile), so each cell archive is first filtered to
its owned subtree (`contour_run._filter_owned_subtree`) — safe because per-stem fork inputs are
pre-clipped at cell boundaries, so a feature never enters a neighbor's tile extent, only its
buffer, and the owning tile's own buffer covers rendering across the edge. Two wrinkles worth
keeping: merge takes layer-schema metadata from its FIRST input, so the lead shard's layer set
must COVER every shard's (a constant-depth cell legitimately emits no contours, so subset shards
are legal); and go-pmtiles is pinned + sha256-verified per arch in the Dockerfile.
**Owed: one preview eyeball at a cell boundary** to confirm the fringe drop leaves no visible seam.

**Content-hash guard on `mosaic_tile` outputs (noted 2026-07-27).** The strip→NY-harbor scope
transition showed a restamped covering CSV re-merging tiles into byte-identical outputs, whose
fresh mtimes then rebuilt every downstream fork/render on the next invocation. Snakemake can't
unschedule a planned cascade mid-run, but across invocations the repo's write_if_changed pattern
cures it — extend it to the merge: after building a tile in scratch, hash-compare against the
existing store tile (the publish \_HashCache already computes these) and skip the replace when
identical, preserving the mtime. Converts any future spurious-trigger class from "wasted build +
full downstream cascade" to "wasted build, cascade dies at the merge." Belt on top of the
scope-independent-covering fix, which removes the known trigger itself.

## Toolchain-evaluation intake (2026-07-27)

From docs/plans/2026-07-27-toolchain-evaluation.md, ranked cheapest/easiest first. Already
delivered: the cell-sharded bundle, the nodata pre-simplification, the pmtiles-merge stitch, and
soundings intermediates as line-delimited `.geojsons` (`soundings_run.py` streams one Feature per
line). Still owed from the "done or in flight" set: skip-if-identical merge outputs (the
mosaic_tile content-hash guard above).

1. **Shallow-bias audit via `gdal raster mosaic --pixel-function max`** (GDAL 3.12+): diff
   shallowest-wins vs priority-wins per pixel over a sample of stems — a free chart-safety check,
   one command, no pipeline change. Elevated by the shipped shoal-bias violations (marsh item
   above).
2. **Trial `--drop-by-attribute-as-needed`** (felt, Mar 2026) on the vector cell runs: drop by
   depth-band importance instead of geometry density, env-gated, gate-verified before adoption.
   A candidate lever for retiring the 800 KB cell tile-byte headroom (vector bundling item).
3. **Split staleness keys — mostly landed 2026-07-27**: `utils.toolchain()` is deleted; tools
   are not rule inputs, and deliberate Dockerfile changes bump the affected rules' `version`
   params (mapping documented in the Dockerfile stanzas). Remaining: scope MASKS to
   intersecting tiles.
4. **Serial-tail box right-sizing**: the vector phase measured 6–9 GiB at 1–6 cores — a CX53
   (€0.047/hr) instead of the ccx63 (€1.37/hr post-reprice) if the tail survives sharding.
   Update 2026-07-29: the sharded tail is measured and it is not the vector phase — cells run
   2–3 min overlapped mid-build; the real tail is the z15 depare residue, which is memory-bound,
   not box-class-bound and now addressed by the priority bands + fitted reservations (see
   optimization-history.md). Re-check the tail shape on the next planet run.
5. **Coverage-safe pre-generalization for the bundle** — the depare pipeline now
   coverage-simplifies bands to the S-58 floor in shapely before tippecanoe (the marsh item
   above), which delivers most of what the `ST_CoverageSimplify` prototype was after. Re-profile
   wagyu's share of bundle CPU on ring-dense tiles (was 60–99%) before investing further.
6. Bigger or gated, in order: LERC_ZSTD mosaic re-encode (~halves the store; needs the shallow
   pre-bias to keep charted ≤ true), pre-baked overzoom leaves (traffic-gated Worker CPU-ms cut),
   Planetiler spike on one dense cell's depare (insurance against the felt fork's bus factor),
   maplibre-contour (chart-grade smoothing/bias questions unresolved).

7. **fork_window outputs → NVMe scratch** (recommended for the next dispatch; mechanism settled
   2026-07-29): do it as a directory bind like the landmask masks — build.yml bind-mounts a
   box-local dir over `store/window/` before the build — NOT as a path change. The bind keeps
   the canonical path, so the DAG is untouched and it can land at any time without a rebuild;
   the dir dies with the box, and a resumed run re-derives windows for stems whose forks hadn't
   finished (pure derivations, ≤~38 min each). Originally parked as a small saving, but run
   30417069133 re-priced it: the window transient peaked at **295 GB / 99 files** on the volume
   (1.8 of 2.0 TB, 250 GB free) because the scheduler's small-job bias let windows outrun
   their fork consumers — the bind moves that whole transient (and its Ceph round-trips) to
   box NVMe. The priority bands shrink the consumer lag but don't bound the transient; land
   the bind (or volume-side disk accounting on `fork_window`) on the next dispatch.
8. **Single-pass dual-ladder gdal_contour** (noted 2026-07-28): contour runs two full-window
   `gdal_contour` passes (metre + feet ladders); one combined-level pass with features
   duplicated onto both `sys` tags at shared levels saves ~2–4 min per z15 stem of raster
   scanning. Fiddly level-membership mapping; only worth it if the window band stays the
   per-stem long pole.

Ops note, not build work: apply for Cloudflare Project Alexandria credits (likely qualifies;
removes the serving-cost question).
