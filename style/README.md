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
import { day, sources, layers } from "@openwaters/seascape";

const myStyle = {
  version: 8,
  glyphs: "...",
  sources: { ...myBasemapSources, ...sources({ tilesBase }) },
  layers: [...myBasemapLayers, ...layers(day)],
};
```

Options: `style()` takes `unit` / `safety` (baked defaults, see below),
`flavor`, `glyphs`, and `osm: false` to skip the base map.

## Runtime parameters

`unit` (`"m" | "ft" | "fm"`) and `safety` (metres, 0 = off) appear in the
generated expressions as literals, so the style runs on any GL JS with the
`color-relief` layer (MapLibre ≥ 5.6). Change them on a live map with
`applyState`, which re-derives every dependent property (depth ramp, isobath
filters, label text, unsafe-sounding emphasis) and sets them in place. Pass the
full settings each call — nothing map-side remembers the previous values:

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
