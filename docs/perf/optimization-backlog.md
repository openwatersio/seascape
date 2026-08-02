# Build optimization backlog

The remaining actionable performance work under the Snakemake build
(docs/plans/2026-07-14-snakemake-build.md). Incident evidence, resolved items, and
discarded hypotheses live in [optimization-history.md](optimization-history.md).

## Remaining action items

Ranked by expected impact on the incremental (weekly-refresh) build first, cold planet second.

### Drain the z15 tail — banded rule priorities + fitted big-job reservations

Run 30417069133 measured the mechanism end-to-end. `--prioritize mosaic_index` sets its whole
dependency closure — every merge — to uniform maximum priority (job logs print
`priority: highest`), erasing the heavy-first `tile_priority` interleave within the merge class.
Under uniform priority the scheduler's knapsack maximizes the **sum** of priorities that fit the
memory budget, so many small jobs always outscore few big ones: hundreds of 2–3 GB coarse merges
ran ahead of the 9 GB z15 merges (last z15 merge landed ~2.5 h in), the z15 fork chain trailed
behind its neighborhoods, and the same bias inside the vector band ran windows/contours ahead of
depare. End state: a ~2.5 h tail of z15 depare draining 6-wide (`DEPARE_GB[15]` = 24 → only
6 × 24 fit in 161 GB) at load 10–25 on 48 cores. The same bias let fork windows outrun their
consumers 99-deep — a 295 GB `store/window` transient, volume peak 1.8 of 2.0 TB.

- **Replace `--prioritize mosaic_index` with explicit rule bands**: `mosaic_index` above the
  merges; `mosaic_tile` as its own graded band (heavy-first `tile_priority` intact) above
  `VECTOR_BAND`; terrain stays bottom. z15 merges then start at t=0 and their fork chains
  overlap the coarse-merge flood instead of trailing it.
- **Re-fit `DEPARE_GB[15]` 24 → ~15** (planet-measured, n=25: mean 10.5 GB, max 13.7 GB, wall
  mean 12.3 min / max 27.5 min; `attempt` covers the tail) — 10-wide instead of 6-wide.
- The `store/window` transient either rides item 8's NVMe bind (below) or gains volume-side
  disk accounting; 250 GB free at peak was closer than comfortable.

Success: the refresh-wave build loses its low-load tail (~2–3 h off run 30417069133's shape).
Effort: small (priority constants + one table entry). Risk: low.

### Marsh-coast depare: the post-contour GEOS pass is the new bottleneck

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

**Simplify the geometry instead of just tolerating it** — stub plan at
[../plans/2026-07-30-shallow-coarsening.md](../plans/2026-07-30-shallow-coarsening.md).
Block-max coarsening of the shallow bands (bias-shallow by construction; channels survive
pixel-exactly), mirroring `deep_coarsen` at the other end of the ladder. Approved in principle
2026-07-30, mechanism sketched, parameters and gates NOT settled. This is the lever that makes
marsh stems cheap; `OGR_GEOJSON_MAX_OBJ_SIZE` only stops them failing.

**Reservation consequence, and the mechanism to fix it.** Fixing the contour stage MOVED
the memory ceiling up: pre-fix these stems died in `gdal_contour -p` and never reached the
GEOS phase, so every depare RSS number on record (mean 10.5, max 13.7, later 19.5 GB) came
from stems that were never the expensive ones. Measured now: `8-63-105-15` peaked ~29 GB
RSS + 3.8 GB swapped against `DEPARE_GB[15]` = 24 — a breach, and at 6-wide on a 161 GB
budget that projects to ~198 GB against 192 GB physical. So `DEPARE_GB[15]` must go UP,
fitted from the post-fix peak (this reverses the earlier "re-fit 24 -> ~15" note above,
which was derived from the pre-fix corpus).

Better than a bigger constant: **reserve from the window's file size.** `input.size_mb` in
a resource callable is evaluated lazily, after the producing job runs (verified 2026-07-29
on a scratch Snakefile: the callable saw the real size at execution; dry runs skip it), and
a COMPRESSED window's size tracks its geometric detail — which is what drives the GEOS
peak. Marsh coastlines compress worst and cost most. Paired data so far is too thin to fit
(6.0 GB window -> 9.7 GB peak = 1.6x; 9.94 GB -> ~29 GB = 2.8x, i.e. superlinear), so
`depare_run.tile` now logs the window size next to its polygon count: one planet run yields
~110 paired z15 points, and the fit can replace the child_z table. Keep the table as a FLOOR
so the change can never reserve less than today. The named end state is still a per-job
kernel cap (see "Memory reservation upkeep"), which turns a wrong estimate into one retried
job instead of a swapping box.

These stems now COMPLETE (they never did before), so this is an optimization, not a
blocker. Where to look first: the nodata differencing and drying algebra scale with band
part count, and the 2026-07-21 fix (STRtree + subdivision + snap-rounded overlay) was
tuned on harbor geometry, not 40k-part marsh bands. Profile with `DEPARE_TIMING=1` on the
preserved windows (`store/profile/depare-pathology/`, plus the Gulf windows on the volume)
before choosing a mechanism.

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
- Add a dry-run gate: `snakemake -n` job summary logged at the top of every build, and a loud
  warning (or abort for scheduled runs) when a refresh-class dispatch schedules planet-scale
  bundle jobs.
- Verify the next dispatch is a near-no-op (hydrate + stage checks, well under an hour). The
  params records are now written, so this specific transition should be one-time — confirm it.
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
harbor cell now builds in 2 m 47 s. Attack items 1–2 below are superseded; the remaining
stitch-cost work is the `pmtiles merge` follow-up (parked, see below).

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
Only tiles that previously crashed change bytes. Upstream PR to felt/tippecanoe pending.

Attack in this order:

1. **`--read-parallel` / input format** for the ~80 min single-threaded read phase, and
   **sharding** for the low-parallelism tiling phase (spatially partition into 4/8 balanced
   shards on NVMe, merge; adopt only with identical addressed tiles, per-layer counts, canonical
   hashes). Target: the joint vector run under ~1 h.
2. **Incremental bundling** — the end state that makes refresh builds minutes, not hours: keep
   per-shard (or per-cell, like `overlay_bundle`) archives cached in the store and rebuild only
   dirty shards, merging cheaply into the served archives at publish. The overlay bundles already
   prove the shape: 239 of them rebuilt in ~2 min wall. Sketch after sharding lands, since shard
   boundaries and the merge step are shared machinery.

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

### Bound DEPARE and re-enable it

The bound is implemented and measured (2026-07-21, working tree; execution plan + full findings:
[../plans/2026-07-21-depare-perf.md](../plans/2026-07-21-depare-perf.md)). The planet corpus put
the tail in **coarse stems**, not the dense coast — `6-21-22-9` (cz9) 8.9 h, `6-19-18-9` 5.2 h on
the box vs 90 min for the densest z14 — all attributed to the nodata pass differencing every OSM
water feature against one monolithic coverage∪drying union. Fix in `depare_run.py`: STRtree
true-intersects prefilter + subdivision of parts over 512 vertices + one `grid_size=1e-6`
snap-rounded difference (float OverlayNG mis-overlays some multi-piece unions — GEOS 3.13,
point-in-polygon-arbitrated). Local results: 366/84/102 s on the three profile stems, bands +
drying byte-exact, nodata deltas arbitrated benign; `DEPARE_TIMEOUT` backstop added. The seam gate
then caught defects across the stage-3 seam, now fixed: coarse windows under-buffered for the smooth
halo (halo-scaled `window_buffer_3857`, band seams pass — this also surfaced and forced the contour
`ogr2ogr -clipsrc` → shapely-clip port, which additionally recovers 2 features ogr2ogr silently
dropped); the drying sliver filter run per-clip not per-source-polygon (moved to a pre-clip area
gate, seam 2.08e-2° → 7.49e-4°); and the nodata + residual-band seam "mismatches", which proved to
be a **false positive in `check_depare` itself** — the depare geometry is already seam-consistent to
~2e-8°, but the on-seam coverage selector used `ON_EDGE_PIXELS` (0.1 px ≈ 15 m), counting OSM
boundary detail that merely _grazes_ near the seam (one-sided) as coverage. Fixed with a tight
`ON_SEAM_PIXELS = 1e-3` selector in `check_depare` only (contour crossings keep the looser point
snap); the real tolerance (`TOL_PIXELS = 3`) is unchanged and the shifted-band negative test still
fails, so real misalignment is still caught. All depare bands, drying, and nodata now PASS the seam
gate (≤2.7e-7°). Remaining before removing `SKIP_DEPARE`: box bbox validation via the build.yml
`depare` input + stale `store/depare` cleanup, per-zoom `DEPARE_GB` re-fit from box benchmarks, then
the planet run. Effort remaining: small-medium. Risk: low-medium.

One real pre-existing seam mismatch (NOT caused by this work) is deferred:

- **Deep Chaikin-smoothed contours diverge at tile seams** (measured on 6-19-18-9|6-19-19-9: the
  20 fm / 50 m / 30 fm levels, all deeper than `NAV_SMOOTH_MAX` = 30 m). Shallow (unsmoothed)
  contours match exactly; the deep lines smooth away from the raw tile-edge per window, so the two
  windows' Chaikin passes disagree at the seam. This is the intentional shallow-bias smoothing the
  module docstring already calls "fundamental," not a clip or window bug (the shapely-clip A/B
  proved the clip faithful). Decision owed: `seam_check.check_contours` should either exclude levels
  above `NAV_SMOOTH_MAX` or carry a wider tolerance for them — it currently flags them as MISMATCH.
  (The `check_depare` false positive above was the same _class_ of over-sensitive-gate issue but a
  distinct mechanism — grazing coverage vs. per-window smoothing — and only the depare side is
  fixed; contours keep `ON_EDGE_PIXELS` because a line crosses the seam at a point.)

### Reintroduce windowed contours — resolved 2026-07-28, different mechanism

The memory problem moved and was fixed where it actually lived: the peak was never the
gdal_contour subprocess (scanline-streamed) but the post-extraction GeoDataFrame phase holding
three full copies of the window's contour set (72 GB measured on z15 UK stems). The refine
(enrich → Chaikin → ring-drop → clip) now streams in 100k-feature batches to an appended
GeoJSONSeq; measured 9.9 GB / 6–9 min at z15 (was 72 GB / 24 min). The shared `fork_window`
rule additionally builds each stem's smoothed+coarsened window once instead of three times
(deep_coarsen strip-streamed, byte-identical by differential test; in-process GDAL block cache
capped — its 5%-of-RAM default was the last hidden multi-GB term). The `6e5fb55` block-windowed
gdal_contour idea is moot.

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
reservation turns a wrong estimate into one retried job instead of a box OOM. Price reservations
from the densest measured tile, never an average — and reserve exactly the measured ceiling, no
pad: fork footprints are window-geometry-deterministic (p50 == max within a class), and
`attempt` escalation plus the box swapfile already cover the tail. As of 2026-07-28 every fork
class (window/contour/soundings/depare × child_z) is measured under the streamed
implementations. Depare z15 is now planet-measured (run 30417069133, n=25: mean 10.5 GB, max
13.7 GB against 24 reserved) — the re-fit to ~15 rides the tail item at the top of this doc.
Terrain z15 was re-fit 2026-07-29 (`TERRAIN_FACTOR` 2.0 → 1.3; n=4 at 18.3–18.7 GB, a 2%
spread). Still owed: the coarse depare classes (z8–z12 entries predate bucket streaming).

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

### GTI native-resolution regression check at release-candidate

The fix is implemented (the `.gti` carries explicit `<ResX>/<ResY>` at the covering's finest
resolution) but the regression validation is still owed before release: Bay Area development/build
previews match at the same viewport; regional overlay sizes don't collapse; a high-res indexed COG
stays distinguishable from planet-z8 through the GTI above z8; global fallback still reads the z8
overview.

### R2 garbage collection and multipart hygiene

Volume GC is largely obsolete: plain stable names overwrite in place, and the legacy-state
retirement freed ~475 GiB. R2 keeps accumulating content-addressed publishes — GC re-roots on
mosaic indexes + build manifests (#84's stated follow-up). Also abort stale R2 multipart uploads
after a safe age (four incomplete mosaic-tile uploads from 2026-07-14 were found dangling).

### Small correctness/robustness items

- Orphan-source guard: `aggregation_covering` skips sources missing `metadata.json`; landmask and
  coverage callers can still load them. Apply the same guard everywhere.
- Release-candidate closeout: validate the accumulated correctness fixes in one planet build, then
  manually dispatch `release.yml` with the validated SHA and verify its live smoke tests
  (feature-branch builds do not auto-release). See ../build-validation.md.
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

From docs/plans/2026-07-27-toolchain-evaluation.md, ranked cheapest/easiest first, cross-referenced
against this branch: already done or in flight there — the cell-sharded bundle measurement, the
nodata pre-simplification, the pmtiles-merge stitch (parked above with WIP), and skip-if-identical
merge outputs (the mosaic_tile content-hash guard above).

1. **Soundings intermediates → line-delimited `.geojsons`** so the bundle passthrough streams
   lines instead of json.load-ing FeatureCollections (the ~80 min single-threaded read at planet).
   Small and separable; a working start sits in the "pmtiles-merge WIP" stash.
2. **Shallow-bias audit via `gdal raster mosaic --pixel-function max`** (GDAL 3.12+): diff
   shallowest-wins vs priority-wins per pixel over a sample of stems — a free chart-safety check,
   one command, no pipeline change.
3. **Trial `--drop-by-attribute-as-needed`** (felt, Mar 2026) on the vector cell runs: drop by
   depth-band importance instead of geometry density, env-gated, gate-verified before adoption.
4. **Split staleness keys — mostly landed 2026-07-27**: `utils.toolchain()` is deleted; tools
   are not rule inputs, and deliberate Dockerfile changes bump the affected rules' `version`
   params (mapping documented in the Dockerfile stanzas). Remaining: scope MASKS to
   intersecting tiles.
5. **Serial-tail box right-sizing**: the vector phase measured 6–9 GiB at 1–6 cores — a CX53
   (€0.047/hr) instead of the ccx63 (€1.37/hr post-reprice) if the tail survives sharding.
   Update 2026-07-29: the sharded tail is measured and it is not the vector phase — cells run
   2–3 min overlapped mid-build; the real tail is the z15 depare residue (top item), which is
   memory-bound, not box-class-bound. Likely moot once the priority bands land; re-check then.
6. **`ST_CoverageSimplify` prototype on the Stockholm fixture** — elevated by the 2026-07-27
   profiling: wagyu hole placement measured 60–99% of bundle CPU on ring-dense tiles, so a
   coverage-safe pre-generalization per zoom tier attacks the dominant remaining cost. Batch step
   feeding the existing FGB → tippecanoe path; no serving change.
7. Bigger or gated, in order: LERC_ZSTD mosaic re-encode (~halves the store; needs the shallow
   pre-bias to keep charted ≤ true), pre-baked overzoom leaves (traffic-gated Worker CPU-ms cut),
   Planetiler spike on one dense cell's depare (insurance against the felt fork's bus factor),
   maplibre-contour (chart-grade smoothing/bias questions unresolved).

8. **fork_window outputs → NVMe scratch** (recommended for the next dispatch; mechanism settled
   2026-07-29): do it as a directory bind like the landmask masks — build.yml bind-mounts a
   box-local dir over `store/window/` before the build — NOT as a path change. The bind keeps
   the canonical path, so the DAG is untouched and it can land at any time without a rebuild;
   the dir dies with the box, and a resumed run re-derives windows for stems whose forks hadn't
   finished (pure derivations, ≤~38 min each). Originally parked as a small saving, but run
   30417069133 re-priced it: the window transient peaked at **295 GB / 99 files** on the volume
   (1.8 of 2.0 TB, 250 GB free) because the scheduler's small-job bias lets windows outrun
   their fork consumers — the bind moves that whole transient (and its Ceph round-trips) to
   box NVMe. Land it together with the tail item's priority bands, now that the novel bundle
   phases have a validated planet run behind them.
9. **Single-pass dual-ladder gdal_contour** (noted 2026-07-28): contour runs two full-window
   `gdal_contour` passes (metre + feet ladders); one combined-level pass with features
   duplicated onto both `sys` tags at shared levels saves ~2–4 min per z15 stem of raster
   scanning. Fiddly level-membership mapping; only worth it if the window band stays the
   per-stem long pole.

Ops note, not build work: apply for Cloudflare Project Alexandria credits (likely qualifies;
removes the serving-cost question).
