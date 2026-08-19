/**
 * Appearance: the Flavor object, the built-in day palette, and the shared type
 * scale. Layer *structure* lives in the per-layer modules; a custom look is a
 * spread-override of `day`.
 */
import type { ExpressionSpecification } from "@maplibre/maplibre-gl-style-spec";

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
  soundingEmphasis: string;
  contourEmphasis: string;
  font: string[];
  /** Soundings only. S-4 B-412.1 sets them in sloping numerals; upright is reserved for
   *  soundings of lower reliability (B-412.4). */
  soundingFont: string[];
  hillshadeShadow: string;
  hillshadeHighlight: string;
  coverage: string;
}

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
  // Drying areas (S-52 DEPIT day green): seabed above chart datum that covers
  // and uncovers with the tide. Darker than the shoalest band — charts weight
  // the foreshore as the heaviest tint in the shoaling sequence.
  drying: "#58af9c",
  // Unknown-depth water (ENC DEPARE nodata): mapped water we hold no depth for. Painted
  // the hazard tint — unsurveyed water warrants the same caution as known-unsafe water
  // (ECDIS treats it as unsafe for the safety check; S-52 hatching is the eventual upgrade).
  nodata: "#1f86cb",
  // Isobaths and their labels recede in chart grey (S-52 DEPCN/SNDG1 #768C97);
  // only unsafe soundings jump, in soundingEmphasis (SNDG2).
  contour: "#768c97",
  label: "#768c97",
  labelHalo: "rgba(255,255,255,0.5)",
  soundingEmphasis: "#000",
  // Safety contour line (S-52 DEPSC day): darker grey, distinct from SNDG2 black.
  contourEmphasis: "#4C5B63",
  font: ["Noto Sans Medium"],
  soundingFont: ["Noto Sans Medium Italic"],
  hillshadeShadow: "#9adcfe",
  hillshadeHighlight: "#ffffff",
  coverage: "#f58231",
};

// Mariner-setting defaults for layers()/style() when the caller omits them.
export const DEFAULT_UNIT: Unit = "m";
export const DEFAULT_SAFETY = 2;
export const DEFAULT_SHADING: Shading = "relief";

// Shared label styling so soundings and contour labels read as one chart. S-52
// puts a sounding digit at ~3.5 mm (§5.2.1(2)) = ~13 px of digit height, and a
// digit is ~0.71 em, so the 18 px em at chart scale is the standard's size, not
// a large one. The ramp down at coarse zooms is a deliberate deviation from
// S-52 §3.1.5 ("text size should never be decreased when zooming out"): that
// rule assumes ECDIS's fixed compilation scale, and at a z8 overview the full-
// size type shouts over a whole sea. The decimetre subscript glyph is cut at
// roughly 0.6 em by the typeface, so the low anchor is also its legibility floor.
export const labelSize: ExpressionSpecification = [
  "interpolate",
  ["linear"],
  ["zoom"],
  8,
  12,
  13,
  16,
];
