# Contour-derived drying geometry — planning doc
*Written 2026-07-08. Point-in-time; the code is the source of truth.*

## Problem

The drying layer (green intertidal foreshore) renders boxy and bleeds. In the Nassau Sound / ICW tiles near Jacksonville it draws chunky, pixel-staircased blobs whose edges overrun the shoreline into land and overlap the adjacent depth bands — while the depth contours in the same tile are smooth. Two independent causes, both verified in code:

- **Boxiness — raster origin.** Drying is a binary threshold (`elev in [0, cap]`) rasterized to a Byte mask and traced with the GDAL polygonizer ([drying_run.py:135-141](../../pipelines/drying_run.py)), so every edge is a 90° staircase at the DEM pixel pitch. The only cleanup is a deliberately weak Douglas–Peucker that keeps axis-aligned runs straight by design ([drying_run.py:144-157](../../pipelines/drying_run.py)). The depth bands and contour lines in the same layer come from `gdal_contour` ([depare_run.py:96](../../pipelines/depare_run.py), [contour_run.py](../../pipelines/contour_run.py)) — interpolated sub-pixel isolines — so they're smooth. The two diverge entirely at raster-threshold vs. interpolated-isoline.
- **Bleed — deliberate raster overlap.** `DRYING_GROW_PX = 2` dilates into land to heal the OSM-shoreline↔DEM-datum registration sliver; `DRYING_SEAWARD_PX = 3` pushes the seaward edge into the water so it becomes the visible waterline (the "close the gap" work), resolved cosmetically by the `rank` sort key. Both exist only because a raster mask can't register against the vector shoreline or the vector band edges.

The seaward edge of a drying area *is* the chart-datum (0 m) isobath — which `gdal_contour` already computes for the depth bands (`DEPARE_LEVELS` ends at 0, [config.py:43](../../pipelines/config.py)), and currently discards as "land" ([depare_run.py:90](../../pipelines/depare_run.py)). So the smooth geometry we want is already being produced and thrown away.

## Goals / Non-goals

**Goals.** Drying edges are `gdal_contour` geometry, smooth like the contours. The seaward edge is byte-identical to the neighbouring shoal depth band's edge — one shared boundary, no double line, no bleed into the bands. The boundary sits precisely between low water (DEM 0 = chart datum, seaward) and high water (the OSM coastline, landward). The grow/overhang/`rank` workarounds and the whole raster drying path are deleted.

**Non-goals.** Datum correction — CUDEM's NAVD88 zero sits ~0.95 m above MLLW at Jacksonville, so some flats read deep regardless of geometry ([#16](https://github.com/openwatersio/seascape/issues/16)); this fixes the *line*, not the DEM's zero. `DRYING_CAP` semantics are unchanged. The `nodata` depth-areas (unknown-depth water) are a separate feature kind, untouched here. The OSM wetland/marsh layer is deferred and out of scope — but this plan's interaction with it is spelled out below, because it's a genuine crossover.

## Interaction with the eventual wetland layer (deferred)

The end goal is to include all water/wetland features in the vector tiles. Drying and wetland are orthogonal and complementary, not competing, and this plan is designed to keep them that way:

- **Drying answers "does this cover/uncover with the tide?" — depth semantics, from the DEM.** The wetland layer answers "what is the surface cover?" — land cover, from OSM tags. A salt marsh is legitimately both.
- **This plan does not classify marsh.** It can't distinguish marsh from a bare tidal flat — both are just `[0, cap]` terrain on the effective-water side of the coastline, and both render as drying (as they do today). The effective-land cut resolves marsh correctly by geometry: marsh seaward of the OSM coastline is effective water → drying; marsh landward (supratidal/freshwater) is inside the land polygon → cut → not drying. The wetland *tag* is not consulted (osmdata land is coastline-derived; wetlands aren't in `water.fgb` either).
- **Chart convention is to compose them.** INT1/S-4 draws tidal marsh as the green drying tint *plus* a vegetation symbol. So the drying fill is the green base and the wetland layer overlays a *symbol* — it must NOT carry its own green fill over intertidal marsh (two fills would compound). A wetland *fill* is wanted only where there is no drying underneath (marsh the DEM misses).
- **Do not subtract marsh from drying.** They coexist; the wetland layer classifies and symbolizes, it doesn't carve the drying fill.
- **Positive interaction:** bounding drying precisely at the coastline with a smooth vector edge (no more grow into land) gives the wetland-symbol overlay a clean seam to key off, instead of a boxy drying fill that overran into the marsh.
- **The crossover to decide when the wetland work happens (not here):** a `wetland=tidalflat` polygon *is* a drying area. Where the DEM covers it, this plan already draws it as drying. Where the DEM does not, should the OSM tidalflat polygon emit a drying feature as a fallback? That is the wetland work's decision — the point where the OSM feed feeds back into the depth-semantics layer.

## Approach

Move drying from raster-then-smooth into `depare_run`, derived from the same `gdal_contour -p` pass that makes the bands.

- **Reclaim the `[0, DRYING_CAP]` bucket.** `depare_run.partitions` already runs `gdal_contour -p` and drops every bucket with `amin >= 0` as land. Add `DRYING_CAP` as a positive level and keep the `[0, DRYING_CAP]` bucket instead of dropping it. Its seaward edge is the 0 m isoline — and because `gdal_contour -p` shares boundaries between adjacent buckets, that ring is the *same geometry* as the shoalest water band's `amax = 0` edge. `--detect-shared-borders` (already on in the depare tippecanoe run) then simplifies the shared edge identically for both polygons at every zoom: no crack, no wobble, no overhang needed.

- **Cut the landward side by EFFECTIVE land, not raw OSM land.** This is the load-bearing correctness point. The drying region is `[0, cap]` restricted to *effective water* — exactly today's raster gate `in_range & (mask == 0)` ([drying_run.py:99-100](../../pipelines/drying_run.py)), where `mask` is the land∖water raster (`rasterize` burns land=1 then water=0, [landmask.py:243,249](../../pipelines/landmask.py)). So the vector cutter must be **`effective_land = OSM_land ∖ OSM_inland_water`**, not the raw land polygons.

  Why it matters — and why raw land re-breaks the ICW: the osmdata land product does **not** punch inland water out as holes, so a tidal channel OSM maps as a water polygon sits *inside* the land coverage. Cutting the `[0, cap]` bucket by raw land would delete the drying flats in and along that channel — the exact ICW/tidal-river failure this whole project exists to fix. Subtracting `land ∖ water` keeps the vector drying identical in extent to the current raster gate, only with smooth contour edges.

  Equivalently (and avoiding a full `land.difference(water)` per tile): `drying = bucket.difference(land) ∪ (bucket ∩ water)` — the exact shapely sequence is an implementation detail (see Open questions on cost). `depare_run` already reads `water.fgb` by bbox and runs `unary_union`/`difference` for the nodata path ([depare_run.py:167-182](../../pipelines/depare_run.py)); this adds a `land.fgb` bbox read alongside.

- **Emit as a depare feature, geometry raw.** `drval1 = -DRYING_CAP`, `drval2 = 0`, no `sys` (unit-independent, ships once), `rank = 2` (now cosmetic — drying and bands are disjoint by construction, so it can stay or go). No Chaikin, no per-feature simplify — same rule the bands follow ([depare_run.py:36-40](../../pipelines/depare_run.py)), so the shared 0 m edge stays aligned. Style needs no change: `drval1 < 0 → flavor.drying`.

- **Delete the raster path.** `drying_mask`, `_dilate`, `polygons`, `smooth_polygons`, `DRYING_GROW_PX`, `DRYING_SEAWARD_PX`, and the `drying_run.generate` fork in `aggregation_run`. `_polys` stays (depare uses it). Net LOC down.

The three depare feature kinds then all come from one generator on one DEM: bands = buckets with `amax ≤ 0`; drying = the `[0, cap]` bucket ∩ effective-water; nodata = OSM water ∖ DEM coverage. Coherent, and all smooth.

## Alternatives considered

- **Cut by raw OSM land polygons.** Rejected — re-breaks the ICW (above): deletes drying flats inside tidal-channel water polygons that the osmdata product nests inside land.
- **Marching squares on a blurred mask.** De-boxes the edge but keeps the raster origin, so the seaward edge is still a re-derived mask edge, not the band's 0 m isoline — the overhang, grow, and `rank` all remain. Cosmetic only; ~equal effort to the contour approach for less payoff.
- **Chaikin the drying ring.** Rejected in existing code and here: self-intersects on thin foreshore necks, biases inward, and per-feature smoothing opens see-through cracks against the abutting band ([depare_run.py:36-40](../../pipelines/depare_run.py), [drying_run.py:145-151](../../pipelines/drying_run.py)).
- **Tune the DP tolerance / pre-blur the mask.** Cheapest stopgap; reduces worst staircasing but keeps straight boxy runs (DP by design) and the bleed. Holding action, not the fix.
- **Contour-derive was previously rejected** ([2026-07-07-depth-areas.md](2026-07-07-depth-areas.md), and the note at inland-water Part 4). Half that rationale — "drying and bands are separate tippecanoe runs, so `--detect-shared-borders` can't align them" — went **stale** when drying folded into the `depare` layer (same layer, same run). It's viable now *because* of that unification; the surviving objection (post-intersecting rebuilds drying with extra steps) is the accepted price of a smooth shared edge.

## Validation

- **Extent parity:** the new vector drying covers the same places as today's raster gate (`[0, cap] & mask==0`) — spot-check a tile against the old `drying_mask` output; differences should be edge-smoothing only, not appear/disappear regions. Critically, the ICW tidal channels keep their drying (the raw-land failure does not occur).
- **Shared edge:** the drying seaward boundary is coincident with the shoal band edge in the tiles (no crack, no overhang into the blue, no green over land).
- **Smoothness:** Nassau Sound preview (`just preview "-81.55,30.28,-81.38,30.44"`) shows smooth foreshore edges, not pixel staircases; no bleed into land or bands.
- **Seam contract:** neighbouring tiles' drying edges meet at the clip line (deterministic on the buffered DEM, same as bands).
- **Disjointness:** bands ∪ drying ∪ nodata still pairwise disjoint (drying now disjoint from bands by elevation sign, not by `rank`).
- Self-check in `depare_run` covering the `[0, cap]`-minus-effective-land derivation, replacing the retired `drying_run` mask checks.

## Open questions

- **Landward-edge source of truth.** Cutting by the OSM coastline makes the OSM high-water line the landward edge, even where it disagrees with the DEM's own near-shore elevation. That matches today's raster gate (which also uses the OSM mask) and is almost certainly right (OSM coastline is the mapped high-water line), but worth confirming in preview where the two diverge.
- **Cost.** Per-tile: one extra positive contour level (near-free) + a `land.fgb` bbox read + shapely difference. The land polygon can be large near complex coastline; `bucket.difference(land) ∪ (bucket ∩ water)` avoids materializing `land ∖ water` globally. Measure on a coastal macrotile.
- **Sliver filter.** The nodata path min-area-filters slivers; does drying need the same along the 0 m seam where the bucket and effective-land edges nearly coincide? Probably yes — reuse the nodata threshold.
- **`drying_run.py` fate.** After deleting the mask path, does anything else consume the module (does the standalone per-tile drying FGB have any remaining reader), or does the file reduce to `_polys` and get folded into depare entirely?
