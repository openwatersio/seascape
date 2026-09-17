# Depth areas compiled per usage band

_Written 2026-09-17 after planet run 35232921772 stopped on the Dutch-coast cell `8-131-84`. Point-in-time; the code is the source of truth. Companion to [2026-07-30-shallow-coarsening.md](2026-07-30-shallow-coarsening.md) (the native-resolution operators this builds on), [depth-areas.md](depth-areas.md) (the layer's contract) and [2026-07-21-depare-perf.md](2026-07-21-depare-perf.md). Standards and literature cited here are catalogued with verification status in [../nautical-chart-references.md](../nautical-chart-references.md)._

## 1. Why

The depth-area layer ships one partition, cut at each stem's native resolution, into every zoom from 6 up. Contours carry a per-curve zoom ladder (`contour_run.CONTOUR_TIERS`) and soundings carry pyramid levels, but `contour_run._build_seqs_and_run` hands every depth-area polygon the same `DEPARE_MINZOOM` of 6. An overview tile therefore carries the full-resolution partition, and on an intricate coast that partition is enormous.

Planet run 35232921772 stopped on cell `8-131-84` (the Dutch coast from the Haringvliet to the Zaanstreek) when tile `10/525/337` reached 993,779 bytes against the 800,000 ceiling, `--coalesce-smallest-as-needed` armed, and the pass discarded soundings the cell census then correctly refused. That tile was a clamp defect, not a generalization problem: 17,913 sub-pixel drainage ditches in the water mask let a coarse source's polder elevation through as 0–2 m depth bands — 556,559 polygons and 36,534 soundings on the specks. #196 gives the clamp's water exemption a width floor of the source's own cell; the stem drops to 380 polygons and 221 soundings and the tile to 42 KB. In the last full planet run (32030835194) that cell was the only one of 4,292 whose overview tiles exceeded the ceiling before coalescing, so with the floor in place no cell does.

What remains is the shape of the layer at overview zooms, on ground that really is water. The heavy tail of the store is `5-9-9-9` (Hudson Bay) at 345 MB, `8-70-104-14` (Georgia sea islands) at 332 MB, `8-70-103-14` at 319 MB, `8-62-105-14` (Atchafalaya) at 221 MB — every one a marsh, delta or archipelago coast where the ground oscillates across a band boundary pixel to pixel and `gdal_contour -p` polygonises it into salt and pepper. Their bytes are vertices rather than parts: the Georgia cell holds 28,430 depth-area polygons at ~11 KB each. Its overview tiles fit only because the tile writer collapses whatever is sub-pixel at each zoom — 20,690 polygons at z8, 8,705 at z10, 3,348 at z12 on that cell, and 43,621 / 20,988 / 2,413 on the Barataria cell (`tiny_polygons` per zoom, run 32030835194). On the Georgia fixture `12-1122-1670-14`, 90% of parts are under 16 px² at z10 and 65% at z12, carrying 15% and 6% of the vertices. A partition thinned per tile by the writer is the per-feature hiding the standards forbid (§2): it is decided by pixel size rather than depth, it can drop a shoal as readily as a pit, and every overview zoom still reads the full native partition into the shallow run. That is the case for compiling a partition per band. It is a cartographic and cost case, not a build blocker.

## 2. What the standards say

**Depth areas cannot be decluttered by hiding them.** `DEPARE` is Group 1 in S-57 and Skin of the Earth in S-101, and both forbid `SCAMIN` on it: "Group 1 and Meta objects must always be displayed. Therefore, SCAMIN must not be encoded on Group 1 and Meta objects." (S-57 App. B.1 Annex A, Ed 4.4.0, §2.2.7; S-101 Annex A DCEG, Ed 2.0.0, §27.156). The skin must tile the cell without gaps at every scale. An ENC handles scale by **compiling a separate, more generalized cell per navigational purpose** — six bands from Overview to Berthing, each with its own compilation scale. A per-feature `minzoom` on a depth-area polygon is the forbidden move in tile clothing, and it is what the constant `DEPARE_MINZOOM` does today. The conformant design is a separately compiled partition per zoom tier, each one complete and gapless.

**The ladder itself thins with scale.** NOAA's reschemed ENC design compiles a different contour set per band (ENC Design Handbook, Table 3, "based on depth intervals specified in the IHO S-101 ENC Product Specification"):

| band | scales | contours (m) |
| --- | --- | --- |
| 1 Overview | 1:10,000,000 / 1:3,500,000 | 100, 200, 300 … / 50, 100, 150, 200 … |
| 2 General | 1:1,500,000 / 1:700,000 | 20, 50, 100, 150, 200 … |
| 3 Coastal | 1:350,000 / 1:180,000 | 20, 30, 50, 100 … |
| 4 Approach | 1:90,000 / 1:45,000 | 5, 10, 15, 20, 30, 50 … (2 optional) |
| 5 Harbour | 1:22,000 / 1:12,000 | 2, 5, 10, 15, 20, 30, 50 … (3, 4, 6, 7, 8 optional, gently sloping bottoms only) |

A Coastal chart has no 2 m or 5 m depth area at all. The oscillation that shreds a marsh partition is almost entirely across the 0, 2 and 5 m levels (band crossings per raster row on the Gulf marsh: 0 m 7.8, −2 m 7.4, −5 m 4.8, against 1–3 below −10 m), so omitting those levels at coastal zooms removes the fragmentation at its source rather than cleaning it up afterwards. With 256 px tiles and the 0.28 mm rendering pixel the standard scales sit at z5.8 and z7.3 (Overview), z8.5 and z9.6 (General), z10.6 and z11.6 (Coastal), z12.6 and z13.6 (Approach), z14.6 and z15.5 (Harbour) — the arithmetic, not an IHO rule.

**Generalization may shoal and may never deepen.** S-4 states the smoothing rule directionally: "Where necessary, smoothing will include deeper water within shoaler contours (that is: it must be shoal-biased), but an attempt to retain a reasonable representation of the seabed should be made." (S-4, Ed 4.10.0, §B-411.5). Guilbert & Zhang (2012, §3.1) give the operator rules a partition needs: "An isobath can only be moved towards greater depths; An isobath can only be removed if the new representation is not deeper than the original representation; Adjacent isobaths can be merged if they are at the same depth and the area in between is deeper; Isobaths delineating fairways must be preserved," and the asymmetry that decides what happens to a fragment too small to draw: "A pit cannot be enlarged or aggregated; A too small or not relevant pit is removed; A peak cannot be removed; A too small peak is enlarged or aggregated with an adjacent peak." Removing a level merges its two bands into one carrying the shoaler bound, which reads shallower everywhere — licensed. A sub-legible fragment may be dissolved into a *shallower* neighbour (a pit removed) and never into a deeper one (a peak deleted). S-4 adds the counterweight: contours "not pushed seaward unduly", lest a steep-to danger be charted as a gradual approach (§B-403.1b), and channels must survive — "even at small scales it is important to show the usable channels" (§B-403.1c).

**Surface-first is documented production practice.** The Australian Hydrographic Office compiles contours by re-gridding at "three pixels per mm at compilation scale" with the shoalest depth per cell, expanding shoals to a "minimum target diameter of approximately 4 mm", an upward-only Laplacian smooth, contouring, then deleting only tiny *deep* closed rings under 10 mm diameter (Rustomji 2019); the Instituto Hidrográfico de la Marina documents the same shape with "a shoal-biased pattern (including deeper water within shoaler contour)" (Manzano 2021). Peters, Ledoux & Meijers (2013, §2) supply the caveat that keeps this honest: shoalest-per-cell gridding is safe per pixel, not per contour, because contouring interpolates between cell centres — so the safety assertion has to be made on the raster, and the polygonization must be pixel-exact, which classify-then-polygonize is.

**The thresholds are in millimetres at scale.** 0.3 mm maximum vertex density (S-58, Ed 8.0.0, check 571 — the one hard number in the ENC standards), 4 mm² minimum area for a solid-fill polygon in a subdivision (Galanda 2003, M4), and Töpfer's radical law as a volume check: areal features should shed roughly an order of magnitude every two zoom steps. At the 0.28 mm rendering pixel, 4 mm² is a 7×7 px square at any zoom.

**A tint boundary sits on a drawn contour.** This project's own rule, and paper-chart practice ([../cartography.md](../cartography.md)). Bands and lines at a zoom therefore have to be cut from the same surface with the same ladder, which is already how the native layer works: contours derive from the depare partition's edges.

## 3. Design

One idea: each zoom tier is a compiled band. The partition at a tier is cut from that tier's surface with that tier's ladder, and a tier's rows carry the zoom range they serve. Everything below is the consequences.

### 3a. One ladder for bands and lines

A tier has two independent dials: the **surface** it is cut from (which resolution) and the **ladder** it is cut at (which levels). Keeping them apart is what lets the visible cartographic question be decided by measurement instead of up front.

`CONTOUR_TIERS` becomes the ladder for both layers, with one boundary added at z13 that changes the surface but not the levels:

| tier | zooms | ≈ scale at z lo | NOAA analogue | surface | metre levels that bound bands |
| --- | --- | --- | --- | --- | --- |
| A | 6 | 1:8.7M | Overview | z6 pyramid level | 0, 200, 500, 1000, 2000, 3000, 4000 |
| B | 7–8 | 1:4.4M | Overview / General | z7 level | 0, 50, 100, 200, 300, 500, 1000, 2000, 3000, 4000, 5000, 6000, 8000, 10000 |
| C | 9–10 | 1:1.1M | General / Coastal | z9 level | 0, 10, 20, 30, 50, 100 … |
| D | 11–12 | 1:270k | Coastal / Approach | z11 level | every level (2, 5, 10 …) in the first pass; see below |
| E | 13+ | 1:68k | Approach / Harbour | native window | every level |

Fathom curves follow the rule `contour_minzoom` applies today: a curve shows once it is at least as deep as the tier's shallowest metre level. Zero closes the shoalest band at the shoreline in every tier, and the `[0, DRYING_CAP]` drying bucket rides every tier's metre pass exactly as it rides the native one.

**The surface dial is set unconditionally.** Tier D is cut from the z11 pyramid level rather than the native window. For a cz15 marsh stem, the native cut is 16× finer than a z11 pixel, which is the over-resolution that leaves 90% of the Georgia fixture's parts under 16 px² at z10, two zooms further in. Cutting at z11 resolution changes nothing about which contours a user sees; it generalizes the band edges shoal-ward exactly as tiers A–C do, and it is the re-grid step the hydrographic offices perform at every compilation scale.

**The ladder dial at z11–12 is decided by the fixtures.** The first pass keeps every level so no contour line changes. NOAA's table draws 5 m from Approach scale (1:90,000 ≈ z13) and 2 m from 1:45,000 (≈ z14); at z11–12 it draws 20, 30, 50 and nothing shoaler. This ladder is one band finer than NOAA's at every scale, which is a legitimate choice for a small-craft chart and costs geometry. If the surface tiering alone brings the cz15 marsh cells' z11–12 tiles under budget with the Töpfer gate met, the finer ladder stays. If it does not, tier D thins to 10 m and deeper, with 5 m entering at z13 and 2 m at z14 (§6, question 1).

### 3b. The tier surface

A tier's surface is the mosaic's own pyramid level at that zoom's resolution, read exactly the way the terrain render reads it (`terrain._read_window(anchor, tier_z, halo)`), then the shared stage-3 smooth at that zoom. That level is produced by `utils._block_reduce`: the class-aware shoal reduction, origin-anchored so neighbouring stems reduce identically, in which any water-domain child wins the block and the value is the max over water alone — a one-pixel channel stays open at every level, shoals bias shoal-ward, and land never leaks into a block holding water. It is the AHO re-grid step (shoalest depth per cell at ~0.3 mm per pixel) and it is the surface the raster relief already serves, so the band tint and the relief agree at every zoom by construction.

Tier E is the native window, unchanged: pond fill and coverage simplification at native resolution stay as they are. Pond fill is already gated to `child_z ≥ 14`; at coarser tiers the block reduction does that job, since a pond smaller than a tier pixel is gone before anything is cut.

Two candidates were considered for the surface and this one is preferred over block-reducing the smoothed native window: the mosaic level is what the served raster reads, so vector and raster cannot disagree at a zoom, and it costs no extra reduction. The known divergence — the terrain render does not apply `prepare_window`'s pond fill — is open question 8 of the shallow-coarsening plan and is unchanged by this design.

### 3c. Cut, clip and generalize per tier

`depare_run._depare_dem` already derives every tolerance from the zoom it is handed as `child_z`: the S-58 simplification tolerance (`SIMPLIFY_MM` at that zoom's pixel), the drying legibility gate (`DRYING_LEGIBLE_MM2` at that scale), nodata simplification (`NODATA_SIMPLIFY_PX`), and the sliver floor. Calling it with the tier's zoom on the tier surface therefore cuts, clips, dissolves the drying bucket, subtracts the land cut, and simplifies the coverage at that tier's legibility, with the seam contract intact — the tier surface is origin-anchored and the clip box is the stem's own bounds.

What the tier cut does not handle is a fragment that survives the coarse pixel and the coverage simplification but is still below 4 mm² at the tier's scale. Those get one more operator, on the tier partition, with the Guilbert & Zhang asymmetry built in:

- Label every band part under the legibility area (4 mm² at tier scale; `DRYING_LEGIBLE_MM2`'s 16 mm² is the stricter figure the drying bucket already uses, and the number is a dial).
- For each, find the bands it shares an edge with.
- If any neighbour is **shallower** (smaller `drval1`), dissolve the part into the shallowest neighbour. The area now reads shallower: a pit removed, safe.
- If every neighbour is **deeper**, the part is a local shoal. It is kept as it is. A peak is never removed; exaggeration to the minimum drawable size is the fuller treatment and is listed under open questions, not built here.

Adjacency comes from the partition's shared edges (the coverage is already noded), the candidates are a small minority of a tier partition that is itself small, and the dissolve is a union of two neighbours along a shared edge — bounded work, unlike anything run on the native partition. Drying parts go through `_illegible_drying` at tier scale as they do now; nodata rows are simplified to the tier pixel and dilated by `NODATA_OVERLAP_PX` at tier resolution, as they are now.

### 3d. Zoom placement and bundling

Every tier row carries `tippecanoe.minzoom` = the tier's first zoom and `tippecanoe.maxzoom` = its last, except tier E, which carries no `maxzoom` and persists through the Worker's overzoom — the rule soundings already use for their finest level (`soundings_run._tc`). `_fgb_to_seq` writes the `maxzoom` it does not write today. The variable-depth leaf guard already forces a cell to subdivide down to every feature's `minzoom`, which is how contour tiers materialise their zooms; each tier's rows force their own tiles the same way, so a coarse ocean cell whose leaf is z8 gains tier C tiles at z9–10 only where tier C rows exist. The completeness census is unchanged: a row's id appears in its own tier's tiles.

Contours keep deriving from the depare partition, now per tier, so at each zoom the lines and the band edges are one geometry. `contour_minzoom` retires: the tier column is the ladder.

The shallow run (z0 to the split) consumes tiers A and B only, which turns its input from tens of gigabytes of native partitions into kilobytes per stem — the "structural" shrink the post-build followups describe for it. Cell runs consume tiers C, D and E. On disk each tier is its own file, `store/depare/{stem}-t{z}.fgb`, produced by the one `depare_tile` job (Snakemake lists several outputs), so the shallow and cell runs read only the tiers they serve and the empty-tile sentinel and size filters work unchanged.

### 3e. What to expect

On the Georgia and Terrebonne fixtures at z10, tier C's ladder has no 2 m or 5 m level and its surface is four times coarser than native, so the 0/2/5 oscillation that fragments a marsh partition is not cut at all. Töpfer's law says the areal feature count should fall by roughly an order of magnitude over those two zoom steps; the ladder change alone should beat that on a marsh, because the fragmentation lives in the levels being omitted. The writer's `tiny_polygons` counts above are the baseline: the 8,705 Georgia parts the writer collapses at z10 should never be cut in the first place, and what the tier does emit is legible by construction.

Across the store, the 200–345 MB partitions become tier E only; tiers A–D for the same stems are kilobytes to low megabytes. The per-cell overview tile bytes and the shallow run's input size — items 10 and 11 of the post-build followups — are both addressed by the same mechanism.

## 4. Gates

Every operator here has a gate that fails on the mistake it is capable of, most of them already in `pipelines/perf/gates.py`:

- **Shoal bias on the raster, per tier.** `(tier_surface >= reduce(native))` pixel-exact — gate 1, run against each tier surface. Free, since the surface is the block reduction the mosaic already proves monotone.
- **Never deeper at any zoom, per polygon.** New. For every native band part, the tier partition's `drval1` at a sample of the part's interior points must be ≤ the part's own `drval1`. This is the polygon form of the surface test and it is what proves the dissolve (§3c) and the ladder merge never let a coarser zoom read deeper than the native layer. Cheap: point-in-polygon on a small partition.
- **Partition contract, per tier.** Gate 4: pairwise-disjoint interiors within a ladder, area conserved through the dissolve to the recorded tolerance.
- **Legibility, per tier.** New. No band part under 4 mm² at tier scale unless every neighbour is deeper (a kept peak). Count the kept peaks and report them; they are the exaggeration backlog.
- **Töpfer, per tier.** New. Areal feature count falls by at least ~85% at each step down the ladder over the same stem — E to D, D to C, C to B. A tier that fails is under-generalized; the gate is a floor on ambition, not a selection rule, and it is the measurement that decides the z11–12 ladder question.
- **Channels stay open at every tier.** Gate 6, the named routes in `perf/routes.geojson`, at each tier — and this matters more at coarse zooms, where NOAA's own 0.5 mm waterway floor is 30 m at z12, wider than parts of the GIWW. Add a Rhine route (Nieuwe Waterweg), the Noordzeekanaal and a Great Lakes route to the file alongside the Gulf set.
- **Seams, per tier.** `seam_check check_depare` and `check_contours` on an adjacent stem pair at each tier, since each tier is an independent cut whose only seam guarantee is the origin-anchored surface.
- **Bytes.** The Georgia, Terrebonne and Amsterdam fixtures' cell runs at every zoom under `VECTOR_CELL_TILE_BYTES` with zero coalesce events, and the shallow run's worst tile likewise. Tile bytes are within budget everywhere after #196; this gate keeps them there.
- **Safety contour at every zoom.** `ab_check`: at each zoom the band set equals the tier's ladder, and a safety depth between levels snaps to the next-deeper level *present at that zoom* — the style is unchanged and keys on `drval1`, so this is a tiles-side check that the snap target exists.

## 5. Sequence

Every step is measured on the local rig before any box time, per the loop in the shallow-coarsening plan (`just perf-fixtures`, `just perf depare <stem> <label>`, `just perf-compare`, `just perf-gate`).

1. **Fixtures.** Sites: `amsterdam` (`8-131-84-12`, cut as the z10 tile itself and reclamped with the #196 floor until the mosaic republishes), `georgia` (`8-70-104-14`), with `terrebonne`, `delmarva` and `iberian-abyssal` as the marsh, lagoon and open-ocean controls. Extend `perf/bench.py` to run a cell bundle on a fixture so tile bytes per zoom are a measured number, not a box surprise. Baseline parts, vertices, bytes and tile bytes per zoom on unmodified code.
2. **Tier surface and tier cut.** `depare_run.tile` loops tiers; for A–D it reads the tier surface through `terrain._read_window` and calls `_depare_dem` with the tier zoom; tier E is the current path. Multiple outputs in `build.smk`, `version` bumped, the tier ladder in `params` so a ladder change re-keys. Measure parts and vertices per tier against the Töpfer gate.
3. **Zoom columns.** `maxzoom` in `_fgb_to_seq`; the shallow and cell runs select tiers by zoom; `contour_run.tile` derives lines per tier; `contour_minzoom` and its call sites go. Census and `vector_selfcheck` green on the fixtures.
4. **The dissolve.** §3c with the never-deeper and legibility gates; measure what the kept-peak count looks like on the marsh fixtures.
5. **Docs.** `schema.md` gains the sentence contours already have — coarse zooms carry fewer bands, and which levels bound a band at a zoom is a display decision, not a schema guarantee. `cartography.md` gets a section beside "Contour generalization by zoom". The comment above `DEPARE_MINZOOM` and the shallow-run and cell-run tile-bytes notes in `contour_run` change to describe the tiers.
6. **Box.** A bbox smoke over the Georgia and Amsterdam stems (the build lane's documented practice), then the planet. This re-keys every `depare_tile`, which regenerates every window and cascades to contours and soundings through the temp-window rule in the dispatch runbook — a full vector rebuild, to be sequenced with a release rather than paid on its own.

## 6. Open questions

1. **The z11–12 ladder.** Keep the 2 m and 5 m bands from z11, or thin tier D to 10 m and deeper with 5 m entering at z13 and 2 m at z14, as NOAA charts those scales. Thinning is shoal-safe by construction — merged bands read shallower and a safety depth between levels snaps to the next-deeper band present, so the hazard tint covers more water, not less — and it removes exactly the levels the marsh fragmentation lives in. Its cost is information: at z12 (1:136,000) a shoal-draft mariner reads "≤ 10 m" where today they read three bands, and recovers them two zooms in. The fixtures decide it (§3a): surface tiering alone within budget keeps the finer ladder; otherwise thin. Either way it changes contour lines at those zooms, so it is not silent.
2. **Exaggerate kept peaks** to the minimum drawable size, as S-4 does for islets and AHO does with a 4 mm shoal expansion, or accept them as-is. The legibility gate counts them; the count decides whether it is worth building.
3. **Surface source parity with the raster.** The terrain render does not run `prepare_window`, so at native zoom the served DEM and tier E's partition can disagree where ponds were filled. Unchanged by this plan, still open.
4. **Provenance.** A generalized zoom is a deliberate accuracy reduction and a chart says so (S-4 caution notes; CATZOC in an ENC). The coverage layer advertises native resolution; the confidence and provenance work should be able to say a zoom is compiled.
5. **Schema.** Recommended as a note, not a bump: no field changes meaning, no zoom range narrows, and the band-set-per-zoom promise is one the schema never made. Worth a second opinion before the release that ships it.
6. **The fathom ladder's tiers** follow the mirror-by-depth rule contours use today; confirm on the fixtures that the ft partition at each tier has the same part budget as the metre one, since it is cut on the same surface with fewer levels.

## 7. Not this plan

- Raising `VECTOR_CELL_TILE_BYTES`. It defers the same failure to the next denser coast and ships larger tiles to every client meanwhile.
- A per-feature `minzoom` on depth-area polygons as the generalization mechanism. That is `SCAMIN` on the skin of the earth, and it opens holes in a partition.
- Vertex simplification alone. Already applied at the S-58 floor; it cannot remove a feature.
- Any change to `drval1`/`drval2` semantics, the style's colour keying, or the raster layer.
