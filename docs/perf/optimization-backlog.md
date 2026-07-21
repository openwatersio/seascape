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

Ranked by expected impact on the cold planet build. No clean end-to-end mosaic number exists yet:
the first planet attempt reached 82% of 3,244 tiles in ~35 minutes on a ccx63 before an fd-limit
crash, and its resume then serialized the stranded z14 heavies for hours — the priority-inversion
tail the ×1000 fix targets, unverified until the next full-planet dispatch. The dominant remaining
cold costs are vector bundling and the pathological DEPARE tail.

### Bound DEPARE and re-enable it

Still the worst pathological tail, and the planet bench corpus relocated it: the worst stems are
**coarse**, not the dense z14 coast — `6-21-22-9` (cz9) **32,126 s = 8.9 h** single-core, `6-19-18-9`
5.2 h, `6-18-19-8` (cz8) 2.3 h; the densest NY z14 stem `8-75-96-14` measured 5,425 s (90 min) vs
208 s through the legacy path. Low RSS (2–4.7 GB) at huge wall = GEOS grinding on continent-scale
land/water unions in coarse windows. The stage-3 `depare` rule ships behind `SKIP_DEPARE` until the
unbounded GEOS operation gets a spatial/window bound or a per-tile timeout with honest failure
reporting. Success: every covering tile finishes DEPARE within an explicit bound; a planet build
publishes a complete DEPARE bundle. Effort: medium. Risk: medium.
Execution plan: [../plans/2026-07-21-depare-perf.md](../plans/2026-07-21-depare-perf.md).

### Vector bundling — the dominant cold cost

Legacy baseline: soundings + contour Tippecanoe + tile-join = **7h01m, 62%** of the 11h19m build,
at 1-6 cores on a 48-core box. Two measured results to build on:

- **Unified invocation** (contours + soundings in one named-layer Tippecanoe, no join): planet-scale
  benchmark saved 25m57s (22.8%) — semantically exact on the 16-stem sample, but under the 30%
  adoption gate. Preferred command shape, not the full fix.
- **Sharding experiment (not yet run):** spatially partition the unified inputs into 4/8 balanced
  Tippecanoe shards on NVMe, merge into one archive; adopt only if total wall < 4,775 s (30% under
  legacy) with identical addressed tiles, per-layer counts, and canonical hashes.

First Snakemake planet run (2026-07-20) measured the tail live: `soundings_bundle` ran ALONE for
**2 h 20 m** (20:16:11–22:36:25) at 0.2 cores / 5 MB/s (single-threaded tippecanoe feature read)
on an otherwise idle 48-core ccx63, and `vector_bundle` (contour tippecanoe, 2,450 inputs) only
then started — serial **by data dependency**: soundings.pmtiles is a vector_bundle input (the
join folds the soundings layer in).
Leads, roughly in order: per-layer archives (below) makes the bundles independent and
parallel; `--read-parallel` on line-delimited input attacks the single-threaded read phase;
sharding attacks the tiling phase.

**Publish hash pass — resolved 2026-07-21:** the no-op publish re-hashed the whole tile store
(~40 min); `_HashCache` (mtime_ns+size keyed, store/mosaic/hash-cache.json) now answers
unchanged files instantly. **Coarse renders — mostly resolved by the per-stem VRT** (measured
run 29795677492 vs the GTI-era corpus): cz9 731→164 s, cz10 592→45 s, cz13 132→16 s. Remaining:
the cz<8 planet-COG readers (z5 anchors up to ~90 min) still read through the GTI, and cz8
regressed 83→257 s (n=9; now decimating z14 tiles 64× through the VRT — check whether the
GTI fall-through was the better path for cz8).

Both may be mooted by the plan's **per-layer archives** direction (contours/depth-areas/soundings
as independent pmtiles, first post-migration change): that deletes the tile-join entirely and lets
layers bundle concurrently as ordinary rules. Decide per-layer vs sharding before investing in
either. One DEPARE FGB makes Tippecanoe 2.79 fail Wagyu cleaning (exit 106) — preserved as a
correctness fixture; resolve before any combined three-layer archive.

Keep the operational lessons regardless: Tippecanoe tmp + output on NVMe (never Ceph), minute-level
heartbeats (elapsed, CPU, RSS, IO deltas, deleted-open tmp bytes, disk headroom) around every long
subprocess.

### Reintroduce windowed contours

Now unblocked: the engine lane has no contour cache keys to preserve (plain names, force-only
code), which was the sole reason the working implementation was reverted. It reduced the dense
contour subprocess from ~14 GiB to ~1 GiB per block at ~1.4× CPU; the implementation is in git
history (`6e5fb55`, reverted by `48e4d77`). Success: bounded memory on the densest contour tile, no
seam gaps or fragment kinks. Effort: small. Risk: medium (seam verification).

### Publish overlap and large-object upload tuning

When the stage-3/publish rules land: uploads are per-product rules that overlap compute in the DAG
(the old stage-hook design is obsolete — rules are the hooks), with pointer-last ordering intact.
Two measured knobs carry over:

- Many-tile copies already sustain ~201 MB/s (peak 542) with `--transfers 32`.
- The single planet-z8 BigTIFF crawled at **25 MB/s** on one rclone transfer: tune
  `--s3-upload-concurrency` + explicit `--s3-chunk-size` (memory ≈ concurrency × chunk) for
  `copyto`/large objects only. Target ≥2× without regressing tile copies. Retain
  `--stats-log-level NOTICE`.

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
is a separate dense region). Weekly volatile refreshes dirty these same tiles — the heavy tail is
steady-state, not a one-time cost. Densest stems by merge-input files: 8-73-99-14 (184),
8-77-95-14 (171), 8-75-96-14 (158), 8-76-95-14 (157), 8-73-101-14 (141). Fresh per-tile peaks now
come from the per-run `bench/mosaic/*.tsv` (the benchmark artifact) on every run.
