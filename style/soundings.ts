/**
 * soundings — spot depths, set as chart typography.
 */
import type {
  ExpressionSpecification,
  LayerSpecification,
} from "@maplibre/maplibre-gl-style-spec";
import { labelSize, type Flavor, type Unit } from "./flavor.js";

// Sub-unit digits on soundings are REAL subscript glyphs (U+2080-2089) from the self-hosted
// stack — the type designer set the drop and the sidebearings. The alternative, a scaled
// `format` section with vertical-align, aligns em-boxes rather than baselines, so the digit
// sat visibly high and tight against the integer. A hair space (U+200A, also in the stack)
// gives the pair its air.
const SUB = "\u2080\u2081\u2082\u2083\u2084\u2085\u2086\u2087\u2088\u2089";
const SUBSCRIPT = [...SUB].map((c, i) => (i ? "\u200A" + c : ""));

// Soundings: props depth_m / depth_ft / depth_fm, all floored toward shallower by
// soundings_run.py — splitting the digits here must not re-round. The whole part is the chart
// number and the sub-unit is set smaller and dropped below it, never shown when zero
// (S-4 B-412.1).
const subUnitSounding = (
  whole: ExpressionSpecification,
  sub: ExpressionSpecification,
): ExpressionSpecification => [
  "concat",
  ["to-string", whole],
  [
    "match",
    sub,
    1,
    SUBSCRIPT[1],
    2,
    SUBSCRIPT[2],
    3,
    SUBSCRIPT[3],
    4,
    SUBSCRIPT[4],
    5,
    SUBSCRIPT[5],
    6,
    SUBSCRIPT[6],
    7,
    SUBSCRIPT[7],
    8,
    SUBSCRIPT[8],
    9,
    SUBSCRIPT[9],
    "",
  ],
];

// Metres: the tenths digit is decimetres. `round` on the residual recovers it through float
// dust (3.9 % 1 == 0.9000000000000004).
const metreSounding = subUnitSounding(
  ["floor", ["get", "depth_m"]],
  ["round", ["*", 10, ["%", ["get", "depth_m"], 1]]],
);

// Fathoms: "Fathoms and Feet up to 11 fathoms and in fathoms only in depths greater than 11
// fathoms" (Canada CHS Chart 1, 2022). A fathom is exactly six feet, so both digits come off
// depth_ft and the tiles never carry a mixed-radix number.
const FT_PER_FATHOM = 6;
const FATHOM_FEET_MAX_FM = 11;
const fathomSounding = subUnitSounding(
  ["floor", ["/", ["get", "depth_ft"], FT_PER_FATHOM]],
  [
    "case",
    [">=", ["get", "depth_ft"], FATHOM_FEET_MAX_FM * FT_PER_FATHOM],
    0,
    ["%", ["get", "depth_ft"], FT_PER_FATHOM],
  ],
);

export function soundingsLayer(
  flavor: Flavor,
  { vector, unit }: { vector: string; unit: Unit },
): LayerSpecification {
  const soundingText: ExpressionSpecification =
    unit === "ft"
      ? ["to-string", ["get", "depth_ft"]]
      : unit === "m"
        ? metreSounding
        : fathomSounding;

  return {
    id: "soundings",
    type: "symbol",
    source: vector,
    "source-layer": "soundings",
    minzoom: 7,
    layout: {
      "text-field": soundingText,
      "text-font": flavor.soundingFont,
      "text-size": labelSize,
      "text-padding": 8,
      // Lower sorts first and wins the collision. A prime sounding outranks the whole field —
      // "must always be shown" (S-4 B-410b) fails if a deeper neighbour can displace it — and
      // the rest fall back to shoalest-first, so where two ordinary soundings collide the
      // safer number survives.
      "symbol-sort-key": [
        "case",
        ["==", ["get", "prime"], 1],
        -1e6,
        ["get", "depth_m"],
      ] as unknown as ExpressionSpecification,
    },
    paint: {
      // One colour for the whole field, like a paper chart: S-4 sets every sounding in
      // one style and reserves type distinctions for reliability (B-412.4), not depth —
      // hazard is carried by the depth-area tint and the isobaths themselves. `prime` (the
      // least depth inside a closed isobath) still governs retention and collision priority
      // above, but it is a topological fact of the build window, not a hazard ranking: a
      // coastal shelf whose contour closes beyond the window edge is never prime, while a
      // 0.5 m wrinkle at 29 m is, so inking it black read exactly backwards.
      "text-color": flavor.label,
      "text-halo-color": flavor.labelHalo,
      "text-halo-width": 1,
    },
  };
}

// Unit changes the label text; the colour rides along so an applyState with a
// different flavor keeps the field coherent.
export const soundingsState = ["layout.text-field", "paint.text-color"];
