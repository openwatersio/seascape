# DEPARE re-enable & performance — planning doc

_Written 2026-07-21. Point-in-time; the code is the source of truth. Owner of record for the re-enable work per [../perf/optimization-backlog.md](../perf/optimization-backlog.md) ("Bound DEPARE and re-enable it"); this doc is that item's execution plan._

## Problem

The stage-3 `depare_tile` rule ships behind `SKIP_DEPARE` ([build.smk:239](../../pipelines/build.smk#L239), [build.yml:54](../../.github/workflows/build.yml#L54), Justfile default), so production has no depth-area layer at all. The planet bench corpus (`bench/depare/*.tsv` from the 12-h planet run) shows where the time actually goes, and it is **not** the dense z14 coast the backlog note assumed — the tail is coarse stems:

| stem | child_z | wall | max RSS |
| --- | --- | --- | --- |
| `6-21-22-9` | cz9 | **32,126 s = 8.9 h** | low (2–4.7 GB band) |
| `6-19-18-9` | cz9 | 5.2 h | ” |
| `6-18-19-8` | cz8 | 2.3 h | ” |
| `8-75-96-14` (densest NY z14) | cz14 | 5,425 s = 90 min | ” |

Low RSS with enormous wall is the signature of pure GEOS grinding, not raster size — which both strengthens hypothesis H-B below and **relocates it**: the killer is continent-scale OSM land/water unions across z6-anchor windows (a cz9 stem's buffered window spans a huge geographic extent of land/water geometry), not NY-harbor vector density.

The second thing to explain is the same-stem regression: the legacy 2026-07-12 profile measured the whole depare fork on `8-75-96-14` at **208 s** (`gdal_contour -p` m+ft = 166 s, `union_all` = 30 s, `make_valid` = 7.6 s); the windowed path measures **5,425 s** on the same stem — a 26× regression. The sections that post-date the legacy profile are the drying/nodata land∖water algebra. The suspects, in code order in `_depare_dem` ([depare_run.py:144](../../pipelines/depare_run.py#L144)):

- **H-A `gdal_contour -p` ×2** at native window res (the legacy co-leader, but bounded: ~3 min measured).
- **H-B monolithic GEOS algebra — the prime suspect, in its coarse-window form**: `valid_union` over the full-window land and water masks, then `bucket.difference(land_geom)` / `bucket.intersection(water_geom)` / the nodata pass's per-feature `difference(subtract)`. On a cz8/cz9 window the land/water bbox reads pull continent-scale OSM geometry, and unary ops over that are the classic super-linear GEOS tail — consistent with the corpus (coarse stems 9× worse than the densest z14, low RSS throughout). These sections are the [2026-07-07-inland-water.md](2026-07-07-inland-water.md) Part 3/4 work (merged as PR #63, `d9cbda0`), which post-dates the 208 s profile — that plan's partition contract (bands ∪ drying ∪ nodata pairwise disjoint, nodata `kind`-carrying and drval-free, seam-matching edges) is frozen semantics any fix must preserve.
- **H-C the per-row Python band loop**: `make_valid(r.geometry).intersection(clip)` per band row, tens of thousands of rows.
- **H-D smooth-at-read + window assembly**: shared with contours/soundings, which finish in minutes — unlikely to be the 65-min term, but the profile should price it so it's excluded honestly.

## The shape of the work

Four phases, strictly gated: profile locally on a bbox → fix and re-measure locally → validate on a box bbox via build.yml → planet build, fix the measured tail, flip the default. No remote dispatch until the local number moves (the 2026-07-12 rule, kept). Every fix carries its correctness gate — DEPARE is chart data; bias-shallow and seam discipline are not negotiable for wall-clock.

### Phase 1 — local bbox profile: attribute the 65 minutes

Method mirrors the proven 2026-07-12 pass, but simpler — `depare_run.py tile <stem>` is now a single-process CLI, so no Pool workarounds:

1. **Stems**: `6-21-22-9` (the 8.9 h worst case — the number that decides the planet run), `8-75-96-14` (the legacy-profiled stem, for the direct 208 s → 5,425 s attribution), and one cheap stem as a control. Both problem shapes — coarse-window GEOS extent and dense-coast vector density — must be in the profile, because a fix tuned on one can miss the other.
2. **Inputs local**: `rclone copy` each stem's intersecting mosaic tiles (`mosaic.intersecting_tiles`, a handful of COGs) plus `land.fgb`/`water.fgb` into the store — network eliminated, same rule as last time. R2 tiles are content-addressed (`<stem>-<hash12>.tif`): map hash names back to plain `store/mosaic/tiles/<stem>.tif` via the released index parquet's `location` column, or the local store won't resolve them.
3. **Instrument**: coarse per-section wall prints in `_depare_dem` (partitions-m, partitions-ft, band clip loop, land read+union, water read+union, drying difference, nodata pass, FGB write) — ~8 `time.monotonic()` lines, kept behind a `DEPARE_TIMING` env so they can ship. Then one run under cProfile for the shapely/GEOS call-site attribution (py-spy needs root on macOS; cProfile + the section timers sufficed before).
4. **Record**: findings table (section × wall × peak RSS via `/usr/bin/time -l`) appended to this doc; artifacts under `pipelines/store/profile/` (gitignored) like last time.

**Exit gate**: ≥90% of the stem's wall attributed to named sections; one hypothesis confirmed. Fix nothing until then.

### Phase 2 — fix locally, verify locally

Candidate fixes, ranked by expected mechanism — the profile picks which get implemented; each is measured on the same stems before/after:

1. **If H-B (GEOS algebra) — the expected winner**: bound every union/difference operand. Two sub-options, cheapest first:
   - **Subdivide + index**: grid-subdivide the land/water masks into bounded-vertex pieces at read (shapely `STRtree` to select only pieces touching the bucket; per-piece `difference`, union the survivors). Turns one O(huge) GEOS call into many O(small) ones — the ST_Subdivide pattern.
   - **Raster gate**: the code comment already states the target semantics ("matching the raster gate — rasterize burns land=1 then water=0"). Rasterize effective-water onto the window grid and gate the drying bucket in pixel space, polygonizing once. Bounded by pixels regardless of OSM vertex count — and the coarse-stem relocation raises its stock: a cz8/cz9 window has *coarser pixels over more geometry*, exactly where pixel-bounded beats vertex-bounded, so weigh it against subdivision up-front rather than as the fallback.
2. **If H-C (band loop)**: vectorize with shapely 2 array ops (`shapely.make_valid` / `shapely.intersection` on the whole GeoSeries, `gpd.clip`) — small, composable with 1.
3. **If H-A (`gdal_contour -p`)**: accept it — it's bounded and native-res partitioning is the product's resolution. No decimated-overview cutting: `average`-resampled overviews are not bias-shallow, so cheaper geometry there would violate the chart invariant.
4. **Backstop regardless of hypothesis**: a per-tile wall-clock bound (`DEPARE_TIMEOUT`, generous — e.g. 3× the post-fix worst stem) that kills the job with a non-zero exit and a loud log line. Honest failure, never a silently-empty `.fgb`. This is the backlog's "explicit bound": after the fix, the timeout is the tripwire proving the bound holds planet-wide, not the fix itself. One interaction to decide here: `depare_tile` has `retries: 2` with attempt-scaled memory, so a timed-out stem burns 3× the timeout before failing — and since the failure mode is wall-clock at low RSS, memory escalation can't rescue it. Either keep the timeout tight enough that 3× is tolerable, or scale `DEPARE_TIMEOUT` *down* with `attempt` so retries fail fast; don't leave it to default to 3× a generous bound.

Correctness gates, all on the profiled stems before anything ships:

- **Decoded A/B vs pre-fix output**: identical `drval1/drval2` band sets, per-band polygon count and total area within tolerance (geometry may legally differ in vertex order/splits; content may not).
- **Seam check**: `seam_check.py check_depare` across a bbox spanning ≥4 mosaic tiles including a hi-res/GEBCO boundary.
- **The Wagyu exit-106 fixture**: the preserved DEPARE FGB that makes tippecanoe 2.79 fail cleaning must pass through `depare_run.py bundle` — a planet run that completes every tile and then dies in the bundle is a failed re-enable. Resolve (tippecanoe version bump, geometry pre-clean, or feature split — whatever the fixture points at) in this phase, not discovered at planet scale.
- `test_engine.py` green; `SLIVER_MIN_PX` / drying semantics untouched unless a fix explicitly targets them.

**Exit gate**: an explicit target for **both problem shapes** — `6-21-22-9` (coarse worst, 8.9 h today) **and** `8-75-96-14` (dense worst, 90 min today) each **≤5 min single-core**. A z14-only gate doesn't save the planet run; the coarse stem is the one that decides it. Revise with evidence if the profile says the floor is higher, but write the revised numbers here first.

### Phase 3 — box bbox via build.yml

1. **Clear stale depare state first — this is a correctness step, not hygiene.** The volume's `store/depare/*.fgb` and `store/bundle/depare.pmtiles` predate `SKIP_DEPARE`, and code is force-only: any stem whose mosaic inputs are unchanged would silently ship old-code geometry on re-enable. Delete `store/depare/*` + the stale bundle on the box (or `-R depare_tile` once) before the first depare-enabled dispatch, and again at phase 4 if phase 2's fixes land in between.
2. **Make the switch dispatchable**: build.yml currently hardcodes `SKIP_DEPARE: "1"`. Add a `depare` boolean workflow input (default false) that clears it — planet-safe default preserved, no branch-only workflow copies needed; composes with the existing `targets` handling.
3. **Dispatch a NY-harbor bbox smoke** on ccx33 with `depare: true` (one dispatch at a time; arm the run monitor per standing practice). The mosaic is warm, so this run is essentially stage-3 + bundles — depare's cost is legible, not buried.
4. **Read the evidence** (bench + logs artifact, unprompted analysis per standing practice): per-stem wall/max-RSS from `bench/depare/*.tsv` vs the local numbers (Ceph-volume and streamed-mask deltas show up here), `DEPARE_GB` reservations vs measured RSS, `depare_bundle` wall and the tile-join delta with depare folded in. **The reservation re-fit must span child_z, not just z14**: the corpus already shows cz8 RSS at 4.7 GB against the current `DEPARE_GB` default of 3, and the z14 table value (6) rests on n=1 — fit per-zoom from measured max + ~10%, priced from each zoom's densest tile.

**Exit gate**: bbox run green end-to-end with depare enabled, no reservation breach, box wall within ~2× local per-stem wall (if worse, the volume/streaming term needs its own look before planet).

### Phase 4 — planet build, fix the tail, flip the default

1. **Dispatch the planet build with `depare: true`** on ccx63. Depare jobs are memory-cheap windowed reads, so they should ride the saturation backfill alongside contours/soundings — the marginal wall cost should be near zero if the fix held; the bound (timeout) is armed planet-wide.
2. **Analyze**: full `bench/depare/` distribution (p50/p99/max wall and RSS by child_z), timeout hits (each is a named defect, not noise), run-timeline check that depare stragglers don't widen the idle tail, `depare_bundle` + tile-join cost at planet scale.
3. **Fix what the tail shows**: worst-stem fixes or reservation re-fits, re-dispatch if a fix changes geometry (correctness gates from phase 2 re-run on affected stems).
4. **Flip the default**: remove `SKIP_DEPARE` from build.yml env and the Justfile default (keep the env knob and the `DEPARE_STEMS` guard — a kill switch costs nothing), delete the "behind SKIP_DEPARE" comments, close the backlog item with the measured numbers, and verify the depare layer serves through the Worker on the next release (the style's `DEPARE_LADDER_M` already consumes it — the dev-only Shading control's production gap closes with this).

**Exit gate** (the backlog's success criteria, now measurable): every covering tile finishes DEPARE within the explicit bound; a planet build publishes a complete DEPARE bundle; the layer renders in production.

## Findings — phases 1–2 (2026-07-21)

*Local baselines on Apple Silicon (10-core, local NVMe), all inputs local: 24 mosaic tiles refetched from the candidate index `8b80337d2e2f` and renamed to plain stems; published planet masks (`water.fgb` 16 GB, `land.fgb` 1.3 GB, both 2026-07-18 — the pair the box's bench corpus ran against). `DEPARE_TIMING=1` section marks in `_depare_dem`; logs in `pipelines/store/profile/`.*

### Phase 1 — attribution (baseline)

**`8-75-96-14` (dense NY z14): total ≈ 2,404 s, of which `nodata-loop` = 1,968 s (82%).** **`6-21-22-9`** wall 3,491 s but CPU-contended — the honest number is user 1,994 s with `nodata-loop` 1,985 s (~94%); the wall/user gap was the user's other processes. **`6-19-18-9`** total 864 s with `nodata-loop` 754 s (87%).

`8-75-96-14` sections, in seconds: window-dem 36 · smooth 92 · gdal_contour-m 117 · read-m 0.2 · bands-clip-m 3.0 · gdal_contour-ft 65 · read-ft 0.1 · bands-clip-ft 2.7 · coverage-union 15.3 · water-read 0.1 · water-union 6.8 · drying-bucket-union 64.4 · land-read-union 0.4 · drying-diff-land 0.7 · drying-water-terms 9.6 · drying-make-valid 0.2 · drying-emit 0.8 · nodata-subtract-union 21.3 · **nodata-loop 1,968** · write-fgb 2.2.

Verdicts: **H-A refuted** (both `gdal_contour -p` passes total 182 s — consistent with the legacy 166 s; the polygonize didn't regress). **H-C/H-D refuted** (band clips ~6 s; window+smooth 128 s). **H-B confirmed, mechanism narrowed to the nodata pass**: per-water-feature `difference` against ONE monolithic coverage∪drying union, every feature paying the whole window's vertex count regardless of locality. The 16 GB planet `water.fgb` is NOT a cost — the FGB spatial index makes the bbox read 0.1–0.4 s. Coarse-stem #2's water-union is the cz9 term: `water-union` 68–82 s.

### The fix (and the failed intermediates)

The intermediates carry the design constraints, so all are recorded:

- **v1** — STRtree envelope query + per-feature union of hit parts: **10× regression** on `6-19-18-9` (754 → 7,094 s). It re-unioned unbounded parts per feature, and envelope queries never prune when a coastal band's envelope spans the whole window.
- **v2** — `predicate="intersects"` + sequential pairwise differences: fixed the coarse stems (754 → 191, 1,985 → 266 s) but not dense z14 (1,968 → 1,841 s). In a harbor every feature truly intersects, and each difference still pays the giant band polygon's full vertex count.
- **v3** — v2 plus `_subdivide` (recursive envelope bisection of any part over 512 vertices, then per-feature union-of-hits + one difference): fast everywhere (`nodata-loop` 13.2 / 10.4 / 10.2 s) but produced 3 phantom nodata rows over banded water on `6-21-22-9`. A GEOS 3.13.1 floating-point OverlayNG failure: `lake∩union(pieces)` returned 4.5e-11 m² while pairwise `part∩lake` returned 0.96 km², arbitrated by point-in-polygon (both the part and the union `.contains` a lake interior point — the union-operand overlay was wrong, pairwise right).
- **v4/v5** — pairwise differences to dodge the bug: correct but slow on dense z14 (`nodata-loop` 1,885 s — sequential differences are O(hits × |geom|)). v4 also accidentally made the drying water term pairwise, which is quadratic because the BUCKET is the unbounded operand there (912 s / hung 2.5 h — reverted).
- **v6/v7 FINAL SHAPE** — union-of-hits + ONE fixed-precision overlay: `shapely.union_all(hit_parts, grid_size=1e-6)` then `shapely.difference(geom, u, grid_size=1e-6)`. Snap-rounding is GEOS's robust overlay mode and fixes the failing lake outright (diff → 0 at every grid size tested); 1 µm in a metres CRS moves nothing cartographically. The drying water term stays FLOAT (one `bucket.intersection(union_all(near))` — byte-exact on every stem, and the bug never manifested there); `grid_size` is scoped to the nodata loop only, where it is required.

### Verified results (v7, all-fresh outputs, no contention)

| stem | baseline | v7 wall | v7 nodata-loop | box corpus |
| --- | --- | --- | --- | --- |
| `8-75-96-14` | 2,404 s | 366 s | 14.7 s | 5,425 s |
| `6-21-22-9` | ~2,010 s (user) | 84 s | 11.7 s | 32,126 s |
| `6-19-18-9` | 864 s | 102 s | 12.2 s | 18,730 s |

RSS 1.4–2.6 GB. Equivalence: bands byte-exact (rel 0.00e+00) and drying byte-exact on all three; the nodata deltas (−33 / −45 rows, rel ≤1.85e-3 at cz9) are fully arbitrated — zero rows inside bands or drying (disjointness holds), and a 5-interior-point probe of the 30 largest baseline-only regions (93–95% of the net area delta) shows 30/30 still covered by v7 nodata: a pure merge/split reshuffle plus threshold-sliver trimming, no lost coverage. `ab_depare.py`'s nodata tolerance is set to 2e-3 to match the invariant (band damage is caught by the exact band gate, not the area number).

### Phase 2 exit gate (revised per the plan's own rule)

Revised numbers first: coarse stems **≤5 min MET** (1.4 / 1.7 min). Dense z14 is 6.1 min total, of which depare-specific logic is now ~100 s — the remaining ~4.5 min is the shared stage-3 floor (window-dem 35 s + smooth 94 s + two `gdal_contour` passes 136 s + drying-bucket-union 54 s), so the **revised dense-stem gate is ≤8 min wall** with the floor documented. `DEPARE_TIMEOUT` sizing in the next phase keys off these numbers (3× worst ≈ 25 min planet-wide bound).

### Still owed before phase 3 (unchanged from the plan body)

`seam_check.py check_depare` over a ≥4-mosaic-tile window, the Wagyu exit-106 fixture through `bundle`, `test_engine`, and the `DEPARE_TIMEOUT` backstop; then the build.yml `depare` input + stale-state deletion.

## Interactions, named

- **Per-layer archives** (the first post-migration change per the snakemake plan): when it lands, `depare.pmtiles` stops riding the tile-join and the Wagyu fixture stops gating the *combined* archive — but phase 2 resolves the fixture anyway, since the re-enable shouldn't wait on that refactor and a standalone depare archive hits the same tippecanoe.
- **Drying-geometry / depth-below-water plans** touch the same `_depare_dem` sections. Perf fixes here keep semantics frozen; if either plan lands mid-stream, re-run the phase-2 gates on its diff rather than interleaving the changes.
- **Windowed contours** (backlog): if H-B's subdivision work produces a shared bounded-mask helper, contours' reintroduction can reuse it — note it, don't couple the PRs.

## Non-goals

- Changing DEPARE levels, drying semantics, sliver thresholds, or any cartographic output — this is a performance pass with equivalence gates.
- Decimated/overview-cut depth areas (violates bias-shallow; named above as rejected).
- Finer aggregation tiles (the backlog's named last-resort fallback; only if windowing + bounding both fail).
- Bundling-pipeline restructuring beyond what re-enabling requires (per-layer archives is its own item).
