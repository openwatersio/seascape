/**
 * Relief view — a shaded-relief picture of the planet, not a chart: no text,
 * no unit or safety settings. Water depth comes from the seascape DEM; land
 * height from Mapterhorn (mapterhorn.com), since the seascape DEM carries land
 * only as a flat code. Water draws over land, so the seascape land code is the
 * coastline, including land below sea level.
 *
 * Layer ids stay distinct from the chart's so applyState(), which keys off
 * chart layer ids, never repaints them.
 */
import type {
  LayerSpecification,
  SourceSpecification,
} from "@maplibre/maplibre-gl-style-spec";
import { LAND_CODE, LSB } from "./depth-shading.js";

export const MAPTERHORN = "mapterhorn";

export const mapterhornSource: SourceSpecification = {
  type: "raster-dem",
  url: "https://tiles.mapterhorn.com/tilejson.json",
  tileSize: 512,
  // MapLibre doesn't read `encoding` from TileJSON — it must be inline.
  encoding: "terrarium",
};

// Elevation (m) → colour, alternating. Land below sea level clamps to the
// lowland green.
const LAND_RAMP = [
  0,
  "#a6c48a",
  200,
  "#c9d79b",
  500,
  "#e6dfa8",
  1000,
  "#d8b77e",
  2000,
  "#b88a5c",
  3000,
  "#9c7a68",
  4500,
  "#e8e4e0",
  6000,
  "#ffffff",
];

// Continuous deep-navy → pale-cyan bathymetric tint. The non-negative codes
// (0 unknown-depth water, 1 drying) keep the shoal colour; land is transparent
// so Mapterhorn shows through.
const WATER_RAMP = [
  -11000,
  "#0b1f3a",
  -6000,
  "#12325c",
  -4000,
  "#1b4a80",
  -2000,
  "#2a69a3",
  -200,
  "#4f93c6",
  -50,
  "#7db7dd",
  -10,
  "#a9d4ee",
  -LSB,
  "#c9e6f5",
  LAND_CODE - LSB,
  "#c9e6f5",
  LAND_CODE,
  "rgba(0,0,0,0)",
];

const relief = (stops: (number | string)[]) => [
  "interpolate",
  ["linear"],
  ["elevation"],
  ...stops,
];

export function reliefLayers({
  dem = "seascape-dem",
  land = MAPTERHORN,
}: { dem?: string; land?: string } = {}): LayerSpecification[] {
  return [
    {
      id: "relief-land",
      type: "color-relief",
      source: land,
      paint: { "color-relief-color": relief(LAND_RAMP) },
    },
    {
      id: "relief-land-hillshade",
      type: "hillshade",
      source: land,
      paint: {
        "hillshade-method": "igor",
        "hillshade-exaggeration": 0.5,
        "hillshade-shadow-color": "#473b24",
        "hillshade-highlight-color": "rgb(255,255,228)",
      },
    },
    {
      id: "relief-water",
      type: "color-relief",
      source: dem,
      paint: { "color-relief-color": relief(WATER_RAMP) },
    },
    {
      id: "relief-water-hillshade",
      type: "hillshade",
      source: dem,
      paint: {
        "hillshade-method": "igor",
        "hillshade-exaggeration": 0.6,
        "hillshade-shadow-color": "#0a2240",
        "hillshade-highlight-color": "#ffffff",
      },
    },
  ] as unknown as LayerSpecification[];
}

// Hillshade view: grey igor shading of the seafloor on white, after
// Mapterhorn's own example. Land, the flat DEM code, gets a mid-grey fill so
// the coastline reads; drying foreshore stays with the water.
export function hillshadeLayers({
  dem = "seascape-dem",
}: { dem?: string } = {}): LayerSpecification[] {
  return [
    {
      id: "hillshade-background",
      type: "background",
      paint: { "background-color": "#ffffff" },
    },
    {
      id: "hillshade-land",
      type: "color-relief",
      source: dem,
      paint: {
        "color-relief-color": relief([
          LAND_CODE - LSB,
          "rgba(0,0,0,0)",
          LAND_CODE,
          "#9a9a9a",
        ]),
        // Crisp coastline; linear resampling feathers the code edge.
        resampling: "nearest",
      },
    },
    {
      id: "hillshade-water",
      type: "hillshade",
      source: dem,
      paint: { "hillshade-method": "igor" },
    },
  ] as unknown as LayerSpecification[];
}
