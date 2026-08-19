# @openwaters/seascape

MapLibre GL style for the [Open Waters Seascape](https://github.com/openwatersio/seascape) bathymetry tiles: depth shading, contour lines and labels, spot soundings, ENC-style depth areas and drying foreshore, and source-coverage provenance.

> **Not for navigational use.** See the [project README](https://github.com/openwatersio/seascape#readme) for the full warning. Depths are approximate, merged from sources of differing datum, age, and resolution.

## Quick start

```js
import * as maplibregl from "maplibre-gl";
import { style } from "@openwaters/seascape";

const map = new maplibregl.Map({
  container: "map",
  style: style({ tilesBase: "https://tiles.openwaters.io/seascape" }),
});
```

Or skip the package entirely and point MapLibre at the served style: `https://tiles.openwaters.io/seascape/style.json` (parameterizable with `?unit=ft&safety=3&shading=bands`).

Requires MapLibre GL JS ≥ 5.6 (the depth shading uses `color-relief`).

## Composing with your own basemap

The package follows the protomaps-basemaps split: layer structure lives in functions, appearance in a plain `Flavor` object, and you own the style. `style()` assembles a complete style (OSM base by default, `osm: false` for bathymetry-only); `sources()` + `layers()` let you graft the bathymetry into an existing style:

```js
import { sources, layers } from "@openwaters/seascape";

const myStyle = {
  version: 8,
  sources: { ...myBasemapSources, ...sources({ tilesBase }) },
  layers: [...myBaseLayers, ...layers()],
};
```

Zooms, bounds, and attribution come from the endpoint's TileJSON documents (`raster.json`, `vector.json`, `coverage.json`) — the style never hardcodes them.

## Mariner settings

Three parameters reach every dependent layer — labels, isobath filters, the depth ramp, sounding emphasis — as literals, so tint and text always agree:

- **`unit`**: `"m"` (default), `"ft"`, or `"fm"`. Feet and fathoms switch to the classic fathom-curve isobaths, not relabelled metric ones.
- **`safety`**: safety depth in metres (`0` = off). Water shallower than it tints as hazard, the safety contour is emphasized, and unsafe soundings render in the emphasis colour. Vector bands snap the boundary to the next-deeper charted isobath (ECDIS behaviour, bias shallow).
- **`shading`**: `"relief"` (default, the continuous raster ramp) or `"bands"` (crisp ENC depth-area fills). Never both at once.

To change settings on a live map without reloading the style, `applyState()` re-derives and sets the dependent properties in place:

```js
import { applyState } from "@openwaters/seascape";

applyState(map, { unit: "ft", safety: 3, shading: "relief" });
```

Pass the full current settings each call — nothing map-side remembers the previous ones. Layers absent from the map (composed subsets) are skipped. `depthRelief(flavor, { unit, safety })` remains exported as the low-level ramp builder if you manage the paint property yourself.

## Appearance

`layers()`, `style()`, and `applyState()` take a `Flavor` — a flat object of colours, band edges, and fonts. `day` is the built-in chart look; custom looks are spread-overrides:

```js
import { day, layers } from "@openwaters/seascape";

const dusk = { ...day, land: "#2a2a2a", contour: "#8899aa" };
const myLayers = layers(dusk, { unit: "m" });
```

## Reading a depth

`readDepth()` decodes the Terrarium DEM tile at a point — unlike MapLibre's `queryTerrainElevation`, it needs no 3D terrain and reads at native resolution:

```js
import { readDepth } from "@openwaters/seascape";

map.on("click", async (e) => {
  const v = await readDepth(tilesBase, e.lngLat, map.getZoom());
});
```

The returned value is chart-datum elevation in metres: negative is depth; the non-negative domain is categorical — `0` water of unknown depth, `1` drying foreshore, `2` land (see the [tile schema](https://github.com/openwatersio/seascape/blob/main/docs/schema.md)). It returns `null` only on a fetch failure — the tile server serves the land code for missing tiles, so those read as land. Browser-only (`createImageBitmap`/`OffscreenCanvas`).

## Schema compatibility

The package exports `SCHEMA`, the tile-contract version it targets. The served TileJSON carries the tileset's version in its `schema` field; compare them and treat a mismatch as fatal — a schema change means tiles may decode plausibly but wrongly:

```js
import { SCHEMA } from "@openwaters/seascape";

const tj = await (await fetch(`${tilesBase}/vector.json`)).json();
if (tj.schema !== undefined && tj.schema !== SCHEMA)
  throw new Error(`tiles are schema v${tj.schema}, this package targets v${SCHEMA}`);
```

A schema bump always ships as a major version of this package, so pinning a major (`^0.2`, `^1`) guarantees a schema migration never arrives through a routine update.

## Development

TypeScript; `tsc` emits `dist/` on install (`prepare`) and via `npm run build`. The `development` export condition points Vite dev straight at `index.ts`, so style edits hot-reload without a build step — but bundlers without that condition (wrangler dev, `vite build`) read `dist/`, so rebuild after edits.

`npm test` (vitest) validates the generated style against the MapLibre style spec and checks the ramp/hazard math and layer-id stability.

## License

BSD-3-Clause. The tiles themselves carry their own per-source attribution, served in the TileJSON.
