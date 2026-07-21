# The published DEM as "depth below water" — analysis & recommendations

_Written 2026-07-16, Point-in-time; the code is the source of truth._

## Problem & scope

The published terrain-tile product (Terrarium WebP, `terrain.py` render → `encode.py`) currently carries all the land topography present in the source data — planet-wide, since GEBCO (the global base, an ice-surface _elevation_ grid) supplies it everywhere, with trusted topobathy sources adding finer land detail near the coasts. For a nautical chart's depth layer this is a waste of bytes and misleading about what the product is. **Scope: any clamp discussed here applies only at the final product encode.** The internal mosaic stays a full signed elevation surface — contours, depare (including the drying bucket extracted from values in `(0, DRYING_CAP]`), and soundings all need it raw.

## What land topography costs (measured on production tiles)

Sampled from `tiles.openwaters.io/seascape`, decoded, re-encoded through the pipeline's own `encode.py` with a `min(elevation, 17)` clamp, lossless-WebP sizes compared (script: session scratchpad `measure_prod.py`):

| Spot               | Tile       | Today  | Pixels > 17 m | Clamped | Saving |
| ------------------ | ---------- | ------ | ------------- | ------- | ------ |
| Colorado Rockies   | 8/52/97    | 100 KB | 100%          | 0.1 KB  | 99.9%  |
| Alps               | 9/267/182  | 194 KB | 100%          | 0.1 KB  | 99.9%  |
| Himalaya           | 8/187/107  | 85 KB  | 100%          | 0.1 KB  | 99.9%  |
| Sahara             | 8/135/111  | 39 KB  | 100%          | 0.1 KB  | 99.9%  |
| Kansas             | 8/58/98    | 25 KB  | 100%          | 0.1 KB  | 99.8%  |
| Norway fjords      | 9/265/145  | 184 KB | 80%           | 31 KB   | 67%    |
| Bay of Fundy       | 10/328/365 | 118 KB | 46%           | 24 KB   | 44%    |
| Boston coast       | 10/310/378 | 322 KB | 9%            | 67 KB   | 11%    |
| Netherlands        | 10/525/335 | 58 KB  | 1.5%          | 30 KB   | 2%     |
| Mid-Atlantic ocean | 8/96/105   | 65 KB  | 0%            | 65 KB   | 0%     |

Sample total: 68% smaller. Two structural notes:

- The waste is amplified by the encoder itself: `SHALLOW_REL` caps the quantization step at 1/16 of `|value|`, so low-elevation land gets ~2 m precision it never needed. We ship a topo map of the Himalaya inside a nautical chart.
- The clamp is self-limiting where it matters: the Netherlands tile (87% of pixels in the 0–17 m band) saves 2% — genuine near-water elevation is untouched. That is the property `min(elevation, DRYING_CAP+1)` buys over masking land to a flat 0.
- Lake Victoria (9/302/257) decodes as 100% "land" — its GEBCO bed elevation (+1100 m re MSL) renders as mountainside. See inland water below.

## Standards frame

A chart is two vertical datums stitched at the shoreline: depths and drying heights against a **low-water chart datum** (LAT internationally, MLLW in the US), coastline and land heights against a **high-water datum** (≈MHW/MHWS). Land elevation is _not on the depth scale at all_ — crossing the coastline changes datum. S-102 carries no land concept (dry land is fill `1.0e6`); ENC partitions the skin of the earth into `DEPARE`/`LNDARE`/`UNSARE`, with drying encoded as `DEPARE` with negative `DRVAL1` bounded by the ~MHW coastline. The pipeline's structure — drying `[0, cap]` seaward of the OSM (~MHWS) line, folded into depare with `drval1 = -16` — is the raster analogue of exactly that partition.

Consequence: collapsing land to a sentinel discards nothing that was ever a valid depth. The positive tail above the drying ceiling is semantically incoherent _as depth_; a sentinel converts it from fake data into an explicit label. The clamp is also **orthogonal to navigation safety**: it touches only values > cap+1, which can never become water, drying, or a shallow-biased depth. Bias-shallow is preserved; nothing anywhere biases deep. Ground above HAT−LAT never floods, so land that reads "+18 in a mega-tidal estuary" is permanent land by definition, correctly collapsed.

How other published raster DEM/bathymetry products mark land: there are only two conventions — real continuous topobathy (GEBCO, ETOPO 2022, SRTM15+, AWS/tilezen Terrain Tiles, Mapterhorn) or a large out-of-band fill (BAG/S-102 `1e6`). An alpha-less, client-hillshadeable encoding rules out the giant fill (it would render a 1,000 km cliff at every coast). A low sentinel plateau is novel, but it is the only option that fits Terrarium + hillshade + a depth-product contract; the drying band and monotone clamp make it well-behaved.

## Evaluating `min(elevation, DRYING_CAP + 1)`

What it does well:

- **Monotone and continuous.** Unlike a "land → 0" mask, the surface never jumps: shore relief rises smoothly from the waterline to the plateau. No fake scarp beyond the gentle cap edge; client hillshade (off by default in the viewer) degrades gracefully.
- **Preserves the foreshore.** Everything in `[0, cap+1)` — the drying band plus a margin — survives exactly, so the raster keeps its intertidal signal for relief shading and `readDepth`.
- **Keeps 0 unambiguous.** 0 already means "chart-datum shoreline" and is the fill for unknown-depth mapped water. A land→0 mask would overload it further; the sentinel keeps land distinguishable from both.
- **Nearly free at the pipeline level.** Applied at the terrain render/encode stage, it re-keys only the render (`TERRAIN_MODULES` includes `encode`/`terrain`), so the planet re-renders from the fully cached mosaic — no re-merge, no source work. The vector products are untouched by construction.

One correction to the proposal as stated: **+17 is the single worst sentinel value under `encode.quantize`.** For magnitude ~17 the depth-aware step is 2 m, and conservative ceil rounding snaps upward: an input of +17 decodes as **+18 at z0–z10** and +17 only at z≥11 (verified against the real `quantize`; +16 and +18 are stable at every zoom). Two clean resolutions:

1. Keep the input clamp at `DRYING_CAP + 1` and specify the client contract as a **threshold, not a value**: decoded `v > DRYING_CAP` ⇒ land/out-of-scope. Rounding is always upward, so the sentinel never dips into the drying band at any zoom. (Recommended — simplest, and the contract reads naturally.)
2. Or clamp to a zoom-stable sentinel (+18) if a single exact decoded value is ever needed.

**Resampling caveat.** The Worker's overzoom path interpolates decoded elevations. At a water/land boundary, blending a negative depth with the positive sentinel can produce intermediate values in `[0, DRYING_CAP]`; these are rendering samples, not necessarily measured drying heights. Likewise, terrain smoothing occurs before the final clamp, so land relief can shallow nearby water or produce positive transition values. The `depare` polygons — not the raster alone — remain authoritative for drying and unknown-water classification.

Client-facing rules on native published pixels: `v < 0` is depth-like elevation and `v > DRYING_CAP` is definitely land/out-of-scope. Values in `[0, DRYING_CAP]` require the `depare` layer to distinguish genuine drying height from shoreline, unknown water, and resampling transitions.

## Inland water (the OSM land-clamp interaction)

The final raster over inland water is currently a patchwork, and the sentinel clamp alone doesn't fix it — but it doesn't hurt it either, and the pieces of a coherent contract already exist:

- **Surveyed lakes** (great*lakes, African lakes, Swiss lakes, Tahoe, …) are offset at prep to \_depth below the lake's own surface datum* — already the right semantic. Official practice matches: inland chart datum is always a per-water-body plane (Great Lakes LWD on IGLD 1985, Caspian's own gauge datum), never MSL.
- **Unsurveyed mapped lakes**: `clamp_positive_water` (#24) clears coarse positive lakebed "topo" to nodata; the merge fills 0; the depare `nodata` polygon labels it unknown-depth water. The ENC analogue is `UNSARE` — water present, depth not asserted. **Keep 0 as the raster fill** (Terrarium has no nodata channel and 0 behaves correctly in every path), but the contract must state that 0 over mapped water means _depth unknown_, not _depth ≈ 0_; the `nodata` vector layer is the disambiguator. The relief-only land wash is fixable — see "Decoded 0 as unambiguous water" below.
- **The Lake Victoria case** shows what happens without #24 coverage or where GEBCO's real above-MSL lakebed survives: the raster carries +1100 m values that are meaningless as depth. The sentinel clamp at least collapses these to "land" instead of shipping fake mountainside relief — an improvement, though the correct end state is `0 + nodata polygon` (water, unknown depth).
- **Cryptodepression lakes** (Baikal, Tanganyika: surface above MSL, bed below) keep MSL-referenced _negative_ GEBCO values that no mariner can use — the positive-only #24 clamp never touches them. Known ceiling (documented in `landmask.rasterize`); the fix is per-lake surface datums, not the sentinel.
- **Coverage ceiling**: channels OSM maps only as waterway centerlines stay "land" in the mask (documented in `prep_water`); they'd collapse to sentinel like any land. Gated on OSM growing polygons or another feed.

The coherent per-pixel contract — "negative = depth below the _local_ water surface, whatever that surface is" — is already half-implemented by the surveyed-lake offsets. The remaining work is per-lake surface datums for the rest (source research below), and the VDatum/datum work must never overwrite the 0-fill of unknown lakes with spurious MSL-derived depths.

## Decoded 0 as unambiguous water (added 2026-07-21)

Goal: relief-only rendering shows `v == 0` as water and `v > 0` as land, per-pixel, no depare lookup. The encoder already guarantees most of this:

- Measured water never decodes 0: `quantize` caps any negative input that would round to ≥ 0 at `-LSB` (self-checked).
- Real land never decodes 0: conservative rounding is ceil, and the depth-aware step floor lifts even `+LSB` to `+0.25` — positive input stays positive at every zoom.

So decoded `0` ⇔ mosaic value exactly `0.0`: the merge's unknown-water 0-fill, plus one contaminant — `land_clamp` cells clamped from negative to 0 on the _land_ side of the OSM line. Terrain smoothing launders the narrow coastal halo of those (mixed neighborhoods smooth to nonzero), but wide flat clamped areas survive as exact 0; Dutch polders below MSL are the concrete case that would tint as water under a style-only fix.

Fix, two pieces, both render-path-only:

1. **Terrain render** (`terrain.py`, after smoothing and nodata-resolution, before encode): rasterize the COMBINED land mask for the window (`landmask.rasterize`, land=1 with inland water burned back to 0, same grid contract as the #24 machinery) and nudge `(v == 0) & land` to `+LSB`. Refinement over this section's original wording (implemented 2026-07-21): key on the combined mask, not the water-only mask. Keying on `~water` would turn every exact-0 ocean pixel into land; keying on `land==1` nudges only the land side — clamped polders and the beyond-build land fringe become land, while exact-0 over ocean AND over mapped inland water stay 0 (water, depth unknown). Degrades to today's behavior (no nudge) when no land mask is published.
2. **Style** (`depthRamp`): replace the `-LSB → 0` water-to-land fade with stops at `0` (water tint — a dedicated unknown-water tint per ECDIS convention, or `bandColors[5]`) and `+LSB` (land). Update the `readDepth` doc comment.

Accepted caveats: overzoom/smoothing interpolation still feathers lake edges between the tints (rendering samples, per the resampling caveat above), and drying foreshore `(0, cap]` keeps rendering land-ish in relief-only mode — depare supplies the drying tint. Composes with the sentinel clamp (same code region, same render-only re-key); ship both in one PR, one planet re-render.

## Per-lake surface datums: where the numbers come from (researched 2026-07-16)

Converting GEBCO's MSL-referenced lakebed values to depth-below-local-surface needs one scalar per lake — the water-surface elevation — joined to the water polygons `prep_water` already pulls from Overture. Surveyed authoritative sources of that scalar, ranked:

- **HydroLAKES v1.0 (primary).** 1,427,688 lake/reservoir polygons ≥ 10 ha, each carrying an `Elevation` attribute (lake surface, m a.s.l.), CC-BY 4.0, ~800 MB GDB/shapefile from hydrosheds.org. The decisive property: `Elevation` is DEM-derived (EarthEnv-DEM90 = SRTM+ASTER majority pixel per lake; GTOPO30 north of 60°N), the same SRTM lineage as GEBCO's land surface — so `GEBCO − Elevation ≈ 0` over unsurveyed lakes (SRTM water-flattening) and becomes genuine depth exactly where GEBCO carries surveyed lakebed relief (Lake Victoria's +1100 m bed → real depths below a ~1134 m surface). The subtraction is sign-blind, so it also fixes the cryptodepression ceiling (Baikal, Tanganyika) that the positive-only #24 clamp can never touch. No shared ids with OSM — the join is spatial (max-overlap of HydroLAKES polygon onto the Overture polygon), computed once at `prep_water` time into a `surface_elev` attribute on `water.fgb`. Known staleness: a ~2000-era snapshot, wrong for post-SRTM reservoirs and big-swing endorheic lakes (Caspian, Aral, Great Salt Lake) — exactly the set worth altimetry overrides later.
- **OSM / Overture attributes (too sparse to be the base).** `ele` sits on only 3.4% of `natural=water` objects (792k of 23M), a chunk of them literal-zero NHD-import placeholders; datum is nominally EGM96 but unenforced. Overture's `base/water` schema has **no** elevation property (only `land`/`land_use` get one) — the OSM `ele` string survives in `source_tags`, and the top-level `wikidata` field is a passthrough of the OSM tag.
- **Wikidata P2044 (sparse high-confidence override).** ~150k lake items carry "elevation above sea level" (about half of Wikidata's lakes, CC0) — but only 0.58% of OSM water polygons carry a `wikidata` tag to reach them, so the join yield is bounded to major named lakes. Datum is unmodeled per value, and 551 values are in feet (unit qualifier must be read, not assumed).
- **Government sources (regional refinements, three real ones).** Norway NVE Innsjødatabase: ~243k lakes with `HØYDE_MOH` (m over havet, NN54), open data. New Zealand LINZ `lake_poly.elevation` ("above mean sea level", CC-BY). Switzerland swissTLM3D: surface elevation as the shoreline geometry's Z (LN02). Everything else came up empty: US NHD's `ELEVATION` field is sparse (mostly reservoirs, datum undocumented) and the 3DHP successor drops it entirely; Canada NHN/CanVec, Copernicus EU-Hydro, and Australia GA carry no surface elevation at all.
- **Satellite altimetry (observed, for the volatile tail).** SWOT LakeSP/PLD (~6M lakes, observed water-surface elevation on EGM2008, PO.DAAC), DAHITI (~20k time series), Copernicus GLS Lake Water Level (~4.2k), Hydroweb. Accurate but time-series-shaped and on an independent datum — an offset against the DEM-lineage baseline is expected, so these are per-lake overrides for the Caspian class, not the join table.
- **Mapterhorn / Copernicus GLO-30 (no beds; a fresher surface reference).** Mapterhorn is a land-surface product end to end: its global tier is Copernicus GLO-30, a TanDEM-X radar surface model whose hydro-editing flattens lakes to one constant elevation and steps rivers monotonically downstream — no bed information exists in it, and its regional lidar tiers are hydro-flattened DTMs too (lidar doesn't penetrate water). So it adds nothing over GEBCO for lake/river beds; GEBCO is the one that actually carries surveyed lake bathymetry. The useful part is the flip side: GLO-30's flattened lake surface *is* a per-lake surface elevation, on EGM2008 from ~2011–2015 acquisitions — a decade fresher than the SRTM-era surface behind HydroLAKES `Elevation`. Sampling it inside a water polygon is a cross-check or fallback for post-SRTM reservoirs, at the cost of the SRTM-lineage self-consistency that makes the HydroLAKES subtraction a near-no-op over unsurveyed lakes.

Integration shape mirrors the dgm_w branch's "subtract the local low-water surface" step, scalar edition: join `surface_elev` at prep, attribute-burn it per tile (a sibling of `rasterize_water`), subtract inside lake polygons on flagged sources; lakes below HydroLAKES' 10 ha floor keep today's #24 → 0-fill path. The self-consistency property makes the change low-risk: where nothing was surveyed, the subtraction is a near-no-op by construction.

## DRYING_CAP: right number, wrong justification

The `config.py` comment anchors 16 m to "~the Bay of Fundy tidal range." The correct anchor for a drying ceiling is the **global maximum of HAT − LAT** — the highest ground that can still flood and dry. Numerically these coincide (Fundy extreme range ~17.0 m at Burntcoat Head; Ungava ~16 m; Bristol Channel ~15 m), so the value stands; rewrite the comment. Two named biases are inherent and acceptable for a global product: over-inclusion on low-MHW coasts (bluff toes up to 16 m tint as foreshore — the existing "spatially-varying MHW" upgrade path), and under-inclusion of the MHW–HAT band in mega-tidal estuaries (it sits on OSM's land side). A future spatially-varying HAT−LAT surface fixes the _classifier_; the _sentinel_ can stay a global constant since it only needs to exceed the planetary maximum.

Mixed datums shift the band where MSL-ish sources win near shore: MSL−MLLW is ~0.1–0.5 m micro-tidal, 0.7–1.5 m meso, 2–8 m macro/mega-tidal. Shallow-biased (drying reads as submerged), so safe but under-mapped; the structural fix is the planned datum unification (prefer LAT everywhere per IHO S-4, or MLLW if US-alignment dominates), which must transform both signs so near-shore submerged-reading drying crosses to positive.

## The published-raster contract (proposed wording for CONTRIBUTING)

> **Published DEM (Terrarium-decoded, metres) — value `v`:**
>
> - `v < 0` — chart-datum elevation below datum. On measured water pixels, `-v` is shallow-biased charted depth. Datum is the winning source's low-water datum (LAT / MLLW / ≈MSL for GEBCO) until datum unification.
> - `v > DRYING_CAP` — definitely **land / out of scope.** This is a sentinel (nominally `DRYING_CAP+1`, though the decoded value varies with zoom quantization), not measured elevation.
> - `v == 0` — water present, **depth unknown** (ENC `UNSARE` analogue), not a measured depth of approximately zero. (Unambiguous once the render-stage 0-nudge lands; until then, requires the `depare` unknown-water polygon.)
> - `0 < v ≤ DRYING_CAP` — a non-submerged sample. The `depare` layer distinguishes genuine drying foreshore from shoreline and values introduced by smoothing or overzoom interpolation.
>
> Raster values form a continuous rendering surface; `depare` supplies the categorical water/drying/unknown distinction.

## Alternatives considered

A note that strengthens every flattening option: the PMTiles writer dedups identical tile bytes (hash → offset + run-length in `pmtiles.writer.Writer.write_tile`), so once inland tiles quantize to an identical flat plateau they are stored once per archive — inland coverage becomes directory entries, not payload.

- **Two DEM products: depth + drying (split).** Ship `depth.pmtiles` (strictly `v ≤ 0`; land, drying, and unknown water all collapse to 0) and a sparse `drying.pmtiles` carrying only `[0, cap]` heights where intertidal pixels exist (a tiny archive — most coastal tiles have none, ocean/inland tiles are absent entirely). Pros: each product has a one-line contract; the depth product finally satisfies "never > 0" literally, and is the natural home for "depth below the *local* surface" semantics once per-lake datums land; the drying product is a raster analogue of an S-102 drying-height surface and could stand alone. Cons: a second raster source in the style (two hillshades, two fetches on intertidal coastal tiles), `readDepth` consults both, and double the product surface (render path, archive, TileJSON, worker route) — though both render from the same cached mosaic, so the pipeline cost is plumbing, not compute. Crucially it does **not** solve the residual 0-ambiguity (shoreline vs unknown-depth water); that moves into the depth product unchanged. Verdict: the principled v2 if a depth-only consumer materializes; nothing in the sentinel clamp forecloses it (the depth product is derivable from the clamped one client- or worker-side as `min(v, 0)`).
- **WebP alpha channel as the land mask.** Ruled out: browsers premultiply alpha on image decode, corrupting the RGB payload wherever alpha < 255 — which is why no terrain-RGB product uses alpha. Non-starter for MapLibre raster-dem.
- **Depth-only raster + vector land.** Clamp everything ≥ 0 to 0 and render land from vector polygons (the OSM land mask is already in hand; the style-extraction plan contemplates a basemap layer). A crisp vector coastline beats raster stairsteps cartographically. Costs the raster its foreshore relief (depare drying polygons are banded, not continuous) and makes the raster depend on a second tileset for basic rendering. Worth revisiting alongside the basemap work, not as this fix.
- **Coarse land quantization instead of a clamp.** Keep land relief as visual context (fjord walls) but quantize values above the cap brutally (32–64 m steps) — most of the byte savings, and the per-pixel step machinery in `quantize` already exists. Rejected for the primary product: it keeps the "this is a topo map" misreading alive, and the vector basemap is the right place for land context.
- **Class-mask companion tileset.** A 4-class byte raster (water / drying / land / unknown-water) as a tiny palette image. The only design that resolves the 0-ambiguity per-pixel for raster-only consumers — but the depare `nodata` polygons already carry that distinction for the primary viewer, so this is shelf-ware unless such a consumer appears.
- **Drop inland tiles from the archive entirely.** Buys nothing over the clamp once dedup collapses them to one stored tile, and costs a worker fallback path.

## Recommendations

1. **Adopt the sentinel clamp at product encode**: `min(elevation, DRYING_CAP + 1)` in the terrain render path (`terrain.py` → `encode.py`), contract specified as the threshold `v > DRYING_CAP ⇒ land`. Internal mosaic unchanged; vector products unchanged. Cache note: re-keys the render only — planet re-renders from the cached mosaic, no re-merge. Expected effect: inland tiles collapse to ~100 bytes; the archive sheds roughly the land share of its payload (68% on the coastal-heavy sample above; more planet-wide, where inland tiles dominate land tile count).
2. **Fourth-quadrant mosaic fix** (positive-in-ocean → 0 for `land_clamp` sources): the sentinel hides coarse false land in the _raster_, but the _drying bucket and depare_ read the unclamped mosaic, so GEBCO shoreline cells in `(0,16]` seaward of the OSM line still fabricate foreshore without it. Correction (2026-07-21): this did NOT exist in the code despite the "keep" wording — implemented fresh as `landmask.clamp_positive_ocean`, a sibling of `clamp`/`clamp_positive_water`, keyed on the combined mask (not land) intersected with the water-only mask (not inland water) → ocean; positive ocean clamps to 0.
3. **Rewrite the `DRYING_CAP` comment**: anchor to global max HAT−LAT; name the over/under-inclusion biases; note the sentinel/classifier distinction.
4. **State the contract** (wording above) in CONTRIBUTING. Document that `readDepth` returns a rendered DEM sample, not a categorical classification; callers must treat values above `DRYING_CAP` as land and consult `depare` before presenting non-negative values as drying height. Consider returning `null` for definite land in a later API revision.
5. **Decoded-0-as-water** (added 2026-07-21): the render-stage 0-nudge + ramp change above, in the same PR as the sentinel clamp — one planet re-render covers both.
6. **Inland-water follow-ups** (separate work): per-lake surface datums via HydroLAKES `Elevation` (see the source-research section above) to convert unknown lakes from `0+nodata` to genuine local depths — the sign-blind subtraction also covers cryptodepression negatives; datum unification must leave 0-filled unknown lakes untouched.
7. **Verification**: check native and Worker-overzoomed tiles at water/drying/land boundaries; assert that definite land remains above `DRYING_CAP`; assert an unsurveyed-lake interior decodes exactly 0 and a below-MSL polder decodes > 0; verify changing `DRYING_CAP` re-keys terrain artifacts; and measure savings on completed PMTiles archives, since deduplication is archive-local. Keep the existing sign-invariant audit on the mosaic clamps.
