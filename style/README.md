# @openwaters/seascape

MapLibre GL style for the [Open Waters](https://openwaters.io) bathymetry
tiles: chart-convention depth shading, isobath contours + labels, spot
soundings, drying areas, and source-provenance overlays.

The zero-build path — the tile Worker serves the assembled style at
`/style.json` (e.g. `https://tiles.openwaters.io/seascape/style.json`); pass
that URL straight to MapLibre's `style:` option or open it in Maputnik.
`?unit=m|ft|fm` and `?safety=<metres>` bake mariner defaults into the served
style.

## Usage

Whole style (OSM raster base + bathymetry). Zooms, bounds, and attribution
come from the endpoint's TileJSON (`raster.json` / `vector.json`) — no other
fetch needed:

```js
import maplibregl from "maplibre-gl";
import { style } from "@openwaters/seascape";

new maplibregl.Map({
  container: "map",
  style: style({ tilesBase: "https://tiles.openwaters.io/seascape" }),
});
```

Composed into your own style (the protomaps-basemaps pattern — you own
`sources`, the package provides layer groups):

```js
import { day, sources, layers, state } from "@openwaters/seascape";

const myStyle = {
  version: 8,
  glyphs: "...",
  state, // global-state defaults for `unit` and `safety`
  sources: { ...myBasemapSources, ...sources({ tilesBase }) },
  layers: [...myBasemapLayers, ...layers(day)],
};
```

Options: `style()` takes `unit` / `safety` (baked defaults, see below),
`flavor`, `glyphs`, and `osm: false` to skip the base map.

## Runtime parameters

`unit` (`"m" | "ft" | "fm"`) and `safety` (metres, 0 = off) are MapLibre
[global-state](https://maplibre.org/maplibre-style-spec/root/#state) variables
(GL JS ≥ 5.6). Change them with `applyState`, which flips the state (every
label, isobath filter, and unsafe-sounding emphasis re-evaluates) **and**
rebuilds the depth-shading ramp — the one piece that can't read global-state
(`interpolate` stops must be literals). Setting one without the other desyncs
tint from labels, so don't:

```js
import { applyState } from "@openwaters/seascape";
applyState(map, { unit: "ft", safety: 3 });
```

`depthRelief(flavor, { unit, safety })` remains exported as the low-level ramp
builder if you manage the paint property yourself.

## Depth readout

`readDepth(tilesBase, lngLat, zoom)` → chart-datum elevation in metres
(negative below datum, positive above; `null` on a tile miss), decoded from
the Terrarium DEM tile pixel at native resolution. Use it instead of
`queryTerrainElevation`, which needs 3D terrain enabled and samples a coarse
mesh that reads land over deep water near coasts. Browser-only.

```js
map.on("click", async (e) => {
  const ele = await readDepth(tilesBase, e.lngLat, map.getZoom());
});
```

## Client-side contours

Instead of the embedded contour tiles, the contour layers can read isolines
generated in the browser from the DEM, via the
[openwatersio/maplibre-contour](https://github.com/openwatersio/maplibre-contour)
fork (it adds fixed `lineLevels` — stock maplibre-contour only supports uniform
intervals, which can't express the INT isobath ladder):

```js
import mlcontour from "maplibre-contour"; // github:openwatersio/maplibre-contour
import { clientContourSource, style } from "@openwaters/seascape";

const dem = new mlcontour.DemSource({
  url: `${tilesBase}/{z}/{x}/{y}.webp`,
  encoding: "terrarium",
  maxzoom: 13,
  worker: true,
});
dem.setupMaplibre(maplibregl);
style({ tilesBase, clientContours: clientContourSource(dem) });
```

Soundings, drying, and coverage still come from the embedded vector source —
only the contour lines/labels switch. Trade-offs vs embedded: no Chaikin
smoothing and no fathom-curve geometry set (ft/fm labels unit-convert the
metric `INT_ISOBATHS_M` levels); in exchange, contours render at any zoom and
level changes need no pipeline rebuild. JS consumers only — the hosted
`/style.json` can't register the DEM protocol, so it always serves embedded
contours.

## Flavors

A flavor is a plain object of colors/fonts (`day` is the only built-in so
far); custom looks are spread-overrides:

```js
layers({ ...day, hazard: "#c00", font: ["Noto Sans Regular"] });
```

## Versioning

Semver against the *tile schema* (protomaps' policy): renaming or removing a
vector layer or feature property the style reads is a major; additive tile or
style changes are minor; visual-only tweaks are patch.

## Development

TypeScript; `tsc` emits `dist/` on install (`prepare`) and via `npm run
build`. The `development` export condition points Vite dev straight at
`index.ts`, so style edits hot-reload without a build step — but bundlers
without that condition (wrangler dev, `vite build`) read `dist/`, so rebuild
after edits.

`npm test` (vitest) validates the generated style against the MapLibre style
spec and checks the ramp/hazard math and layer-id stability.
