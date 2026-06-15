import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { Protocol } from "pmtiles";

// ─── PMTiles protocol ─────────────────────────────────────────────────────
const protocol = new Protocol();
maplibregl.addProtocol("pmtiles", protocol.tile);

// ─── Tile sources ───────────────────────────────────────────────────────
// BBOX and TILES_BASE are injected at build time via vite.config.js.
// eslint-disable-next-line no-undef
const BBOX = __BBOX__ ? __BBOX__.split(",").map(Number) : [-180, -85, 180, 85];
const suffix = __BBOX__ ? `_${__BBOX__}` : "";
// eslint-disable-next-line no-undef
const tilesBase = __TILES_BASE__ || location.origin;
const terrainUrl = `pmtiles://${tilesBase}/terrain${suffix}.pmtiles`;
const contourUrl = `pmtiles://${tilesBase}/contours${suffix}.pmtiles`;

// ─── Map style ────────────────────────────────────────────────────────────
const style = {
  version: 8,
  name: "GEBCO Bathymetry",
  glyphs: "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf",
  sources: {
    osm: {
      type: "raster",
      tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
      tileSize: 256,
      attribution:
        "&copy; <a href='https://www.openstreetmap.org/copyright'>OpenStreetMap</a>",
    },
    "terrain-dem": {
      type: "raster-dem",
      url: terrainUrl,
      tileSize: 512,
      // maxzoom auto-detected from the PMTiles header so regional high-res bands render where present
      encoding: "mapbox",
      bounds: BBOX,
      attribution: "&copy; <a href='https://www.gebco.net'>GEBCO</a>",
    },
    contours: {
      type: "vector",
      url: contourUrl,
      bounds: BBOX,
    },
  },
  layers: [
    {
      id: "osm-base",
      type: "raster",
      source: "osm",
      paint: { "raster-opacity": 0.3 },
    },
    {
      id: "depth-shading",
      type: "color-relief",
      source: "terrain-dem",
      paint: {
        "color-relief-color": [
          "interpolate",
          ["exponential", 0.8],
          ["elevation"],
          // Deep ocean — very subtle, just for context
          -11000,
          "#F0F4F8",
          -6000,
          "#E4EBF2",
          -2000,
          "#D4DFEA",
          -200,
          "#C0D0E0", // continental shelf edge
          // Continental shelf
          -50,
          "#90ABC8",
          -20,
          "#7899BC", // ECDIS deep contour
          -15,
          "#6A8DB4", // Panamax draft + UKC
          // Critical navigation zone
          -10,
          "#5B80AC",
          -5,
          "#4A72A2", // ECDIS safety contour
          -3,
          "#3A6498", // recreational draft limit
          -2,
          "#2C5690",
          -1,
          "#1E4888",
          0,
          "#133C80", // chart datum
          // Land — transparent so base map shows through
          0.001,
          "rgba(0, 0, 0, 0)",
        ],
        "color-relief-opacity": 0.85,
      },
    },
    {
      id: "hillshade",
      type: "hillshade",
      source: "terrain-dem",
      layout: { visibility: "none" },
      paint: {
        "hillshade-exaggeration": 0.6,
        "hillshade-shadow-color": "#000022",
        "hillshade-highlight-color": "#ffffff",
        "hillshade-illumination-direction": 315,
      },
    },
    {
      id: "contour-lines",
      type: "line",
      source: "contours",
      "source-layer": "contours",
      paint: {
        "line-color": "rgba(0, 20, 60, 0.2)",
        "line-width": [
          "step",
          ["to-number", ["get", "depth_abs_m"]],
          0.6, // < 50m
          50,
          0.8,
          200,
          1.0,
          1000,
          1.2,
        ],
      },
    },
    {
      id: "contour-labels",
      type: "symbol",
      source: "contours",
      "source-layer": "contours",
      filter: ["==", ["%", ["to-number", ["get", "depth_abs_m"]], 10], 0],
      minzoom: 8,
      layout: {
        "symbol-placement": "line",
        "text-field": ["concat", ["to-string", ["get", "depth_abs_m"]], "m"],
        "text-size": 11,
        "text-font": ["Open Sans Regular"],
        "text-max-angle": 30,
        "text-padding": 50,
      },
      paint: {
        "text-color": "rgba(0, 20, 60, 0.8)",
        "text-halo-color": "rgba(255, 255, 255, 0.6)",
        "text-halo-width": 1.5,
      },
    },
  ],
};

// ─── Create map ───────────────────────────────────────────────────────────
const map = new maplibregl.Map({
  container: "map",
  style,
  bounds: BBOX,
  hash: true,
});

map.addControl(new maplibregl.NavigationControl());

// Enable terrain so queryTerrainElevation() can read from the DEM.
// exaggeration: 0 keeps the map visually flat.
map.on("load", () => {
  map.setTerrain({ source: "terrain-dem", exaggeration: 0.0001 });
});

// ─── Layer toggles ────────────────────────────────────────────────────────
const toggles = {
  "toggle-depth": ["depth-shading"],
  "toggle-hillshade": ["hillshade"],
  "toggle-contours": ["contour-lines"],
  "toggle-labels": ["contour-labels"],
};

map.on("load", () => {
  for (const [inputId, layerIds] of Object.entries(toggles)) {
    document.getElementById(inputId).addEventListener("change", (e) => {
      const vis = e.target.checked ? "visible" : "none";
      layerIds.forEach((id) => map.setLayoutProperty(id, "visibility", vis));
    });
  }
});

// ─── Click to inspect ─────────────────────────────────────────────────────
map.on("click", (e) => {
  // Read elevation from terrain-RGB DEM tiles
  const eleRaw = map.queryTerrainElevation(e.lngLat);
  if (eleRaw == null) return;

  // queryTerrainElevation returns elevation * exaggeration
  const exaggeration = map.getTerrain()?.exaggeration || 1;
  const ele = eleRaw / exaggeration;
  const depth = Math.round(-ele);
  const depthFt = Math.round(depth * 3.28084);
  const label =
    ele <= 0 ? `${depth}m (${depthFt}ft)` : `${Math.round(ele)}m elevation`;

  new maplibregl.Popup()
    .setLngLat(e.lngLat)
    .setHTML(`<strong>${label}</strong>`)
    .addTo(map);
});

map.on(
  "mouseenter",
  "contour-lines",
  () => (map.getCanvas().style.cursor = "pointer"),
);
map.on(
  "mouseleave",
  "contour-lines",
  () => (map.getCanvas().style.cursor = ""),
);
