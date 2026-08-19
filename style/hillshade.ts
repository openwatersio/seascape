/**
 * depth-hillshade — bathymetric hillshading over the water shading.
 */
import type { LayerSpecification } from "@maplibre/maplibre-gl-style-spec";
import type { Flavor } from "./flavor.js";

export function hillshadeLayer(
  flavor: Flavor,
  { dem, hillshade }: { dem: string; hillshade: boolean },
): LayerSpecification {
  return {
    id: "depth-hillshade",
    type: "hillshade",
    source: dem,
    layout: { visibility: hillshade ? "visible" : "none" },
    paint: {
      "hillshade-exaggeration": 0.5,
      "hillshade-shadow-color": flavor.hillshadeShadow,
      "hillshade-highlight-color": flavor.hillshadeHighlight,
      "hillshade-illumination-direction": 315,
    },
  };
}

// The hillshade toggle applyState re-derives in place.
export const hillshadeState = ["layout.visibility"];
