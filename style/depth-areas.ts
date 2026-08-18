/**
 * depth-areas — the ENC DEPARE fill, the vector twin of depth-shading, and the
 * charted isobath ladders the safety contour snaps against.
 */
import type {
  ExpressionSpecification,
  LayerSpecification,
} from "@maplibre/maplibre-gl-style-spec";
import { day, type Flavor, type Shading, type Unit } from "./flavor.js";

// The depare layer's data floor (tippecanoe -Z in the pipeline) and the contour
// lines' presentation floor — depth shading carries lower zooms.
export const BANDS_MIN_ZOOM = 6;

// Charted isobath ladders, positive-down metres — must mirror pipelines/config.py
// DEPARE_LEVELS / DEPARE_LEVELS_FT (the bucket edges baked into the depare
// layer). The safety contour snaps UP this ladder to the next-deeper rung
// (ECDIS behaviour, bias shallow) — vector bands can only flip at charted
// levels, unlike the raster ramp's continuous crisp edge.
const DEPARE_LADDER_M = [
  2, 5, 10, 20, 30, 50, 100, 200, 300, 500, 1000, 2000, 3000, 4000, 5000, 6000,
  8000, 10000,
];
const DEPARE_LADDER_FT = [
  1, 2, 3, 5, 10, 20, 30, 50, 100, 200, 300, 500, 1000, 2000, 3000, 5000,
].map((fm) => fm * 1.8288);

// Comparisons against drval1 subtract this: tiles may carry the fathom-curve
// drvals (1.8288, 5.4864, …) as 32-bit floats, which can land a hair below the
// exact edge value. Ladder rungs are ≥ ~1.8 m apart, so 0.01 m is safely
// inside every gap.
export const DRVAL_EPS = 0.01;

// The safety depth snapped UP the active ladder to the next-deeper charted
// level; 0 when safety is off. Shared by the band recolour here and the
// emphasized isobath in contours.ts, so tint and line always agree.
export function snapSafetyContour(unit: Unit, safety: number): number {
  if (!(safety > 0)) return 0;
  const ladder = unit === "m" ? DEPARE_LADDER_M : DEPARE_LADDER_FT;
  return (
    ladder.find((l) => l >= safety - DRVAL_EPS) ?? ladder[ladder.length - 1]
  );
}

// Fill colour for the depare partitions: the band tint keyed off drval1 (the
// band's shallow bound), with every band shallower than the snapped safety
// contour painted the hazard tint. Literals only, like depthRelief — runtime
// changes go through applyState().
export function depthAreasColor(
  flavor: Flavor = day,
  { unit = "m", safety = 0 }: { unit?: Unit; safety?: number } = {},
): ExpressionSpecification {
  const metric = unit === "m";
  const edges = [...flavor.bandEdges[metric ? "m" : "ft"]].reverse(); // shoalest → deepest
  const step: unknown[] = ["step", ["get", "drval1"], flavor.bandColors[5]];
  edges.forEach((d, i) => step.push(d - DRVAL_EPS, flavor.bandColors[4 - i]));
  if (!(safety > 0)) return step as unknown as ExpressionSpecification;
  const snap = snapSafetyContour(unit, safety);
  return [
    "case",
    ["<", ["get", "drval1"], snap - DRVAL_EPS],
    flavor.hazard,
    step,
  ] as unknown as ExpressionSpecification;
}

// The depare layer carries three ENC DEPARE feature kinds in one fill, keyed by attribute
// presence: depth bands (drval1/drval2, sys-tagged per m/ft ladder, drval1 >= 0), drying
// foreshore (drval1 < 0, no sys), and unknown-depth water (no drval1, no sys). Bands
// duplicate per sys, so only the active ladder shows, and only in bands mode; drying +
// nodata have NO sys and ship once, so `!has sys` selects them and they render in BOTH
// shading modes — relief filters the bands out (the raster ramp carries depth; two 0.85
// fills would compound) but keeps the honesty fills, since a #24-cleared lake must read as
// unknown water — the render now tints 0-fill as unknown water too, and the depare polygon
// keeps that categorical (and adds the drying tint).
export function depthAreasLayer(
  flavor: Flavor,
  {
    vector,
    unit,
    safety,
    shading,
  }: { vector: string; unit: Unit; safety: number; shading: Shading },
): LayerSpecification {
  const bandSys = unit === "m" ? "m" : "ft";
  const depareFilter = (shading === "bands"
    ? ["any", ["!", ["has", "sys"]], ["==", ["get", "sys"], bandSys]]
    : ["!", ["has", "sys"]]) as unknown as ExpressionSpecification;
  // Fill: nodata (no drval1) → provisional flat tint; drying (drval1 < 0) → foreshore green;
  // else the band ramp keyed off drval1. `case` short-circuits, so the drval1 comparison only
  // runs once the no-drval1 branch has ruled nodata out.
  const depareColor = [
    "case",
    ["!", ["has", "drval1"]],
    flavor.nodata,
    ["<", ["get", "drval1"], 0],
    flavor.drying,
    depthAreasColor(flavor, { unit, safety }),
  ] as unknown as ExpressionSpecification;
  // nodata carries the provisional lighter wash it had as its own layer; bands + drying stay
  // at the depth-fill opacity.
  const depareOpacity = [
    "case",
    ["!", ["has", "drval1"]],
    0.55,
    0.85,
  ] as unknown as ExpressionSpecification;

  return {
    // ENC DEPARE fill — all three depare feature kinds keyed by attribute presence (see the
    // expressions above): depth bands (crisp tint per drval1, safety recolour snapped to the
    // next-deeper charted level), drying foreshore (INT-1 green, negative drval1), and
    // unknown-depth water (provisional flat tint, no drval1). The three are disjoint by
    // construction, so `fill-sort-key: rank` is only a stable tie-breaker at an incidental
    // simplification-wobble edge — nodata under bands (real depth wins), drying over the shoal
    // band it abuts along their shared 0 m seam. Bands are filtered out in relief mode (the
    // raster ramp carries depth); drying + nodata stay in both modes. Where no drying/nodata
    // polygon covers a >=0 pixel the DEM ramp still paints the land wash, so a mask miss
    // degrades to plain land, never to water-over-land.
    id: "depth-areas",
    type: "fill",
    source: vector,
    "source-layer": "depare",
    filter: depareFilter,
    minzoom: BANDS_MIN_ZOOM,
    layout: { "fill-sort-key": ["get", "rank"] },
    paint: {
      "fill-color": depareColor,
      "fill-opacity": depareOpacity,
    },
  };
}

// The unit/safety/shading-dependent properties applyState re-derives in place.
export const depthAreasState = ["filter", "paint.fill-color"];
