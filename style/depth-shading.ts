/**
 * depth-shading — the raster color-relief layer (elevation → colour).
 * The hazard tint folds into this one color-relief ramp: two color-relief
 * layers on one DEM source don't composite (only the first renders), so water
 * shallower than the safety depth is painted into the ramp itself.
 */
import type {
  ExpressionSpecification,
  LayerSpecification,
} from "@maplibre/maplibre-gl-style-spec";
import { day, type Flavor, type Shading, type Unit } from "./flavor.js";
import { BANDS_MIN_ZOOM } from "./depth-areas.js";

type RampStops = (number | string)[]; // alternating elevation, colour

// Terrarium's vertical LSB. The encoder clamps water to <= -LSB; the non-negative domain is
// categorical (0 unknown-depth water, 1 drying foreshore, 2 land — flat codes, exact at every
// zoom): the shoal tint runs flat to -LSB, then the code tints; fractional values between codes
// are overzoom/interpolation transitions and blend smoothly.
const LSB = 1 / 256;
const DRYING_CODE = 1;
const LAND_CODE = 2;

// Width (metres) of the normal→hazard colour transition at the safety depth.
const EDGE = 0.01;

const depthRamp = (flavor: Flavor, edges: number[]): RampStops => {
  const stops: RampStops = [-10000, flavor.bandColors[0]];
  // Blend on the shallow side of each edge: the encoder rounds toward shallow
  edges.forEach((d, i) =>
    stops.push(-d, flavor.bandColors[i], -d + 0.1, flavor.bandColors[i + 1]),
  );
  // The unknown tint is a knife-edge at exact 0 (native code pixels only): pinning drying
  // green at +LSB keeps overzoom's wet/dry blend fractions out of the slate — otherwise the
  // whole (0, 1) interval renders as a wide gray band along every foreshore seam.
  stops.push(
    -LSB,
    flavor.bandColors[5],
    0,
    flavor.nodata,
    LSB,
    flavor.drying,
    DRYING_CODE,
    flavor.drying,
    LAND_CODE - LSB,
    flavor.drying,
    LAND_CODE,
    flavor.land,
  );
  return stops;
};

// Colour of the ramp at elevation e (linear-interpolated). Used to pin a
// normal-coloured stop right at the safety depth so the flip to the hazard
// colour is a crisp ~0.01 m edge at ANY safety value — otherwise the blend
// feathers by however far −safety lands from the nearest ramp stop.
const rampColorAt = (ramp: RampStops, e: number): string => {
  for (let i = 2; i < ramp.length; i += 2)
    if (e <= (ramp[i] as number)) {
      const e0 = ramp[i - 2] as number,
        c0 = ramp[i - 1] as string;
      const e1 = ramp[i] as number,
        c1 = ramp[i + 1] as string;
      if (c0[0] !== "#" || c1[0] !== "#") return c0;
      const t = (e - e0) / (e1 - e0);
      const p = (c: string) =>
        [1, 3, 5].map((k) => parseInt(c.slice(k, k + 2), 16));
      const a = p(c0),
        b = p(c1);
      return `rgb(${a.map((v, k) => Math.round(v + t * (b[k] - v))).join(",")})`;
    }
  return ramp[1] as string;
};

// The color-relief expression for the active unit system and safety depth.
// Unit/safety changes rebuild this and apply it with
// setPaintProperty("depth-shading", ...) — applyState() does exactly that.
export function depthRelief(
  flavor: Flavor = day,
  { unit = "m", safety = 0 }: { unit?: Unit; safety?: number } = {},
): ExpressionSpecification {
  // The crisp-edge stops below need s + EDGE to land strictly between the
  // safety depth and the -LSB water stop, or the interpolate stops go out of
  // ascending order and MapLibre rejects the whole expression. No real safety
  // depth is centimetres, so floor tiny values instead of failing.
  if (safety > 0) safety = Math.max(safety, EDGE + 2 * LSB);
  const ramp = depthRamp(flavor, flavor.bandEdges[unit === "m" ? "m" : "ft"]);
  const s = -safety;
  const stops: RampStops = [];
  for (let i = 0; i < ramp.length; i += 2)
    if (!(safety > 0) || (ramp[i] as number) < s)
      stops.push(ramp[i], ramp[i + 1]);
  // Crisp edge: normal colour pinned at the safety depth, hazard from just
  // shallower up to shore.
  if (safety > 0)
    stops.push(
      s,
      rampColorAt(ramp, s),
      s + EDGE,
      flavor.hazard,
      -LSB,
      flavor.hazard,
      0,
      flavor.nodata,
      LSB,
      flavor.drying,
      DRYING_CODE,
      flavor.drying,
      LAND_CODE - LSB,
      flavor.drying,
      LAND_CODE,
      flavor.land,
    );
  return [
    "interpolate",
    ["linear"],
    ["elevation"],
    ...stops,
  ] as unknown as ExpressionSpecification;
}

export function depthShadingLayer(
  flavor: Flavor,
  {
    dem,
    unit,
    safety,
    shading,
  }: { dem: string; unit: Unit; safety: number; shading: Shading },
): LayerSpecification {
  return {
    id: "depth-shading",
    type: "color-relief",
    source: dem,
    // Bands mode: the vector depth areas take over at their z6 data floor;
    // the relief carries lower zooms (same palette, so the handoff is a
    // sharpness change, not a colour change).
    ...(shading === "bands" ? { maxzoom: BANDS_MIN_ZOOM } : {}),
    paint: {
      "color-relief-color": depthRelief(flavor, { unit, safety }),
      "color-relief-opacity": 0.85,
      // Linear resampling paints a light seam along every unknown-water boundary
      resampling: "nearest",
    },
  } as unknown as LayerSpecification;
}

// The unit/safety/shading-dependent properties applyState re-derives in place.
export const depthShadingState = ["paint.color-relief-color"];
