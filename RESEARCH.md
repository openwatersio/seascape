# Bathymetry Vector Tiles — Research Summary

Research conducted March 2026; reviewed June 2026. This document captures the findings
that informed this pipeline's design.

---

## Bathymetry Data Sources

### Global

| Source           | Resolution         | Format          | License       | Notes                                                                                                                                                                                                                           |
| ---------------- | ------------------ | --------------- | ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **GEBCO 2026**   | 15 arc-sec (~450m) | NetCDF, GeoTIFF | Public domain | **Current.** Released 23 Apr 2026. Gold standard, Seabed 2030 initiative. 28.7% of ocean floor mapped to modern standards (up from 27.3%). Cadence is annual but not fixed — 2026 shipped in April, prior releases in July/Aug. |
| **ETOPO 2022**   | 15 arc-sec         | NetCDF, GeoTIFF | Public domain | NOAA. Integrates GEBCO + BlueTopo + CUDEM into one seamless model. Still the latest ETOPO.                                                                                                                                      |
| **SRTM15+ v2.8** | 15 arc-sec         | GeoTIFF         | Public domain | GEBCO's base grid between 50°S–60°N. v2.8 (used in GEBCO 2026) derives predicted bathymetry from **SWOT satellite gravity + machine learning** — a notable quality jump in deep/sparse areas.                                   |

### Regional / High-Resolution

| Source            | Resolution                    | Coverage               | Notes                                                                |
| ----------------- | ----------------------------- | ---------------------- | -------------------------------------------------------------------- |
| **NOAA BlueTopo** | 2–16m                         | US navigable waters    | Best public US bathymetry. AWS: `s3://noaa-bathymetry-pds/BlueTopo/` |
| **NOAA CUDEM**    | ~3–10m                        | US coast + territories | Seamless topo-bathy. Good for land-water transition.                 |
| **EMODnet**       | ~115m composite, ~3.6m hi-res | European seas          | 200+ contributing DTMs.                                              |
| **NIWA NZ**       | 250m grid                     | NZ EEZ                 | Contours at 50m (deep) and 10m (shallow) intervals.                  |
| **GLOBathy**      | Modeled                       | 1.4M+ lakes globally   | For inland waters. Published in Nature Scientific Data.              |

### Crowd-Sourced

- **NOAA CSB** (Crowdsourced Bathymetry) — contributed via IHO DCDB
- **OpenSeaMap** — GEBCO-derived contours clipped by OSM water polygons

---

## Three Approaches to Distributing Bathymetry

### 1. Raster DEM Tiles (Terrain RGB)

Elevation encoded in RGB channels of PNG/WebP image tiles. Two encodings:

- **Mapbox**: `elevation = -10000 + ((R×256×256 + G×256 + B) × 0.1)`
- **Terrarium**: `elevation = (R×256 + G + B/256) - 32768`

**Tools**: `rio-rgbify` → MBTiles → `pmtiles convert`

**MapLibre support**:

- GL JS: `raster-dem` source → hillshade, color-relief, 3D terrain, client-side contours via `maplibre-contour`
- Native (iOS/Android): `raster-dem` source → hillshade + color-relief ✅, 3D terrain 🚧 in progress
- React Native: via style JSON (no dedicated components yet)

**Pros**: One file serves hillshade + color-relief + 3D + contours (on web). Runtime unit switching. Color scheme configurable in style JSON. PMTiles now works on Native too (see below).
**Cons**: Client-side contours are JS-only.

### 2. Pre-Generated Vector Contour Tiles

`gdal_contour` → FlatGeobuf → `tippecanoe` → MBTiles/PMTiles

**Pros**: Works everywhere (web, iOS, Android). Zero client compute. Pre-baked attributes. Non-uniform contour intervals via `gdal_contour -fl`.
**Cons**: Fixed at generation time. Must regenerate for different intervals.

### 3. Server-Side Dynamic Contours

**PostGIS**: `ST_Contour` (3.2+) + `ST_AsMVT` → Martin tile server
**dem-tiler**: Lambda + COG → `gdal_contour` on demand
**TechIdiots-LLC/contour-generator**: Node.js, reads Terrain RGB tiles, marching squares per tile. Only uniform intervals, 10-60s per tile — too slow for global pre-rendering.

**Pros**: Always fresh. Configurable per-request.
**Cons**: Compute cost per request. Needs caching layer.

---

## Pipeline Design — Current Approach

**This project uses a hybrid of Approaches 1 + 2:**

1. **Terrain-RGB tiles** for depth shading (color-relief layer, hillshade, 3D terrain)
2. **Pre-generated vector contour tiles** for contour lines and labels

This combination works on all platforms (web + native) with no client-side compute for contours, and the depth shading color scheme is runtime-configurable.

### Terrain-RGB Pipeline

```
GEBCO DEM → gdal_edit.py -unsetnodata → rio-rgbify (Mapbox encoding) → MBTiles → pmtiles convert
```

**Key learnings:**

- `rio-rgbify` is the only tool that correctly generates Terrain-RGB tiles. It resamples the raw elevation at each zoom level _before_ encoding to RGB. This avoids the artifacts that `gdal2tiles.py` produces (averaging/interpolating already-encoded RGB bytes produces corrupted elevation values).
- `rio-rgbify` outputs 512×512 tiles. Set `tileSize: 512` in MapLibre source config.
- Must unset NoData metadata before encoding — `rio-rgbify` encodes NoData pixels as valid elevation values, producing artifacts.
- GEBCO is EPSG:4326; `rio-rgbify` handles reprojection to Web Mercator internally.
- Zoom 0–9 is a good match for GEBCO's 15 arc-second native resolution (~305m/pixel at z9).
- Global estimate at z0–9: ~350K tiles, ~15–30 GB.

### Contour Pipeline

```
GEBCO DEM
  ├─ Light smoothing (5×5 kernel) → gdal_contour -fl [shallow levels] → shallow.fgb
  ├─ Heavy smoothing (9×9 kernel) → gdal_contour -fl [deep levels]   → deep.fgb
  └─ ogr2ogr merge + enrich → all_contours.fgb
                                   └─ tippecanoe -j (zoom filter) → contours.pmtiles
```

**Non-uniform contour intervals via `gdal_contour -fl`:**
The `-fl` flag accepts specific elevation values, enabling fine intervals for shallow water (where navigation decisions happen) and coarse intervals for deep water:

```bash
# Shallow (0–200m): navigation-critical
SHALLOW_LEVELS="-1 -2 -3 -5 -7 -10 -15 -20 -30 -50 -75 -100 -150 -200"

# Deep (200m+): context only
DEEP_LEVELS="-200 -500 -1000 -1500 -2000 -3000 -4000 -5000 -6000 -8000 -10000"
```

**Differential smoothing:** Shallow contours use light smoothing to preserve coastal detail. Deep contours use heavy smoothing (9×9 Gaussian) to eliminate the grid stairstepping that is visually distracting at ocean scale.

**Zoom-dependent filtering via tippecanoe `-j`:** The `index` attribute encodes contour significance (0=finest, 5=coarsest). tippecanoe's filter drops fine contours from low-zoom tiles:

- z0–3: 1000m+ contours only
- z4–5: 500m+
- z6–7: 200m+
- z8–9: 100m+
- z10–11: 50m+
- z12+: all contours

**FlatGeobuf vs GeoJSONSeq:** FlatGeobuf is ~8× faster for tippecanoe to read (auto-parallelized). Enrichment via `ogr2ogr -sql` is much faster than piping through `jq`.

**Output size estimates (global, ~25 non-uniform levels):**

- Raw FlatGeobuf: ~5–15 GB
- Final PMTiles (z0–14, with zoom filtering): ~2–8 GB

### Depth Shading via color-relief Layer

MapLibre GL JS v5+ and MapLibre Native both support the `color-relief` layer type, which reads elevation values directly from `raster-dem` tiles and applies a color ramp on the GPU. One set of Terrain-RGB tiles serves triple duty: color-relief + hillshade + 3D terrain.

**Color scheme follows S-52/ECDIS marine conventions:**

- Shallow water = dark blue (danger draws the eye)
- Deep water = light/near-white (context, not information)
- Sharp break at 0m to green (land/water boundary)
- `exponential` interpolation base 0.5 compresses deep range, expands shallow range
- No red/orange in water (avoids confusion with IALA navigation aid colors)

**Key depth thresholds in the color ramp:**

- 0m: chart datum (darkest blue)
- -2m: shoal draft sailboat limit
- -3m: recreational powerboat draft limit
- -5m: ECDIS default safety contour
- -15m: Panamax draft + under-keel clearance
- -20m: ECDIS deep contour default
- -200m: continental shelf edge

### maplibre-contour (Client-Side) — Evaluated and Set Aside

[maplibre-contour](https://github.com/onthegomap/maplibre-contour) generates contour lines client-side from Terrain-RGB tiles using marching squares in a web worker. We evaluated it extensively:

**What works well:**

- Smooth contour lines (with `subsampleBelow: 800` for upsampling)
- Zero build step — contours generated on the fly
- The [prozessor13 fork](https://github.com/prozessor13/maplibre-contour) adds `lineLevels` for explicit elevation values (not just uniform intervals), plus isoband polygons and spot soundings

**Why we moved away from it:**

- **Performance:** `thresholds` generates contours at every N meters across the full depth range. With 10m intervals, that's 1,100 contour levels per tile — marching squares is expensive. Even with `lineLevels`, ~25 levels still causes noticeable jank.
- **No native support:** Runs in JS web workers, not available on iOS/Android.
- **No depth-range filtering:** The `generateIsolines` function processes all elevations uniformly. There is no built-in `minElevation`/`maxElevation` to limit computation to shallow water only.

For web-only prototyping it's useful, but for production cross-platform use, pre-rendered vector tiles are the better path.

---

## Prior Art

| Project                                                                                                 | Approach                                                  | Notes                                                                                                                                        |
| ------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| [shiwaku/gebco-2025-grid-tile-on-maplibre](https://github.com/shiwaku/gebco-2025-grid-tile-on-maplibre) | Terrain-RGB + color-relief + hillshade + maplibre-contour | Reference implementation. Uses MapLibre v5 color-relief layer, 3D terrain, globe projection. Tiles at z0–9 with nearest-neighbor resampling. |
| [acalcutt/GEBCO_to_MBTiles](https://github.com/acalcutt/GEBCO_to_MBTiles)                               | GEBCO → raster MBTiles                                    |                                                                                                                                              |
| [TechIdiots-LLC/contour-generator](https://github.com/TechIdiots-LLC/contour-generator)                 | Node.js, Terrain RGB → contour PBF tiles                  | Only uniform intervals, 10-60s/tile. Not viable for global pre-rendering.                                                                    |
| [nst-guide/terrain](https://github.com/nst-guide/terrain)                                               | USGS DEM → contour vector tiles                           | Uses `gdal_contour` + `tippecanoe`. US-only, starts from pre-made contour shapefiles.                                                        |
| [onthegomap/maplibre-contour](https://github.com/onthegomap/maplibre-contour)                           | Client-side contours from DEM tiles                       | JS-only. Good for web prototyping.                                                                                                           |
| [prozessor13/maplibre-contour](https://github.com/prozessor13/maplibre-contour)                         | Fork with `lineLevels`, isobands, spot soundings          | Explicit elevation values instead of uniform intervals.                                                                                      |
| [OpenSeaMap/opendem.info](https://opendem.info/download_bathymetry.html)                                | GEBCO-derived contours                                    |                                                                                                                                              |

---

## Key Tools

| Tool                               | Purpose                                                 | Install                                       |
| ---------------------------------- | ------------------------------------------------------- | --------------------------------------------- |
| **GDAL**                           | Raster processing, contour extraction, DEM smoothing    | `apt install gdal-bin` / `brew install gdal`  |
| **rio-rgbify**                     | DEM → Terrain-RGB MBTiles (correct per-zoom resampling) | `pip install rio-rgbify`                      |
| **tippecanoe** (Felt fork)         | Vector tile generation with zoom-dependent filtering    | `brew install tippecanoe` / build from source |
| **pmtiles** CLI                    | MBTiles ↔ PMTiles conversion                            | `go install github.com/protomaps/go-pmtiles`  |
| **ogr2ogr** (part of GDAL)         | FlatGeobuf merge, SQL enrichment                        | Included with GDAL                            |
| **tile-join** (part of tippecanoe) | Merge multiple MBTiles/PMTiles tilesets                 | Included with tippecanoe                      |
| **gdaldem**                        | Color-relief, hillshade rendering from DEMs             | Included with GDAL                            |

---

## MapLibre Native Terrain Support Status (reviewed June 2026)

- **Hillshade**: ✅ Supported. Algorithms: basic, combined, igor, multidirectional.
- **Color-relief**: ✅ Supported (Andrew Calcutt's PR). Reads `raster-dem` tiles, applies color ramp on GPU. Recent fixes (invisible above fill layers on Metal/Vulkan/WebGPU).
- **3D Terrain**: 🚧 Still in active development by Jesse Crocker & Nathan Olson (per April 2026 newsletter).
- **PMTiles protocol**: ✅ **Built in** as of Android 11.7.0 (Jan 2025). Prefix the source with `pmtiles://` (then `https://`, `asset://`, or `file://`). Enabled via the `MLN_WITH_PMTILES` CMake flag. Caveat: Android PMTiles sources don't support offline pack downloads. No tile server or tile extraction needed.
- **maplibre-contour**: ❌ JS-only (web workers). Not available on Native.
- **Vector tiles**: ✅ Full support. Pre-rendered contour PMTiles load directly via `pmtiles://`.
- **MapLibre Tile (MLT)**: 🆕 New vector tile format released Jan 2026, more efficient than MVT. Not yet wired into the tippecanoe→PMTiles path — watch as the eventual MVT successor for contour tiles.

---

## Build Infrastructure

GEBCO updates annually (release month varies — 2026 shipped in April, earlier grids in July/Aug). The build is resource-intensive for global data:

| Option                              | Cost                       | Notes                                                        |
| ----------------------------------- | -------------------------- | ------------------------------------------------------------ |
| **Local machine**                   | Free                       | Run once, upload to object storage. Fine for annual builds.  |
| **GitHub Actions large runners**    | $0.064/min (16-core Linux) | Up to 64-core/256GB. Could work for regional builds.         |
| **Spot instance (AWS/GCP/Hetzner)** | ~$0.10–0.50/hr             | Spin up, build, upload to R2/S3, shut down. Best for global. |
| **Docker**                          | —                          | Dockerfile provided for reproducible builds.                 |

For distribution, PMTiles on Cloudflare R2 (or S3 + CloudFront) is the cheapest option — static file serving with HTTP range requests, no tile server needed. This now works for **both web and native**: MapLibre Native reads `pmtiles://https://…` directly (Android 11.7.0+), so the same R2-hosted PMTiles file serves every platform. No Martin proxy or tile extraction required.
