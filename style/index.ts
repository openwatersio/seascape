/**
 * @openwaters/seascape — MapLibre GL style for the Open Waters bathymetry tiles.
 *
 * Follows the protomaps-basemaps split: layer *structure* lives in the
 * per-layer modules (one file per layer family), appearance lives in a plain
 * "flavor" object (flavor.ts), and the consumer owns the style — either
 * assembled piecemeal (`sources()` + `layers()` concat with other layer
 * groups) or whole via `style()` (what the tile Worker serves at /style.json).
 *
 *   import { style } from "@openwaters/seascape";
 *   new maplibregl.Map({ style: style({ tilesBase }) });
 *
 * Zooms, bounds, and attribution come from the endpoint's TileJSON documents
 * (raster.json / vector.json).
 *
 * Runtime parameters: `unit` ("m" | "ft" | "fm") and `safety` (metres, 0 = off)
 * appear in the generated expressions as literals — every label, isobath
 * filter, and the depth-shading ramp. Literal expressions run on any GL JS
 * with color-relief (MapLibre >= 5.6). To change them on a live map,
 * applyState() re-derives each layer's declared state-dependent properties and
 * sets them in place; or fetch a regenerated style
 * (`/style.json?unit=ft&safety=3`). `shading` ("relief" | "bands") picks the
 * water shading: the raster ramp, or the vector ENC depth-area bands above z6
 * (the relief keeps z<6 either way).
 */
import type {
  LayerSpecification,
  SourceSpecification,
  StyleSpecification,
} from "@maplibre/maplibre-gl-style-spec";
import {
  DEFAULT_SAFETY,
  DEFAULT_SHADING,
  DEFAULT_UNIT,
  day,
  type Flavor,
  type Shading,
  type Unit,
} from "./flavor.js";
import { depthShadingLayer, depthShadingState } from "./depth-shading.js";
import { depthAreasLayer, depthAreasState } from "./depth-areas.js";
import { hillshadeLayer, hillshadeState } from "./hillshade.js";
import {
  contourLabelsLayer,
  contourLabelsState,
  contourLinesLayer,
  contourLinesState,
} from "./contours.js";
import { soundingsLayer, soundingsState } from "./soundings.js";
import { coverageLayers } from "./coverage.js";

export { day, type Flavor, type Shading, type Unit } from "./flavor.js";
export { depthRelief } from "./depth-shading.js";
export { depthAreasColor } from "./depth-areas.js";

// The tile-contract version this package targets (docs/schema.md). Compare it
// against the `schema` field of the endpoint's TileJSON: a mismatch means the
// tiles may decode plausibly but wrongly — treat it as fatal, not cosmetic.
export const SCHEMA = 1;

// Self-hosted (openwatersio/tile-fonts). The MapLibre demo stack this replaced is a subset —
// of the ten subscript digits it ships three — and a missing glyph is not an error, it simply
// does not draw, so a decimetre digit would vanish off the chart.
const DEFAULT_GLYPHS =
  "https://tiles.openwaters.io/fonts/{fontstack}/{range}.pbf";

// ─── Sources ─────────────────────────────────────────────────────────────────
// tilesBase is the Worker endpoint; the sources reference its TileJSON docs
// (raster.json / vector.json / coverage.json), which carry the tile URLs, zoom
// range, bounds, and the combined per-source attribution. Coverage is its own
// source: its tileset ends at a low maxzoom and MapLibre overzooms it
// independently of the vector source (a layer inside vector.pmtiles would
// vanish above the zoom it was tiled to).
export function sources({
  tilesBase,
  dem = "seascape-dem",
  vector = "seascape-vector",
  coverage = "seascape-coverage",
}: {
  tilesBase: string;
  dem?: string;
  vector?: string;
  coverage?: string;
}): Record<string, SourceSpecification> {
  tilesBase = tilesBase.replace(/\/+$/, ""); // tolerate a trailing slash
  return {
    [dem]: {
      type: "raster-dem",
      url: `${tilesBase}/raster.json`,
      tileSize: 512,
      // MapLibre doesn't read `encoding` from TileJSON — it must be inline.
      encoding: "terrarium",
    },
    [vector]: {
      type: "vector",
      url: `${tilesBase}/vector.json`,
    },
    [coverage]: {
      type: "vector",
      url: `${tilesBase}/coverage.json`,
    },
  };
}

// ─── Layers ──────────────────────────────────────────────────────────────────
// One entry per layer family, in paint order. `unit` picks every sounding and
// contour label and which isobath set shows; `safety` moves the hazard tint
// and the emphasized contour. Everything is a literal, so runtime changes go
// through applyState(), which regenerates these.
export function layers(
  flavor: Flavor = day,
  {
    dem = "seascape-dem",
    vector = "seascape-vector",
    coverage = "seascape-coverage",
    unit = DEFAULT_UNIT,
    safety = DEFAULT_SAFETY,
    shading = DEFAULT_SHADING,
    hillshade = true,
  }: {
    dem?: string;
    vector?: string;
    coverage?: string;
    unit?: Unit;
    safety?: number;
    shading?: Shading;
    /** Bathymetric hillshading over the water shading, on by default; false hides it. */
    hillshade?: boolean;
  } = {},
): LayerSpecification[] {
  return [
    depthShadingLayer(flavor, { dem, unit, safety, shading }),
    depthAreasLayer(flavor, { vector, unit, safety, shading }),
    hillshadeLayer(flavor, { dem, hillshade }),
    contourLinesLayer(flavor, { vector, unit, safety }),
    contourLabelsLayer(flavor, { vector, unit }),
    soundingsLayer(flavor, { vector, unit }),
    ...coverageLayers(flavor, { coverage }),
  ];
}

// ─── Whole style ─────────────────────────────────────────────────────────────
// A complete, drop-in StyleSpecification: OSM raster base (osm: false for
// layers-only over your own basemap) + the bathymetry sources and layers.
// `unit`/`safety` parameterize every layer, ramp included, so labels and tint
// always agree.
export function style({
  tilesBase,
  flavor = day,
  glyphs = DEFAULT_GLYPHS,
  osm = true,
  unit = DEFAULT_UNIT,
  safety = DEFAULT_SAFETY,
  shading = DEFAULT_SHADING,
  hillshade = true,
}: {
  tilesBase: string;
  flavor?: Flavor;
  glyphs?: string;
  osm?: boolean;
  unit?: Unit;
  safety?: number;
  shading?: Shading;
  hillshade?: boolean;
}): StyleSpecification {
  const osmSource: Record<string, SourceSpecification> = osm
    ? {
        osm: {
          type: "raster",
          tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
          tileSize: 256,
          attribution:
            "&copy; <a href='https://www.openstreetmap.org/copyright'>OpenStreetMap</a>",
        },
      }
    : {};
  const osmBase: LayerSpecification[] = osm
    ? [{ id: "osm-base", type: "raster", source: "osm" }]
    : [];
  return {
    version: 8,
    name: "Open Waters Seascape",
    glyphs,
    sources: { ...osmSource, ...sources({ tilesBase }) },
    layers: [
      ...osmBase,
      ...layers(flavor, { unit, safety, shading, hillshade }),
    ],
  };
}

// ─── Runtime helpers ──────────────────────────────────────────────────────────
// The unit/safety literals in the layers only change when the layers are
// regenerated, and depth readout decodes Terrarium pixels. These live here so
// consumers don't re-derive either.

// The subset of maplibregl.Map that applyState touches (structural, so the
// package needs no maplibre-gl dependency).
export interface ChartMap {
  setFilter(layerId: string, filter: unknown): unknown;
  setLayoutProperty(layerId: string, name: string, value: unknown): unknown;
  setPaintProperty(layerId: string, name: string, value: unknown): unknown;
  getLayer(id: string): unknown;
}

// Which properties applyState re-derives, declared BY each layer module beside
// the layer it describes — a layer that gains a state-dependent property
// declares it there, in the same diff, instead of this file quietly not
// updating it (the failure mode of the hardcoded per-layer switch this table
// replaced).
const STATEFUL: Record<string, string[]> = {
  "depth-shading": depthShadingState,
  "depth-areas": depthAreasState,
  "depth-hillshade": hillshadeState,
  "contour-lines": contourLinesState,
  "contour-labels": contourLabelsState,
  soundings: soundingsState,
};

// Change the mariner settings on a live map: re-derive each layer's declared
// state-dependent properties (depth ramp, isobath filters, label text) and set
// them in place — the in-place equivalent of reloading a regenerated style.
// Takes the full settings each call: nothing map-side stores the previous
// values, so callers pass what their controls currently show. `shading` also
// gates the depare fill's filter (relief hides the depth bands but keeps
// drying/nodata), so pass the current mode — it defaults to the relief mode
// when omitted. Layers absent from the map (composed subsets) are skipped.
export function applyState(
  map: ChartMap,
  {
    unit,
    safety,
    shading = DEFAULT_SHADING,
    hillshade = true,
  }: { unit: Unit; safety: number; shading?: Shading; hillshade?: boolean },
  flavor: Flavor = day,
): void {
  const spec = Object.fromEntries(
    layers(flavor, { unit, safety, shading, hillshade }).map((l) => [l.id, l]),
  ) as Record<
    string,
    {
      filter?: unknown;
      layout?: Record<string, unknown>;
      paint?: Record<string, unknown>;
    }
  >;
  for (const [id, props] of Object.entries(STATEFUL)) {
    if (!map.getLayer(id)) continue;
    for (const prop of props) {
      if (prop === "filter") map.setFilter(id, spec[id].filter);
      else if (prop.startsWith("layout."))
        map.setLayoutProperty(
          id,
          prop.slice(7),
          spec[id].layout?.[prop.slice(7)],
        );
      else if (prop.startsWith("paint."))
        map.setPaintProperty(
          id,
          prop.slice(6),
          spec[id].paint?.[prop.slice(6)],
        );
    }
  }
}

// Chart-datum elevation at a point, in metres. `v < 0` is depth (shallow-biased
// encoding; the datum is per-source — ≈MSL sources read deep vs a low-water chart
// datum until datum unification). The non-negative domain is categorical: 0 water
// of unknown depth (ENC UNSARE), 1 drying foreshore (height not carried — depare
// drval bands are the only drying-height source), 2 land. Fractions between codes
// are overzoom/interpolation transitions — round to the nearest code or consult
// depare. Returns null only on a fetch failure: the Worker serves the land code
// for missing tiles, so those read as land. Decodes the Terrarium DEM pixel directly, at
// native resolution — unlike queryTerrainElevation, which needs 3D terrain enabled
// and samples the coarse terrain mesh (it reads land over deep water near the
// coast). Browser-only (createImageBitmap / OffscreenCanvas).
export async function readDepth(
  tilesBase: string,
  lngLat: { lng: number; lat: number },
  zoom: number,
): Promise<number | null> {
  tilesBase = tilesBase.replace(/\/+$/, ""); // tolerate a trailing slash
  const z = Math.max(0, Math.round(zoom));
  const n = 2 ** z;
  const fx = ((lngLat.lng + 180) / 360) * n;
  const fy =
    ((1 - Math.asinh(Math.tan((lngLat.lat * Math.PI) / 180)) / Math.PI) / 2) *
    n;
  const X = Math.floor(fx);
  const Y = Math.floor(fy);
  const px = Math.min(511, Math.floor((fx - X) * 512));
  const py = Math.min(511, Math.floor((fy - Y) * 512));
  const r = await fetch(`${tilesBase}/${z}/${X}/${Y}.webp`);
  if (!r.ok) return null;
  const bmp = await createImageBitmap(await r.blob());
  const cx = new OffscreenCanvas(512, 512).getContext("2d")!;
  cx.drawImage(bmp, 0, 0);
  const [r8, g8, b8] = cx.getImageData(px, py, 1, 1).data;
  return r8 * 256 + g8 + b8 / 256 - 32768; // Terrarium decode → metres
}
