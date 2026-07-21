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
 * Runtime parameters: `unit` ("m" | "ft" | "fm") and `safety` (metres, 0 = off)
 * appear in the generated expressions as literals — every label, isobath
 * filter, sounding emphasis, and the depth-shading ramp. Literal expressions
 * run on any GL JS with color-relief (MapLibre >= 5.6). To change them on a
 * live map, applyState() re-derives the handful of unit/safety-dependent
 * properties and sets them in place; or fetch a regenerated style
 * (`/style.json?unit=ft&safety=3`). `shading` ("relief" | "bands") picks the
 * water shading: the raster ramp, or the vector ENC depth-area bands above z6
 * (the relief keeps z<6 either way).
 */
import type {
  ExpressionSpecification,
  LayerSpecification,
  SourceSpecification,
  StyleSpecification,
} from "@maplibre/maplibre-gl-style-spec";

export type Unit = "m" | "ft" | "fm";
// Water shading: the raster color-relief ramp (continuous, fuzzy edges) or the
// vector ENC depth-area bands (crisp edges on the charted isobaths, safety
// snapped to the next-deeper charted level). Never both at once — the 0.85
// opacities would compound.
export type Shading = "relief" | "bands";

export interface Flavor {
  bandColors: string[]; // deepest → shoalest (6 entries)
  bandEdges: { m: number[]; ft: number[] }; // band-edge depths, metres
  hazard: string;
  land: string;
  drying: string;
  nodata: string;
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
  // Unknown-depth water (ENC DEPARE nodata): mapped water we hold no depth for. A flat,
  // desaturated slate-blue — reads as water but distinct from the crisp surveyed depth
  // bands, and deliberately provisional (S-52's NODATA fill is the eventual reference).
  nodata: "#a9bccb",
  contour: "#4a7a9c",
  label: "#036",
  labelHalo: "#fff",
  font: ["Noto Sans Regular"],
  hillshadeShadow: "#9adcfe",
  coverage: "#f58231",
};

// Mariner-setting defaults for layers()/style() when the caller omits them.
const DEFAULT_UNIT: Unit = "m";
const DEFAULT_SAFETY = 2;
const DEFAULT_SHADING: Shading = "relief";

// The depare layer's data floor (tippecanoe -Z in the pipeline) and the contour
// lines' presentation floor — depth shading carries lower zooms.
const BANDS_MIN_ZOOM = 6;

const DEFAULT_GLYPHS =
  "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf";

// ─── Depth-shading ramp (elevation → colour) ─────────────────────────────────
// The hazard tint folds into this one color-relief ramp: two color-relief
// layers on one DEM source don't composite (only the first renders), so water
// shallower than the safety depth is painted into the ramp itself.
type RampStops = (number | string)[]; // alternating elevation, colour

// Terrarium's vertical LSB. The encoder clamps water to <= -LSB; the non-negative domain is
// categorical (0 unknown-depth water, 1 drying foreshore, 2 land — flat codes, exact at every
// zoom): the shoal tint runs flat to -LSB, then the code tints; fractional values between codes
// are overzoom/interpolation transitions and blend smoothly.
const LSB = 1 / 256;
const DRYING_CODE = 1;
const LAND_CODE = 2;

// Width (metres) of the normal→hazard colour transition at the safety depth.
const EDGE = 0.01;

const depthRamp = (flavor: Flavor, edges: number[]): RampStops => {
  const stops: RampStops = [-10000, flavor.bandColors[0]];
  edges.forEach((d, i) =>
    stops.push(-d - 0.1, flavor.bandColors[i], -d, flavor.bandColors[i + 1]),
  );
  stops.push(-LSB, flavor.bandColors[5], 0, flavor.nodata,
             DRYING_CODE, flavor.drying, LAND_CODE, flavor.land);
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
// Unit/safety changes rebuild this and apply it with
// setPaintProperty("depth-shading", ...) — applyState() does exactly that.
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
      flavor.nodata,
      DRYING_CODE,
      flavor.drying,
      LAND_CODE,
      flavor.land,
    );
  return [
    "interpolate",
    ["linear"],
    ["elevation"],
    ...stops,
  ] as unknown as ExpressionSpecification;
}

// ─── Depth-area (ENC DEPARE) band colour ─────────────────────────────────────
// Charted isobath ladders, positive-down metres — must mirror pipelines/config.py
// DEPARE_LEVELS / DEPARE_LEVELS_FT (the bucket edges baked into the depare
// layer). The safety contour snaps UP this ladder to the next-deeper rung
// (ECDIS behaviour, bias shallow) — vector bands can only flip at charted
// levels, unlike the raster ramp's continuous crisp edge.
const DEPARE_LADDER_M = [
  2, 5, 10, 20, 30, 50, 100, 200, 300, 500, 1000, 2000, 3000, 4000, 5000, 6000,
  8000, 10000,
];
const DEPARE_LADDER_FT = [
  1, 2, 3, 5, 10, 20, 30, 50, 100, 200, 300, 500, 1000, 2000, 3000, 5000,
].map((fm) => fm * 1.8288);

// Comparisons against drval1 subtract this: tiles may carry the fathom-curve
// drvals (1.8288, 5.4864, …) as 32-bit floats, which can land a hair below the
// exact edge value. Ladder rungs are ≥ ~1.8 m apart, so 0.01 m is safely
// inside every gap.
const DRVAL_EPS = 0.01;

// Fill colour for the depare partitions: the band tint keyed off drval1 (the
// band's shallow bound), with every band shallower than the snapped safety
// contour painted the hazard tint. Literals only, like depthRelief — runtime
// changes go through applyState().
export function depthAreasColor(
  flavor: Flavor = day,
  { unit = "m", safety = 0 }: { unit?: Unit; safety?: number } = {},
): ExpressionSpecification {
  const metric = unit === "m";
  const edges = [...flavor.bandEdges[metric ? "m" : "ft"]].reverse(); // shoalest → deepest
  const step: unknown[] = ["step", ["get", "drval1"], flavor.bandColors[5]];
  edges.forEach((d, i) => step.push(d - DRVAL_EPS, flavor.bandColors[4 - i]));
  if (!(safety > 0)) return step as unknown as ExpressionSpecification;
  const ladder = metric ? DEPARE_LADDER_M : DEPARE_LADDER_FT;
  const snap =
    ladder.find((l) => l >= safety - DRVAL_EPS) ?? ladder[ladder.length - 1];
  return [
    "case",
    ["<", ["get", "drval1"], snap - DRVAL_EPS],
    flavor.hazard,
    step,
  ] as unknown as ExpressionSpecification;
}

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
export function layers(
  flavor: Flavor = day,
  {
    dem = "seascape-dem",
    vector = "seascape-vector",
    coverage = "seascape-coverage",
    unit = DEFAULT_UNIT,
    safety = DEFAULT_SAFETY,
    shading = DEFAULT_SHADING,
  }: {
    dem?: string;
    vector?: string;
    coverage?: string;
    unit?: Unit;
    safety?: number;
    shading?: Shading;
  } = {},
): LayerSpecification[] {
  // `unit` picks every sounding/contour label and which isobath set shows;
  // `safety` sets the unsafe-sounding emphasis. Everything is a literal, so
  // runtime changes go through applyState(), which regenerates these.
  //
  // Soundings: props depth_m / depth_ft / depth_fm, all floored toward
  // shallower; depth_m already carries one decimal in the shoal band (<6 m)
  // and an integer deeper, so metres just print it.
  const soundingText: ExpressionSpecification = [
    "to-string",
    [
      "get",
      unit === "ft" ? "depth_ft" : unit === "fm" ? "depth_fm" : "depth_m",
    ],
  ];
  // Contours: metre isobaths (sys != "ft", also legacy no-sys tiles) vs the
  // fathom-curve set (sys == "ft"), which labels as feet or fathoms. Both
  // systems label every curve — the standard isobath sets are sparse enough
  // that GL collision thins the labels.
  const contourLineFilter: ExpressionSpecification =
    unit === "m" ? ["!=", ["get", "sys"], "ft"] : ["==", ["get", "sys"], "ft"];
  const contourLabelText: ExpressionSpecification = [
    "concat",
    [
      "to-string",
      [
        "get",
        unit === "ft" ? "depth_ft" : unit === "fm" ? "depth_fm" : "depth_abs_m",
      ],
    ],
    unit,
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

  // The depare layer carries three ENC DEPARE feature kinds in one fill, keyed by attribute
  // presence: depth bands (drval1/drval2, sys-tagged per m/ft ladder, drval1 >= 0), drying
  // foreshore (drval1 < 0, no sys), and unknown-depth water (no drval1, no sys). Bands
  // duplicate per sys, so only the active ladder shows, and only in bands mode; drying +
  // nodata have NO sys and ship once, so `!has sys` selects them and they render in BOTH
  // shading modes — relief filters the bands out (the raster ramp carries depth; two 0.85
  // fills would compound) but keeps the honesty fills, since a #24-cleared lake must read as
  // unknown water — the render now tints 0-fill as unknown water too, and the depare polygon
  // keeps that categorical (and adds the drying tint).
  const bandSys = unit === "m" ? "m" : "ft";
  const depareFilter = (
    shading === "bands"
      ? ["any", ["!", ["has", "sys"]], ["==", ["get", "sys"], bandSys]]
      : ["!", ["has", "sys"]]
  ) as unknown as ExpressionSpecification;
  // Fill: nodata (no drval1) → provisional flat tint; drying (drval1 < 0) → foreshore green;
  // else the band ramp keyed off drval1. `case` short-circuits, so the drval1 comparison only
  // runs once the no-drval1 branch has ruled nodata out.
  const depareColor = [
    "case",
    ["!", ["has", "drval1"]],
    flavor.nodata,
    ["<", ["get", "drval1"], 0],
    flavor.drying,
    depthAreasColor(flavor, { unit, safety }),
  ] as unknown as ExpressionSpecification;
  // nodata carries the provisional lighter wash it had as its own layer; bands + drying stay
  // at the depth-fill opacity.
  const depareOpacity = [
    "case",
    ["!", ["has", "drval1"]],
    0.55,
    0.85,
  ] as unknown as ExpressionSpecification;

  return [
    {
      id: "depth-shading",
      type: "color-relief",
      source: dem,
      // Bands mode: the vector depth areas take over at their z6 data floor;
      // the relief carries lower zooms (same palette, so the handoff is a
      // sharpness change, not a colour change).
      ...(shading === "bands" ? { maxzoom: BANDS_MIN_ZOOM } : {}),
      paint: {
        "color-relief-color": depthRelief(flavor, { unit, safety }),
        "color-relief-opacity": 0.85,
      },
    },
    {
      // ENC DEPARE fill — the vector twin of depth-shading, carrying all three depare
      // feature kinds keyed by attribute presence (see the expressions above): depth bands
      // (crisp tint per drval1, safety recolour snapped to the next-deeper charted level),
      // drying foreshore (INT-1 green, negative drval1), and unknown-depth water (provisional
      // flat tint, no drval1). The three are disjoint by construction, so `fill-sort-key: rank`
      // is only a stable tie-breaker at an incidental simplification-wobble edge — nodata under
      // bands (real depth wins), drying over the shoal band it abuts along their shared 0 m seam.
      // Bands are filtered out in relief mode (the raster ramp carries depth); drying
      // + nodata stay in both modes. Where no drying/nodata polygon covers a >=0 pixel the DEM
      // ramp still paints the land wash, so a mask miss degrades to plain land, never to
      // water-over-land.
      id: "depth-areas",
      type: "fill",
      source: vector,
      "source-layer": "depare",
      filter: depareFilter,
      minzoom: BANDS_MIN_ZOOM,
      layout: { "fill-sort-key": ["get", "rank"] },
      paint: {
        "fill-color": depareColor,
        "fill-opacity": depareOpacity,
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
        "text-color":
          safety > 0
            ? ["case", ["<=", ["get", "depth_m"], safety], "#000", flavor.label]
            : flavor.label,
        "text-halo-color": flavor.labelHalo,
        "text-halo-width": 1,
      },
    },
    // Source coverage (provenance): footprint polygons with props source_id /
    // source_name / source_maxzoom, from the standalone coverage tileset.
    // Hidden by default; a viewer can toggle them on for click-to-identify.
    {
      id: "source-fill",
      type: "fill",
      source: coverage,
      "source-layer": "coverage",
      layout: { visibility: "none" },
      paint: { "fill-color": coverageColor, "fill-opacity": 0.12 },
    },
    {
      // Brightened fill of one source (filter set by the consumer on click).
      id: "source-highlight",
      type: "fill",
      source: coverage,
      "source-layer": "coverage",
      filter: ["==", ["get", "source_id"], "__none__"],
      layout: { visibility: "none" },
      paint: { "fill-color": coverageColor, "fill-opacity": 0.4 },
    },
    {
      id: "source-outline",
      type: "line",
      source: coverage,
      "source-layer": "coverage",
      layout: { visibility: "none" },
      paint: { "line-color": coverageColor, "line-width": 1.5 },
    },
    {
      id: "source-labels",
      type: "symbol",
      source: coverage,
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
}: {
  tilesBase: string;
  flavor?: Flavor;
  glyphs?: string;
  osm?: boolean;
  unit?: Unit;
  safety?: number;
  shading?: Shading;
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
    layers: [...osmBase, ...layers(flavor, { unit, safety, shading })],
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

// Change the mariner settings on a live map: re-derive the unit/safety-
// dependent layer properties (depth ramp, isobath filters, label text, unsafe-
// sounding emphasis) and set them in place — the in-place equivalent of
// reloading a regenerated style. Takes the full settings each call: nothing
// map-side stores the previous values, so callers pass what their controls
// currently show. `shading` also gates the depare fill's filter (relief hides
// the depth bands but keeps drying/nodata), so pass the current mode — it
// defaults to the relief mode when omitted. Layers absent from the map
// (composed subsets) are skipped.
export function applyState(
  map: ChartMap,
  {
    unit,
    safety,
    shading = DEFAULT_SHADING,
  }: { unit: Unit; safety: number; shading?: Shading },
  flavor: Flavor = day,
): void {
  const spec = Object.fromEntries(
    layers(flavor, { unit, safety, shading }).map((l) => [l.id, l]),
  ) as Record<string, { filter?: unknown; layout?: any; paint?: any }>;
  if (map.getLayer("depth-shading"))
    map.setPaintProperty(
      "depth-shading",
      "color-relief-color",
      spec["depth-shading"].paint["color-relief-color"],
    );
  if (map.getLayer("depth-areas")) {
    map.setFilter("depth-areas", spec["depth-areas"].filter);
    map.setPaintProperty(
      "depth-areas",
      "fill-color",
      spec["depth-areas"].paint["fill-color"],
    );
  }
  if (map.getLayer("contour-lines"))
    map.setFilter("contour-lines", spec["contour-lines"].filter);
  if (map.getLayer("contour-labels")) {
    map.setFilter("contour-labels", spec["contour-labels"].filter);
    map.setLayoutProperty(
      "contour-labels",
      "text-field",
      spec["contour-labels"].layout["text-field"],
    );
  }
  if (map.getLayer("soundings")) {
    map.setLayoutProperty(
      "soundings",
      "text-field",
      spec["soundings"].layout["text-field"],
    );
    map.setPaintProperty(
      "soundings",
      "text-color",
      spec["soundings"].paint["text-color"],
    );
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
