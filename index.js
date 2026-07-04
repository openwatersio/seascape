import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { applyState, readDepth, style } from "@openwaters/seascape";

// The style itself (sources, layers, depth ramp, unit/safety expressions) lives
// in the @openwaters/seascape package (style/) — this file is the demo app:
// endpoint config, manifest fetch, UI controls, click-to-inspect.
//
// VITE_TILES_BASE is the full bathymetry endpoint base — it includes the
// /seascape route prefix in prod (e.g. https://tiles.openwaters.io/seascape);
// the dev default points at the worker root. VITE_BBOX sets the initial view.
const BBOX = import.meta.env.VITE_BBOX
  ? import.meta.env.VITE_BBOX.split(",").map(Number)
  : [-180, -85, 180, 85];
const tilesBase = (
  import.meta.env.VITE_TILES_BASE || "http://localhost:8787"
).replace(/\/$/, "");
const MAX_ZOOM = 13; // deepest zoom readDepth fetches (the Worker overzooms past it)

// ─── Create map ───────────────────────────────────────────────────────────
// The style is self-contained: zooms/bounds/attribution come from the
// endpoint's TileJSON, so there's nothing to fetch before creating the map.
const map = new maplibregl.Map({
  container: "map",
  style: style({ tilesBase }),
  bounds: BBOX,
  hash: true,
  dragRotate: false,
  pitchWithRotate: false,
  attributionControl: { compact: true },
});
window.map = map; // exposed for debugging / verification

// MapLibre keeps compact attribution expanded until the first map interaction;
// collapse it once the style (and its attributions) have loaded.
map.on("load", () =>
  map
    .getContainer()
    .querySelector(".maplibregl-ctrl-attrib")
    ?.classList.remove("maplibregl-compact-show"),
);

map.addControl(new maplibregl.NavigationControl());
map.addControl({
  onAdd: () => document.getElementById("controls"),
  onRemove: () => {},
});

// No setTerrain(): enabling 3D terrain drapes the DEM layers (depth-shading, hillshade)
// over MapLibre's terrain mesh, which resamples the land+water DEM coarsely below native
// zoom. Near coasts the large positive land values bleed across the 0 m transparency
// cutoff, so narrow/shallow water (bays, sounds, shoals) renders transparent at z<8 —
// leaving only the deep open water, which then looks like coarse GEBCO. color-relief reads
// the tiles at native resolution instead. Click-depth reads the DEM tile directly
// (readDepth), so terrain isn't needed for it. Re-add behind a toggle for 3D seafloor.

// ─── Layer toggles ────────────────────────────────────────────────────────
const toggles = {
  "toggle-depth": ["depth-shading"],
  "toggle-hillshade": ["hillshade"],
  "toggle-contours": ["contour-lines"],
  "toggle-labels": ["contour-labels"],
  "toggle-soundings": ["soundings"],
  "toggle-drying": ["drying-areas"],
  "toggle-sources": [
    "source-fill",
    "source-highlight",
    "source-outline",
    "source-labels",
  ],
};

map.on("load", () => {
  // Mariner settings — unit (m/ft/fm) and safety depth (metres, 0 = off; drives
  // the hazard tint and black-sounding emphasis, S-52 style). applyState flips
  // the global-state AND rebuilds the depth ramp together (the ramp can't read
  // global-state), so the controls just forward their current values.
  const applyControls = () =>
    applyState(map, {
      unit: document.getElementById("unit-select")?.value || "m",
      safety: parseFloat(document.getElementById("safety-depth")?.value) || 0,
    });
  applyControls();
  document
    .getElementById("safety-depth")
    ?.addEventListener("input", applyControls);
  document
    .getElementById("unit-select")
    ?.addEventListener("change", applyControls);
  // Layer toggles: the checkboxes are the source of truth — sync once on load
  // (the style's own defaults may differ, e.g. hillshade ships hidden) and on
  // every change.
  for (const [inputId, layerIds] of Object.entries(toggles)) {
    const input = document.getElementById(inputId);
    if (!input) continue;
    const sync = () => {
      const vis = input.checked ? "visible" : "none";
      layerIds.forEach((id) => {
        if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", vis);
      });
    };
    sync();
    input.addEventListener("change", sync);
  }
});

// ─── Click to inspect ─────────────────────────────────────────────────────
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
  const ele = await readDepth(
    tilesBase,
    e.lngLat,
    Math.min(map.getZoom(), MAX_ZOOM),
  );
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
