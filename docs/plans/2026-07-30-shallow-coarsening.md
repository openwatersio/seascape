# Shallow-band generalization for marsh coasts

_Written 2026-07-30. Design reviewed by the gis and oceanographer agents against a standards/literature sweep, then measured on four 4096² crops range-read from the Gulf marsh stems. The numbers in §3 are real; the integration is not built._

## 1. Why

At native z15 a fragmented marsh coast resolves thousands of individual mud islands, ponds, and bayou fingers, and every one becomes its own ring in the shoalest depth bands. The Louisiana delta is the worst case in the covering:

| stem | bbox (W,S,E,N) | what happened |
| --- | --- | --- |
| `8-63-105-15` | `-91.4062,29.5352,-90.0000,30.7513` | Atchafalaya → Barataria. 9.94 GB window, 2.5 h, 33 GB peak, then failed |
| `8-63-106-15` | `-91.4062,28.3044,-90.0000,29.5352` | delta mouth. failed |
| `8-64-106-15` | `-90.0000,28.3044,-88.5938,29.5352` | Birdfoot → Breton Sound. failed |
| `8-64-105-15` | `-90.0000,29.5352,-88.5938,30.7513` | Pontchartrain. 30.6 GB, 2 h 19 m, OK |
| `8-61-105-15` | `-94.2188,29.5352,-92.8125,30.7513` | Galveston. 209,481 polygons, OK |

Measured on this class (run 30506340413, ccx63): band crossings per raster row concentrate in the shallow ladder (**0 m = 7.8, −2 m = 7.4, −5 m = 4.8** against ~1–3 below −10 m); the partition set reaches **730 MB** for one stem with 40k+ parts in a single band; and three stems produced a single band feature over GDAL's 200 MB GeoJSON ceiling until `OGR_GEOJSON_MAX_OBJ_SIZE` was lifted (`93da2d3`) — which stops the failure without making the stems cheap. The class also sets `DEPARE_GB[15] = 36` ([`build.smk:316`](../../pipelines/build.smk#L316)), admitting only four concurrent depare jobs in a 161 GB budget when most z15 stems need ~12 GB, so it is the binding constraint on stage-3 throughput for every other stem too.

**The detail being defended is finer than the data can support**, and this is the load-bearing argument — the cost table is only the occasion:

- Grand Isle (8761724) great diurnal range ≈ **0.4 m**; MSL ≈ 0.2 m above MLLW.
- Winter frontal setup/setdown runs **0.3–0.6 m every 3–7 days** — the meteorological tide routinely exceeds the astronomical one. Seasonal MSL cycle adds ~0.2–0.3 m.
- Relative sea-level trend ≈ **9 mm/yr**, the highest in the NOAA network → ~0.3 m of relative rise since the 1983–2001 NTDE midpoint. The published MLLW plane here is stale by roughly the entire tidal range.
- Bay bed slopes run **1:2,000 to 1:10,000**, so 0.4 m of water-level swing sweeps the waterline **0.8–4 km horizontally**.
- Source datums are a patchwork with no offsets applied: CUDEM covers this marsh on **uncorrected NAVD88**, `noaa_estuarine` and `noaa_s102` are MLLW, GEBCO is ~MSL. S-44 6th ed. TVU at 2 m depth is 0.25 m (Special Order) at best, and green lidar in bays with Secchi under 1 m frequently returns no bottom, so some of the "detail" is interpolation.
- Coastal Louisiana loses ~43 km²/yr (Couvillion et al.), so an individual Terrebonne mud island from a 2014–2020 survey has a real chance of not existing.

Confirmed directly in the data: over a 2048² window of interior Barataria marsh, water pixels carry **14 unique depth values, p50 = p95 = 0.10 m**. Those ponds hold an outline and no depth information at all.

Resolving the 0/−2 m band at 2.4 m horizontally asserts a position two to three orders of magnitude finer than the vertical determinacy supports. NOAA's own chart of Terrebonne already generalizes this marsh, and the standards license it explicitly (§2).

## 2. What established practice does

Surface-first generalization — coarsen the grid, then contour — is documented production practice at two hydrographic offices. The literature states the fork explicitly (Skopeliti, Tsoulos & Pe'eri 2021 §2.2): generalizing the surface then extracting contours "is robust, fast and allows depth contours to be generated at various scales"; generalizing contours directly is "more reliable, thus contributing to the safety of navigation." Both are accepted. The first is this pipeline's requirement and what `smooth.py` already does.

The Australian Hydrographic Office workflow ([IHR 2019](https://ihr.iho.int/articles/semi-automated-generation-of-depth-contours-for-encs/), CARIS BASE Editor) is five steps: re-grid at "three pixels per mm at compilation scale" using shoalest-depth-per-cell; shoal-expand sub-legible peaks to a "minimum target diameter of approximately 4 mm"; Laplacian smooth that "raises the elevation of grid cells" and "infills low areas"; contour; then **delete tiny _deep_ closed contours** below a 10 mm-diameter-at-scale area. IHM Spain ([IHR 2021](https://ihr.iho.int/articles/the-bathymetric-compilation-a-true-challenge-in-the-nautical-chart-generation-process/)) documents the same shape, "merging those tiny, closed contours", with "a shoal-biased pattern … to ensure the safety constraint."

The mandates that bind this design:

- **Smoothing must be shoal-biased.** S-4 B-411.5: "Where necessary, smoothing will include deeper water within shoaler contours, but an attempt to retain a reasonable representation of the seabed should be made."
- **Vertex density may not exceed 0.3 mm at compilation scale** — S-58 Ed. 7.0.0 check 571, the only hard numeric geometry rule in the ENC standards. NOAA works at 0.4 mm to guarantee it on output. **This pipeline currently violates it by ~5× (§3).**
- **An isobath may only move toward greater depth**, and pits/peaks are asymmetric: "a pit cannot be enlarged or aggregated; a too small or not relevant pit is removed; a peak cannot be removed; a too small peak is enlarged or aggregated" (Guilbert & Zhang 2012).
- **Never delete an isolated shoal — exaggerate it.** Four independent sources. A morphological max/closing does this natively; a vector area sieve does the opposite.
- **The marsh coastline is officially approximate.** S-57 UOC 4.7.3 requires `QUAPOS = 4` where a marsh meets the coastline.
- **Group 1 objects may not carry `SCAMIN`**, so scale is handled by compiling a separate generalized cell per usage band — the standards-level reason per-zoom *geometry* generalization is the only available lever.

Licensed by judgment, with the numbers that set our thresholds: **fill every marsh hole narrower than 1.27 mm at scale** (USGS NHD, "break SWAMP/MARSH for clearings that are ≥ 0.05″ along the shortest axis"); **0.8 mm minimum islet dimension** (USGS + NOAA `Rescheme Cook Book`, independently); **4 mm² minimum solid-fill polygon area** (Galanda M4 — and note 0.4 mm² is folklore with no primary source); a braided delta may be emitted as one AREA OF COMPLEX CHANNELS feature; and S-57 UOC 5.8.3.1 licenses collapsing an area's bathymetry to one shoalest `DEPARE` under a caution area.

Ground metres at 29.5°N, 512-px tiles, 0.28 mm rendering pixel (the pipeline's nominal "2.4 m/px at z15" is the *equatorial* figure; local resolution is `156543.034·cos φ / 2^(z+1)`):

| z | m/px | scale | m per map mm | 0.3 mm | 0.8 mm | 1.27 mm | 4 mm² |
| --- | --- | --- | --- | --- | --- | --- | --- |
| z13 | 8.32 | 1:29,700 | 29.7 | 8.9 m | 23.8 m | 37.7 m | 3,530 m² |
| z14 | 4.16 | 1:14,850 | 14.85 | 4.5 m | 11.9 m | 18.9 m | 882 m² |
| **z15** | **2.08** | **1:7,425** | **7.43** | **2.2 m** | **5.9 m** | **9.4 m** | **220 m²** |

At z15 the grid is already ~0.29 mm/px — *at* the AHO minimum re-grid resolution. There is no over-resolution to remove; the fragmentation is in the terrain.

## 3. Measured

Four 4096² crops range-read from the mosaic COGs on R2 (67 MB of range reads out of a 13.5 GB tile, ~26 s each): `terrebonne` and `birdfoot` and `barataria` (marsh-dominated, the cost sites) and `atchafalaya` (open bay with real bathymetry). Cost metrics from `gdal_contour -p` at `DEPARE_LEVELS`.

**`gdal_contour -p` emits one MultiPolygon per band, so feature count is a constant 4 and useless as a cost metric.** Parts and vertices are what the GEOS partition pass and the FGB write pay for, and they behave completely differently.

Where the vertex budget lives (terrebonne, 4,483 parts / 574,600 vertices):

| part size | parts | share of parts | vertices | share of budget |
| --- | --- | --- | --- | --- |
| < 4 mm² | 3,638 | 81.2% | 54,114 | 9.4% |
| < 16 mm² | 4,217 | 94.1% | 84,101 | 14.6% |
| < 1024 mm² | 4,433 | 98.9% | 123,251 | 21.4% |
| largest single part | 1 | 0.02% | 128,757 | **22.4%** |

**98.9% of the parts hold 21.4% of the vertices; one part holds 22.4%.** So removing small rings collapses part count and barely touches bytes — the vertex budget is the long convoluted shoreline of the *connected* network, which is fractal and yields to no local operator. Parts drive wall time; vertices drive bytes and memory. The 730 MB partition set and the 200 MB per-feature ceiling are vertex problems.

Operator comparison (full sweep in the scratchpad scripts; representative rows):

| terrebonne | parts | vertices | FGB MB | wall | water lost | network intact | shoal-safe |
| --- | --- | --- | --- | --- | --- | --- | --- |
| native | 4,483 | 574,600 | 9.3 | 3.0 s | — | — | — |
| pond-fill 16 mm² | 385 (11.6×) | 412,448 (1.4×) | 6.6 | 0.5 s | 1.4% | **yes** | ok |
| closing r=4 (19 m) | 405 (11.1×) | 393,224 (1.5×) | 6.3 | 0.5 s | 2.4% | yes | ok |
| closing r=8 (38 m) | 197 (22.8×) | 355,950 (1.6×) | 5.7 | 0.5 s | 3.7% | **no** | ok |
| block-max, fully gated | 4,378 (1.0×) | — | — | — | **0.0%** | yes | ok |
| `[shipped]` `-r average` | 1,759 | — | — | — | 2.2% | yes | **FAIL** |

And the vertex lever, which is the one the standards already mandate — `shapely.coverage_simplify` at the S-58 0.3 mm floor (2.23 m at z15), which simplifies a polygonal coverage while preserving shared edges:

| | vertices | overlapping pairs | area drift | all valid |
| --- | --- | --- | --- | --- |
| native | 574,600 | 0 | — | yes |
| `coverage_simplify` 0.3 mm | 122,490 (**4.7×**) | **0** | **0.00000%** | yes |
| naive per-geometry simplify 0.3 mm | 78,989 (7.3×) | **4** | 0.0052% | yes |

Naive simplification is cheaper and **breaks the partition contract**; coverage simplification keeps it exactly. The full stack, measured end to end rather than by multiplying factors:

| crop | native parts / vertices | pond-fill 16 mm² + `coverage_simplify` 0.3 mm | wall |
| --- | --- | --- | --- |
| terrebonne | 4,483 / 574,600 | 385 / 68,248 — **11.6× parts, 8.4× vertices** | 3.0 → 0.5 s |
| birdfoot | 6,815 / 899,326 | 2,109 / 158,566 — **3.2× parts, 5.7× vertices** | 5.1 → 1.6 s |
| barataria | 7,268 / 1,258,689 | 3,304 / 235,612 — **2.2× parts, 5.3× vertices** | 11.2 → 3.5 s |

Zero overlapping pairs, area drift ≤ 0.00015%, every geometry valid, on all three.

### 3b. The local rig — every change is profiled here before a production build

[`pipelines/perf/`](../../pipelines/perf/) runs the **real** stage entry points (`smooth.py prepare-window`, `depare_run.py tile`) against small real windows over the same marsh. No Snakemake, no covering, no mosaic — `depare_run.tile` needs only a window TIF, the two masks, and `gdal_contour`/`ogr2ogr`.

The fixtures are synthetic z12 macrotile stems: `2^(15-12)·512 = 4096` px core at z15 **native** resolution, so real detail over 1/256 the area of a production z8/cz15 stem (65,666 px, 17.2 GB Float32, not runnable on a 17 GB machine). Nothing in the stage-3 path reads `utils.macrotile_z`, and geometry comes from the stem's mercantile bounds plus the raster's own transform, so the code cannot tell a fixture from a production stem. Each crop is written as the stem's *own* mosaic tile at exactly the buffered bounds, so `intersecting_tiles(stem) == [stem]` and `window_dem`'s `-te` matches — no nodata fill at the halo.

Baseline on unmodified HEAD, four sites, `contour-p` as production uses:

| stem | site | window | depare | parts | vertices | FGB |
| --- | --- | --- | --- | --- | --- | --- |
| `12-1015-1699-15` | terrebonne | 4.0 s / 787 MB | 6.5 s / 675 MB | 9,615 | 833,381 | 14.6 MB |
| `12-1031-1699-15` | birdfoot | 3.2 s / 1109 MB | 11.4 s / 655 MB | 9,055 | 1,370,928 | 23.1 MB |
| `12-1027-1697-15` | barataria | 3.3 s / 1166 MB | 18.7 s / 1080 MB | 10,895 | 1,858,929 | 31.2 MB |
| `12-1009-1695-15` | atchafalaya | 3.3 s / 1123 MB | 6.0 s / 514 MB | 1,005 | 838,032 | 13.5 MB |

Repeat runs are byte-identical on parts/vertices/bytes, which is what makes the regression gate meaningful.

### 3c. Production scale — the measurement that changes the plan

The fixture phase split does **not** survive to production scale, and neither does §1's cost model. `8-63-105-15` run locally end-to-end (window 9.93 GB compressed, matching the figure in §1; the two neighbour tiles south of it were absent, so that 65 px halo strip reads nodata — immaterial to cost, but this window is not byte-identical to the box's):

| | production `8-63-105-15` | fixture terrebonne |
| --- | --- | --- |
| wall | 6,476 s | 6.5 s |
| peak RSS | 7.31 GB (macOS, indicative) | 675 MB |
| parts | 354,818 | 9,615 |
| vertices | 62,291,749 | 833,381 |
| FGB | 1.04 GB | 14.6 MB |

| phase | production | share | fixture |
| --- | --- | --- | --- |
| **`drying-bucket-union`** | **5,353.9 s** | **83%** | 0.7 s (11%) |
| `bands-clip-m` | 340.5 s | 5% | 0.9 s |
| `bands-clip-ft` | 233.5 s | 4% | 0.8 s |
| `gdal_contour` (ft) | 139.6 s | 2% | 0.5 s |

**83% of a production marsh depare is one `unary_union` call.** Not the partition pass (§1's model), not the band clipping (the fixture's). `valid_union` ([`depare_run.py:216`](../../pipelines/depare_run.py#L216)) is `unary_union([make_valid(g) for g in geoms])`, and the drying bucket it unions is **88,997 parts / 20.7 M vertices in one feature**. Against the fixture's 213 parts that is 418× the parts for 6,105× the time — superlinear, which is exactly why no fixture-scale run could see it.

The bucket comes from a single `gdal_contour -p` pass, so its parts are **disjoint by construction** — the case `shapely.coverage_union_all` exists for. On the fixture it is 4.8× faster with symmetric difference exactly 0.0 (bit-identical). Two candidate wins follow, both with **zero cartographic risk**, neither of them in §4:

- **Use `coverage_union_all` for the disjoint bucket** instead of `unary_union`.
- **Guard `make_valid`.** `valid_union` applies it unconditionally to every part; `shapely.is_valid` is far cheaper, and `contour-p` output is likely already valid. At 89k parts the step is expensive enough to matter on its own.

**This does not retire §4, and must not be read as doing so.** The two tracks fix different problems and both are needed:

| | build cost (wall, peak RSS) | shipped artifact (bytes, vertices) | standards | depiction |
| --- | --- | --- | --- | --- |
| union fix (§3e) | **4.6× faster** | unchanged — emits identical bytes | — | unchanged |
| generalization (§4) | helps, via fewer drying parts | **4.7–5.7× fewer vertices** | S-58 check 571 | honest at the data's real determinacy |

The union fix cannot touch what ships. This stem emitted **1.04 GB of FGB and 62.3 M vertices**; a faster union emits exactly the same ones. Tile size, bundle size and serving cost move only with generalization. Independently, S-58 check 571 caps vertex density at 0.3 mm at compilation scale and the measured output is ~5× denser — a compliance gap, not an optimization. And §1's determinacy argument never depended on cost: interior marsh ponds in this window carry **14 unique depth values, p50 = p95 = 0.10 m** — outline, no depth information.

They also compound in the right direction. The bucket that costs 5,354 s to union has 88,997 parts *because* the marsh is riddled with sub-legible ponds; §4b removes them at the source, so it makes the union cheaper on top of the algorithm fix.

Sequencing is only about risk order: the algorithm swap carries no cartographic risk and is gated by an identity assertion, so it lands first and re-baselines the numbers §4 is then measured against.

### 3e. The union fix — implemented and measured

`depare_run.coverage_union` (+58/−4): explode the bucket to parts, `make_valid` only where `is_valid` rejects (1 of 88,997), dissolve with `coverage_union_all`, **gate the result** on `area identity AND u.is_valid`, fall back to `unary_union` with a printed warning. Matches the pattern this file already documents on the coverage path ("never one union … the 8.9 h stems"); the drying bucket had kept the monolithic form.

Measured on the production stem, end to end:

| | before | after | |
| --- | --- | --- | --- |
| wall | 6,476.4 s | **1,516.7 s** | **4.27×** |
| peak RSS (macOS, indicative) | 7.31 GB | **4.91 GB** | −33% |
| `drying-bucket-union` | 5,353.9 s | **416.7 s** | **12.8×** |
| parts / vertices / bytes | 354,818 / 62.29 M / 1.0436 GB | +0 / +4 / +64 B | — |

A/B verdict **PASS**: bands bit-identical (area delta 0.0); drying symmetric difference **exactly 0.0** (8,652 rows differ only in WKB vertex order); the entire output delta is +4 vertices / area rel 4.4e-6 on nodata — 455× inside `ab_depare.py`'s documented 2e-3 grid-snap tolerance. Offline, `coverage_union_all` vs `unary_union` on the same parts: symmetric difference exactly 0.0, 193.7 s vs 1,309.5 s. Fixtures: parts/vertices/bytes 0.0% on all four.

Two design facts learned in implementation, recorded so nobody re-litigates them:

- **The area identity alone is NOT a guard.** `coverage_union_all` returns overlapping parts *unmerged*, and `MultiPolygon.area` sums members — the identity holds exactly on broken output. `u.is_valid` on the result is what catches both failure modes (unmerged overlaps; line-touching members); it costs 119 s of the 417 s phase and is the price of not shipping a silent wrong dissolve.
- **`coverage_is_valid` is not viable at runtime** — it did not finish in 33 minutes against the 194 s union it would have guarded. Correctness was proven offline by geometric equality against `unary_union` instead; the runtime protection is the result-gate + loud fallback (`unary_union` at 1,310 s still beats the 5,354 s it replaces).

`just test-engine` fails on `vector_join` (soundings drop) — verified pre-existing by reverting the diff: it is the unpatched local tippecanoe's minzoom issue ([[tippecanoe-variable-depth-minzoom-fix]]), unrelated.

Note the box cannot corroborate the phase split: `DEPARE_TIMING` is not enabled in CI and the depare logs in the benchmark artifact are empty (the truncating `2> {log}` redirect from `93da2d3`). This local run is the only phase-level attribution of a production-scale marsh depare in existence, and `8-63-105-15` failed on the box, so it is also the first successful completion of that stem anywhere. **Enable `DEPARE_TIMING` on the box** so this is not a one-off.

### 3d. The memory reservation, settled from the box's own rows

Pulled from `snakemake-bench-30506340413` (27 cz15 depare rows):

| | max | p50 | min |
| --- | --- | --- | --- |
| depare cz15 `max_rss` | **31.72 GB** (`8-64-105-15`, Pontchartrain, 10,014 s) | 10.57 GB | 9.84 GB |

`DEPARE_GB[15] = 36` is therefore **correctly sized for the worst stem** — 4.3 GB of headroom over a measured 31.72 GB — and is not over-reserving, contrary to what the local 7.31 GB figure suggests in isolation. The real problem is the distribution: 25 of 27 stems need ≤ 14.8 GB, so the worst case is reserved for every stem and caps depare at four concurrent. That is exactly the per-stem reservation from window size that the `DEPARE_GB` comment already names as the durable fix, and it is independent of everything in §4.

Local peak RSS is **not** comparable to these rows — macOS `libmalloc` vs Linux `glibc`, different arena and reclaim behaviour. Use the local rig for wall, phase ranking, and geometry counts; use the box's TSVs for memory.

Two toolchain notes. `contour-p` must be built locally (`just contour-p`) — production sets `DEPARE_CONTOUR_BIN` to it, and measuring with stock `gdal_contour` measures a path that does not ship (3.18 s → 0.44 s on a crop, identical output; 4839 s → 121 s on a full wetland window). And `DEPARE_TIMEOUT` must stay unset while measuring: a timeout reroutes through `_uniform_coarsen`, which is a different algorithm, so `bench.py` refuses to run with it set.

## 4. Design

Two operators. Both use already-installed dependencies; neither needs a new block grid, an origin anchor, or a `MIN_CHILD_Z` gate.

**4a. Coverage-preserving simplification at the S-58 vertex floor.** `shapely.coverage_simplify(bands, tol, simplify_boundary=True)` on the partition after `contour-p`, with `tol` derived per zoom from 0.3–0.4 mm at scale (2.2–3.0 m at z15). This is the largest single win (4.7–5.7× vertices), it is the standards-mandated behaviour rather than a discretionary optimization, and it is roughly two lines against a dependency the project already has. It attacks the byte problems specifically: the 730 MB partition set and the 200 MB per-feature ceiling.

It is the one operator here that is **not** shoal-biased by construction: boundary churn measured 0.185% of area at 0.3 mm and runs in both directions, so a band edge can move shallower or deeper. The bounded-tolerance argument is what Peters §2 says you must fall back to, and here it is strong — displacement is capped at the tolerance, which is the S-58 *legibility floor* (below what can be drawn at that scale), and it is three orders of magnitude below the datum error budget from §1 (2.2 m of horizontal displacement against 0.8–4 km of waterline sweep from routine water-level variation). Record the bound; do not claim exactness. If a hard guarantee is ever required, Skopeliti's **double-buffering** is the named shoal-bias-honouring vector operator and is "commonly employed in commercial hydrographic software."

**4b. Connectivity-gated pond fill.** Label the water set (`scipy.ndimage.label`), keep every component connected to the stem's water network, and fill the rest below a minimum area to its shoalest surrounding value. Start at **16 mm²** (890 m² at z15) — measured 11.6× parts at terrebonne for 1.4% water loss; 4 mm² (Galanda M4, the more conservative published figure) gives 5.8× for 0.7%.

The distinction that makes this work is **enclosed vs connected**, and it is topological, not value-based. Established practice fills enclosed sub-legible ponds explicitly (USGS's 0.05″ clearing rule; `QUAPOS = 4` on the marsh coastline) and forbids closing a channel. No local never-deepening operator can tell the two apart — which is why every value-gated candidate either does nothing or eats a channel (§5). Measured: the largest-water-component share is unchanged (0.54 → 0.54 birdfoot, 0.77 → 0.78 terrebonne), so the channel network survives intact, while closing at r ≥ 4 degrades it (barataria 0.49 → 0.44, and → 0.34 at r=8).

Place 4b in `prepare_window` ([`smooth.py:259`](../../pipelines/smooth.py#L259)) so band edges and contour lines stay coincident — both forks read `store/window/{stem}.tif` and gate identically. (The invariant already excludes the 0 m band: `CONTOUR_LEVELS` has no 0 level, and depare adds one only to close the shoalest band at the shoreline. That asymmetry is correct — the datum line is a shoreline-class symbol with its own accuracy qualifier, US Chart No. 1 supplementary (a)/(b), not an isobath. If it should be drawn, stroke the existing drying/depare boundary in the style; do not add a contour level.)

Deliberately **not** in the design, and why: block-max in any form (§5), a depth threshold as a safety mechanism (§5), and a vector area sieve (it deletes isolated shoals, which four sources forbid — the raster operators exaggerate them instead, which is mandated).

## 5. Alternatives refuted

**Block-MAX raster coarsening, mirroring `deep_coarsen` with the comparison inverted.** Refuted four ways, and the measurements are unambiguous:

- **Level-set invariance.** With mask `(arr >= t) & (up >= t)` and replacement `up >= t`, the set `{arr >= L}` is identical before and after for every `L <= t`. Only bands *shallower* than the threshold can change. Confirmed on real data: block-max at t=−1 or −3 leaves the −2 m and −5 m crossings and parts bit-identical at atchafalaya (10.57 and 370 across every variant).
- **The threshold is over-constrained.** Protecting the GIWW by depth value requires a threshold shallower than its 3.66 m project depth; reaching the −5 m band requires at least 5 m. No number does both.
- **With the safety gates it is a measured no-op.** Relief gate 0.25 m + sign-homogeneous blocks, across all four crops: crossings unchanged to two decimals, 0.0% water lost, drying does not grow, parts within 0.5%. The relief gate *alone* can make things worse — barataria 4,714 → 5,545 parts — because partial coarsening fragments bands. Block-max is also non-monotone in part count (atchafalaya p2..5 370 → 623 at f=8).
- **Without the gates it eats channels and manufactures uncertainty.** Water raised into `(0, DRYING_CAP]` enters the drying bucket, gets cut against effective land, and the surviving OSM water polygon emits an **unknown-depth feature** — grey "unsurveyed water" ribbons along mapped tidal channels, [[icw-landmask-clamp-gap]] by a new route. Measured 8.5–16.9% water loss at f=8.

**A −2.0 m threshold specifically.** `DEEP_COARSEN_THRESHOLD_M = -250` was chosen to sit *between* ladder levels so no level rides the transition ([`smooth.py:47`](../../pipelines/smooth.py#L47)). −2 is exactly a `CONTOUR_LEVELS` entry, 1 fm = −1.8288 m sits 17 cm away, and it is the shallowest S-52 selectable safety contour (a tested case in `style/index.test.ts`). Make the 2 m band a generalization artifact and shoal-draft users lose their safety contour to the 5 m one. If a depth gate is ever needed as a *cost* knob, −3.0 m is the ladder-clean choice.

**"Bias-shallow by construction" as a provable gate.** True per pixel, false per contour. Peters, Ledoux & Meijers §2, on max rasterization specifically: "picking the shallowest point per grid cell does not guarantee safe contours in principle… contour extraction algorithms perform a linear interpolation on top of the points present in the data structure… A bigger cellsize will result in more and more severely violated points." Any hard assertion has to be made on the raster, not the decoded vector.

**Post-polygonization area filtering.** The partition contract requires bands ∪ drying ∪ nodata to be pairwise disjoint and jointly cover the water ([`depare_run.py:24`](../../pipelines/depare_run.py#L24), asserted at :655). Dropping small parts punches holes; absorbing them needs a topological merge across band boundaries, which is more GEOS work than the pass being cheapened, arriving *after* `contour-p`, `make_valid`, `_subdivide`, and the 730 MB set. Note §4a is not this: coverage simplification is a vector operation that provably preserves the contract (0 overlaps, 0.00000% drift), which is exactly why it is the vector lever we take.

**Grey-scale morphological closing.** Genuinely attractive — extensive, offset-invariant, so seam exactness needs only a 2r halo and the existing `halo_px()` = 65 px covers r ≤ 32 free. But it is a local never-deepening operator and therefore closes narrow water: r=8 (38 m) fragments the barataria network from 0.49 to 0.34 largest-component share, and r=4 gives no better parts-per-water-lost ratio than pond fill. Keep it available as a tuning knob at r=2, not as the primary operator.

## 6. Current-code fixes

Four independent problems, three of them shoal-bias violations already shipped. All testable on their own.

**6a. `_uniform_coarsen` charts deeper than truth.** [`depare_run.py:160`](../../pipelines/depare_run.py#L160) rescues timed-out stems with `gdal_translate -q -r average`, averaging a −1 m shoal with −5 m water to −3 m. Measured on the real Atchafalaya fixture: **2,785,984 px charted deeper than source, worst 0.92 m** — roughly 4× the 0.25 m S-44 Special Order TVU at that depth — plus 91,692 px of connected water raised into the drying bucket. On the crops it also *increases* part count 779 → 1,374, so it is counterproductive as well as unsafe. `2026-07-21-depare-perf.md:48` explicitly rejected average-resampled overviews before this shipped as the retry path. `gdal_translate -r` has no `max` — use `gdalwarp -r max`. Its `-outsize 25%` is also not origin-anchored, so the rescue already breaks the seam contract; sizing the warp to the origin-anchored grid fixes both.

**6b. `smooth_array` blurs the shallows symmetrically.** [`smooth.py:70`](../../pipelines/smooth.py#L70) applies a symmetric Gaussian (σ = 4 px ≈ 1.1 mm at scale) to water ≤ `DEPTH_FULL` (30 m). A symmetric kernel can move a shoal deeper, against S-4 B-411.5. The slope gate limits where it applies but does not make it shoal-biased. Fix: clamp the shallow-band output to `max(out, dem)` — one line, keeps the denoise, monotone-shoaling. AHO step 3's upward-only Laplacian is the fuller version if the clamp proves too blunt.

**6c. `SMOOTH_CFG` does not carry the coarsening dials.** ~~[`build.smk:287`](../../pipelines/build.smk#L287) hashes the eight smooth knobs but not `DEEP_COARSEN_THRESHOLD_M / FACTOR / MIN_CHILD_Z`, though `prepare_window` applies them, so any sweep silently re-measures the old surface.~~ **Done** — the three dials are in the hash and a change to any of them re-keys the window. Any new §4 dial must be added there too; the comment above `SMOOTH_CFG` now says so.

**6e. `_rss_kb` is blind on macOS.** [`depare_run.py`](../../pipelines/depare_run.py) read peak RSS from `/proc/self/status`, which does not exist on macOS, so the `MemoryError` path reported `peak RSS -1 kB` on the very platform the local memory work runs on. **Done** — `resource.getrusage` is the fallback, normalized for the byte/kB difference between macOS and Linux.

**6d. `SLIVER_MIN_PX = 4` is below every published minimum.** [`depare_run.py:98`](../../pipelines/depare_run.py#L98) — 4 px ≈ 17 m² at z15, a 0.55 mm square, against 0.8 mm minimum islet dimension and 4 mm² minimum fill area. Make it scale-derived: ~8 px² for a 0.8 mm square, ~100 px² for 4 mm². The existing comment already names the right upgrade path ("a width/compactness gate is the targeted tool"), and §4b's connected-component labelling supplies compactness for free, so the two land together.

## 7. Gates

All six are implemented in [`pipelines/perf/gates.py`](../../pipelines/perf/gates.py) and run locally against a fixture in seconds (`just perf-gate raster <before.tif> <after.tif>`). Verdicts on the real Atchafalaya fixture: pond-fill 16 mm² **passes**; closing r=8 **fails** (largest water component 0.836 → 0.799, 100,732 px of connected water turned to drying); the shipped `-r average` rescue **fails** (2,785,984 px charted deeper than source, worst 0.92 m).

- **Shoal-bias on the raster, exactly.** `(out >= arr - 1e-9).all()` for every operator in §4b and §6. Cheap and provable. `-r average` fails it today; both §4 operators pass.
- **Partition contract on the vector.** Pairwise-disjoint interiors and area preserved after §4a — measured 0 overlaps and 0.00000% drift on three crops, so this is a regression test, not a hope. Calibration facts baked into the gate: disjointness holds *within* a ladder (m-vs-ft rows cover the same water by design); nodata rows are excluded (simplified to stem resolution by design — that is what `rank` is for); and shipped output already carries sub-pixel band-vs-band seam artifacts up to ~3.9 m² (make_valid/subdivide edges), so the violation threshold starts above that noise floor.
- **Bounded displacement for §4a**, since it is not shoal-biased by construction: assert boundary churn stays within the simplification tolerance, and record the tolerance as the S-58 legibility floor.
- **Named-route continuity.** GIWW, Bayou Lafourche, Houma Navigation Canal, Barataria Waterway, Southwest Pass as linestrings; assert each is wholly covered by water bands at its project depth in both A and B. Five lines, and it tests the thing that actually matters.
- **Connected-component count of the water-band union must not decrease**, and the largest-component share must not drop. This is the measurement that distinguishes pond fill from closing, so it belongs in CI.
- **Drying area must not increase along connected water.** Note the plain form of this gate ("drying area must not increase") would reject §4b, since filling an enclosed pond to marsh *is* drying growth — which is the licensed behaviour. Scope the assertion to water connected to the network.
- `seam_check check_depare` and `check_contours` across a coarsened/uncoarsened boundary and across a mixed-`child_z` seam.
- `ab_depare.py` decoded A/B on the delta stems: band sets identical. Rendered eyeball over the five bboxes in §1, channels specifically.

## 8. What this does not fix

The binding throughput constraint is `DEPARE_GB[15] = 36`, whose own comment already names the durable fix: "a per-stem reservation from the window's compressed size (perf backlog), not a bigger constant." Most z15 stems need ~12 GB. That fix stops the marsh class capping concurrency for every other stem, and it is independent of everything here. §3 shows §4 buys 2.2–11.6× on parts and 5.3–8.4× on vertices, which should bring the peak down materially — but the scheduler fix is still the cheaper lever for whole-build throughput and should land regardless.

The drying-geometry redesign ([[drying-geometry-plan]]) rebuilds the 0 m/drying geometry that §4b operates on, so sequence the two deliberately. One worry can be struck: **no crack is possible at the shared 0 m edge.** Bands and the `[0, DRYING_CAP]` bucket come from one `gdal_contour -p` pass on one surface with `--detect-shared-borders`, so an operator in `prepare_window` keeps the shared edge shared by construction.

A full fork rebuild is the eventual cost — this invalidates window, contour, soundings, and depare for every stem at or above the `child_z` gate. Sequence it with another change that already forces that rebuild rather than paying twice.

## 9. Open questions

1. **Where to put §4a in the depare flow**, given `contour-p` produces the partition and the contract is asserted right after. Coverage simplification has to run before the FGB write and before the assertion, on the whole band set at once (it needs the coverage, not one band).
2. **Per-zoom tolerance derivation** for §4a: 0.3 mm is the S-58 floor, but tippecanoe already simplifies at tile time, so the two must not compound into over-generalization. Check what tippecanoe's own tolerance works out to at each zoom before picking.
3. **Is the wetland polygon a better domain for §4b** than a connectivity+area rule? Coarsening inside mapped `natural=wetland` is the identity gate matching what NOAA charts. The drying plan contemplates a wetland layer but notes wetlands are in neither `land.fgb` nor `water.fgb` — new data, and OSM/Overture coverage in coastal LA needs verifying.
4. **Channel protection by identity, not by depth or connectivity.** Connectivity protects a channel that is *currently* connected in the DEM; a maintained channel silted below the surface between surveys is not. The durable answer is a no-coarsen mask from USACE NCD dredged areas or NOAA ENC `DRGARE` — `landmask.rasterize` already burns vector masks onto the window's grid. Interim proxy with no new source: don't generalize where a `priority > 0` chart-datum source wins (`noaa_s102` covers Houma, Morgan City, Port Fourchon, the Mississippi, Pontchartrain) — standard CATZOC logic, generalize hardest where the data is worst. Needs per-pixel provenance.
5. **S-102 band 2 (uncertainty) is the physically correct per-pixel gate**, discarded today via `band: 1` in every VRT. The eventual right answer, not this pass.
6. **Drop the −2 m band below z13.** NOAA's reschemed ENC contour table carries the 2 m contour only at 1:45,000 and larger (≈z13+ here), and 3/4/6/7/8 m only at 1:12,000 and larger. Free to justify and independent of everything above. Countervailing: NOAA CUSP removes waterways below 0.5 mm at scale, which is 3.7 m at z15 but **29.7 m at z12** — wider than parts of the GIWW, so low-zoom generalization needs channel protection *more*, not less.
7. **The undifferentiated-area endgame at low zoom.** S-57 UOC 5.8.3.1 licenses one shoalest `DEPARE` under a caution area; USGS emits one AREA OF COMPLEX CHANNELS. At z ≤ 12 the Louisiana delta is arguably one marsh polygon plus the channels.
8. **`terrain.py` never calls `prepare_window`** — its own `_read_window` + `smooth_tiff` path means the published DEM is never generalized. Harmless for `deep_coarsen` at −3000 m; at the shoreline the viewer's depth ramp and `readDepth` will visibly disagree with the depare polygon at the same pixel, with the raster reading *deeper*. Direction is acceptable, visibility is not. Decide and document, or generalize both.
9. **Mixed-source mosaic artifact.** Coarser neighbours upsample bilinearly into the window, so an operator straddling a GEBCO/S-102 boundary can propagate a coarse-source shoal into hi-res data. Bounded and in the safe direction (a max across a source seam preferentially selects the MLLW-referenced source, since NAVD88 reads deeper here) — but it should be a known artifact.
10. **Provenance honesty.** This is a deliberate accuracy reduction and a chart that generalizes says so (S-4 caution notes, CATZOC). The coverage layer will advertise z15 native resolution over depth areas generalized toward z13. Generalized regions should be attributable through the confidence/provenance work.
11. **Soundings.** Least depths survive a shoaling operator and spreading a shoal's least depth is chart-conventional, so they stay safe — but `SOUND_MIN_DEPTH_M = 1.0` means shoaled cells drop out, and a plateau of identical values reads as an artifact under the collision rules. Cosmetic; check in the A/B.
12. **The vessel-class question has no answer; stop asking it.** Pirogue on the platform · skiff 0.3–0.6 m · shrimp/crew boat 1.5–2.4 m · GIWW tug-and-barge 3.66 m project · Houma 15 ft · Southwest Pass 15.2 m. There is no depth above which nobody navigates here. Justify by positional determinacy and data provenance, never by "nobody goes there".

## 10. Status

Premise approved 2026-07-30 and reaffirmed after the production measurement: **the generalization ships regardless of the perf work.** The union fix (§3e) makes the build 4.6× faster and emits byte-identical output; only §4 touches the 1.04 GB / 62.3 M-vertex artifact, the S-58 vertex-density compliance gap, and the depiction. Design in §4 is measured (§3) but **not integrated** — neither operator exists in the pipeline yet.

Landed in the working tree (uncommitted): the local rig (§3b), the prerequisites §6c/§6e, **the §3e union fix**, and **§4a `coverage_simplify`**. §4a actuals on the production stem: vertices **62.29 M → 11.09 M (5.62×)**, FGB **1.0436 GB → 224 MB (4.65×)**, wall 1,517 → 1,104 s — with the union fix, the stem is **6,476 → 1,104 s (5.9×)** end to end and the artifact 4.65× smaller. Trap resolutions, measured: the ft-mode drying interface carries ≤1.25 m of `rank`-hidden overlap (0.17 mm at scale, below the legibility floor) and **gains no cracks** (raw drying rides both ladders' coverages; only m emits); band edges sit 1.108 m (0.53 px) off the drawn contour at cz15, against tippecanoe's own `-S 8` = 1.0 px at every non-leaf zoom, and Douglas-Peucker does not compound (0.00–0.33 m difference between covsimp-then-tippecanoe and tippecanoe-alone); `seam_check depare` on a real independently-built adjacent stem pair is **bit-exact** (sym-diff 0.0 on every band). Two pre-existing output defects fell out fixed: invalid rings from the 3857→4326 write fold (6 rows in production, now 0 — the sink rounds to the write grid in memory and repairs failures) and hairline band overlaps amplified by 7-decimal per-row rounding (3.9 → 18.9 m² raw→simplified, → 0.2 m² at 9 decimals).

Remaining, ordered by risk:

1. **Re-measure `DEPARE_GB[15]` on the box — now a PREREQUISITE, not a follow-up.** §4a holds a whole ladder resident to simplify it: local peak went 4.91 → 7.38 GB (+50%). The box measured 31.72 GB for the worst cz15 stem under the *old* code against a 36 GB reservation; a comparable increase would blow through it. The union fix pushes the other way (−33%), so the net is unknown until measured — no planet run before that number exists. Enable `DEPARE_TIMING` in the same dispatch.
2. **§4b pond fill — LANDED.** `smooth.pond_fill` in `prepare_window`: enclosed + sub-legible (16 mm²) + compact (75 m diagonal) + shallower than 2 m + `child_z ≥ 14`, filled to the ring maximum (monotone-shoaling structurally, 8-connected). Measured: marsh fixture parts **4.1–8.2×** down; production stem 1,104 → 903 s, depare peak RSS 7.38 → **5.09 GB** (repaying §4a's resident-ladder increase); window stage stays O(block) at 2.58 GB on the 65,666² window with 131,202 ponds filled; seam pair bit-exact; all six gates pass, gate 3's connected-water scoping confirmed at zero on every fixture. No strip union-find needed — the extent bound makes blocked processing exact (a component reaching a block edge is too big to fill by construction). True inland water is protected three ways: no-DEM-depth water ships via the untouched nodata layer, the 2 m depth ceiling spares surveyed basins, and the cz14 floor keeps mm-at-scale areas from inflating over coarse stems.
3. **Per-stem depare reservation — LANDED.** `depare_gb`: `rss_GB = 0.28 + 1.805 × own_tile_GB + 4` fitted on run 30634360224's 2,170 rows (7 cz15 anchors — the pad and `attempt` escalation carry the tail; constants remain the fresh-store fallback). Replaces the 36 GB constant that held cz15 depare to 4 concurrent while live RSS summed to ~12 GB.
4. **Coverage dissolve second chance — LANDED.** Run 30634360224 tripped the §3e guard 12× in 2,170 stems, always area-preserving invalidity (edge-sharing/pinched dissolve members). Before paying the full input union, re-node the dissolve *output* — a handful of mostly-disjoint regions, seconds instead of the 89k-part marsh bucket's 22 minutes. Pinned in `_check` with a minimal edge-sharing fixture.
5. **§6a / §6b / §6d** — independent shoal-bias and threshold fixes, each with a gate that already exists. Still open.

Gate 6 (named-route continuity) is implemented but has no data: `perf/routes.geojson` still needs ~40 lines of waypoints for the GIWW, Bayou Lafourche, Houma Navigation Canal, Barataria Waterway and Southwest Pass. Write it before §4b, since that is the gate that would catch a closed channel.

**Every change here is profiled locally before a production build.** The loop:

```
just contour-p                                    # once — production's DEPARE_CONTOUR_BIN
just perf-fixtures                                # once — small real windows over the marsh
just perf depare 12-1015-1699-15 base             # baseline on unmodified HEAD
#   ... make the change ...
just perf depare 12-1015-1699-15 change
just perf-compare base change                     # nonzero exit on a regression
just perf-gate raster <before.tif> <after.tif>    # shoal-bias, connectivity, drying growth
just perf-gate vector <before.fgb> <after.fgb>    # partition, displacement, routes
just test-perf                                    # every harness self-check
```

One thing the rig cannot answer, so it stays on the box: **peak RSS**. macOS `libmalloc` and Linux `glibc` differ in arena and reclaim behaviour, so local peaks are indicative only — the box's benchmark TSVs (`gh run download <id> -n snakemake-bench-<id>`) are the authority, as used in §3d. Wall, phase ranking and geometry counts do transfer: GDAL 3.13.1 and GEOS 3.13.1 match the container, and a production-scale window runs locally in full (§3c).

`DEPARE_TIMING` is not enabled on the box and its depare logs arrive empty (the truncating `2> {log}` redirect noted in `93da2d3`), so the §3c phase attribution exists from one local run only. **Enable `DEPARE_TIMING` in CI** so the split can be confirmed on Linux at scale rather than inferred from a laptop.
