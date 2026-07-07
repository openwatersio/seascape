/**
 * @openwaters/seascape — MapLibre GL style for the Open Waters bathymetry tiles.
 *
 * Follows the protomaps-basemaps split: layer *structure* lives in these
 * functions, appearance lives in a plain "flavor" object, and the consumer owns
 * the style — either assembled piecemeal (`sources()` + `layers()` concat with
 * other layer groups) or whole via `style()` (what the tile Worker serves at
 * /style.json).
 *
 *   import { style } from "@openwaters/seascape";
 *   new maplibregl.Map({ style: style({ tilesBase }) });
 *
 * Zooms, bounds, and attribution come from the endpoint's TileJSON documents
 * (raster.json / vector.json).
 *
 * Runtime parameters: the `unit` ("m" | "ft" | "fm") and `safety` (metres,
 * 0 = off) global-state variables drive every label, filter, and sounding-
 * emphasis expression — flip them with map.setGlobalStateProperty() and the
 * style re-evaluates, no per-layer restyle. The one exception is the
 * depth-shading ramp: interpolate stops must be literals, so on unit or safety
 * change rebuild it with depthRelief() and setPaintProperty (see below).
 */
import type {
  ExpressionSpecification,
  LayerSpecification,
  SourceSpecification,
  StyleSpecification,
} from "@maplibre/maplibre-gl-style-spec";

export type Unit = "m" | "ft" | "fm";

export interface Flavor {
  bandColors: string[]; // deepest → shoalest (6 entries)
  bandEdges: { m: number[]; ft: number[] }; // band-edge depths, metres
  hazard: string;
  land: string;
  drying: string;
  contour: string;
  label: string;
  labelHalo: string;
  font: string[];
  hillshadeShadow: string;
  coverage: string;
}

// ─── Flavor (appearance only — custom looks are spread-overrides) ────────────
// Depth-shading tints follow chart convention: shoal-dark → deep-white
// (INT/NOAA), flat white beyond the deepest edge so tint stays monotonic in
// depth. Bands are perceptually spaced (adjacent ΔE ≥ 7 after 0.85-opacity
// compositing), weighted toward the shoal bands where depth discrimination
// matters. Band edges sit on isobaths the chart draws — metric levels, or the
// classic fathom curves in ft/fm mode — so tint boundaries land on contour
// lines rather than between them (paper-chart practice).
export const day: Flavor = {
  bandColors: [
    "#e9f7ff", // deepest band
    "#c9e9fd",
    "#a5d9fb",
    "#7fc7f8",
    "#5db5f0",
    "#3fa2e4", // shoalest band
  ],
  bandEdges: {
    m: [50, 20, 10, 5, 2],
    ft: [30, 10, 5, 3, 1].map((fm) => fm * 1.8288), // fathom curves 30/10/5/3/1 fm
  },
  // One perceptual step darker than the shoalest band — water shallower than
  // the safety depth.
  hazard: "#1f86cb",
  // Land above datum: translucent buff wash (paper-chart figure-ground — white
  // stays unambiguously "deep water"); a raster base reads through it.
  land: "rgba(247,240,221,0.66)",
  // Drying areas (INT-1 foreshore green): seabed above chart datum that covers
  // and uncovers with the tide.
  drying: "#a8d5ba",
  contour: "#4a7a9c",
  label: "#036",
  labelHalo: "#fff",
  font: ["Noto Sans Regular"],
  hillshadeShadow: "#9adcfe",
  coverage: "#f58231",
};

// global-state defaults (the style root `state` object).
export const state: { unit: { default: Unit }; safety: { default: number } } = {
  unit: { default: "m" },
  safety: { default: 2 },
};

const DEFAULT_GLYPHS =
  "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf";

// ─── Depth-shading ramp (elevation → colour) ─────────────────────────────────
// The hazard tint folds into this one color-relief ramp: two color-relief
// layers on one DEM source don't composite (only the first renders), so water
// shallower than the safety depth is painted into the ramp itself.
type RampStops = (number | string)[]; // alternating elevation, colour

// Terrarium's vertical LSB. The encoder clamps water to <= -LSB — elevation 0
// means land — so the shoal tint runs flat down to -LSB and land starts at 0.
const LSB = 1 / 256;

// Width (metres) of the normal→hazard colour transition at the safety depth.
const EDGE = 0.01;

const depthRamp = (flavor: Flavor, edges: number[]): RampStops => {
  const stops: RampStops = [-10000, flavor.bandColors[0]];
  edges.forEach((d, i) =>
    stops.push(-d - 0.1, flavor.bandColors[i], -d, flavor.bandColors[i + 1]),
  );
  stops.push(-LSB, flavor.bandColors[5], 0, flavor.land);
  return stops;
};

// Colour of the ramp at elevation e (linear-interpolated). Used to pin a
// normal-coloured stop right at the safety depth so the flip to the hazard
// colour is a crisp ~0.01 m edge at ANY safety value — otherwise the blend
// feathers by however far −safety lands from the nearest ramp stop.
const rampColorAt = (ramp: RampStops, e: number): string => {
  for (let i = 2; i < ramp.length; i += 2)
    if (e <= (ramp[i] as number)) {
      const e0 = ramp[i - 2] as number,
        c0 = ramp[i - 1] as string;
      const e1 = ramp[i] as number,
        c1 = ramp[i + 1] as string;
      if (c0[0] !== "#" || c1[0] !== "#") return c0;
      const t = (e - e0) / (e1 - e0);
      const p = (c: string) =>
        [1, 3, 5].map((k) => parseInt(c.slice(k, k + 2), 16));
      const a = p(c0),
        b = p(c1);
      return `rgb(${a.map((v, k) => Math.round(v + t * (b[k] - v))).join(",")})`;
    }
  return ramp[1] as string;
};

// The color-relief expression for the active unit system and safety depth.
// Interpolate stops can't be live global-state values, so unit/safety changes
// rebuild this and apply it with setPaintProperty("depth-shading", ...).
export function depthRelief(
  flavor: Flavor = day,
  { unit = "m", safety = 0 }: { unit?: Unit; safety?: number } = {},
): ExpressionSpecification {
  // The crisp-edge stops below need s + EDGE to land strictly between the
  // safety depth and the -LSB water stop, or the interpolate stops go out of
  // ascending order and MapLibre rejects the whole expression. No real safety
  // depth is centimetres, so floor tiny values instead of failing.
  if (safety > 0) safety = Math.max(safety, EDGE + 2 * LSB);
  const ramp = depthRamp(flavor, flavor.bandEdges[unit === "m" ? "m" : "ft"]);
  const s = -safety;
  const stops: RampStops = [];
  for (let i = 0; i < ramp.length; i += 2)
    if (!(safety > 0) || (ramp[i] as number) < s)
      stops.push(ramp[i], ramp[i + 1]);
  // Crisp edge: normal colour pinned at the safety depth, hazard from just
  // shallower up to shore.
  if (safety > 0)
    stops.push(
      s,
      rampColorAt(ramp, s),
      s + EDGE,
      flavor.hazard,
      -LSB,
      flavor.hazard,
      0,
      flavor.land,
    );
  return [
    "interpolate",
    ["linear"],
    ["elevation"],
    ...stops,
  ] as unknown as ExpressionSpecification;
}

// ─── Sources ─────────────────────────────────────────────────────────────────
// tilesBase is the Worker endpoint; the sources reference its TileJSON docs
// (raster.json / vector.json), which carry the tile URLs, zoom range, bounds,
// and the combined per-source attribution.
export function sources({
  tilesBase,
  dem = "seascape-dem",
  vector = "seascape-vector",
}: {
  tilesBase: string;
  dem?: string;
  vector?: string;
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
  };
}

// ─── Layers ──────────────────────────────────────────────────────────────────
export function layers(
  flavor: Flavor = day,
  {
    dem = "seascape-dem",
    vector = "seascape-vector",
    // Baked into the initial depth-shading ramp; keep in sync with the state
    // defaults (runtime changes go through depthRelief() + setPaintProperty).
    unit = state.unit.default,
    safety = state.safety.default,
  }: {
    dem?: string;
    vector?: string;
    unit?: Unit;
    safety?: number;
  } = {},
): LayerSpecification[] {
  // One global-state variable — `unit` — drives every sounding/contour label
  // and which isobaths show.
  //
  // Soundings: props depth_m / depth_ft / depth_fm, all floored toward
  // shallower; depth_m already carries one decimal in the shoal band (<6 m)
  // and an integer deeper, so metres just print it.
  const UNIT: ExpressionSpecification = ["global-state", "unit"];
  const soundingText: ExpressionSpecification = [
    "case",
    ["==", UNIT, "ft"],
    ["to-string", ["get", "depth_ft"]],
    ["==", UNIT, "fm"],
    ["to-string", ["get", "depth_fm"]],
    ["to-string", ["get", "depth_m"]], // metres (default; also covers unit unset)
  ];
  // Contours: metre isobaths (sys != "ft", also legacy no-sys tiles) vs the
  // fathom-curve set (sys == "ft"), which labels as feet or fathoms. Both
  // systems label every curve — the standard isobath sets are sparse enough
  // that GL collision thins the labels.
  const IS_FEET: ExpressionSpecification = [
    "any",
    ["==", UNIT, "ft"],
    ["==", UNIT, "fm"],
  ];
  const contourLineFilter: ExpressionSpecification = [
    "case",
    IS_FEET,
    ["==", ["get", "sys"], "ft"],
    ["!=", ["get", "sys"], "ft"],
  ];
  const contourLabelText: ExpressionSpecification = [
    "case",
    ["==", UNIT, "ft"],
    ["concat", ["to-string", ["get", "depth_ft"]], "ft"],
    ["==", UNIT, "fm"],
    ["concat", ["to-string", ["get", "depth_fm"]], "fm"],
    ["concat", ["to-string", ["get", "depth_abs_m"]], "m"],
  ];

  // Shared label styling so soundings and contour labels read as one chart.
  const labelSize: ExpressionSpecification = [
    "interpolate",
    ["linear"],
    ["zoom"],
    8,
    9,
    13,
    12,
  ];

  const coverageColor = flavor.coverage;

  return [
    {
      id: "depth-shading",
      type: "color-relief",
      source: dem,
      paint: {
        "color-relief-color": depthRelief(flavor, { unit, safety }),
        "color-relief-opacity": 0.85,
      },
    },
    {
      id: "hillshade",
      type: "hillshade",
      source: dem,
      layout: { visibility: "none" },
      paint: {
        "hillshade-exaggeration": 0.5,
        "hillshade-shadow-color": flavor.hillshadeShadow,
        "hillshade-highlight-color": "#ffffff",
        "hillshade-illumination-direction": 315,
      },
    },
    {
      // Green foreshore fill, above depth-shading and below the contour lines.
      // Where no drying polygon covers a >=0 pixel the DEM ramp still paints
      // the land wash, so a mask miss degrades to plain land rendering, never
      // to water-over-land.
      id: "drying-areas",
      type: "fill",
      source: vector,
      "source-layer": "drying",
      paint: { "fill-color": flavor.drying, "fill-opacity": 0.55 },
    },
    {
      id: "contour-lines",
      type: "line",
      source: vector,
      "source-layer": "contours",
      filter: contourLineFilter,
      // Presentation floor, not a data limit: below z6 isobaths read as clutter over depth shading.
      minzoom: 6,
      paint: {
        "line-color": flavor.contour,
        "line-width": 0.5,
        "line-opacity": 0.6,
      },
    },
    {
      id: "contour-labels",
      type: "symbol",
      source: vector,
      "source-layer": "contours",
      filter: contourLineFilter,
      minzoom: 8,
      layout: {
        "symbol-placement": "line",
        "text-field": contourLabelText,
        "text-size": labelSize,
        "text-font": flavor.font,
        "text-letter-spacing": 0.1,
        "text-max-angle": 30,
        "text-padding": 50,
      },
      paint: {
        "text-color": flavor.label,
        "text-halo-color": flavor.labelHalo,
        "text-halo-width": 1,
      },
    },
    {
      id: "soundings",
      type: "symbol",
      source: vector,
      "source-layer": "soundings",
      minzoom: 7,
      layout: {
        "text-field": soundingText,
        "text-font": flavor.font,
        "text-size": labelSize,
        "symbol-sort-key": ["get", "depth_m"], // shoalest first → wins collisions
        "text-padding": 8,
      },
      paint: {
        // Soundings at or shoaler than the safety depth print black — S-52
        // shows unsafe-water soundings in black and lets the hazard tint carry
        // the alarm; safety=0 → all normal.
        "text-color": [
          "case",
          [
            "all",
            [">", ["global-state", "safety"], 0],
            ["<=", ["get", "depth_m"], ["global-state", "safety"]],
          ],
          "#000",
          flavor.label,
        ],
        "text-halo-color": flavor.labelHalo,
        "text-halo-width": 1,
      },
    },
    // Source coverage (provenance): footprint polygons with props source_id /
    // source_name / source_maxzoom. Hidden by default; a viewer can toggle
    // them on for click-to-identify.
    {
      id: "source-fill",
      type: "fill",
      source: vector,
      "source-layer": "coverage",
      layout: { visibility: "none" },
      paint: { "fill-color": coverageColor, "fill-opacity": 0.12 },
    },
    {
      // Brightened fill of one source (filter set by the consumer on click).
      id: "source-highlight",
      type: "fill",
      source: vector,
      "source-layer": "coverage",
      filter: ["==", ["get", "source_id"], "__none__"],
      layout: { visibility: "none" },
      paint: { "fill-color": coverageColor, "fill-opacity": 0.4 },
    },
    {
      id: "source-outline",
      type: "line",
      source: vector,
      "source-layer": "coverage",
      layout: { visibility: "none" },
      paint: { "line-color": coverageColor, "line-width": 1.5 },
    },
    {
      id: "source-labels",
      type: "symbol",
      source: vector,
      "source-layer": "coverage",
      layout: {
        visibility: "none",
        "text-field": ["get", "source_name"],
        "text-size": 11,
        "text-font": flavor.font,
      },
      paint: {
        "text-color": coverageColor,
        "text-halo-color": "#fff",
        "text-halo-width": 1.2,
      },
    },
  ];
}

// ─── Whole style ─────────────────────────────────────────────────────────────
// A complete, drop-in StyleSpecification: OSM raster base (osm: false for
// layers-only over your own basemap) + the bathymetry sources and layers.
// `unit`/`safety` bake mariner defaults into both the depth ramp and the
// style's global-state defaults, so labels and tint always agree.
export function style({
  tilesBase,
  flavor = day,
  glyphs = DEFAULT_GLYPHS,
  osm = true,
  unit = state.unit.default,
  safety = state.safety.default,
}: {
  tilesBase: string;
  flavor?: Flavor;
  glyphs?: string;
  osm?: boolean;
  unit?: Unit;
  safety?: number;
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
    state: { unit: { default: unit }, safety: { default: safety } },
    sources: { ...osmSource, ...sources({ tilesBase }) },
    layers: [...osmBase, ...layers(flavor, { unit, safety })],
  };
}

// ─── Runtime helpers ──────────────────────────────────────────────────────────
// The style is not pure JSON: the depth ramp can't read global-state, and depth
// readout decodes Terrarium pixels. These live here so consumers don't re-derive
// either.

// The subset of maplibregl.Map that applyState touches (structural, so the
// package needs no maplibre-gl dependency).
export interface ChartMap {
  setGlobalStateProperty(name: string, value: unknown): unknown;
  getGlobalState(): Record<string, unknown>;
  setPaintProperty(layerId: string, name: string, value: unknown): unknown;
  getLayer(id: string): unknown;
}

// Change the mariner settings on a live map. Flipping the `unit` / `safety`
// global-state re-evaluates every label, filter, and sounding-emphasis
// expression — but the depth-shading ramp's interpolate stops must be literals,
// so it is rebuilt here with the SAME values. This pairing is the whole point:
// setting the state without repainting the ramp (or vice versa) desyncs tint
// from labels.
export function applyState(
  map: ChartMap,
  changes: { unit?: Unit; safety?: number },
  flavor: Flavor = day,
): void {
  if (changes.unit !== undefined)
    map.setGlobalStateProperty("unit", changes.unit);
  if (changes.safety !== undefined)
    map.setGlobalStateProperty("safety", changes.safety);
  const current = {
    unit: state.unit.default,
    safety: state.safety.default,
    ...map.getGlobalState(),
    ...changes,
  } as { unit: Unit; safety: number };
  if (map.getLayer("depth-shading"))
    map.setPaintProperty(
      "depth-shading",
      "color-relief-color",
      depthRelief(flavor, current),
    );
}

// Chart-datum elevation at a point, in metres — negative below datum (depth),
// positive above (land / drying foreshore); null if the tile is unavailable.
// Decodes the Terrarium DEM tile pixel directly, at native resolution — unlike
// queryTerrainElevation, which needs 3D terrain enabled and samples the coarse
// terrain mesh (it reads land over deep water near the coast). Browser-only
// (createImageBitmap / OffscreenCanvas).
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
