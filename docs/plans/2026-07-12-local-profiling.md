# Local build profiling — planning doc

*Written 2026-07-12. Point-in-time; the code is the source of truth.*

## Problem

Three remote runs (two planet windows, one bbox smoke) produced zero completed planet tiles and a >70-minute harbor bbox, with diagnosis limited to coarse box metrics (CPU/network graphs) and inference. The per-tile cost is hours even with tuned streaming — but *where the time goes inside a tile* has never been measured. Remote boxes can't be profiled properly; the laptop can (more CPU than the ccx33, half the RAM: ~16 GB → 2–3 aggregate workers, or 1 for clean profiles).

## Goal

A per-stage, per-fork time/memory profile of the real pipeline over the same NY-harbor window the smoke build ran (`-74.30,40.40,-73.75,40.80`), with all sources on local disk so network is eliminated as a variable. Output: a findings table (stage × wall × CPU × peak RSS) + flame graphs, mapped to a ranked fix list. No remote dispatch until a local fix moves the local number.

## Hypotheses (each has a different fix — the profile picks)

- **H1 — Python vector ops dominate**: Chaikin smoothing / shapely per-feature work over z13–14 contour sets is pure Python. Fix: vectorize (shapely 2 array ops), simplify-before-smooth, or decimate tiers.
- **H2 — S-102 HDF5 open/read cost dominates**: hundreds of small `.h5` products per tile, each an HDF5 driver open. Fix: convert the R2 mirror to COG at mirror time (already noted as deferred in the sources-mirror plan) — this also fixes streaming.
- **H3 — merge/feather numpy over multi-GB Float32 dominates**: fix = chunked windows / dtype discipline.
- **H4 — gdal_contour / depare polygonize at native res dominates**: fix = cut from decimated tiers where the zoom ladder allows.
- **H5 — encode/pmtiles writing dominates**: fix = writer batching.

## Method

1. **Branch: `production-build-immutable` (PR #74, the stack tip)** — the code that actually ships, so the fix can't conflict with #72/#74. Runs in that branch's worktree against the shared downloaded store (symlinked); dirtiness there is key-based, so the run also exercises the keys/content-addressing path on real data pre-merge. (Originally planned against `main`; redirected before the baseline ran.)
2. **Registration fetch (tiny)**: `bounds.csv` + `catalog.json` + footprint gpkgs per source from the public data bucket; `land.fgb`/`water.fgb` masks.
3. **Cover + manifest**: `BBOX=<harbor> just cover`, then `aggregation_run.py sources-manifest` — the exact `(source, filename)` union the window's tiles read. Sum sizes before downloading; fetch via public HTTP in parallel into `store/source/<id>/`. Everything local: `SOURCE_VSI_BASE` unset, masks at local defaults.
4. **Baseline stage-by-stage run** under `/usr/bin/time -l` (wall + peak RSS per stage): cover → aggregate → downsample → bundle → soundings → depare → contours. `AGG_PROCESSES=2` (16 GB budget), everything else default.
5. **Profile aggregate** with `py-spy record --subprocesses` (speedscope output); one run at `AGG_PROCESSES=1 --native` for clean native (GDAL/C) frames. tippecanoe/gdal_contour subprocess wall times come from the stage prints + `time`.
6. **Attribute**: top self-time frames per fork → H1–H5. **Fix the top item only**, re-run the same window, require a measured improvement before anything ships or any remote dispatch happens.
7. **Optional control**: one single-tile run with `SOURCE_VSI_BASE` set (streamed) to put a hard number on the streaming delta — after the local profile, not before.

## Non-goals

Tuning remote knobs, new infrastructure, phases 5–6 — all paused behind the profile. The bbox smoke keeps running remotely only because killing a nearly-done run buys nothing.

## Deliverables

`docs/plans/2026-07-12-local-profiling.md` (this doc) updated with the findings table + flame-graph paths under `pipelines/store/profile/`, and the single targeted fix as its own small PR with before/after numbers on the same window.

---

## Findings — run 1 (blocked: inputs missing)

*Run against the stack tip `production-build-immutable` (PR #74, content-hash keys), worktree `.claude/worktrees/production-build-immutable` with its `pipelines/store` symlinked to the shared `/Users/bkeepers/projects/openwaters/gebco/pipelines/store`. All main-branch derived state (aggregation, pmtiles, contour, soundings, depare, bundle, drying, source-manifest.txt) deleted first so this branch's code regenerated everything. `SOURCE_VSI_BASE`/`LANDMASK`/`WATERMASK` unset (local resolution); `TOOLCHAIN` unset → keys fall back to GDAL version `3.13.1`, held constant across cover + manifest + aggregate. Machine: Apple Silicon Mac17,3, 10 cores, 16 GB RAM, macOS 26.5.1, GDAL 3.13.1.*

### TL;DR — the profile is BLOCKED by missing local inputs; H1–H5 are unmeasured

**The window's two defining sources are not on disk.** The freshly regenerated covering (`8-75-96-14-aggregation.csv`, 64 source rows) references 64 files; only **3 are present locally** (1 GEBCO tile + 2 NOAA estuarine tifs, ≈0.29 GB). The other **61 are missing**: **52 NOAA S-102 `.h5`**, **8 CUDEM ninth**, **1 CUDEM third**. These are exactly the sources that make a harbor tile expensive — S-102 (52 dense HDF5 products, the whole premise of hypothesis **H2**) and CUDEM (the finest source, the z13 detail). With `SOURCE_VSI_BASE` unset, `config.source_path` resolves their `objects/...` bounds-rows to `store/source/<id>/objects/...` on disk, which do not exist (`noaa_s102/` and `cudem/objects/` are absent; only `bounds.csv` + `.recipe-hash` are there). They exist nowhere on the machine (a full-machine `find` turned up only unrelated freqtrade `.h5` test files), and no download is in flight. Per the task's hard constraint (*no network fetches; a missing file is a loud failure to report, not something to download*) I did **not** fetch them.

**The aggregate cannot run past reproject.** With the inputs as-is, a baseline aggregate (`AGG_PROCESSES=1`) crashes at **8.24 s** in `aggregation_reproject.reproject`: S-102 has `priority=1` so it is the first (highest-priority) group, it is `mixed_crs`, and `per_tile_vrts` → `_build_tile_vrt` runs `gdalbuildvrt -overwrite -b 1 …/0-0.vrt store/source/noaa_s102/…/102US005NY1BG262257.h5` → GDAL "Can't open … Skipping it" → no usable datasets → `gdalbuildvrt` exit 1 → `_build_tile_vrt` retries 3× (2 s + 4 s sleeps, ≈6 s of the 8.24 s) → raises → the pool propagates and the run dies. No merged DEM is ever produced, so **smooth / terrain-encode / contour / soundings / depare / downsample / bundle never execute** and none of H1–H5 can be attributed. Log: `store/profile/aggregate-attempt.log`.

**Consequence for deliverables (b)(c)(d)(e):** the per-fork breakdown, the top-10 hotspot list, and the H1–H5 verdict are **not obtainable** from local disk until the 61 files are placed under `store/source/`. I deliberately did **not** manufacture a substitute profile (see "Why no degraded profile" below) — a GEBCO+estuarine-only merge at the covering's forced z14 grid would measure a fabricated tile (near-empty at z14, zero S-102 cost) and would mis-rank the very hypotheses the plan exists to decide.

### Step 0 — the "1 of 4 dirty" anomaly: NOT a bug (planner log over-counts; the dirty-key model is correct)

On the empty store, `cover` logs `write 4 aggregation items`, yet only **one** CSV is written (`8-75-96-14-aggregation.csv`) and `sources-manifest` reports `64 file(s) across 1 dirty tile(s)`. The "4" and the "1" are two different numbers, not "1 of 4 tiles came up dirty."

Mechanism (verified with `store/profile/probe_covering.py`): the harbor BBOX populates exactly **one** z8 macrotile, `(75, 96)`. Its z7 parent's four z8 children are `(74,96) (75,96) (74,97) (75,97)`. `get_aggregation_tiles_dfs` recurses to z8 because the z7 tile is below `maxzoom(14) − num_overviews(4) = 10`, and its base case `if candidate.z == macrotile_z: return [candidate]` emits **every** z8 child unconditionally — including the 3 with no cell in the macrotile map. So `get_aggregation_tiles` returns 4 tiles and `main()` prints `write 4 aggregation items` (a **pre-filter candidate count**). `write_aggregation_items` then drops the 3 data-free tiles via `if not rows: continue`, writing exactly 1 real covering tile.

```
populated z8 macrotiles: [(75, 96)]
get_aggregation_tiles → 4 tiles:
  8-74-96: EMPTY (filtered by `if not rows`)   0 rows
  8-74-97: EMPTY (filtered by `if not rows`)   0 rows
  8-75-96: HAS DATA                            64 rows   → 8-75-96-14-aggregation.csv
  8-75-97: EMPTY (filtered by `if not rows`)   0 rows
```

So there is **one** covering tile for this window, and on the empty store the key model marks it **100% dirty** (all four forks stale — `keys.fork_fresh` is false when neither the content-named artifact nor the `.empty` marker exists). That is exactly the required behavior; the keys derivation is sound. The apparent "1 of 4" is the planner's inflated 4 (candidate tiles incl. empty siblings) compared against the manifest's true 1 — different denominators. **No live bug in the key-based dirtiness.** The only defect is cosmetic: `aggregation_covering.main()`'s `write N aggregation items` log counts candidates before the empty-tile filter, so its number overstates what lands on disk. (A clean fix would count post-filter, or prune the empty base-case children in `get_aggregation_tiles_dfs` — logging-only, zero effect on what builds. Not fixed here.)

*Main-branch note (as requested, no deep dive):* `aggregation_covering.py` is byte-identical between `main` and this branch except one behavior-neutral line (`load_metadata(...).get` → `source_property`), so `main` prints the same inflated `write 4 aggregation items` and also writes one real CSV. The "1 of 4 dirty" observed on `main` is the same conflation of the planner's candidate count (4) with the real dirty-tile count (1); there were never 3 real tiles that failed to be flagged. (Independently, `main`'s covering-diff can legitimately mark a *subset* of real tiles dirty when a *previous* covering id and its surviving pmtiles are still on disk — but that is a different situation than the empty-store observation and did not create the 4.)

### What actually ran (stage × wall × CPU% × peak RSS)

| Stage | Command | Wall | user+sys → CPU% | Peak RSS | Result |
|---|---|---|---|---|---|
| cover | `aggregation_covering.py` (BBOX) | 0.46 s | 0.10+0.05 s → ~33% | 54.2 MB | OK — 1 real covering tile (planner logs 4) |
| sources-manifest | `aggregation_run.py sources-manifest` | ~0.5 s (not isolated under `time -l`) | — | — | OK — "64 file(s) across 1 dirty tile(s)" |
| aggregate | `aggregation_run.py` (AGG_PROCESSES=1) | 8.24 s | 1.39+0.42 s → ~22% | 76.5 MB | **FAILED** in reproject on missing S-102 `.h5` |
| downsample / bundle / soundings / depare / contours | — | — | — | — | **not reached** (no merged DEM) |

Per-fork breakdown (deliverable b), top-10 hotspots (c): **N/A — blocked before any fork runs.**

### H-verdict (deliverable d)

| Hypothesis | Verdict | Evidence |
|---|---|---|
| H1 Python vector ops dominate | **Unresolved** | Contour/soundings forks never ran (no merged DEM). |
| H2 S-102 HDF5 open/read dominates | **Unresolved — and unmeasurable locally today** | The 52 S-102 `.h5` are absent; the run dies at the first `gdalbuildvrt` open. H2 is precisely what's untestable without the files. |
| H3 numpy over multi-GB Float32 | **Unresolved** | Merge/smooth never ran. |
| H4 gdal_contour/depare at native res | **Unresolved** | No DEM to contour/polygonize. |
| H5 encode/pmtiles writing | **Unresolved** | No tiles produced. |

### Ranked next actions (deliverable e) — and the ONE thing to do first

1. **[UNBLOCK — do this first] Place the 61 missing CUDEM + S-102 files under `store/source/`** so the local, network-eliminated profile can run at all. This is an operator/prep action, not a code fix, and it is explicitly out of scope for this profiling pass under the no-download constraint — hence flagged, not performed. The exact set is `store/source-manifest.txt` minus the 3 present files (52× `noaa_s102/objects/ed3.0.0/…/*.h5`, 8× `cudem/objects/dem/…`, 1× `cudem_third/objects/dem/…`). *Verification once present:* re-run the plan's Step 1/2 verbatim on this same window; a completed aggregate + a cProfile/py-spy capture of the single `8-75-96` tile is the real deliverable this pass could not produce.
2. **[cosmetic, optional] Make `aggregation_covering.main()` log the post-filter tile count** (or prune empty base-case children in `get_aggregation_tiles_dfs`) so "write N aggregation items" matches what's written and the "1 of 4" confusion can't recur. Logging-only; no build-output change.

There is **no performance fix to select** from this pass: with the heavy inputs absent, no hotspot was measured, so committing a "top fix" now would be guessing. The predicted-improvement / verify-on-this-window step is deferred to the re-run after the inputs are staged.

### Why no degraded profile was produced

A GEBCO+estuarine-only run would still be built at the covering's forced `maxzoom=14` grid (a ~32768 px, multi-GB Float32 tile) but filled almost entirely with upsampled z8 GEBCO and partial z11 estuarine — zero S-102 cost, near-zero contour/sounding features at z14. It would over-state H3/H5 (raster memory scales with pixel dims regardless of content) and under-state H1/H2/H4 (feature/HDF5 cost scales with the absent data), i.e. it would actively mis-rank the hypotheses. Producing it also requires a behavior-changing edit to `reproject` to skip all-missing groups. Given the plan's own rule ("measure the real mechanism; no substitutes"), reporting the blocker is the correct output rather than a misleading number.

### Artifacts (under `pipelines/store/profile/`, gitignored)

- `cover.log` — covering run under `/usr/bin/time -l`.
- `sources-manifest.log` — "64 file(s) across 1 dirty tile(s)".
- `aggregate-attempt.log` — the reproject failure on the first missing S-102 `.h5` (`/usr/bin/time -l`).
- `probe_covering.py` — read-only reproduction of the 4-candidate-tiles vs 1-with-data covering behavior.

No tracked file was modified; no dataset was fetched. The regenerated covering lives at `store/aggregation/01KXA249NXXXBVQNAYD8BK4FKS/` and `store/source-manifest.txt` (both gitignored scratch).

---

## Findings — run 2

*2026-07-12, same setup as run 1 (worktree `production-build-immutable`, shared symlinked store, local-path resolution, TOOLCHAIN→GDAL 3.13.1, 10-core/16 GB Apple Silicon). Run 1's blocker resolved: all 64 manifest files on disk and size-verified (2.03 GiB; the misses were a macOS `xargs -I` 255-byte-replacement download bug, not a pipeline issue). Covering `01KXA249NXXXBVQNAYD8BK4FKS` (baseline) / `01KXAZ0RCMKGP9Y25DEKJ365E1` (profiled re-cover): one real tile, `8-75-96-14`, 64 source rows — a 32768 px z14 macrotile.*

**Fidelity checks.** The profiled run (in-process cProfile driver, no Pool — see `store/profile/profile_driver.py`; needed because `run_all`'s `with Pool(…)` runs `run()` in spawned children cProfile can't see, and the pool's context-exit `terminate()` even discards the workers' buffered stdout, which is why the baseline log carries no per-fork prints) reproduced the baseline within 2.4% wall (872.65 s vs 852.03 s) and produced byte-for-byte the SAME four content-addressed artifact names (`08301b127b8e` terrain / `665343e96c0a` contour / `c8ab34a48c81` soundings / `ddfd76508229` depare) — same keys, same work, deterministic outputs. py-spy was tried first and requires root on macOS ("This program requires root on OSX"), so per the plan it was skipped (no sudo) in favor of cProfile.

### (a) Findings table — stage × wall × CPU × peak RSS (baseline, `/usr/bin/time -l`, AGG_PROCESSES=2)

| Stage | Wall | user+sys | eff. CPU | Peak RSS | Log |
|---|---:|---:|---:|---:|---|
| cover | 0.46 s | 0.15 s | 33% | 54 MB | `cover.log` |
| **aggregate (1 tile, all four forks)** | **852.0 s** | **917.3 s** | **107.7%** | **3.70 GB** | `aggregate.log` |
| downsample cover | 0.53 s | 0.19 s | 36% | 54 MB | `downsample-cover.log` |
| downsample run (z13→z0 pyramid) | 38.1 s | 163.3 s | 429% | 128 MB | `downsample-run.log` |
| bundle (planet + overlay concat) | 1.2 s | 2.1 s | 177% | 250 MB | `bundle.log` |
| soundings bundle (tippecanoe) | 3.6 s | 5.5 s | 156% | 146 MB | `soundings-bundle.log` |
| depare bundle (tippecanoe) | 31.0 s | 77.9 s | 251% | 1.17 GB | `depare-bundle.log` |
| contours bundle (tippecanoe + tile-join) | 2.3 s | 6.6 s | 289% | 167 MB | `contours-bundle.log` |
| **whole window** | **~929 s** | | | | |

The aggregate is **91.7% of the window's wall**, and it runs at **~108% CPU on a 10-core machine** — the single most important systemic number in this profile. Everything inside a tile is serial: the only stages that exceed 150% CPU are the ones with their own pools (downsample) or multithreaded tools (tippecanoe). With one tile in the covering, `AGG_PROCESSES` provides no parallelism at all; on the planet build it parallelizes across tiles but is RAM-capped at 2–3 workers per 16 GB, so per-tile serial cost translates almost 1:1 into wall.

### (b) Per-fork breakdown of the heavy tile (profiled run, 872 s; baseline mtime-reconstruction agrees within seconds)

| Phase | Wall (cumtime) | % of aggregate | What's inside (measured) |
|---|---:|---:|---|
| reproject | 221.8 s | 25.4% | `gdal_translate -of COG` ×6 groups = **150.9 s**; `gdalwarp` (mixed-CRS) ×3 = 32.6 s; `negate_band1` (S-102 depth→elevation, in-place COG) = **25.1 s**; per-tile VRTs (52 S-102 opens) = 5.9 s; landmask+watermask rasterize+clamp = 3.0 s |
| merge | 84.8 s | 9.7% | self (per-window numpy fill/feather loop) 46.4 s; `binary_erosion` ×20 015 = 17.9 s; seam gaussians + windowed rasterio reads = rest |
| smooth | 80.2 s | 9.2% | 289 blocks × 3 `gaussian_filter` each (light σ4 / heavy σ16 / mask σ4); scipy `correlate1d` total (smooth+merge) = 69.7 s |
| terrain encode | 220.3 s | 25.3% | **`save_terrarium_tile` self = 178.0 s — the lossless-WebP encode of 4096 z14 tiles (~43 ms each, single-threaded)**; terrarium quantize (`encode.encode`) 21.8 s; 512-px window reads ~15 s; `create_archive` (pmtiles write) just **0.7 s** |
| contour fork | 43.8 s | 5.0% | `gdal_contour` (m 18 s + ft 20 s) + ogr2ogr clip/reproject ≈ 40 s subprocess; **Chaikin+simplify+smooth_geom = 1.9 s**; the whole `smooth_and_enrich` = 2.5 s |
| soundings fork | 11.3 s | 1.3% | `_shoalest_grid` 5.5 s + pyramid + GeoJSON write |
| depare fork | 208.1 s | 23.9% | `partitions` (`gdal_contour -p`, m 111 s + ft 58 s off snapshots) = **166.4 s**; shapely `union_all` (coverage/land/drying) = 30.0 s; `make_valid` 7.6 s; write 195.8 MB FGB |

Reproject group detail (snapshot mtimes, profiled run): S-102 z14 (52 files) 28 s, S-102 z12 34 s, CUDEM z13 89 s, CUDEM-third 5 s, estuarine 7 s, GEBCO+mask-clamps 58 s. **All 52 S-102 HDF5 products — open, VRT, warp, COG, negate — total ~87 s ≈ 10% of the tile.**

### (c) Top-10 hotspots (% of 872 s aggregate wall)

| # | Hotspot | Time | % |
|---|---|---:|---:|
| 1 | lossless WebP encode, 4096 tiles (`save_terrarium_tile` self / imagecodecs) | 178.0 s | 20.4% |
| 2 | `gdal_contour -p` ×2 (depare m+ft ladders, native-res 32768² DEM) | ~163 s | 18.7% |
| 3 | `gdal_translate -of COG` ×6 (reproject per-group COGs, ZSTD) | 150.9 s | 17.3% |
| 4 | scipy `correlate1d` (smooth's 3 gaussians/block + merge feather) | 69.7 s | 8.0% |
| 5 | `aggregation_merge.merge` self (per-window fill/feather numpy loop) | 46.4 s | 5.3% |
| 6 | `gdal_contour` lines ×2 (contour fork m+ft) | ~38 s | 4.4% |
| 7 | `gdalwarp` mixed-CRS ×3 (S-102 UTM zones, estuarine) | 32.6 s | 3.7% |
| 8 | shapely `union_all` ×3 (depare valid_union: coverage/land/drying) | 30.0 s | 3.4% |
| 9 | `negate_band1` (S-102 sign flip, in-place COG rewrite ×2) | 25.1 s | 2.9% |
| 10 | terrarium quantize (`encode.encode`, 4096 tiles) | 21.8 s | 2.5% |

(11–12: `binary_erosion` in merge 17.9 s / 2.1%; per-tile DEM window reads 15.0 s / 1.7%. Total GDAL-subprocess wall = 400.7 s = 45.9% — the parent spends it in `select.poll`.)

### (d) H1–H5 verdict

| Hypothesis | Verdict | Evidence |
|---|---|---|
| **H1** Python vector ops (Chaikin/shapely per-feature) dominate | **REFUTED** | Chaikin+simplify over 8031 features: **1.9 s (0.2%)**. The whole contour fork is 5%. shapely IS visible in depare (`union_all` 30 s + `make_valid` 7.6 s = 4.3%) but as C-level GEOS unions, not per-feature Python. |
| **H2** S-102 HDF5 open/read dominates | **REFUTED (on local disk)** | All 52 products end-to-end (open+VRT+warp+COG+negate) ≈ 87 s = 10%; the pure open/VRT part is 5.9 s. The COG-at-mirror-time conversion may still pay for *streaming* builds, but HDF5 cost is not the local bottleneck. |
| **H3** merge/feather numpy over multi-GB Float32 | **PARTIALLY CONFIRMED** | merge 84.8 s + smooth 80.2 s = **165 s (18.9%)** — material, but neither dominates alone. Memory discipline is fine: 3.70 GB peak on a 4 GB-DEM tile (windowed reads work). |
| **H4** gdal_contour / depare polygonize at native res | **CONFIRMED (co-leader)** | All four `gdal_contour` passes rescan the same 32768² DEM: depare's two `-p` polygonize passes = 163 s (18.7%) + contour's two line passes = 38 s → **~201 s (23%)**. |
| **H5** encode/pmtiles writing | **CONFIRMED (co-leader), mechanism refined** | It is NOT the PMTiles writer (`create_archive` = 0.7 s). It's the **lossless WebP encode: 178 s (20.4%)**, plus 21.8 s quantize — one 43 ms C call per tile, 4096 tiles, strictly serial. |

**Overall verdict: no single hypothesis dominates.** The tile's cost is three nearly-equal ~20% blocks — WebP encode (H5′), depare polygonize (H4), and per-group COG translate (reproject plumbing no hypothesis named) — sitting on an entirely serial per-tile pipeline (108% CPU on 10 cores). The leverage is therefore less "make X faster" than "stop running everything one-at-a-time."

### (e) Ranked fixes — and THE one to implement first

1. **[THE FIX] Parallelize the terrain tile encode** (`aggregation_tile.create_tiles`): a `ThreadPoolExecutor` over the 4096 (window-read → quantize → `webp_encode` → write) tasks. `imagecodecs.webp_encode` and rasterio's GDAL reads release the GIL, so threads scale without spawn overhead and without duplicating the DEM in RAM (each task holds one 512² window, ~1 MB). Predicted: the 200 s parallelizable portion → ~25–40 s on 8–10 threads, i.e. **terrain fork 220 s → ~45–65 s, aggregate −155 to −175 s (−18 to −20%), window ~929 s → ~755 s.** Verify on this same window: the encode is deterministic, so the terrain pmtiles' *content* must be identical — but note the code edit re-keys the fork (aggregation_tile.py bytes are hashed), so compare extracted tile bytes / tile counts across the old and new archives plus the stage wall; peak RSS should stay ≈3.7 GB. Lowest-risk of all candidates: no output change, no cartographic judgment, one function.
2. **Run the two `gdal_contour` passes per fork concurrently** (depare m ∥ ft; contour m ∥ ft — independent subprocesses over a read-only DEM): depare 163→~111 s, contour 38→~20 s, **−70 s** for a few lines of `Popen`/wait. Composable with fix 1 (different phases). Then, bigger but structural: overlap the four forks themselves (terrain ∥ contour ∥ soundings ∥ depare all read the same immutable merged DEM; each is single-threaded) — up to a further −200 s, but sequence it after 1–2 prove out.
3. **Cheapen or eliminate the per-group COG translate** (150.9 s): the per-group COGs are transients the merge reads once and deletes — try plain tiled+ZSTD GTiff instead of the COG driver (COG does an extra internal copy), or let `gdalwarp` write the tiled GTiff directly for VRT-path groups. Measure; watch the ADD_ALPHA mask semantics `negate_band1`/merge rely on.
4. **Fold the S-102 sign flip into the warp** (kill `negate_band1`'s read-modify-write of two ~GB COGs): `gdalwarp`-stage `-wo`/VRT pixel function or negate during merge's window reads. **−25 s.**
5. **Fuse smooth's gaussians** (σheavy² = σlight² + σΔ² ⇒ heavy = gaussian(light, σΔ), reusing the light pass; and blur the mask at lower resolution): ~−20 s of the 69.7 s correlate1d bill. Keep for a cleanup pass; H3's chunking is already sound.

### Artifacts (under `pipelines/store/profile/`)

- `aggregate.log` — baseline aggregate (`/usr/bin/time -l`, timestamped) · `cover.log`, `downsample-*.log`, `bundle.log`, `soundings-bundle.log`, `depare-bundle.log`, `contours-bundle.log` — the other stages.
- `aggregate.prof` — cProfile of the full tile (in-process) · `profile_driver.py` (the no-Pool driver) · `analyze_prof.py` + `prof-analysis.txt` — top-25 cum/self + per-phase attribution.
- `aggregate-prof.log` — profiled run, per-fork prints timestamped · `tmp-snapshots.txt` — 15 s mtime snapshots of the tile tmp dir (per-group COG / merge / smooth / webp-span / contour+depare intermediate boundaries; the flame-graph substitute for the subprocess phases py-spy would have covered).
- No flame graph: py-spy needs root on macOS (recorded in `prof-analysis.txt`'s methodology note above); cProfile + the snapshot timeline provide equivalent attribution at the fork/phase level.

No tracked file was modified in either tree; the only writes were gitignored store state, `store/profile/` artifacts, and this section.

### Fix verification

*2026-07-12. Fix (e) — hotspot #1 — implemented on branch `encode-parallel` (off `production-build-immutable`, same shared symlinked store, GDAL 3.13.1, 10-core/16 GB Apple Silicon, all mask/VSI env unset). The edit: `aggregation_tile.create_tiles` now submits the 4096 per-tile (window-read → quantize → `webp_encode` → write) tasks to a `ThreadPoolExecutor(max_workers=min(os.cpu_count(),8))` instead of a serial double loop — 20 insertions, 3 deletions, one file. `encode.py` untouched (bias-shallow quantization intact). Same window (`8-75-96-14`, 64 source rows); derived state deleted and re-covered before the re-measure.*

**Before/after (aggregate wall, single-worker; baseline `AGG_PROCESSES=2` / re-measure `AGG_PROCESSES=1` — one tile in the covering, so no cross-tile parallelism either way):**

| Run | Aggregate wall | user+sys | eff. CPU (10 cores) | Peak RSS |
|---|---:|---:|---:|---:|
| baseline (serial encode) | 852.03 s | 917.28 s | 107.7% | 3.70 GB |
| encode-parallel (this fix) | **682.22 s** | 1054.54 s | 154.6% | 3.485 GB |
| delta | **−169.81 s (−19.9%)** | — | +47 pp | −0.22 GB |

**Measured vs predicted:** the fix predicted −155 to −175 s (−18 to −20%); measured **−169.81 s (−19.9%)** — squarely in the band, and past the ≈690 s target. Peak RSS fell slightly (threads share the one DEM; per-task 512² windows are ~1 MB), confirming the memory ceiling holds.

**Determinism / content byte-compare (the contract).** The re-keyed terrain fork published `8-75-96-14-2dbeb6d2a55f.pmtiles` (new content name — `aggregation_tile.py` bytes are hashed into `TERRAIN_MODULES`); the pre-fix serial archive `8-75-96-14-08301b127b8e.pmtiles` was preserved as `store/profile/prefix-terrain-08301b127b8e.pmtiles`. Both are 91,645,788 bytes. Extracting all tiles with the `pmtiles.reader` (`all_tiles`, as `test_engine` uses):

- tile-id sets identical (4096 z14 tiles each); **all 4096 tiles byte-identical, 0 mismatches**; the required ≥50-tile sample incl. all four z14 corners — every tile identical.
- Whole-archive `cmp` differs in **exactly 4 bytes** (offsets 131–132, 8932–8933) — the gzip MTIME fields of the pmtiles Writer's two compressed directories (root + leaf; gzip magic `1f8b08` starts the root dir at byte 127, MTIME at +4). Timestamp non-determinism from gzip, not from the parallel encode — two serial runs would differ in the same 4 bytes. Tile data (91.6 MB) is byte-identical.
- The unchanged forks re-keyed to the **same** content names as the profiled run (contour `665343e96c0a`, depare `ddfd76508229`), confirming the shared merged DEM is byte-identical and only terrain moved.

**Tests:** `just test-engine` (engine e2e + reproject/keys/store_manifest self-checks) and `just test-sources` both green in the worktree against the edited code.

**Artifacts:** `store/profile/aggregate-encode-parallel.log` (re-measure, `/usr/bin/time -l`) · `store/profile/prefix-terrain-08301b127b8e.pmtiles` (preserved pre-fix archive) · `store/profile/run-aggregate-encode-parallel.sh` (the wrapper).

### Fix 2 (contour-pass concurrency) — REVERTED, null result

*Fix (e) #2 — run each vector fork's metre and feet/fathom `gdal_contour` passes concurrently (predicted −70 s). Implemented, measured, reverted.*

| Run | Aggregate wall | user+sys | eff. CPU |
|---|---:|---:|---:|
| encode-parallel (fix 1 only) | 682.22 s | 1054.54 s | 154.6% |
| + contour concurrency | 682.95 s | 1144.98 s | 154.6% |
| delta | **+0.73 s (none)** | +90 s user | — |

The two passes genuinely ran in parallel (user-time rose ~90 s) but bought **zero wall-clock**: once the encode is parallelized, the contour/depare `gdal_contour` passes are off the critical path, and two native-res `gdal_contour -p` processes contend on memory bandwidth. **Reverted** — no threading complexity for 0.7 s. Recorded so nobody retries it. (Log: `store/profile/aggregate-contour-parallel.log`.)

### Fix 3 (byte-reproducible pmtiles) — committed, perf-neutral

*Not a hotspot fix — a correctness/reproducibility follow-up prompted by the 4-byte gzip-MTIME diff found during fix 1's byte-compare. The pmtiles Writer gzips its root+leaf directories with `gzip`, which stamps `time.time()` when no mtime is given. Pinned to mtime=0 (the reproducible-builds convention; a constant, not derived from git/sources — no consumer needs a real timestamp).*

Same archive built twice now `cmp`-**identical** (previously differed in exactly the 4 gzip-MTIME bytes); the isolation control confirmed unpatched double-builds differ, so the test is sensitive. Perf-neutral (directory gzip is <1 s of a ~682 s aggregate). **Does not affect the content-addressed cache** — keys hash inputs ‖ code ‖ config ‖ toolchain, never artifact bytes; this only helps byte-level QA / attestation / artifact mirroring.

### Hotspot #3 (COG→GTiff transients, −100 s) — DEFERRED on a correctness flag

*Fix (e) #3 — replace the per-group `gdal_translate -of COG` transients (150.9 s, hotspot #3) with plain tiled GTiff to skip the COG driver's overview build. The biggest remaining lever, but NOT shipped: an isolation probe found the format change alters band-1 values **at invalid (nodata) pixels** while the valid-mask and valid pixels stayed identical.*

The merge detects holes via the **`-9999` sentinel value**, not `read_masks`, so a transient that stores something other than `-9999` at invalid pixels could change merge behavior — a correctness question in a bias-shallow chart pipeline, not a free win. Deferred to a dedicated pass that (a) pins exactly what the two formats write at nodata pixels, (b) proves the merged-DEM bytes are identical (unchanged forks re-key to the same content names), before any wall-time claim. **Do not ship the −100 s number without clearing the sentinel question.**

### Net shipped (PR #75)

Fix 1 (encode −19.9%, 852→682 s, byte-identical tiles) + Fix 3 (byte-reproducible pmtiles). Fix 2 reverted (null); hotspot #3 deferred (correctness). The remaining ranked runner-ups (fold negate into warp −25 s, fuse smooth gaussians −20 s) are single-digit-percent and were not pursued — the single-tile micro-optimization has diminishing returns against the real lever, which is cross-tile parallelism on the planet build (the aggregate is serial per tile at 108% CPU on 10 cores; the planet build parallelizes across tiles via `AGG_PROCESSES`).
