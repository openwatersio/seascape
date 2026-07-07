// TileJSON for the standalone coverage tileset (source-provenance footprints,
// its own coverage.pmtiles — see /coverage.json in index.ts). Separated from the
// handler so the shape is testable under plain node (index.ts's WASM imports
// aren't loadable there).

export interface CoverageHeader {
  minZoom: number;
  maxZoom: number;
  minLon: number;
  minLat: number;
  maxLon: number;
  maxLat: number;
}

// header = null means coverage.pmtiles is absent (a pre-coverage release served
// by a new Worker): degrade to a valid empty document — zero zooms, same tiles
// URL (whose requests then 204) — never a throw/500.
export function coverageTileJSON(
  h: CoverageHeader | null,
  tilesBase: string,
  attribution: string,
) {
  return {
    tilejson: "3.0.0",
    name: "Open Waters Bathymetry (source coverage)",
    tiles: [`${tilesBase}/coverage/{z}/{x}/{y}.pbf`],
    minzoom: h?.minZoom ?? 0,
    maxzoom: h?.maxZoom ?? 0,
    bounds: h
      ? [h.minLon, h.minLat, h.maxLon, h.maxLat]
      : [-180, -85.051129, 180, 85.051129],
    vector_layers: [
      {
        // Per-source data-extent polygons (provenance / click-to-identify).
        id: "coverage",
        fields: {
          source_id: "String",
          source_name: "String",
          source_maxzoom: "Number",
        },
      },
    ],
    attribution,
  };
}
