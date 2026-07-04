import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";

// ─── Tile sources ───────────────────────────────────────────────────────
// The serving Worker (worker/) presents one unified XYZ endpoint per layer:
//   {base}/{z}/{x}/{y}.webp   — Terrarium terrain raster (planet + overlays, overzoomed)
//   {base}/{z}/{x}/{y}.pbf    — MVT vector (contours, more layers later)
// VITE_TILES_BASE is the full bathymetry endpoint base — it includes the
// /seascape route prefix in prod (e.g. https://tiles.openwaters.io/seascape);
// the dev default points at the worker root. VITE_BBOX sets the initial view.
const BBOX = import.meta.env.VITE_BBOX
  ? import.meta.env.VITE_BBOX.split(",").map(Number)
  : [-180, -85, 180, 85];
const tilesBase = (
  import.meta.env.VITE_TILES_BASE || "http://localhost:8787"
).replace(/\/$/, "");
const terrainTiles = `${tilesBase}/{z}/{x}/{y}.webp`;
const contourTiles = `${tilesBase}/{z}/{x}/{y}.pbf`;
const MAX_ZOOM = 13; // deepest source; the Worker overzooms the base for the rest

// The Worker overzooms the raster terrain server-side up to MAX_ZOOM, but vector
// contours are a plain passthrough — so tell MapLibre their true max zoom (the
// deepest overlay cell in the manifest) and it overzooms them client-side above that.
const manifest = await fetch(`${tilesBase}/manifest.json`)
  .then((r) => r.json())
  .catch(() => null);
const contourMax = manifest
  ? Math.max(
      manifest.planet.max_zoom,
      ...Object.values(manifest.overlay?.cells ?? {}),
    )
  : MAX_ZOOM;

// ─── Source coverage (provenance) ───────────────────────────────────────────
// The `coverage` layer baked into contours.pmtiles (source footprints, props
// source_id / source_name / source_maxzoom). Drawn as polygons and queried on click to
// report which source a depth came from. Colour each source by id off the manifest's
// source_ids (a match expression); ids not in the manifest fall back to grey.
const COVERAGE_PALETTE = [
  "#e6194b",
  "#3cb44b",
  "#f032e6",
  "#4363d8",
  "#f58231",
  "#911eb4",
  "#008080",
  "#9a6324",
  "#800000",
  "#000075",
];
const coverageColor =
  manifest && (manifest.source_ids ?? []).length
    ? [
        "match",
        ["get", "source_id"],
        ...manifest.source_ids.flatMap((id, i) => [
          id,
          COVERAGE_PALETTE[i % COVERAGE_PALETTE.length],
        ]),
        "#888",
      ]
    : "#f58231";

// ─── Unit-aware chart expressions (driven by the `unit` global state) ─────────
// One global-state variable — `unit` ("m" | "ft" | "fm") — drives every sounding/contour
// label and which isobaths show. The Units control flips it with a single
// setGlobalStateProperty call; MapLibre re-evaluates the expressions below (filters included),
// so there's no per-layer setFilter/setLayoutProperty on change.
//
// Soundings (`soundings` layer): props depth_m / depth_ft / depth_fm, all floored toward
// shallower; depth_m already carries one decimal in the shoal band (<6 m) and an integer deeper,
// so metres just print it. symbol-sort-key = depth_m places the shoalest first, so GL collision
// keeps the most dangerous sounding when labels clash.
const UNIT = ["global-state", "unit"];
const soundingText = [
  "case",
  ["==", UNIT, "ft"],
  ["to-string", ["get", "depth_ft"]],
  ["==", UNIT, "fm"],
  ["to-string", ["get", "depth_fm"]],
  ["to-string", ["get", "depth_m"]], // metres (default; also covers unit unset)
];
// Contours: metre isobaths (sys != "ft", also legacy no-sys tiles) vs the fathom-curve set
// (sys == "ft"), which labels as feet or fathoms. Both systems label every curve — the
// standard isobath sets are sparse enough that GL collision thins the labels.
const IS_FEET = ["any", ["==", UNIT, "ft"], ["==", UNIT, "fm"]]; // metres is the fallback (unit unset)
const contourLineFilter = [
  "case",
  IS_FEET,
  ["==", ["get", "sys"], "ft"],
  ["!=", ["get", "sys"], "ft"],
];
const contourLabelText = [
  "case",
  ["==", UNIT, "ft"],
  ["concat", ["to-string", ["get", "depth_ft"]], "ft"],
  ["==", UNIT, "fm"],
  ["concat", ["to-string", ["get", "depth_fm"]], "fm"],
  ["concat", ["to-string", ["get", "depth_abs_m"]], "m"],
];

// Shared label styling so soundings and contour labels read as one chart (same font, size,
// colour, halo). Soundings at/shoaler than the safety depth print black (S-52).
const LABEL_FONT = ["Open Sans Regular"];
const LABEL_SIZE = ["interpolate", ["linear"], ["zoom"], 8, 9, 13, 12];
const LABEL_COLOR = "#036";
const LABEL_HALO = "#fff";

// Depth-shading colour ramp (elevation → colour), chart-convention tints: shoal-dark →
// deep-white (INT/NOAA), flat white beyond the deepest edge so tint stays monotonic in depth.
// Bands are perceptually spaced (adjacent ΔE ≥ 7 after 0.85-opacity compositing), weighted
// toward the shoal bands where depth discrimination matters. Band edges sit on isobaths the
// chart draws — metric levels, or the classic fathom curves in ft/fm mode — so tint boundaries
// land on contour lines rather than between them (paper-chart practice). The hazard tint folds
// into THIS one color-relief ramp: two color-relief layers on one DEM source don't composite
// (only the first renders), so water shallower than the safety depth is painted into the ramp
// itself as HAZARD_COLOR. Rebuilt in JS on safety or unit change — an interpolate stop can't
// be a live global-state value, so unlike the unit/safety filters this one uses setPaintProperty.
const BAND_COLORS = [
  "#e9f7ff", // deepest band
  "#c9e9fd",
  "#a5d9fb",
  "#7fc7f8",
  "#5db5f0",
  "#3fa2e4", // shoalest band
];
const BAND_EDGES = {
  m: [50, 20, 10, 5, 2],
  ft: [30, 10, 5, 3, 1].map((fm) => fm * 1.8288), // fathom curves 30/10/5/3/1 fm
};
// Land above datum: translucent buff wash (paper-chart figure-ground — white stays
// unambiguously "deep water"); OSM streets/names read through it.
const LAND_COLOR = "rgba(247,240,221,0.66)";
const depthRamp = (edges) => {
  const stops = [-10000, BAND_COLORS[0]];
  edges.forEach((d, i) =>
    stops.push(-d - 0.1, BAND_COLORS[i], -d, BAND_COLORS[i + 1]),
  );
  stops.push(-0.01, BAND_COLORS[5], 0, LAND_COLOR);
  return stops;
};
let DEPTH_RAMP = depthRamp(BAND_EDGES.m); // swapped by applyUnit on unit change
const HAZARD_COLOR = "#1f86cb"; // one perceptual step darker than the shoalest band
// Colour of the depth ramp at elevation e (linear-interpolated). Used to pin a normal-coloured
// stop right at the safety depth so the flip to HAZARD_COLOR is a crisp ~0.01 m edge at ANY safety
// value — otherwise the blend feathers by however far −safety lands from the nearest ramp stop.
const rampColorAt = (e) => {
  for (let i = 2; i < DEPTH_RAMP.length; i += 2)
    if (e <= DEPTH_RAMP[i]) {
      const e0 = DEPTH_RAMP[i - 2],
        c0 = DEPTH_RAMP[i - 1];
      const e1 = DEPTH_RAMP[i],
        c1 = DEPTH_RAMP[i + 1];
      if (c0[0] !== "#" || c1[0] !== "#") return c0;
      const t = (e - e0) / (e1 - e0);
      const p = (c) => [1, 3, 5].map((k) => parseInt(c.slice(k, k + 2), 16));
      const a = p(c0),
        b = p(c1);
      return `rgb(${a.map((v, k) => Math.round(v + t * (b[k] - v))).join(",")})`;
    }
  return DEPTH_RAMP[1];
};
const depthReliefColor = (safety) => {
  const s = -safety;
  const stops = [];
  for (let i = 0; i < DEPTH_RAMP.length; i += 2)
    if (!(safety > 0) || DEPTH_RAMP[i] < s)
      stops.push(DEPTH_RAMP[i], DEPTH_RAMP[i + 1]);
  // Crisp edge: normal colour pinned at the safety depth, hazard from just shallower up to shore.
  if (safety > 0)
    stops.push(
      s,
      rampColorAt(s),
      s + 0.01,
      HAZARD_COLOR,
      -0.01,
      HAZARD_COLOR,
      0,
      LAND_COLOR,
    );
  return ["interpolate", ["linear"], ["elevation"], ...stops];
};

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
      tiles: [terrainTiles],
      tileSize: 512,
      maxzoom: MAX_ZOOM, // Worker overzooms the z8 base for z>8 where no overlay exists
      encoding: "terrarium",
      attribution: "&copy; <a href='https://www.gebco.net'>GEBCO</a>",
    },
    contours: {
      type: "vector",
      tiles: [contourTiles],
      maxzoom: contourMax,
    },
  },
  layers: [
    {
      id: "osm-base",
      type: "raster",
      source: "osm",
      paint: { "raster-opacity": 1 },
    },
    {
      id: "depth-shading",
      type: "color-relief",
      source: "terrain-dem",
      paint: {
        // One color-relief ramp for both depth-shading and the hazard tint (see DEPTH_RAMP /
        // depthReliefColor); the JS override on load / safety-change folds in the hazard band.
        "color-relief-color": [
          "interpolate",
          ["linear"],
          ["elevation"],
          ...DEPTH_RAMP,
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
        "hillshade-exaggeration": 0.5,
        "hillshade-shadow-color": "#9adcfe",
        "hillshade-highlight-color": "#ffffff",
        "hillshade-illumination-direction": 315,
      },
    },
    {
      id: "contour-lines",
      type: "line",
      source: "contours",
      "source-layer": "contours",
      filter: contourLineFilter,
      paint: {
        "line-color": "#4a7a9c",
        "line-width": 0.5,
        "line-opacity": 0.6,
      },
    },
    {
      id: "contour-labels",
      type: "symbol",
      source: "contours",
      "source-layer": "contours",
      filter: contourLineFilter,
      minzoom: 8,
      layout: {
        "symbol-placement": "line",
        "text-field": contourLabelText,
        "text-size": LABEL_SIZE,
        "text-font": LABEL_FONT,
        "text-letter-spacing": 0.1,
        "text-max-angle": 30,
        "text-padding": 50,
      },
      paint: {
        "text-color": LABEL_COLOR,
        "text-halo-color": LABEL_HALO,
        "text-halo-width": 1,
      },
    },
    {
      id: "soundings",
      type: "symbol",
      source: "contours",
      "source-layer": "soundings",
      minzoom: 7,
      layout: {
        "text-field": soundingText,
        "text-font": LABEL_FONT,
        "text-size": LABEL_SIZE,
        "symbol-sort-key": ["get", "depth_m"], // shoalest first → wins collisions
        "text-padding": 8,
      },
      paint: {
        // Soundings at or shoaler than the safety depth print black — S-52 shows unsafe-water
        // soundings in black and lets the hazard tint carry the alarm; safety=0 → all normal.
        "text-color": [
          "case",
          [
            "all",
            [">", ["global-state", "safety"], 0],
            ["<=", ["get", "depth_m"], ["global-state", "safety"]],
          ],
          "#000",
          LABEL_COLOR,
        ],
        "text-halo-color": LABEL_HALO,
        "text-halo-width": 1,
      },
    },
    {
      id: "source-fill",
      type: "fill",
      source: "contours",
      "source-layer": "coverage",
      // Hidden by default (the toggle starts unchecked); turning it on enables click-to-identify.
      layout: { visibility: "none" },
      paint: { "fill-color": coverageColor, "fill-opacity": 0.12 },
    },
    {
      // Brightened fill of the source a click landed in; filter set on click.
      id: "source-highlight",
      type: "fill",
      source: "contours",
      "source-layer": "coverage",
      filter: ["==", ["get", "source_id"], "__none__"],
      layout: { visibility: "none" },
      paint: { "fill-color": coverageColor, "fill-opacity": 0.4 },
    },
    {
      id: "source-outline",
      type: "line",
      source: "contours",
      "source-layer": "coverage",
      layout: { visibility: "none" },
      paint: { "line-color": coverageColor, "line-width": 1.5 },
    },
    {
      id: "source-labels",
      type: "symbol",
      source: "contours",
      "source-layer": "coverage",
      layout: {
        visibility: "none",
        "text-field": ["get", "source_name"],
        "text-size": 11,
        "text-font": ["Open Sans Regular"],
      },
      paint: {
        "text-color": coverageColor,
        "text-halo-color": "#fff",
        "text-halo-width": 1.2,
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
  attributionControl: { compact: true },
});
window.map = map; // exposed for debugging / verification

map.addControl(new maplibregl.NavigationControl());

// No setTerrain(): enabling 3D terrain drapes the DEM layers (depth-shading, hillshade)
// over MapLibre's terrain mesh, which resamples the land+water DEM coarsely below native
// zoom. Near coasts the large positive land values bleed across the 0 m transparency
// cutoff, so narrow/shallow water (bays, sounds, shoals) renders transparent at z<8 —
// leaving only the deep open water, which then looks like coarse GEBCO. color-relief reads
// the tiles at native resolution instead. Click-depth reads the DEM tile directly
// (readElevation), so terrain isn't needed for it. Re-add behind a toggle for 3D seafloor.

// ─── Layer toggles ────────────────────────────────────────────────────────
const toggles = {
  "toggle-depth": ["depth-shading"],
  "toggle-hillshade": ["hillshade"],
  "toggle-contours": ["contour-lines"],
  "toggle-labels": ["contour-labels"],
  "toggle-soundings": ["soundings"],
  "toggle-sources": [
    "source-fill",
    "source-highlight",
    "source-outline",
    "source-labels",
  ],
};

map.on("load", () => {
  // Safety depth (metres, default 2): drives the hazard tint (via the depth-shading ramp) and
  // the red-sounding emphasis (via the `safety` state var). 0 = off.
  let safetyMetres = 2;
  const applySafety = () => {
    map.setGlobalStateProperty("safety", safetyMetres); // red-sounding emphasis
    // Hazard tint lives in the depth-shading ramp (one color-relief per source), rebuilt here.
    map.setPaintProperty(
      "depth-shading",
      "color-relief-color",
      depthReliefColor(safetyMetres),
    );
  };
  // Unit change swaps the ramp's band edges to the active system's isobaths and repaints (via
  // applySafety); every other unit-aware expression re-evaluates off the `unit` state variable.
  const applyUnit = (u) => {
    map.setGlobalStateProperty("unit", u);
    DEPTH_RAMP = depthRamp(BAND_EDGES[u === "m" ? "m" : "ft"]);
    applySafety();
  };
  const safetyInput = document.getElementById("safety-depth");
  if (safetyInput) safetyInput.value = safetyMetres;
  applyUnit(document.getElementById("unit-select")?.value || "m");
  safetyInput?.addEventListener("input", (e) => {
    safetyMetres = parseFloat(e.target.value) || 0;
    applySafety();
  });
  for (const [inputId, layerIds] of Object.entries(toggles)) {
    document.getElementById(inputId)?.addEventListener("change", (e) => {
      const vis = e.target.checked ? "visible" : "none";
      layerIds.forEach((id) => {
        if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", vis);
      });
    });
  }
  document
    .getElementById("unit-select")
    ?.addEventListener("change", (e) => applyUnit(e.target.value));
});

// ─── Click to inspect ─────────────────────────────────────────────────────
// Decode the elevation straight from the DEM tile pixel (Terrarium). This reads the tile
// at native resolution — unlike queryTerrainElevation, which needs 3D terrain enabled and
// samples the coarse terrain mesh (it reads land over deep water near the coast).
async function readElevation(lngLat) {
  const z = Math.min(Math.round(map.getZoom()), MAX_ZOOM);
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
  const cx = new OffscreenCanvas(512, 512).getContext("2d");
  cx.drawImage(bmp, 0, 0);
  const [r8, g8, b8] = cx.getImageData(px, py, 1, 1).data;
  return r8 * 256 + g8 + b8 / 256 - 32768; // Terrarium decode → metres
}

// Which source covers a clicked point: the deepest footprint wins (lex-first id on a
// tie), matching the build's merge rule, so this names the source the depth came from.
// undefined → coverage layer hidden (skip the line); null → no footprint here = GEBCO.
function sourceAt(point) {
  if (map.getLayoutProperty("source-fill", "visibility") === "none")
    return undefined;
  const hits = map.queryRenderedFeatures(point, { layers: ["source-fill"] });
  if (!hits.length) return null;
  return hits
    .map((f) => f.properties)
    .sort(
      (a, b) =>
        b.source_maxzoom - a.source_maxzoom ||
        (a.source_id < b.source_id ? -1 : 1),
    )[0];
}

map.on("click", async (e) => {
  const ele = await readElevation(e.lngLat);
  const src = sourceAt(e.point);
  if (map.getLayer("source-highlight"))
    map.setFilter("source-highlight", [
      "==",
      ["get", "source_id"],
      src?.source_id ?? "__none__",
    ]);

  const lines = [];
  if (ele != null) {
    const depth = Math.round(-ele);
    lines.push(
      `<strong>${
        ele <= 0
          ? `${depth}m (${Math.round(depth * 3.28084)}ft)`
          : `${Math.round(ele)}m elevation`
      }</strong>`,
    );
  }
  if (src !== undefined)
    lines.push(
      `<small>source: ${src ? src.source_name : "GEBCO (global)"}</small>`,
    );
  if (!lines.length) return;

  new maplibregl.Popup()
    .setLngLat(e.lngLat)
    .setHTML(lines.join("<br>"))
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
