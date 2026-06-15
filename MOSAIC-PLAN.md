# Priority-Mosaic Plan — Multi-Source Bathymetry Tiles

Goal: one terrain-RGB tileset + one contour tileset that uses **GEBCO 2026 as the
global base** and **defers to higher-quality regional data where it exists**,
extending to **deeper zoom only where the data supports it**.

## Core idea

Two independent mechanisms, kept separate so neither gets complicated:

1. **Priority handled at the DEM level (a GDAL VRT).** `gdalbuildvrt` draws
   later-listed sources on top. List sources worst→best; the best data wins per
   pixel. This is "defer to higher quality" in one command. Datum offsets and
   reprojection are baked into each source *before* mosaicking.

2. **Variable zoom handled by disjoint zoom bands.** The global base owns
   **z0–9** (full extent). Each high-res region owns **z10+ within its bbox**.
   Tiles are produced from the already-prioritized VRT, so merging tilesets is a
   near-trivial union (`INSERT OR IGNORE` on MBTiles / `tile-join` for vector) —
   overlapping tiles are byte-identical, conflicts are impossible.

The existing `terrain` and `contour` scripts run **unchanged** on the VRT. The
new work is: normalize sources, build the VRT, tile per zoom-band, union.

## Source priority (worst → best, last wins)

| Priority | Source   | Native res | Zoom ceiling | Coverage        | Datum   |
| -------- | -------- | ---------- | ------------ | --------------- | ------- |
| 0 (base) | GEBCO 2026 | ~450 m   | z9           | global          | MSL     |
| 1        | EMODnet  | ~115 m     | z11          | European seas   | varies  |
| 1        | NIWA NZ  | 250 m      | z10          | NZ EEZ          | varies  |
| 2        | CUDEM    | ~3–10 m    | z13–14       | US coast        | varies  |
| 2        | BlueTopo | 2–16 m     | z14–15       | US navigable    | MLLW    |

Zoom ceilings are display caps, not native res (BlueTopo's 2 m ≈ z18; we cap
where it stops being worth the tile count). Tune per region.

---

## Phase 0 — Pivot to GEBCO base (done / trivial)

GEBCO 2026 is already the configured source and is the best global grid today
(SWOT+ML deep ocean, newer than ETOPO 2022). No global ETOPO layer. Nothing to
build here beyond confirming `DATASET`/source naming — fold into Phase 1.

---

## Phase 1 — Source abstraction + single-region prototype ✅ DONE

Prove the whole pattern end to end with GEBCO global + **one** CUDEM region.

**Implemented:** `scripts/sources.conf`, `scripts/ingest`, `scripts/mosaic`,
`scripts/merge-tiles`; `terrain`/`contour` take `MIN_ZOOM` + `OUT_MBTILES`
(banded mode); `scripts/build` orchestrates (no sources → original GEBCO-only
build, unchanged). Viewer auto-detects terrain maxzoom from the PMTiles header.

**Validated with real data** — NOAA CUDEM 1/9 arc-sec (~3.4 m) coastal Georgia
tile over GEBCO 2025:
- Priority: in-tile the mosaic returns CUDEM's −5.18 m over GEBCO's −6 m; outside
  the tile GEBCO shows through. (gdalbuildvrt needs homogeneous band types —
  `mosaic` promotes all sources to Float32 VRTs to mix Int16 GEBCO + Float32 CUDEM.)
- Zoom bands: merged terrain has base z0–9 (full extent) + z10–13 only over the
  CUDEM tile (56 tiles at z13), no loss/collision. Raster via sqlite union,
  contours via tile-join. One 3.7 MB terrain + 1 MB contour PMTiles.

**Notes / next-phase hooks:**
- `sources.conf` ships with the verified CUDEM row active. `./scripts/build`
  produces the mosaic; CI is untouched because `ci.yml` calls `terrain`/`contour`
  directly (never `build`), so released tiles stay GEBCO-only until later phases
  wire the R2 mirror (Phase 2) and the source/region CI matrix (Phase 3).
- Smoothing (`smooth-dem`/`smooth-contours`) needs both rasterio and osgeo, which
  only coexist in Docker's `--system-site-packages` venv — run the full pipeline
  via the Docker image, or `SKIP_SLOPE_SMOOTH=1 SKIP_CHAIKIN=1` for a local
  smoke test.

**Work items**

- `scripts/sources.conf` — one row per source: `id|url|crs|datum_offset_m|priority|bbox|min_zoom|max_zoom`. Plain text, sourced by bash. (ponytail: a config file, not a registry abstraction.)
- `scripts/ingest <source-id>` — download → `gdalwarp` to EPSG:4326 → apply constant `datum_offset_m` (`gdal_calc.py -A ... --calc="A+off"`) → set nodata outside the data footprint → write `work/<id>_norm.tif`. One code path; format quirks handled by per-source `case` branches as they're added.
- `scripts/mosaic` — `gdalbuildvrt` over the normalized sources **ordered by priority ascending** → `work/mosaic.vrt`. The VRT is virtual, so this is instant and cheap.
- Extend `scripts/terrain` and `scripts/contour` to accept `MIN_ZOOM` (default 0) alongside `MAX_ZOOM`, and to take the VRT as input. Already accept `BBOX`.
- `scripts/merge-tiles <out.mbtiles> <in...>` — raster: `sqlite3` ATTACH + `INSERT OR IGNORE INTO tiles`, merge `metadata` maxzoom. Vector: `tile-join` (handles this natively). Then one `pmtiles convert` at the end.
- `scripts/build` orchestration: ingest each source → mosaic → tile base (full extent, z0–9) → tile each region (its bbox, z10–maxz) → merge → convert.

**Output:** `output/terrain.pmtiles`, `output/contours.pmtiles` — GEBCO everywhere, CUDEM detail + deep zoom over the US East Coast.

**Validation:** open in the viewer; confirm (a) seamless GEBCO at z0–9, (b) CUDEM tiles appear at z10+ only in-bbox, (c) no tile-key collisions (`pmtiles show`), (d) a known shoal reads correct depth via `queryTerrainElevation`.

---

## Phase 2 — Full CUDEM coverage (multi-tile ingest) (~2–4 days)

Phase 1 proved the mechanism with one CUDEM tile. CUDEM is the awkward part to
scale, and it's the same multi-tile shape BlueTopo will need — so nail it here
before adding other sources.

**The problem:** CUDEM isn't one file. It's hundreds of 0.25°×0.25° tiles spread
across regional index dirs (southeast, northeast_sandy, Guam, PuertoRico, …),
~190 MB each, tens of GB total. `chs.coast.noaa.gov` throttles bulk pulls.

**Work items**

- **Multi-tile `ingest`:** let a source's `url` resolve to *many* tiles — a `.vrt`,
  a glob, or an index/bucket listing — then `gdalbuildvrt` them into one DEM
  before warp+offset. Single-file sources keep working unchanged (this is the
  generic capability BlueTopo reuses in Phase 3).
- **Tile enumeration:** scrape the NCEI region `index.html`, or list the AWS open
  data bucket `s3://noaa-nos-coastal-lidar-pds/dem/` (cleaner, no scraping).
- **R2 mirror:** one-time bulk copy of the CUDEM regions you want to R2, both for
  reproducibility and because CI runners can't pull the NOAA dirs.
- **Coverage scope:** decide which regions (CONUS coasts + territories) and add a
  `cudem_*` row per region (each reusing the same multi-tile ingest), or one row
  whose ingest pulls every tile intersecting the bbox.
- **Datum:** CUDEM is NAVD88; constant ~0 offset holds on most US coasts. Note any
  region where the tidal datum drifts enough to seam (revisit in Phase 5).

**Output:** GEBCO base + full CUDEM US coastal coverage at z10–13/14, one terrain
+ one contour PMTiles.

**Validation:** build a multi-tile region (e.g. all of southeast), confirm tiles
mosaic without gaps/seams, check total size stays sane (sparse high-zoom only over
the coast), spot-check depths against known soundings.

---

## Phase 3 — Other sources + seams (~1–2 weeks)

**Work items**

- Add BlueTopo, EMODnet, NIWA to `sources.conf`. BlueTopo reuses Phase 2's
  multi-tile ingest (UTM per-tile); EMODnet (NetCDF) and NIWA (grid) need one
  `ingest` `case` branch each. This is the bulk of the effort — format wrangling,
  not architecture.
- Constant per-source datum offsets to MSL (good enough for shading/contours). `# ponytail: constant offset, swap for VDatum in Phase 5 if seams show`.
- **Seam feathering**: `gdalwarp` cutlines with a blend distance at region boundaries, or accept hard cuts where invisible. Only feather where a discontinuity is actually visible.
- **Contours cross sources cleanly because they're cut from the VRT**, not merged per-source — so no Phase-1 change needed, but verify boundary lines don't kink.
- CI (`ci.yml`): the `tiles` matrix becomes per-region tiling jobs that all merge into the two final tilesets. Mirror each source to R2 (runners can't reliably pull NOAA/EMODnet bulk dirs — same reason GEBCO already comes from R2).

**Output:** global GEBCO + US (BlueTopo/CUDEM) + EU (EMODnet) + NZ (NIWA), variable zoom per region, two PMTiles files.

**Validation:** spot-check each region's boundary for datum cliffs; confirm zoom ceilings; check total size stays sane (sparse high-zoom, not global).

---

## Phase 4 — Unify GEBCO as just another source (~1 hour refactor)

Today GEBCO is special-cased as "the base" in `build` (separate `download`, base
band tiled from the base-res DEM, regions tiled from the mosaic). It's different
in three ways — but only one is incidental:

1. **Fetch** (real): GEBCO is a ~4 GB zip → extract → maybe VRT-mosaic sub-tiles
   → ±85° clamp. Other sources are a single `curl` of a GeoTIFF/COG.
2. **Tiling resolution** (real): the base band must tile from a *base-resolution*
   DEM. Tiling z0–9 from the `-resolution highest` mosaic (3.4 m globally once
   CUDEM is in it) would downsample a 3.4 m global grid — absurd.
3. **Zoom band + bbox** (incidental): base = z0–9 full extent, regions = z10+ in
   a bbox. This is just config — a `min_zoom` and a global bbox.

**Unified model:** each source tiles from *the priority mosaic of everything ≤
its priority, at its own resolution, clipped to its bbox*. GEBCO (priority 0) →
mosaic of just GEBCO at GEBCO res. CUDEM (priority 1) → GEBCO+CUDEM at 3.4 m in
CUDEM's bbox. This removes the base/region special-case from `build` (one loop)
and lets you swap GEBCO→ETOPO or run base-less by editing config alone.

**Work items:**
- `sources.conf` row gains `min_zoom`: `id|url|datum_offset|priority|bbox|min_zoom|max_zoom`,
  with `gebco | <zip-url> | 0 | 0 | -180,-85,180,85 | 0 | 9`.
- `ingest` branches on URL type — `.zip` → today's `download` logic; else the
  current curl+warp. Either way it outputs a normalized DEM. `download` is
  absorbed into `ingest`.
- `mosaic` builds a per-source ≤-priority VRT at that source's resolution.
- `build` becomes one loop (ingest → tile `[min_zoom,max_zoom]` over bbox →
  merge); `REGION_MIN_Z` and the dead no-sources branch both disappear.

**Honest caveat:** this *relocates* the specialness (zip fetch → an `ingest`
branch) rather than removing it; net lines are about the same. The win is
conceptual — one uniform pipeline, swappable base, config as the single source of
truth. Do it once the source set is stable (Phases 2–3), so the refactor isn't
chasing a moving target.

---

## Phase 5 — Fidelity & ops (ongoing, as needed)

- **Proper VDatum vertical transforms** replacing constant offsets, where Phase 3 seams prove inadequate.
- **GEBCO TID-based quality masking** — prefer measured cells over interpolated when blending.
- **NOAA CSB** crowdsourced fill; **GLOBathy** lakes (separate inland layer).
- **Auto-refresh** as sources update (GEBCO annual, others irregular).

Pull these in only when a concrete need appears — most users won't notice the
difference between a constant offset and full VDatum at these zooms.

---

## What does *not* change

- `terrain` (rio-rgbify), `contour` (gdal_contour → tippecanoe), smoothing, color
  ramp, encoding — all operate on whatever DEM the VRT hands them.
- Viewer: still one `terrain.pmtiles` + one `contours.pmtiles`. Only bump the
  source `maxzoom` so MapLibre requests the deeper regional tiles.
- Distribution: single PMTiles per layer on R2, same as today.

## Effort summary

| Phase | Scope                          | Effort      |
| ----- | ------------------------------ | ----------- |
| 0     | GEBCO base confirmed           | ~0          |
| 1     | Abstraction + 1-region proof   | 1–2 days    |
| 2     | Full CUDEM (multi-tile ingest) | 2–4 days    |
| 3     | Other sources + seams + CI     | 1–2 weeks   |
| 4     | Unify GEBCO as a source        | ~1 hour     |
| 5     | VDatum, CSB, lakes, refresh    | ongoing     |

Phase 1 reuses the entire existing pipeline; the only genuinely hard, open-ended
work (datum normalization, format adapters, seams) is isolated in `ingest` and
deferred to Phases 2–3.
