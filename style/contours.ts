/**
 * contour-lines and contour-labels — the isobaths and their depth numbers.
 */
import type {
  ExpressionSpecification,
  LayerSpecification,
} from "@maplibre/maplibre-gl-style-spec";
import { labelSize, type Flavor, type Unit } from "./flavor.js";
import { snapSafetyContour } from "./depth-areas.js";

// Contours: metre isobaths (sys != "ft", also legacy no-sys tiles) vs the
// fathom-curve set (sys == "ft"), which labels as feet or fathoms. Both
// systems label every curve — the standard isobath sets are sparse enough
// that GL collision thins the labels. The 0 m drying line is unit-independent
// and ships once with NO sys (like depare's drying/nodata), so both filters
// admit sys-less features.
const contourFilter = (unit: Unit): ExpressionSpecification =>
  (unit === "m"
    ? ["!=", ["get", "sys"], "ft"]
    : [
        "any",
        ["!", ["has", "sys"]],
        ["==", ["get", "sys"], "ft"],
      ]) as unknown as ExpressionSpecification;

export function contourLinesLayer(
  flavor: Flavor,
  { vector, unit, safety }: { vector: string; unit: Unit; safety: number },
): LayerSpecification {
  // The safety contour snaps UP the charted ladder to the next-deeper level, exactly
  // as depthAreasColor recolours the bands, so the emphasized line always bounds the
  // hazard tint. Contours carry integer depth props (depth_abs_m / depth_fm,
  // contour_run.py), so the match is exact equality on the active system's prop.
  const safetyContour = snapSafetyContour(unit, safety);
  const isSafetyContour: ExpressionSpecification =
    unit === "m"
      ? ["==", ["get", "depth_abs_m"], safetyContour]
      : ["==", ["get", "depth_fm"], Math.round(safetyContour / 1.8288)];

  return {
    id: "contour-lines",
    type: "line",
    source: vector,
    "source-layer": "contours",
    filter: contourFilter(unit),
    // Presentation floor, not a data limit: below z6 isobaths read as clutter over depth shading.
    minzoom: 6,
    // Full-strength linework at DEPCN weight — translucent hairlines read as
    // shading artefacts rather than isobaths.
    paint: {
      // The safety contour is the one emphasized isobath — thicker, in the emphasis
      // colour, like S-52's DEPSC over DEPCN (IMO MSC.232 requires the emphasis);
      // every other contour stays uniform DEPCN weight (S-4 B-411.1 recommends
      // against emphasizing fixed standard contours).
      "line-color": safetyContour
        ? ["case", isSafetyContour, flavor.contourEmphasis, flavor.contour]
        : flavor.contour,
      "line-width": safetyContour ? ["case", isSafetyContour, 1.5, 0.8] : 0.8,
    },
  };
}

// safety moves the emphasized contour, so the paint is safety-dependent too.
export const contourLinesState = [
  "filter",
  "paint.line-color",
  "paint.line-width",
];

export function contourLabelsLayer(
  flavor: Flavor,
  { vector, unit }: { vector: string; unit: Unit },
): LayerSpecification {
  // Depth number only, like paper charts (S-4 B-411.3) — the unit lives in the
  // consumer's UI, and soundings are already unitless.
  const contourLabelText: ExpressionSpecification = [
    "to-string",
    [
      "get",
      unit === "ft" ? "depth_ft" : unit === "fm" ? "depth_fm" : "depth_abs_m",
    ],
  ];

  return {
    id: "contour-labels",
    type: "symbol",
    source: vector,
    "source-layer": "contours",
    filter: contourFilter(unit),
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
  };
}

export const contourLabelsState = ["filter", "layout.text-field"];
