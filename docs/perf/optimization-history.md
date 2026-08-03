# Build optimization history

Historical record: incident evidence, resolved items, and discarded hypotheses. Current decisions
and ranked work live in [optimization-backlog.md](optimization-backlog.md). The former
`aggregate-tile-peaks.md` was consolidated on 2026-07-18 — its surviving density tables moved to
the backlog; its memory findings are recorded (and corrected) below.

## Resolved by the Snakemake migration (2026-07-18)

- **The merge memory mystery.** Per-process attribution on the densest S-102 stem overturned the
  long-standing `warp_mixed` hypothesis: `gdalwarp` peaked at 1.27 GiB; the real driver was
  `negate_band1` reading the full 32,830² band + masks, with glibc retaining the high-water for the
  process's life (the box's 12-15 GiB rows were that footprint unreclaimed). Windowing the negate
  dropped the merge-only job to **1.74 GiB container / 3.9 GiB box** and cut 27% wall. The S-102
  file-count "driver" correlation was an artifact of which tiles negate streamed sources.
- **Admission scheduling.** `scheduler.py`'s budget/cheap-lane machinery is replaced by engine
  scheduling with benchmark-fit reservations; the planet tile phase ran **82% of 3,244 tiles in
  ~35 min at ~79% mean CPU** on ccx63 before an fd-limit crash. `AGG_MEM_FACTOR=4` is
  legacy-lane-only and dies with it.
- **Vector forks decoupled from the merge** (the plan's 5c): contour/soundings/depare read buffered
  mosaic windows through per-job VRTs; a fork change reruns only that fork.
- **Explicit cache versions / key discipline**: superseded by engine provenance (inputs + params)
  with code force-only; `keys.py` survives only as publish-time hashing.
- **Independently runnable build stages**: delivered by the DAG itself (every product a named
  target) rather than a workflow split.
- **Coverage in the sources cadence** and **host-metrics capture**: shipped and live in both lanes.
- **Stage-level peak dataset**: superseded by per-job `benchmark:` TSVs on every run.
- **Two new operational findings**: the Snakemake parent needs a raised fd limit at planet width
  (`--ulimit nofile=65536`, Errno 24 at ~96-wide); and small `priority:` values lose to the
  scheduler's count-maximizing packing — priorities must be banded orders of magnitude apart
  (masks 10M > mirrors 5M > byte-weighted preps; merge tiles ×1000) or heavies strand into a
  serial tail, observed twice (the second: the resume run serialized its stranded z14 heavies for
  hours).

## Resolved 2026-07/08: the z15 scheduling tail (run 30417069133)

Run 30417069133 measured the mechanism end-to-end. `--prioritize mosaic_index` set its whole
dependency closure — every merge — to uniform maximum priority (job logs printed
`priority: highest`), erasing the heavy-first `tile_priority` interleave within the merge class.
Under uniform priority the scheduler's knapsack maximizes the **sum** of priorities that fit the
memory budget, so many small jobs always outscore few big ones: hundreds of 2–3 GB coarse merges
ran ahead of the 9 GB z15 merges (last z15 merge landed ~2.5 h in), the z15 fork chain trailed
behind its neighborhoods, and the same bias inside the vector band ran windows/contours ahead of
depare. End state: a ~2.5 h tail of z15 depare draining 6-wide (`DEPARE_GB[15]` = 24 → only
6 × 24 fit in 161 GB) at load 10–25 on 48 cores. The same bias let fork windows outrun their
consumers 99-deep — a 295 GB `store/window` transient, volume peak 1.8 of 2.0 TB.

Fixed by two mechanisms, both in build.smk:

- **Explicit rule bands** replaced `--prioritize mosaic_index`: `INDEX_BAND` (3M) >
  `MOSAIC_BAND` (2M, heavy-first `tile_priority` interleave intact within it) >
  `VECTOR_BAND` (1M) > terrain. z15 merges start at t=0 and their fork chains overlap the
  coarse-merge flood instead of trailing it.
- **Fitted per-stem depare reservations** (`depare_gb`): fit from the stem's own mosaic-tile
  size (run 30634360224, 2,170 rows), floored per child_z at the class median. The interim
  "re-fit `DEPARE_GB[15]` 24 → ~15" idea (from n=25 planet peaks, mean 10.5 / max 13.7 GB) was
  first reversed by the post-contour-fix marsh peaks (~29 GB on `8-63-105-15`; the pre-fix
  corpus had never sampled the expensive stems, which died in `gdal_contour -p` before the
  GEOS phase), then superseded by the fit. Run 30641774632 ran 21 stems past their
  reservations with zero failures and the best utilization measured.

The `store/window` transient is not fully bounded by the bands; the fork_window NVMe bind that
absorbs it is still in the backlog's intake list (item 7).

## Resolved 2026-08-02: shipped with the native-resolution release (v0.3.0)

- **Depth areas re-enabled end-to-end** (execution record: docs/plans/2026-07-21-depare-perf.md). The bounded nodata pass (STRtree true-intersects prefilter + subdivision of parts over 512 vertices + one `grid_size=1e-6` snap-rounded difference), halo-scaled window buffers, the contour `ogr2ogr -clipsrc` → shapely-clip port, the drying pre-clip area gate, and the `ON_SEAM_PIXELS = 1e-3` fix to `check_depare`'s grazing-coverage false positive all shipped; all depare bands, drying, and nodata pass the seam gate (≤2.7e-7°). The one deferred piece (deep-contour seam tolerance in `check_contours`) stays in the backlog.
- **Windowed contours, resolved at the real peak (2026-07-28).** The 72 GB z15 peak was never the gdal_contour subprocess (scanline-streamed) but the post-extraction GeoDataFrame phase holding three full copies of the window's contour set. The refine (enrich → Chaikin → ring-drop → clip) streams 100k-feature batches to an appended GeoJSONSeq (9.9 GB / 6–9 min at z15, was 72 GB / 24 min), and the shared `fork_window` rule builds each stem's smoothed+coarsened window once instead of three times (deep_coarsen strip-streamed, byte-identical by differential test; in-process GDAL block cache capped — the 5%-of-RAM default was the last hidden multi-GB term). The `6e5fb55` block-windowed gdal_contour idea is moot.
- **GTI native-resolution regression check.** The `.gti` carries explicit `<ResX>/<ResY>` at the covering's finest resolution; validated in the v0.3.0 candidate QA (same-viewport dev/build comparisons plus decoded tiles from z5 to z15) and shipped.
- **Marsh-coast coarsening.** Drying coverage-union + band coverage-simplify to the S-58 floor (4.7×) and fork-window pond fill (11.6×); block-max refuted — part count, not vertex count, drives the GEOS cost. The z15 marsh class completes in production under the fitted reservations.
- **Vector chain validated end-to-end.** The first complete planet build census-verified every cell archive against its fork sidecars (94.1 M feature ids, fringe drops accounted) and proved the `pmtiles merge` concat by tile-count identity — ~10 s at planet scale vs tile-join's 4 m 46 s on the same inputs.

## Complete run 29518661202 performance baseline

Run 29518661202 completed in 11h18m54s on a 48-core, 184 GiB ccx63. The workflow resource sampler
produced the following critical-path breakdown. CPU figures are cores in use, not percentages.

| Stage | Wall time | Avg / median / max CPU | Peak memory | Interpretation |
| --- | ---: | ---: | ---: | --- |
| Coverage / no-op aggregate | 35m48s | 1.0 / 1 / 2 | 4 GiB | Warm-build serial gate; 99% of samples used at most one core. |
| Local mosaic index | 24m02s | 2.4 / 1 / 12 | 5 GiB | Long serial prefix, then modestly parallel planet-z8 work. |
| Terrain render | 2h36m03s | 31.0 / 24 / 48 | 38 GiB | Only phase that substantially utilized the ccx63. |
| Mosaic publish | 28m20s | 1.0 / 0 / 4 | 4 GiB | Network-bound critical-path transfer. |
| Terrain bundle/upload | 3m17s | 1.1 / 1 / 2 | 7 GiB | Small tail. |
| Soundings bundle | 2h44m55s | 1.1 / 1 / 15 | 6 GiB | Tippecanoe was active but overwhelmingly serial. |
| Contour Tippecanoe + tile-join | 4h16m26s | 6.4 / 2 / 28 | 9 GiB | Serial input/sort, short parallel burst, then serial archive rewrite. |
| Final vector upload/manifest | 7m03s | 0.1 / 0 / 1 | Transfer tail. |

Soundings plus contour/tile-join consumed about 7h01m, or 62% of the build. Even eliminating terrain
entirely would leave roughly 8h43m, so aggregate rendering is not the first cold-path target. Swap
was never used. For most of the run the 184 GiB box held under 10 GiB; terrain averaged about
20 GiB and peaked at 38 GiB.

The Hetzner API capture available for this run spans 17:10 UTC through 00:57 UTC, not the full build,
so it covers only the first 50 minutes of the contour phase. Within that window it confirms terrain
at about 32 cores, mosaic publication near 100 MB/s average outbound, and low CPU everywhere else.
Do not use it to infer the provider-level IO profile of the later tile-join.

SSH inspection during soundings found one Tippecanoe process consuming 4,956 GeoJSON files totaling
20 GiB. A 30-second `/proc` sample showed about 52 MB/s logical reads and 143 MB/s logical writes,
but only about 14 MB/s physical writes; the process was progressing through a serial algorithm and
temporary shuffle rather than waiting on raw disk reads. It held deleted `/tmp/geom*` files, making
ordinary `du` under-report temporary space. The attached volume then had 125 GiB free while NVMe
had 791 GiB free, supporting NVMe-first temporary and output work.

## Regional benchmark 29578999167

The Bay Area ccx33 benchmark completed successfully in 29m36s of build/publish time. Aggregation
dominated at 23m24s: four z13 tiles ran serially because the legacy scheduler always reserved a
16 GiB cheap lane even with no cheap tasks pending (later fixed). Terrain rendered 35 stems in
5m08s. The region is too small to benchmark the planet vector bottleneck (soundings and contour
Tippecanoe each took 4s); a representative benchmark needs substantially more persisted planet
vector inputs without replaying aggregation.

## Convergence and storage incidents

- Early runs did not publish a manifest, so R2-based resume had no trusted pointer and restarted
  from scratch. Moving the store between NVMe, a persistent volume, and staged layouts also made
  completed artifacts unreachable.
- A persistent Hetzner volume finally made interrupted progress durable. The 750 GiB ext4 filesystem
  later filled completely. Growing the Hetzner block device to its 1 TiB maximum was insufficient
  until `resize2fs` expanded ext4 online. Commit `89f0525` made that boot-time resize idempotent.
- Run 29506912668 then reported 7,197 source files across all 3,163 tiles dirty. Key diagnostics on
  run 29508747331 showed mosaic, soundings, DEPARE, toolchain, masks, inputs, and configuration all
  matched; only contour differed. Windowed-contour commit `6e5fb55` changed `contour_run.py`, a key
  determinant, and the coupled planner consequently dirtied every tile.
- Run 29508747331 was canceled because publishing new contours superseded reusable old siblings.
  Commit `48e4d77` restored the old contour implementation byte-for-byte. Run 29510297215 then
  dropped to 736 source files across 16 dirty tiles, confirming that the volume retained nearly all
  planet work.
- Those 16 tiles completed. The same run failed at local mosaic indexing because planet-z8 exceeded
  classic TIFF's 4 GiB limit. Commit `e42639a` injects `BIGTIFF=YES` only for mosaic-index.
- 2026-07-18: the legacy derived state was retired from the volume (production serves from R2), and
  the plain-named Snakemake lane became the producer of `store/mosaic/`.

## OOM and utilization history (legacy pool lane)

- Dense z14 coastal tiles peaked 12-14 GiB under the pool lane. `AGG_MEM_FACTOR=1`, calibrated from
  a light log window, admitted ~28 heavies and OOM-killed two builds; factor 4 was the recovery
  setting. (Root cause later found and fixed — see the negate finding above.)
- Long-lived workers retained allocator arenas, so accumulated `ru_maxrss` was sometimes attributed
  to the wrong tile; a source-count correlation derived from those contaminated peaks was
  misleading. `maxtasksperchild=1` improved measurement.
- Coverage was once estimated near 130 GiB by subtraction; direct measurement showed ~4 GiB and one
  core.
- Worker-side admission caused blocked heavy tasks to occupy pool workers, leaving cores idle.
  PR #85 moved reservation into the parent and added a cheap-task lane.
- Run 29437160867 reached 1,214/1,245 dirty tiles then stalled for hours; tile `5-10-10-9` spent
  19 h before soundings/contour printed and never finished DEPARE — the origin of `SKIP_DEPARE`.

## Workflow failures and fixes

- Self-hosted Actions jobs still inherit a 360-minute default timeout. A healthy build was killed at
  exactly six hours; the workflows set 2,880 minutes explicitly.
- `rclone copy --max-age` forced an expensive full-prefix scan and was reverted. Exact manifest or
  `--files-from` hydration is the retained rule.
- Staging sources beneath `store/aggregation` confused the covering-directory glob and produced a
  false `nothing to do`; staging moved to a sibling NVMe directory.
- Publishing across the NVMe/volume mount boundary required a copy fallback because `os.replace`
  cannot cross filesystems.
- A queued run in the `r2-store` concurrency group is EVICTED by the next dispatch — one dispatch
  at a time (observed twice).

## Process lessons and standing constraints

- Get one build through its manifest before changing a stage that is already working; change one
  uncertain part at a time and let its run supply evidence.
- Measure before surgery: the warp hypothesis survived two docs and a backlog item until one
  per-process attribution run overturned it in an afternoon.
- Price memory from the densest measured tile with fresh instrumentation, never an average, an
  accumulated RSS, or a stale benchmark row.
- Never hydrate by scanning an R2 prefix; use an exact manifest/files list.
- Never publish pointers or `manifest.json` before every referenced artifact exists.
- Keep the 2,880-minute self-hosted job timeout until builds are comfortably shorter than six hours.
- Preserve the persistent cache until a published build proves a replacement recovery path.
