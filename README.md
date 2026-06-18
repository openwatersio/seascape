# Open Waters: Bathymetry

Bathymetry as web map tiles for MapLibre / Mapbox GL, build from a mosaic of global and regional sources. Served as raster DEM tiles for depth shading and hillshade, and vector contour tiles for crisp lines and labels.

Coverage is global through z8 (~15″) with regional detail to z14 (~0.25″) where high-res sources exist.

### **[Preview](https://openwatersio.github.io/bathymetry/)**

> [!WARNING]
> **Not for navigational use.** Do not use this bathymetry for navigation, or in any
> situation where inaccuracies could result in harm to people or property. It is intended
> for general-purpose web mapping and visualization.
>
> Depths are **not** reduced to a chart datum and do not account for tides or water
> level — they are not charted depths. The data is interpolated and merged from sources
> of differing age, resolution, and datum, then smoothed during tiling, so values are
> approximate. Gridded bathymetry also omits navigational hazards (rocks, wrecks,
> obstructions, shoals, aids to navigation) shown on official charts.
>
> Always consult official nautical charts for navigation.

## Usage

### TileJSON

The bathymetry is available as XYZ tiles in two flavors:

- **Raster** (Terrarium-encoded DEM) — [TileJSON](https://tiles.openwaters.io/bathymetry/raster.json) - depth per pixel, for depth shading (color-relief), hillshade, and 3D terrain.<br/>
- **Vector** (MVT) - [TileJSON](https://tiles.openwaters.io/bathymetry/vector.json) — bathymetric contour lines at non-uniform depth intervals.<br\>

Point any mapping library that supports XYZ tiles at these URLs. The TileJSONs include attribution and metadata, so libraries that support TileJSON will credit the sources automatically.

### MapLibre

Point a `raster-dem` source and a `vector` source at the two TileJSONs:

```js
import maplibregl from "maplibre-gl";

const BASE = "https://tiles.openwaters.io/bathymetry";

const map = new maplibregl.Map({
  container: "map",
  center: [-73.96, 40.55],
  zoom: 11,
  style: {
    version: 8,
    sources: {
      "bathymetry-dem": {
        type: "raster-dem",
        url: `${BASE}/raster.json`,
        encoding: "terrarium", // required — MapLibre defaults to Mapbox encoding
        tileSize: 512,
      },
      bathymetry: {
        type: "vector",
        url: `${BASE}/vector.json`,
      },
    },
    layers: [
      {
        id: "hillshade",
        type: "hillshade",
        source: "bathymetry-dem",
        paint: { "hillshade-exaggeration": 0.6 },
      },
      {
        id: "contours",
        type: "line",
        source: "bathymetry",
        "source-layer": "contours",
        paint: { "line-color": "#3b7", "line-width": 0.5, "line-opacity": 0.4 },
      },
    ],
  },
});

// Optional: 3D seafloor from the same DEM
// map.on("load", () => map.setTerrain({ source: "bathymetry-dem", exaggeration: 1 }));
```

For depth **color-relief** shading, contour **labels**, and a layer-toggle UI, see the
demo viewer in [`index.js`](index.js). Attribution is carried in each TileJSON, so
MapLibre's attribution control credits the sources automatically.

## Data sources

Sources are merged by priority derived from resolution, so finer data wins where they overlap. Each is built under [`sources/`](sources/):

- **[NOAA BlueTopo](sources/bluetopo/)** — ~2–16 m, US coastal · NOAA Office of Coast Survey · public domain
- **[NOAA CUDEM 1/9″](sources/cudem/)** — ~3 m, US coast · NOAA NCEI · public domain
- **[NOAA CUDEM 1/3″](sources/cudem_third/)** — ~10 m, US coast · NOAA NCEI · public domain
- **[Danmarks Dybdemodel (DDM)](sources/ddm/)** — 50 m, Danish waters · SDFI / Dataforsyningen
- **[EMODnet Bathymetry 2024](sources/emodnet/)** — ~115 m, European waters · EMODnet Bathymetry Consortium · CC-BY 4.0
- **[GEBCO 2026 Grid](sources/gebco/)** — 15″ (~450 m), global base · GEBCO Compilation Group / BODC · public domain

See [RESEARCH.md](./RESEARCH.md) for the wider source survey and
[CONTRIBUTING.md](CONTRIBUTING.md) for adding one.

## Building & contributing

The build pipeline, local development, container usage, and CI/deploy live in
[CONTRIBUTING.md](CONTRIBUTING.md). Drag any `.pmtiles` into the
[PMTiles Viewer](https://protomaps.github.io/PMTiles/) to inspect it.

## License

Code: BSD-3-Clause (see [LICENSE](LICENSE)). The `pipelines/*.py` vendored/adapted
from [mapterhorn](https://github.com/mapterhorn/mapterhorn) also carry its BSD-3
copyright (`pipelines/LICENSE.mapterhorn`).

Output data inherits GEBCO's terms (public domain, attribution required):

> _GEBCO Bathymetric Compilation Group 2026 (2026) The GEBCO_2026 Grid
> (doi:10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa)_

## Prior art

- https://github.com/versatiles-org/opendem-gebco-bathymetry/
- https://github.com/shiwaku/gebco-2025-grid-tile-on-maplibre
