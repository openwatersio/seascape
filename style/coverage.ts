/**
 * source-fill / source-highlight / source-outline / source-labels — source
 * coverage (provenance): footprint polygons with props source_id / source_name /
 * source_maxzoom, from the standalone coverage tileset. Hidden by default; a
 * viewer can toggle them on for click-to-identify.
 */
import type { LayerSpecification } from "@maplibre/maplibre-gl-style-spec";
import type { Flavor } from "./flavor.js";

export function coverageLayers(
  flavor: Flavor,
  { coverage }: { coverage: string },
): LayerSpecification[] {
  const coverageColor = flavor.coverage;
  return [
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
        "text-halo-color": flavor.labelHalo,
        "text-halo-width": 1.2,
      },
    },
  ];
}
