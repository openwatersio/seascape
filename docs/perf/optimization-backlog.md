# Build optimization backlog

The remaining actionable performance work under the Snakemake build
(docs/plans/2026-07-14-snakemake-build.md). The former backlog and `aggregate-tile-peaks.md` were
consolidated on 2026-07-18: the migration resolved or restructured most of that program — per-job
isolation, engine provenance, measured reservations, and per-tile `benchmark:` TSVs replaced the
hand-rolled admission scheduler, key machinery, and ad-hoc peak sampling. OOM is no longer the
build's failure mode; memory *budgeting* (reservations fit from fresh benchmarks) and *windowing*
(bounding per-job footprints) remain live concerns. Incident evidence, resolved items, and
discarded hypotheses live in [optimization-history.md](optimization-history.md).

## Remaining action items

Ranked by expected impact on the incremental (weekly-refresh) build first, cold planet second.
Run 29847332817 (2026-07-21, ccx63, first dispatch after the DAG unification) is the reference
incremental measurement: **8 h 12 m wall for a build with zero changed source data**. All
mosaic-side work (239 overlay bundles, 344 terrain renders, index, publish) finished inside the
first hour; the remaining 7 h+ was the serial `soundings_bundle → vector_bundle → stage_build`
chain on an otherwise idle 48-core box. No clean cold planet number exists yet: the first planet
attempt reached 82% of 3,244 tiles in ~35 minutes on a ccx63 before an fd-limit crash. The
dominant costs are spurious rebuild triggers, vector bundling, and the pathological DEPARE tail.

### Stop false planet rebuilds — trigger hygiene

Run 29847332817 rebuilt the world off a metadata artifact, not data: `mosaic_index`,
`soundings_bundle`, and all 239 `overlay_bundle` jobs re-ran with reason *"Params have changed
since last execution: before: `<nothing exclusive>` now: `''`"* — the params-provenance transition
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

Success: a build with no changed inputs finishes in minutes, and any planet-scale re-run is
deliberate. Effort: small. Risk: low.

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

Attack in this order:

1. **Per-layer archives** (contours/depth-areas/soundings as independent pmtiles, first
   post-migration change): deletes the 1 h 42 m tile-join outright and breaks the serial
   dependency — the two tippecanoe runs become parallel rules, wall ≈ max(2 h 56 m, 2 h 53 m)
   ≈ 3 h. Also removes the one ~20-core consumer, making the ccx33 default safe. **Wagyu
   exit-106 — resolved 2026-07-23.** The regenerated fixture (Stockholm-archipelago stem FGB
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
2. **`--read-parallel` / input format** for the ~80 min single-threaded read phase, and
   **sharding** for the low-parallelism tiling phase (spatially partition into 4/8 balanced
   shards on NVMe, merge; adopt only with identical addressed tiles, per-layer counts, canonical
   hashes). Target: each per-layer bundle under ~1 h.
3. **Incremental bundling** — the end state that makes refresh builds minutes, not hours: keep
   per-shard (or per-cell, like `overlay_bundle`) archives cached in the store and rebuild only
   dirty shards, merging cheaply at publish. The overlay bundles already prove the shape: 239 of
   them rebuilt in ~2 min wall. Sketch after per-layer + sharding land, since shard boundaries
   and the merge step are shared machinery.

Prior data points that still inform the work: unified invocation (contours + soundings in one
named-layer tippecanoe) saved 22.8% at planet scale, semantically exact on the 16-stem sample —
preferred command shape if a combined archive ever returns. Legacy baseline was 7 h 01 m / 62% of
the 11 h 19 m build.

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
boundary detail that merely *grazes* near the seam (one-sided) as coverage. Fixed with a tight
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
  (The `check_depare` false positive above was the same *class* of over-sensitive-gate issue but a
  distinct mechanism — grazing coverage vs. per-window smoothing — and only the depare side is
  fixed; contours keep `ON_EDGE_PIXELS` because a line crosses the seam at a point.)

### Reintroduce windowed contours

Now unblocked: the engine lane has no contour cache keys to preserve (plain names, force-only
code), which was the sole reason the working implementation was reverted. It reduced the dense
contour subprocess from ~14 GiB to ~1 GiB per block at ~1.4× CPU; the implementation is in git
history (`6e5fb55`, reverted by `48e4d77`). Success: bounded memory on the densest contour tile, no
seam gaps or fragment kinks. Effort: small. Risk: medium (seam verification).

### Publish overlap and large-object upload tuning

`stage_build` in run 29847332817 was a pure upload tail: **36 min** reading ~35 GB of bundles at
~16 MB/s effective, starting only after vector_bundle finished, at ~0.15 cores. Two fixes:

- Overlap: make bundle uploads per-product rules that run as each bundle lands (overlays were done
  7 h before their upload started), pointer-last ordering intact.
- Throughput: the large single objects (vector ~10 GB, soundings ~6 GB) crawl on one transfer —
  same knob as the planet-z8 BigTIFF (**25 MB/s** measured): tune `--s3-upload-concurrency` +
  explicit `--s3-chunk-size` (memory ≈ concurrency × chunk) for `copyto`/large objects only.
  Target ≥2× without regressing tile copies, which already sustain ~201 MB/s (peak 542) with
  `--transfers 32`. Retain `--stats-log-level NOTICE`.

### Memory reservation upkeep

`MERGE_FACTOR` (build.smk) is fit from measured peaks — re-fit from the per-run `bench/mosaic/`
(the run's benchmark artifact) after each planet run. Benchmarks are per-run scratch now, so the
rows are already fresh — no cross-run stale rows to filter. Per-job kernel caps (cgroup /
`docker run --memory` per job) land once reservations are benchmark-backed — a cap equal to the
reservation turns a wrong estimate into one retried job instead of a box OOM. Price reservations
from the densest measured tile, never an average.

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
- NVMe for hot reads: if host metrics show the per-tile mask rasterize contending on the Ceph
  volume, `cp -p` the masks to NVMe and bind-mount over `store/landmask` (paths and mtimes
  unchanged, so provenance is untouched). The broader NVMe-hot-store idea is superseded by the
  plan's named metadata-hydration path (Non-goals: Distribution).

### Fallback, named but not scheduled

Finer aggregation tiles (descend dense cells below the z8 macrotile floor, ~8192 px working
rasters) remain the escape hatch if windowing ever proves insufficient. Changes mosaic tiling, GTI
overview registration, and the serving pyramid — last resort. The stage-3 CLIs already treat their
tile id as a window id, so a finer stage-3 grid is a parameter change.

## Workload shape (planet covering, 2026-07-15)

Kept for reservation fitting and bbox selection (regenerate via `aggregation_covering.py`):

| metric | value |
| --- | --- |
| tiles total | 3163 |
| child_z histogram | z8:1079 · z9:735 · z10:758 · z11:81 · z12:151 · z13:181 · **z14:178** |
| files/tile | max **184** · p99 86 · median 2 |
| distinct sources/tile | max 6 · median 2 |

~84% of tiles are cheap; the heavy tail is the 178 z14 coastal macrotiles, clustered on the US
NE/mid-Atlantic coast (S-102 + CUDEM stacks, bbox `-77.344,36.598,-70.312,42.033`; `8-128-85-14`
is a separate dense region). Weekly raw-source refreshes dirty these same tiles — the heavy tail is
steady-state, not a one-time cost. Densest stems by merge-input files: 8-73-99-14 (184),
8-77-95-14 (171), 8-75-96-14 (158), 8-76-95-14 (157), 8-73-101-14 (141). Fresh per-tile peaks now
come from the per-run `bench/mosaic/*.tsv` (the benchmark artifact) on every run.
