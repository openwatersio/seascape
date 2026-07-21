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
come from the endpoint's TileJSON (`raster.json` / `vector.json` /
`coverage.json`) — no other
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

`readDepth(tilesBase, lngLat, zoom)` → the decoded Terrarium DEM sample in metres (`null` only on a fetch failure — the Worker serves the land sentinel for missing tiles, so those read as land), read at native resolution. The published DEM is a depth product, not a topo map — decoded value `v` (thresholds are `DRYING_CAP`, currently 16):

- `v < 0` — elevation below the winning source's datum; `-v` is charted depth on measured water. The encoding is shallow-biased; the datum is per-source (≈MSL sources read deep vs a low-water chart datum until datum unification).
- `v == 0` — water present, **depth unknown** (ENC `UNSARE` analogue), not a measured depth of ~0.
- `0 < v ≤ 16` — a non-submerged sample: genuine drying foreshore, or a transition value from smoothing/overzoom interpolation. Consult the `depare` layer before presenting it as drying height.
- `v > 16` — **land / out of scope**: a synthetic sentinel (the pipeline clamps land to `DRYING_CAP + 1`; the Worker also serves it for missing tiles), not measured elevation.

Use it instead of `queryTerrainElevation`, which needs 3D terrain enabled and samples a coarse mesh that reads land over deep water near coasts. Browser-only.

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
