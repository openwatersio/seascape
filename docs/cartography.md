# Cartographic decisions

Why the tiles and the style look the way they do. The [schema doc](schema.md) says what a reader gets; this says why those choices were made. The guiding principle throughout is the chart-making rule that where the data must err, it errs toward showing _less_ water — a mariner surprised by extra depth is fine, one surprised by a shoal is not.

## Why the raster's non-negative domain is categorical

The published raster treats `v < 0` as measured depth and everything at or above zero as one of three flat codes: 0 unknown-depth water, 1 drying foreshore, 2 land. Land elevation is deliberately discarded rather than published, for three reasons:

- **A depth chart has no use for topography.** The product's one job is "how deep is the water"; publishing land heights would spend encoding range and tile bytes on data the style never reads.
- **Flat codes have no slope.** Client hillshade computes slope from the DEM, so any real land topography would render fake terrain relief — and a land value adjacent to water would shade a halo ring along every shoreline and 0-filled lake. Constants shade as nothing.
- **Codes must survive resampling and quantization.** All three values are exact multiples of the coarsest quantization floor (0.25 m), so they decode exactly at every zoom instead of drifting into plausible-looking shallow depths.
- **Depth and land elevation don't share a datum.** Depths count down from a local low-water chart datum; land heights count up from a land datum near mean sea level. A DEM publishing both would seam every shoreline where the two differ.

Zero specifically means _unknown_ rather than "zero depth" because real water is never allowed to quantize to it: a depth shallower than one encoding step is floored at −1/256 m instead of rounded up to 0. That keeps 0 unambiguous — a pixel the sources don't cover, or whose depth a coarse zoom averaged away — and the style renders it as uncharted water rather than inventing a depth of zero.

Drying is a code rather than a height for the same robustness reason: a drying height that quantized or resampled across the land threshold would silently reclassify. The classification happens once, at render time, against the land mask and the drying cap.

### Two datums meet at the shoreline

In tidal waters the datum gap is bridged by physical ground: the foreshore climbs from the low-water datum through MSL to the high-water line, so the real surface passes through both reference frames continuously, and the drying code covers exactly that band. A DEM could, in principle, carry that transition without a discontinuity.

Non-tidal waters have no foreshore to do the bridging, and the gap can be enormous. A lake's surface sits at land-datum _elevation_ while its soundings count down from the lake's own datum: Lake Huron's low water datum is about 176 m above sea level, so a lakebed value of −77 m sits next to shoreline land at +74 m — a 150 m cliff at the waterline that no physical surface connects. Publishing land elevation would bake that wall into the DEM, and hillshade would shade it as a scarp ringing every lake and reservoir.

Flat codes dissolve the problem instead of reconciling the datums: the shoreline becomes a classification boundary rather than an elevation step, water values stay meaningful in their own local datum, and nothing renders at the seam.

## Shoal bias

Every lossy step in the pipeline is constrained to err shallow, because a depth that reads deeper than truth is the one error a chart must not make:

- **Quantization rounds toward the surface.** The Terrarium encode's per-zoom rounding always moves a depth shallower, never deeper, so a decoded value is never deeper than the source data.
- **The pyramid is monotone.** Every coarse raster tile is clamped shoal-ward against the zoom below it, so zooming out can only shoal. Without the clamp, smoothing kernels measured in each zoom's own pixels reach twice as far in metres at every step out, and a coarse tile can chart deeper than the fine tile over the same water.
- **The mosaic's overviews reduce class-aware and shoal-ward**, so no zoom charts water deeper than the survey under it, and no coarse pixel closes a channel its finest data holds open.
- **Sounding labels floor shallow.** The feet and fathom conversions truncate toward shallower, matching chart sounding practice.
- **The safety contour snaps deeper.** A safety depth between charted isobaths snaps _up_ the ladder to the next-deeper charted level (ECDIS behaviour), so the hazard tint always covers at least the requested depth.

## The drying cap

Drying foreshore — seabed above chart datum that covers and uncovers with the tide — is classified as elevation in (0, 16 m] seaward of the OSM land line. The 16 m cap anchors to the global maximum of HAT−LAT, the highest ground that still floods and dries: ~16.3–17 m at Burntcoat Head in the Bay of Fundy, ~16.8 m in Ungava Bay, ~15 m in the Bristol Channel. Sixteen is deliberately the round value just below that extreme tail, accepting that genuine 16–17 m drying at the two or three most extreme sites on Earth classifies as land.

A single global cap has two known biases: over-inclusion on low-tidal-range coasts (a bluff toe below 16 m tints as foreshore) and under-inclusion of the MHW–HAT band in mega-tidal estuaries (it sits on OSM's land side of the line, since OSM shorelines approximate high water). A spatially varying HAT−LAT surface is the upgrade path.

## Depth bands and their colours

The depth-shading tints follow paper-chart convention (INT/NOAA): darkest blue in the shallows, fading to white in the deep, so attention concentrates where depth discrimination matters. Beyond the deepest band edge the tint stays flat white, keeping tint monotonic in depth.

The band edges sit on isobaths the chart actually draws — the metric levels, or the classic fathom curves in feet/fathoms mode — so a tint boundary always lands on a contour line rather than between two of them, again paper-chart practice. Adjacent bands are spaced for perceptual distinctness (ΔE ≥ 7 after the fill's opacity compositing), weighted toward the shoal bands.

## The feet/fathom ladder

Metric charts and imperial charts draw _different_ isobaths: 5/10/20 m versus the classic fathom curves (6, 12, 18, 30, 60 ft…). Rather than relabelling metric contours in feet — which produces unfriendly numbers like "33 ft" — the tiles carry a second full contour and depth-area set cut at the fathom curves. The friendly feet depths are exactly whole fathoms in feet, so one geometry labels as either feet or fathoms and the viewer picks. The `sys` tag keeps the two ladders apart; a renderer shows one, never both. The 0 m drying line is the same physical curve in every unit system, so it ships once, untagged.

This is a hard requirement of the product, not an optimization target: imperial-chart users get real fathom curves, and size or complexity budgets are found elsewhere.

## The safety contour

The safety depth is the one runtime parameter with navigation semantics: everything shallower than it is a hazard. Both shading modes honour it, differently by their nature — the continuous raster ramp pins a crisp hazard edge exactly at the safety depth, while the vector depth-area bands can only flip at charted isobaths and therefore snap the boundary to the next-deeper level. The contour at the snapped safety level is emphasized (the S-52 safety-contour role), and soundings at or shoaler than the safety depth render in the emphasis colour.

## Contour generalization by zoom

Zoomed out, the full isobath set is noise — abyssal contours stipple at small scales. Charts thin the deep, not the shelf: coarse zooms carry only the widely spaced deep levels, and each zoom-in adds finer levels until everything shows. Which levels appear at which zoom is a display decision the tiles make; it is deliberately _not_ part of the schema contract.

For the same reason, contour geometry is generalized against a smoothed surface — the same per-zoom smoothing the raster render uses, so isobaths and shading agree — and smoothing is depth-gated: aggressive in the abyss where detail is oceanographic noise at chart scales, minimal in the 0–30 m band where every metre matters.

## Quantization that respects the shallows

The raster's per-zoom vertical quantization (coarse steps at low zoom, full resolution deep in) was designed for terrain, where relief is measured in kilometres. Applied naively to a shelf it terraces the shallows: a 2 m step in 3 m of water is a cliff. So the step is additionally capped per-pixel at roughly 1/16 of the local depth, floored at 0.25 m (matching sounding label precision in the shoal band) and snapped up to a power of two so the RGB packing stays lossless. Deep water keeps the byte-saving coarse steps; shallow water keeps chart detail at every zoom, and the step never spans more than one colour band.
