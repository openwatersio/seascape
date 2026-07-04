# CLAUDE.md

Guidance for Claude Code working in this repository. Development flow, layout,
and conventions are in [CONTRIBUTING.md](CONTRIBUTING.md) — this file is the
orientation + the why.

## What this is

A pipeline that turns [GEBCO](https://www.gebco.net/) global bathymetry plus
regional high-res sources (CUDEM, EMODnet, …) into web tiles, served through one
Cloudflare Worker endpoint, with a small Vite/MapLibre(/Mapbox) viewer. Each source
is an independent build step; a planet build *combines* them. Two tile layers:

- **terrain** — Terrarium-encoded raster (depth shading, hillshade, 3D), per-zoom quantized.
- **contours** — bathymetric vector contours at non-uniform depth intervals.

The engine is Python (`uv` project under `pipelines/`) driving GDAL/tippecanoe as
CLI subprocesses; `just` is the task runner. There's no compiled app code. The
predecessor pure-Bash pipeline (`scripts/`) has been **retired** — don't resurrect it.

## Commands

All recipes run from the repo root (the `Justfile` executes them in `pipelines/`).
The container (`Dockerfile`) is the source of truth for the toolchain; run recipes
through it if deps aren't installed locally.

```bash
just source <id>   # prepare one source (fetch → datum → normalize → bounds → polygon → tarball)
just sources       # prepare every source under ../sources/
just planet        # cover → aggregate → downsample → bundle  (BBOX="W,S,E,N" for a region)
just preview       # build the NY-harbor demo + seed the local Worker R2 (needs GEBCO in ../data/)
just test-sources  # offline source-stage self-check (synthetic, no network)
just test-engine   # offline aggregation/bundle self-check (priority, zoom cap, pyramid)
```

Outputs land in `pipelines/store/bundle/`: `planet.pmtiles`, one
`overlay-{z}-{x}-{y}.pmtiles` per populated grid cell, `contours.pmtiles`, and
`manifest.json`. Inspect a `.pmtiles` by dragging it into
https://protomaps.github.io/PMTiles/.

## Architecture

### Four stages

`source → aggregation → downsampling → bundle`, all cwd-relative under a single
`pipelines/store/` dir (gitignored), one subdir per stage (`store/source`,
`store/aggregation`, `store/pmtiles`, `store/contour`, `store/bundle`, …).

- **source** (`source_*.py`, per `sources/<id>/`): fetch → datum offset → normalize to a 4326 COG → `bounds.csv` → coverage polygon → tarball. Each source owns its recipe (`sources/<id>/Justfile`) composing the shared steps; `metadata.json` is attribution + an optional `max_zoom` cap. Priority derives from `(maxzoom, id)` — GEBCO loses (smallest maxzoom), so regional sources win in overlap.
- **aggregation** (`aggregation_*.py`): `cover` slices the planet into source-aware aggregation tiles (one CSV each); `run` reprojects each source by priority into a merged Float32 DEM (Gaussian seam feather), slope-smooths it (`smooth.py`), encodes Terrarium raster tiles, and forks contours (`contour_run.py`) off the same merged DEM.
- **downsampling**: 2×2-average overview pyramid below each source's native maxzoom.
- **bundle** (`bundle.py`): concat single-zoom pmtiles into `planet.pmtiles` (z0..`PLANET_MAX_ZOOM` = `macrotile_z`) + one `overlay-{cell}.pmtiles` per populated `OVERLAY_SPLIT_Z` grid cell (default z5) + `manifest.json`. Contours bundle via tippecanoe.

### Why a planet cap + grid overlays + Worker (the serving model)

GEBCO is ~z8 native; regional sources reach ~z14. Baking a full z0–14 pyramid
means upsampling GEBCO globally (hundreds of GB, no new data). Instead: **planet
capped at `macrotile_z`** (complete, all-sources-merged base, ~1–2 GB), **fixed-grid
overlays** above it — one archive per populated `OVERLAY_SPLIT_Z` cell, each carrying
the GEBCO-filled merged mosaic (Terrarium has no transparency, so an overlay must not
punch holes) — and the **`worker/` Cloudflare Worker** resolves per tile: z≤cap →
planet; z>cap in a populated cell → that cell's archive (the cell is computed from
the tile address — no footprint search); else → overzoom the planet — one endpoint,
no global upsampling, no holes. Overlays are grid cells rather than per-source
archives on purpose: a cell is a fixed fraction of the globe, so a new source adds
*cells* instead of growing any single archive (a per-source overlay's size tracked
its footprint and outgrew CI runner disks). Reads all pmtiles from R2 over HTTP
range. The viewer (`index.js`) points at the Worker; `VITE_TILES_BASE` selects the
endpoint (empty → `localhost:8787` dev).

### Contours (the custom vector layer)

A parallel consumer of each aggregation tile's merged DEM: `gdal_contour` at the
non-uniform `CONTOUR_LEVELS` → Chaikin smooth (shapely) → clip to the unbuffered
tile bbox → 4326. Seam continuity comes from **buffer the DEM input, restrict the
tile output** (deterministic merge → byte-identical overlap → lines meet at the clip).
Contour tiles are tile-keyed in `store/contour` so clean tiles persist across runs.

### CI / incremental (`ci.yml` checks, `build.yml` build)

`ci.yml` runs the light per-commit checks (image → check + web) on every push.
The full build lives in `build.yml`:
`image → sources (matrix) → plan (cover + dirty-diff) → aggregate (sharded fan-out)
→ bundle → publish/worker/pages`. State persists in R2 under `store/`, so a rebuild
diffs the new covering against the previous (`get_dirty_aggregation_filenames`) and
only changed aggregation tiles rebuild; clean tiles' pmtiles/contours are reused.
`aggregate` shards by strided slice of the dirty list (`aggregation_run.py shard i n`).
The build runs on `workflow_dispatch` only (a shared store shouldn't be mutated by
routine pushes). Releasing a commit promotes the
build a dispatch produced for it — so dispatch a build before you release that commit.
On release, bundles promote to `bathymetry/<year>/`, the Worker deploys (`wrangler`),
and the viewer ships to Pages.

## Conventions

- Recipes are invoked from the repo root but execute in `pipelines/`; everything generated lives under `pipelines/store/` (gitignored). GDAL/tippecanoe are CLI subprocesses, not Python bindings.
- `pipelines/*.py` vendored from mapterhorn keep its BSD-3 attribution (`pipelines/LICENSE.mapterhorn`).
- Each non-trivial step ships a runnable self-check (`test_*.py`, `python encode.py`, `python smooth.py`).
- Mark deliberate simplifications with a plain comment naming the ceiling + upgrade path (no special label).
- Design docs: [ROADMAP.md](ROADMAP.md) (goal, workstreams, source/coverage, build scaling — where the work is going), [CONTRIBUTING.md](CONTRIBUTING.md) (dev flow), `RESEARCH.md`, [docs/nautical-chart-references.md](docs/nautical-chart-references.md) (IHO/NOAA chart standards + sounding-selection literature — for chart-data-model work), and the port plan referenced in commits.
