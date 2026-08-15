# Tile schema

**Schema version: 1**

This is the contract between the published tilesets and anything that reads them — the normative statement of what each layer, field, and pixel value means. It is versioned by a single integer, `schema`, served in every TileJSON document (`raster.json`, `vector.json`, `coverage.json`), recorded in the build manifest, and exported by the `@openwaters/seascape` style package as the version it targets. The bump rule lives in [CONTRIBUTING](../CONTRIBUTING.md#schema): the number moves only when a change makes a previously valid reader wrong.

All depths are relative to chart datum, which varies by source (LAT, MLLW, or approximately MSL — see the [README warning](../README.md)). Elevation 0 means chart datum everywhere in this contract.

## Raster tiles

`{z}/{x}/{y}.webp` — a WebP-compressed 512×512 [Terrarium](https://github.com/tilezen/joerd/blob/master/docs/formats.md#terrarium)-encoded DEM: `elevation = R*256 + G + B/256 − 32768` metres, with a 1/256 m least significant bit.

The value domain splits at zero:

- **`v < 0`** — water depth: metres below chart datum, negative-down. This is the only part of the domain that carries a measurement.
- **`v ≥ 0`** — three flat category codes, never a height:
  - **`0` — water of unknown depth.** Water the sources don't cover, or whose depth was averaged away at a coarse zoom. Render as water without inventing a depth.
  - **`1` — drying foreshore.** Seabed above chart datum that covers and uncovers with the tide. The code carries no drying height; the classifier's bound is the build's drying cap (16 m above datum).
  - **`2` — land**, or out of scope — never a measured land elevation. Missing raster tiles also read as this code, so the DEM cannot provide land relief.

The codes are flat by design: they have no slope, so client hillshade renders nothing on land, and all three are exact multiples of the coarsest quantization floor, so they decode exactly at every zoom. Real water never quantizes to a code — depths shallower than one encoding step are floored at −1/256 m rather than rounded to 0. Fractional values between codes can appear at resampled or overzoomed boundaries; round to the nearest code, or consult `depare` for categorical geometry.

### Quantization

Elevations are rounded at encode time to a per-zoom vertical step, `2^(19−z)/256` m (z0 ≈ 2048 m, z12 = 0.5 m, z19 = full 1/256 m resolution), capped per-pixel so shallow water keeps chart detail at every zoom: the cap is 1/16 of the local depth, floored at 0.25 m and then snapped **up** to the next power of two (so the packing stays lossless — the effective step can therefore exceed 1/16 of the depth, e.g. 1 m rather than 0.625 m in 10 m of water). Rounding is conservative — always toward shallower — so a decoded depth is never deeper than the source data.

### Zoom range and overzoom

The TileJSON advertises `minzoom`/`maxzoom` from the current build; `maxzoom` runs a few levels past the deepest native data because the server synthesizes overzoomed tiles with a C2 cubic B-spline (smooth band edges and shorelines) rather than leaving the renderer to bilinearly stretch the last native tile. Consumers should always trust the TileJSON rather than assuming a range.

### Pyramid monotonicity

Zooming out can only shoal: every coarse tile is clamped shoal-ward against the finer zoom below it, so a coarser zoom never reads deeper than the data beneath it. A client may rely on the coarse pyramid never charting a depth the fine data contradicts in the dangerous direction.

## Vector tiles

`{z}/{x}/{y}.pbf` — Mapbox Vector Tiles. Layer and field lists are also advertised machine-readably in `vector.json`'s `vector_layers`. Zoom range comes from the TileJSON; the archive is a variable-depth pyramid and the server synthesizes tiles by overzoom between the deepest baked level and the advertised `maxzoom`.

### `contours` — isobath lines

| Field | Type | Meaning |
|---|---|---|
| `depth_m` | Number | Elevation of the isobath in metres: negative below datum (`-10` is the 10 m curve), `0` is the drying line. |
| `depth_abs_m` | Number | The same level as a positive-down integer (`10`) — the label/lookup form. |
| `depth_ft` | Number | Positive-down integer feet. |
| `depth_fm` | Number | Positive-down integer fathoms. |
| `sys` | String | Which contour ladder the curve belongs to: `"m"` for the metric levels, `"ft"` for the fathom curves (depths that are whole fathoms, labelable as feet or fathoms). The 0 m drying line is one curve shared by every unit and never carries `sys`. |

Coarse zooms carry fewer levels (deep-ocean contours thin out zoomed out); a level's presence at a zoom is a display decision, not a schema guarantee.

### `soundings` — spot depths

| Field | Type | Meaning |
|---|---|---|
| `depth_m` | Number | Depth in metres, **positive-down** (unlike `contours.depth_m`), floored toward shallower; carries one decimal shallower than 6 m, whole metres beyond. |
| `depth_ft` | Number | Integer feet, floored toward shallower. |
| `depth_fm` | Number | Integer fathoms, floored toward shallower. |

### `depare` — depth-area polygons (ENC DEPARE)

A partition of the water into polygons, three feature kinds keyed by attribute presence:

| Kind | Signature | Meaning |
|---|---|---|
| Depth band | `drval1 ≥ 0`, `sys` present | Water between two charted isobaths: `drval1`/`drval2` are the shallow/deep bounds in positive-down metres. `sys` (`"m"` or `"ft"`) tags which isobath ladder cut the band — render one ladder, never both. |
| Drying | `drval1 < 0`, no `sys` | Drying foreshore. `drval1`/`drval2` are the drying bucket's fixed bounds (−drying cap and 0) on every feature, **not** a measured per-feature drying height — only the sign is information. |
| Unknown water | no `drval1` | Water of unknown depth (the vector twin of raster code 0). |

`rank` (Number) orders the features for painting within the fill.

The band edges follow the charted isobath ladders (the metric levels and the classic fathom curves), so a safety-contour tint can only flip at a charted level. Fathom-curve `drval` values are exact multiples of 1.8288 m stored as 32-bit floats; compare with a small epsilon rather than exact equality.

## Coverage tiles

`coverage/{z}/{x}/{y}.pbf` — source-provenance footprints, published as its own small tileset (`coverage.json`) that renderers overzoom independently.

### `coverage` — per-source data extents

| Field | Type | Meaning |
|---|---|---|
| `source_id` | String | Stable source identifier (keys into the manifest's `source_ids`). |
| `source_name` | String | Human-readable source name. |
| `source_maxzoom` | Number | The zoom the source's data is built to. |

## Semantics a reader cannot see

These hold everywhere and are part of the contract precisely because no field advertises them:

- **Zero is chart datum.** Every depth, drying height, and band edge is relative to it.
- **Positive-down vs. negative-down.** `drval1`/`drval2`, `soundings.depth_m`, and the `depth_abs_m`/`depth_ft`/`depth_fm` trio are positive-down; `contours.depth_m` and raster elevations are negative-down. Each field's convention is fixed; a sign-convention change is a schema bump.
- **Shoal bias.** Quantization rounds shallow, sounding unit conversions floor shallow, the raster pyramid is monotone shoal-ward, and the safety contour snaps to the next-deeper charted level. Where the data must err, it errs toward showing less water.
- **Drying is bounded.** Drying classification stops at the drying cap (16 m above datum); higher ground is land.
