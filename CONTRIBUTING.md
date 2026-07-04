# Contributing

This repo turns bathymetry DEMs (GEBCO + regional high-res sources) into MapLibre/Mapbox tiles, served through a single Cloudflare Worker endpoint. Per-source build
steps feed a planet build, which produces a base + fixed-grid overlays.

<!-- FIXME[claude]: remove this illustration or convert to mermaid? -->

```
sources/<id>/   →  pipelines/  →  store/bundle/        →  worker/        →  viewer
(fetch recipe)     (the engine)   planet + overlays       unified XYZ      (index.js)
                                  + manifest.json         endpoint
```

## Layout

| Path                                      | What                                                                                                          |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `sources/<id>/`                           | One dir per source: `metadata.json` (attribution), `file_list.txt` (URLs), `Justfile` (its fetch→DEM recipe). |
| `pipelines/`                              | The Python engine (`uv` project) + `Justfile`. Stages: source → aggregation → downsampling → bundle.          |
| `worker/`                                 | Cloudflare Worker (TypeScript) that serves the unified tile endpoint from R2.                                 |
| `index.js`, `index.html`                  | Vite/MapLibre viewer (repo root).                                                                             |
| `data/`, `pipelines/store/`, `dist/`      | Build artifacts (gitignored). All pipeline stages write under `pipelines/store/`.                              |
| `ROADMAP.md`                              | Roadmap: goal, workstreams, source/coverage, build scaling. Port plan lives in the Claude plan file referenced in commits. |

## Prerequisites

The toolchain is heavy native tooling; the `Dockerfile` is the source of truth.
To work locally you need:

- **[uv](https://docs.astral.sh/uv/)** — Python deps for `pipelines/` (run `uv sync --project pipelines` once).
- **[just](https://github.com/casey/just)** — task runner.
- **GDAL CLI** — `gdalwarp`, `gdal_translate`, `gdal_contour`, `ogr2ogr`, `gdalbuildvrt`, `ogrinfo`.
- **tippecanoe** + **tile-join** — contour vector tiles.
- **Node + npm** — the viewer and the Worker (`wrangler` installs via `npm` in `worker/`).

GDAL/tippecanoe are invoked as subprocesses (not Python bindings), so they come
from whatever's on `PATH` — no version pinning to the Python ABI.

## Quick start

Builds a real GEBCO + CUDEM mosaic and serves it locally. Needs the GEBCO grid
extracted in `data/` (the recipe clips it locally — no 4 GB fetch).

```bash
uv sync --project pipelines   # once
just preview                  # build the harbor demo + seed the local Worker R2
```

Then run the two dev servers in separate terminals:

```bash
cd worker && npm install && npm run dev              # tile Worker on :8787
VITE_TILES_BASE=http://localhost:8787 npm run dev    # viewer on :5173 (repo root)
```

Open <http://localhost:5173/#12/40.55/-73.96>.

## The pipeline (run from the repo root)

```bash
just source <id>     # prepare one source (fetch → datum → normalize → bounds → polygon → tarball)
just sources         # prepare every source under sources/
just planet          # cover → aggregate → downsample → bundle  (set BBOX="W,S,E,N" for a region)
just test-sources    # offline source-stage self-check (synthetic, no network)
just test-engine     # offline aggregation/bundle self-check (priority, zoom cap, pyramid)
```

`just planet` writes to `pipelines/store/bundle/`:

- `planet.pmtiles` — the all-sources-merged base, z0–`macrotile_z` (z8 = GEBCO native, no upsampling).
- `overlay-{z}-{x}-{y}.pmtiles` — one per populated `OVERLAY_SPLIT_Z` grid cell (default z5), z`macrotile_z+1`→cell-max, carrying the GEBCO-filled merged mosaic under that cell.
- `contours.pmtiles` — MVT contours (GEBCO baked to the deepest zoom by tippecanoe).
- `manifest.json` — planet metadata + `overlay.cells` ({cell: max_zoom}) for the Worker.

Key knobs (env vars, read by `pipelines/utils.py` / `bundle.py`):
`MACROTILE_Z` (base/overlay split, default 8), `OVERLAY_SPLIT_Z` (overlay grid
cell zoom, default 5 — raise it if a cell's bundle ever outgrows a CI runner),
`NUM_OVERVIEWS`, `BBOX`,
`SMOOTH_DEM_SIGMA`/`SMOOTH_SLOPE_LOW`/`SMOOTH_SLOPE_HIGH`, `SKIP_SMOOTH`,
`SKIP_CONTOURS`, `SKIP_SOUNDINGS`, `CONTOUR_NAV_SMOOTH_MAX` (navigable-band
contour-smoothing gate; replaces the retired `SKIP_CONTOUR_SMOOTH`),
`SOUND_CELL_PX`/`SOUND_MIN_DEPTH_M` (sounding spacing / min charted depth).

## In the container

The `Dockerfile` bundles the full toolchain (GDAL, tippecanoe, the `uv` env), so you
can run the pipeline without installing native deps locally:

```bash
docker build -t bathymetry .
docker run --rm -v "$(pwd)/pipelines/store:/app/pipelines/store" \
  bathymetry just planet          # or: just source <id> / just sources
```

Set `BBOX="W,S,E,N"` for a regional build; `just --list` shows all recipes.

## Adding a source

1. Create `sources/<id>/`:
   - `metadata.json` — `name`, `producer`, `website`, `license`, and an optional `max_zoom` cap (omit to use the source's native resolution; cap it for high-res lidar like CUDEM).
   - `file_list.txt` — source URL(s): `https://…`, `s3://…` (read via `/vsis3/`), an ERDDAP `…/griddap/…` base, a `.zip`, or a local path.
   - `Justfile` — compose the shared `pipelines/source_*.py` steps the source needs. Copy an existing recipe and adjust:
     - http GeoTIFF → `source_download`; ERDDAP → `source_download_erddap --bbox …`; public COG tiles → `source_register_remote_urllist` (flat urllist, e.g. CUDEM) or `source_register_remote_geopkg` (GeoPackage tile index, e.g. NOAA S-102) — streaming `/vsicurl` refs, no download; zip → `source_download` + `source_unzip`.
     - positive-down depths or a datum offset → `source_datum --negate --offset N`.
     - always: `source_normalize --crs EPSG:… [--nodata N]` → `source_bounds` → `source_polygonize <id> 8` → `source_create_tarball`.
2. `just source <id>` (verify it lands in `pipelines/store/source/<id>/`).
3. `just planet` — its tiles fold into the grid-cell overlays + manifest automatically (priority is derived from `(maxzoom, id)`).

Transform params live in the recipe (CLI args); `metadata.json` is attribution +
the optional `max_zoom` cap only.

## Serving (`worker/`)

The Worker presents one endpoint per layer and resolves per tile:

```
GET /seascape/{z}/{x}/{y}.webp   raster: z≤8 → planet · z>8 in a populated grid cell → that cell's overlay · else → overzoom the planet · miss → transparent
GET /seascape/{z}/{x}/{y}.pbf    vector: contours.pmtiles passthrough
GET /seascape/raster.json        TileJSON (terrarium raster)
GET /seascape/vector.json        TileJSON (vector layers)
```

This keeps the base at native z8 (no global upsampling) while presenting a single
maxzoom-13 source — the Worker synthesizes the high-zoom GEBCO fallback on demand
and caches it. Overlays carry GEBCO-fill (Terrarium has no transparency, so a
source's nodata would otherwise punch holes over the base).

- Local: `npm run seed` (from `worker/`, populates the local sim R2 from `pipelines/store/bundle/`) then `npm run dev`.
- Production: `npm run deploy` (set the R2 bucket + `RELEASE_PREFIX` in `wrangler.toml`).

## CI / build & release

`.github/workflows/ci.yml` (per-commit checks), `build.yml` (full build), and `release.yml` (publish + deploy):

- **Every push** (ci.yml) builds the toolchain image and runs the offline self-checks (`test-sources`, `test-engine`); the viewer builds too.
- **Manual dispatch** (build.yml) runs the full build: prepare each source (matrix) → plan the covering and diff it against the previous run → aggregate the changed tiles (sharded across runners) → bundle planet + overlays + contours + manifest. Build state and per-commit bundles persist in the public **data bucket** (`data.openwaters.io`) under `bathymetry/`, so rebuilds are incremental. Manual runs (Actions → Build → Run workflow) accept an optional `bbox` and shard count.
- **On a published release** the build for that commit is promoted from the data bucket into the Worker-fronted **serving bucket** (`tiles.openwaters.io`) at `bathymetry/<sha>/`, the Worker is deployed pointing at that release (`RELEASE_PREFIX`), and the viewer ships to GitHub Pages. Re-dispatching `release.yml` with a prior built sha republishes it with no rebuild.

Two R2 buckets — `data` (public, all build state) and the serving bucket. Required
repository secrets: `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
`R2_BUCKET` (the serving bucket), and `CLOUDFLARE_API_TOKEN` (Worker deploy). The R2
credentials need read/write on **both** buckets; the Worker's binding
(`worker/wrangler.toml` `bucket_name`, overwritten at deploy) names the serving bucket.

## Conventions

- `pipelines/*.py` vendored from mapterhorn keep its BSD-3 attribution (`pipelines/LICENSE.mapterhorn`).
- Each non-trivial step ships a runnable self-check (`test_*.py`, `python smooth.py`, `python encode.py`).
- Don't commit build artifacts (`pipelines/store/`, `data/`, `dist/`, `output/`).
