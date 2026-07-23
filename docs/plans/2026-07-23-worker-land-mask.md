# Serve-time land masking on the raster overzoom path — planning doc

*Written 2026-07-23. Point-in-time; the code is the source of truth.*

Discussion: [seascape#99](https://github.com/openwatersio/seascape/discussions/99) — in the Baltic (EMODnet ~115 m over GEBCO ~460 m), z14 views show bays averaged into land and small islands flooded. One native DEM cell spans ~12 screen pixels there, so any sub-cell shoreline feature is lost at build time and no amount of resampling recovers it.

## Problem

The published raster in coarse-source regions carries the shoreline at native DEM resolution. The Worker's cubic-B-spline overzoom ([worker/src/index.ts](../../worker/src/index.ts), `synthesize()`) smooths band edges but interpolates the *wrong* shoreline smoothly: a cell that averaged a narrow bay into land stays land at every zoom, and a sub-cell island stays water. The build-time land clamp ([landmask.py](../../pipelines/landmask.py)) already bounds the error in one direction (no false water rim over land) but runs at source resolution — it cannot re-cut the shoreline finer than the DEM grid.

Meanwhile a shoreline far finer than any coarse DEM already exists in the pipeline: `store/landmask/land.fgb` (OSM land polygons) minus `store/landmask/water.fgb` (Overture inland-water polygons) — the exact effective-land definition the build-time clamp and the drying redesign ([2026-07-08-drying-geometry.md](2026-07-08-drying-geometry.md)) use. It is consulted only at build time, at DEM resolution.

## Approach in one paragraph

Ship the effective-land mask as a small vector tileset (`land.pmtiles`, two layers, maxzoom z12) and have the Worker composite it into every tile it *synthesizes* (the overzoom path only): rasterize the mask polygons onto the 512² output grid with a scanline fill, stamp mask-land pixels with the land code, and knock mask-water pixels that the DEM insists are land down to the unknown-depth-water code. The artifact exists only where native resolution is coarse, which is exactly and only the overzoom path — hi-res regions (S-102, CUDEM, AusSeabed cells at z13–14) serve native tiles that never enter `synthesize()`, so they pay nothing and native tiles keep their pass-through path and content ETags.

## Goals / Non-goals

**Goals.** At z ≥ 11 in coarse-source regions, the raster land/water boundary is the OSM effective-land line (sub-pixel at z14), not the DEM cell grid. Both artifact directions fixed: flooded islands render as land; land-filled bays render as unknown-depth water (the honest wash — their depth genuinely isn't known). Tidal rivers and fairways (Elbe, ICW, Amazon) stay open: the mask is land∖water, never raw coastline. Drying signal survives masking. No storage inflation of the DEM tiles, no change to native-tile serving, revalidation (304) still skips synthesis.

**Non-goals.** Inventing bay depths — a recovered bay gets code 0 (unknown-depth water), not a fabricated sounding. Fixing the *vector* layers (contours/soundings/depare still carry the coarse shoreline in these regions; the drying-geometry redesign converges them separately). Masking native tiles (artifact is ≤ 1–2 px at native zoom; not worth losing the pass-through path). Replacing the build-time clamp — it still protects contours, soundings, depare, and native tiles. Anti-aliasing the mask edge (hard pixel edge first; feather only if it visibly aliases).

## Part 1 — build `land.pmtiles`

One tileset, **two layers**, so the Worker subtracts water at rasterize time exactly the way `landmask.rasterize` does (burn land = 1, then burn water = 0). This avoids a planet-scale `land.difference(water)` geometry job — tippecanoe just tiles what already exists:

- layer `land` ← `store/landmask/land.fgb` (already prepped by `landmask.py prep`)
- layer `water` ← `store/landmask/water.fgb` (already prepped by `landmask.py prep-water`)

```
tippecanoe -o store/bundle/land.pmtiles \
  -L land:store/landmask/land.fgb -L water:store/landmask/water.fgb \
  -Z0 -z12 --coalesce --projection=EPSG:3857
```

- **`--projection=EPSG:3857` is required.** The prepped masks are EPSG:3857 and tippecanoe does not reproject FlatGeobuf from its declared CRS — without the flag it reads meter coordinates as degrees and mangles the planet (verified; the landmask self-check asserts against regression).
- **maxzoom z12 is the precision floor that works.** MVT extent 4096 in a z12 tile ≈ 2.4 m/unit at the equator; a z14 512 px tile is ~4.8 m/px — sub-pixel. At z16 the mask edge quantizes to ~2 px steps; acceptable for a chart, and the knob is just `-z13` (≈ 2× size) if it ever isn't.
- **minzoom z0**: the Worker only fetches mask tiles above the planet max zoom, but the full pyramid costs a rounding error (z0–7 is ≤ ~22k tiles, mostly absent ocean, aggressively simplified) and makes `land.pmtiles` useful beyond the mask — a publishable land/water polygon tileset in its own right, which has already been asked for. Serving it publicly (a `/land/{z}/{x}/{y}` route + TileJSON) is a small worker follow-up, not part of this pass.
- **Default limits stand.** Tippecanoe simplifies to tile resolution at every zoom anyway — the wanted generalization. Its overflow handling (tiny-polygon reduction, detail-lowering on tiles past 500 KB) only binds on the densest archipelago tiles, and even a detail-lowered mask tile (extent 256 ≈ 9.5 m at z12) is ~50× finer than the GEBCO shoreline it corrects. Watch the build log for detail/drop warnings on z10–12 coastal tiles; escalate to `--no-tile-size-limit` only on visual evidence of lost islands. Interior solid-land tiles coalesce to trivial squares; open-ocean tiles are absent (which the Worker reads as all-water — see Part 3).
- Wire as a rule in the publish graph next to the coverage product; it rebuilds only when the mask inputs change (OSM land-polygons snapshot or Overture release bump), not per DEM build.
- Size: coastline and river tiles carry the vertices; expect single-digit GB. Measure on the first build before promising anything downstream.

## Part 2 — publish

`bundle.py stage-build` copies `land.pmtiles` into `build/<sha>/` alongside `coverage.pmtiles` (release dir stays self-contained; manifest-last publish keeps the pointer atomic). **`manifest.json` is untouched** — the Worker lazily reads the `land.pmtiles` header via the existing `pm()` helper and treats a failed header read as "no mask this release": pre-land releases, local dev without a seeded mask, and rollbacks all keep working with today's behavior. `seed.sh` in the Worker gains the mask copy for local preview.

## Part 3 — Worker masking

New module [worker/src/mask.ts](../../worker/src/mask.ts) + `mask.test.mjs`, two small deps (`@mapbox/vector-tile`, `pbf` — ~15 KB, nothing next to the jsquash wasm).

**Fetch.** For a synthesized tile at z/x/y: mask tile is `(lz, x >> (z-lz), y >> (z-lz))` with `lz = min(z, landMaxZoom)`. Fetch it in parallel with the DEM ancestor. Three outcomes, kept distinct:

- tile bytes → decode and rasterize;
- tile absent within a present archive → all-water mask (open ocean; correct, and safe — see the compositing rule);
- archive absent or fetch/decode **error** → serve unmasked and log, never dead-end the tile (same tolerance the overlay walk already has).

**Rasterize.** Scanline even-odd fill of the MVT rings onto a 512² `Uint8Array`, in tile-local integer coords with the same shift/scale-into-sub-tile-window arithmetic `synthesize()` uses for the DEM ancestor. Burn `land` rings as 1, then `water` rings as 0 — the second burn only ever opens water, mirroring `landmask.rasterize`. Even-odd parity per feature handles holes without ring classification. Cost: 512 rows × edge count; a dense coastal z12 tile at a few thousand simplified vertices is a few ms — same order as the existing 4M-tap B-spline loop, inside the existing `overzoomGate`. Active-edge-table and per-mask-tile LRU caching are profiling-gated follow-ups, not part of this pass.

**Composite.** Operate on the height array inside `synthesize()` before packing (published raster codes: 0 unknown-depth water, 1 drying, 2 land):

| mask says | B-spline height `h` | write | why |
|---|---|---|---|
| land | any | `2` | the land code the ramp already paints; matches what native land pixels carry |
| water | `h ≥ 2` | `0` | DEM says land where the mask says water — the averaged-away bay; no real depth exists, so the unknown-water code, never an invented one |
| water | `0 ≤ h < 2` | keep | drying (1) and shoreline code blends must survive — OSM land is roughly the high-water line, so the foreshore is mask-water and zeroing it would erase every drying flat |
| water | `h < 0` | keep | real interpolated depth |

The `h ≥ 2` threshold is the load-bearing line: it catches genuine land topo (+5..+20 m in an averaged bay) and the land code itself, while passing every legitimate water-side value including spline blends between codes. An all-water mask (absent ocean tile) is a no-op on real ocean (`h < 0` everywhere) by the same rule.

**ETag.** The synthesized tile is currently a pure function of the DEM ancestor bytes; it becomes a pure function of (ancestor bytes ‖ mask tile bytes). Validator = `contentEtag` over both (absent mask tile hashes as empty), and bump `OVERZOOM_TAG_VERSION` so every previously cached synthesized tile re-fetches once. The 304 short-circuit still skips decode → fill → B-spline → encode.

**Scope of application.** Both synthesis paths — overlay-ancestor overzoom and planet overzoom — and nothing else: not native tiles, not the land-fallback tile (already land), not vector/coverage.

## Validation

- `mask.test.mjs`: rasterizer (convex ring, ring-with-hole, multipolygon, water-over-land burn, sub-tile window at 1/2/4 levels of overzoom, buffer geometry past tile edges) and the four compositing rows, plus the absent-tile/absent-archive distinction.
- Visual, via `just preview` + the Worker pointed at a staged build: the exact Baltic frames from discussion #99 (island recovered as land, bay as unknown-water wash); an ICW/Elbe frame (tidal river still open — the water layer is doing its job); a Wadden Sea frame (drying flats intact — the `0 ≤ h < 2` row is doing its job); an open-ocean z14 frame (byte-identical output modulo ETag version).
- Perf: log synthesize wall time before/after on a burst over a dense coastline; the budget is "same order as today", not a number picked in advance.
- Cache: confirm the `OVERZOOM_TAG_VERSION` bump turns over the colo cache and that 304s still bypass synthesis.

## Alternatives considered

- **Build-time upscale of coarse regions to z14** — 256× the tile count from z10, storage blowup for tiles that are pure interpolation; rejected (the premise of the discussion question).
- **Style-only fix** (basemap land drawn above the raster) — recovers flooded islands for map-style consumers only; nothing can paint unknown-water over false land in a bay, and raster-only consumers see no fix. Complementary, not sufficient.
- **Raster mask tileset (1-bit, z14)** — z14 planet tile count for no precision gain over z12 vectors; rejected.
- **Single pre-differenced `effective_land` layer** — cleaner Worker (one burn) but requires a planet-scale polygon difference in the pipeline; the two-layer burn is 5 extra Worker lines and byte-for-byte mirrors the build-time clamp semantics. If the drying redesign ends up materializing `effective_land.fgb` anyway, switching the tileset to one layer is a trivial follow-up.

## Open questions / ceilings

- **Vector/raster shoreline disagreement.** Contours and depare still carry the DEM shoreline in coarse regions, so at z14 a depare band edge can sit a few pixels off the masked raster shoreline. Accepted here; the drying-geometry redesign cuts the vector side by the same effective-land and converges them.
- **OSM shoreline quality is now user-visible at z14** in regions where it never was. Errors in OSM coastline become chart errors; that trade is already accepted at build time (the clamp trusts the same polygons), this just renders it sharper.
- **`land.pmtiles` snapshot cadence** is decoupled from DEM releases but the file is copied per release dir — a stale mask ships until someone re-runs the prep. Fine while the prep is manual; revisit if the mask ever versions independently.
- **Datum.** OSM land ≈ high-water line, chart datum is lower; the compositing table's drying row absorbs this, but macrotidal coasts where a coarse source is sole coverage still degrade intertidal signal to unknown-water — same ceiling `clamp_positive_ocean` already documents.
