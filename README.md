# Bathymetry Tiles

Bathymetry web map tiles comprised of global ([GEBCO](https://www.gebco.net/)) and
regional high-res sources (CUDEM, EMODnet, …):

- **terrain** — Terrarium-encoded raster, for depth shading (color-relief), hillshade, and 3D terrain
- **contours** — bathymetric vector contour lines at non-uniform depth intervals

Part of the [OpenWaters](https://github.com/openwatersio) project — modern
open-source tools for marine navigation.

## Quick start

The toolchain is heavy native binaries; the `Dockerfile` is the source of truth.
Locally you need [uv](https://docs.astral.sh/uv/), [just](https://github.com/casey/just),
GDAL, and tippecanoe (see [CONTRIBUTING.md](CONTRIBUTING.md) for the full setup).

```bash
uv sync --project pipelines   # once
just preview                  # build the NY-harbor demo (GEBCO + CUDEM) + seed the local Worker
```

Then, in separate terminals, run the Worker and the viewer:

```bash
cd worker && npm install && npm run dev              # tile Worker on :8787
VITE_TILES_BASE=http://localhost:8787 npm run dev    # viewer on :5173 (repo root)
```

Open <http://localhost:5173/#12/40.55/-73.96>. See [CONTRIBUTING.md](CONTRIBUTING.md)
for the pipeline (`just source`/`sources`/`planet`), adding sources, and the serving
model. Drag any `.pmtiles` into the [PMTiles Viewer](https://protomaps.github.io/PMTiles/)
to inspect it.

## In the container

```bash
docker build -t gebco-tiles .
docker run --rm -v "$(pwd)/pipelines/store:/app/pipelines/store" \
  gebco-tiles just planet          # or: just source <id> / just sources
```

Set `BBOX="W,S,E,N"` for a regional build. `just --list` shows all recipes.

## GitHub Actions

The workflow at `.github/workflows/ci.yml`:

- **Every push** builds the toolchain image and runs the offline self-checks (`test-sources`, `test-engine`); the viewer builds too.
- **Default branch / release / manual dispatch** runs the full build: prepare each source (matrix) → plan the covering and diff it against the previous run → aggregate the changed tiles (sharded across runners) → bundle planet + overlays + contours + manifest. State persists in R2 so rebuilds are incremental.
- **On a published release** it promotes the bundles to Cloudflare R2 (`tiles.openwaters.io`), deploys the serving Worker, and ships the viewer to GitHub Pages.
- **Manual runs** (Actions → Build → Run workflow) accept an optional `bbox` and shard count.

Publishing requires these repository secrets: `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`,
`R2_SECRET_ACCESS_KEY`, and `CLOUDFLARE_API_TOKEN` (for the Worker deploy). The R2
bucket name (`openwaters-tiles`) is set in `ci.yml` / `worker/wrangler.toml`.

## Data sources

[GEBCO](https://www.gebco.net/) (15 arc-second global grid) is the base. Regional
high-res sources are added under `sources/<id>/` (each with its own fetch→DEM recipe);
priority is derived from resolution, so finer data wins in overlap. Current sources
include NOAA CUDEM (~3–10 m, US coast) and EMODnet (~115 m, European waters). See
[RESEARCH.md](./RESEARCH.md) and [CONTRIBUTING.md](CONTRIBUTING.md) (Adding a source).

## License

Code: BSD-3-Clause (see [LICENSE](LICENSE)). The `pipelines/*.py` vendored/adapted
from [mapterhorn](https://github.com/mapterhorn/mapterhorn) also carry its BSD-3
copyright (`pipelines/LICENSE.mapterhorn`).

Output data inherits GEBCO's terms
(public domain, attribution required):

> _GEBCO Bathymetric Compilation Group 2026 (2026) The GEBCO_2026 Grid
> (doi:10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa)_

## Prior art

- https://github.com/versatiles-org/opendem-gebco-bathymetry/
- https://github.com/shiwaku/gebco-2025-grid-tile-on-maplibre
