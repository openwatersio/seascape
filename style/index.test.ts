import { expect, test } from "vitest";
import { validateStyleMin } from "@maplibre/maplibre-gl-style-spec";
import {
  applyState,
  day,
  state,
  depthRelief,
  sources,
  layers,
  style,
  type ChartMap,
} from "./index";

// Expressions are opaque tuple unions; tests poke at their raw stops.
const raw = (e: unknown) => e as (number | string)[];

test("generated style validates against the MapLibre style spec", () => {
  const variants = [
    style({ tilesBase: "https://t.example/seascape" }),
    style({
      tilesBase: "https://t.example",
      osm: false,
      unit: "ft",
      safety: 3,
    }),
  ];
  for (const s of variants) expect(validateStyleMin(s)).toEqual([]);
});

test("sources reference the endpoint's TileJSON, encoding inline", () => {
  const src = sources({ tilesBase: "https://t.example" }) as Record<
    string,
    { url?: string; encoding?: string }
  >;
  expect(src["seascape-dem"].url).toBe("https://t.example/raster.json");
  // A trailing slash on tilesBase must not produce double-slash URLs.
  const slashed = sources({ tilesBase: "https://t.example/" }) as Record<
    string,
    { url?: string }
  >;
  expect(slashed["seascape-dem"].url).toBe("https://t.example/raster.json");
  // MapLibre doesn't read `encoding` from TileJSON — it must be inline.
  expect(src["seascape-dem"].encoding).toBe("terrarium");
  expect(src["seascape-vector"].url).toBe("https://t.example/vector.json");
});

test("unit/safety bake into both state defaults and the depth ramp", () => {
  const s = style({ tilesBase: "https://t.example", unit: "fm", safety: 3 });
  expect(s.state).toEqual({ unit: { default: "fm" }, safety: { default: 3 } });
  const shading = s.layers.find((l) => l.id === "depth-shading");
  const ramp = raw(
    (shading as { paint: Record<string, unknown> }).paint[
      "color-relief-color"
    ],
  );
  expect(ramp).toContain(day.hazard);
  // Fathom-curve band edges are active; the 3 fm edge is deeper than the 3 m
  // safety depth so it survives the hazard fold (shoaler stops are dropped).
  expect(ramp).toContain(-3 * 1.8288);
  expect(ramp).not.toContain(-1 * 1.8288);
});

test("depthRelief folds a crisp hazard edge at the safety depth", () => {
  expect(raw(depthRelief(day, { unit: "m", safety: 0 }))).not.toContain(
    day.hazard,
  );
  const on = raw(depthRelief(day, { unit: "m", safety: 2 }));
  const hz = on.indexOf(day.hazard);
  expect(hz).toBeGreaterThan(0);
  expect(on[hz - 1]).toBe(-2 + 0.01); // normal colour pinned just below…
  expect(on[hz + 1]).toBe(-0.01); // …hazard up to the shore
  expect(on[on.length - 1]).toBe(day.land); // land wash still terminates the ramp
});

test("layers reference only the caller's source names", () => {
  const named = layers(day, { dem: "bathy-dem", vector: "bathy" });
  expect(
    [...new Set(named.map((l) => (l as { source: string }).source))].sort(),
  ).toEqual(["bathy", "bathy-dem"]);
});

test("layer ids are stable — consumers key toggles/queries off them", () => {
  expect(layers().map((l) => l.id)).toEqual([
    "depth-shading",
    "hillshade",
    "drying-areas",
    "contour-lines",
    "contour-labels",
    "soundings",
    "source-fill",
    "source-highlight",
    "source-outline",
    "source-labels",
  ]);
});

test("state defaults exist for every global-state key the expressions read", () => {
  expect(Object.keys(state).sort()).toEqual(["safety", "unit"]);
});

test("applyState keeps global-state and the ramp in sync", () => {
  const globals: Record<string, unknown> = { unit: "m", safety: 2 };
  const paints: unknown[] = [];
  const map: ChartMap = {
    setGlobalStateProperty: (k, v) => (globals[k] = v),
    getGlobalState: () => globals,
    setPaintProperty: (layer, prop, value) => paints.push({ layer, prop, value }),
    getLayer: (id) => (id === "depth-shading" ? {} : undefined),
  };

  // Changing only safety must rebuild the ramp with the CURRENT unit.
  applyState(map, { safety: 5 });
  expect(globals.safety).toBe(5);
  expect(paints).toHaveLength(1);
  const ramp = raw(
    (paints[0] as { layer: string; prop: string; value: unknown }).value,
  );
  expect(ramp).toContain(day.hazard); // safety > 0 folds the hazard band
  expect(ramp).toContain(-10); // metric band edge → unit stayed "m"

  // Changing unit swaps the ramp onto the fathom curves.
  applyState(map, { unit: "fm" });
  expect(globals.unit).toBe("fm");
  const fmRamp = raw(
    (paints[1] as { layer: string; prop: string; value: unknown }).value,
  );
  expect(fmRamp).toContain(-30 * 1.8288); // 30 fm curve → ft/fm band edges
  // Same-call values win over (possibly async) map state reads.
  expect(fmRamp).toContain(day.hazard); // safety 5 still folded

  // No depth-shading layer (composed style without it) → state set, no repaint.
  const bare: ChartMap = { ...map, getLayer: () => undefined };
  applyState(bare, { safety: 0 });
  expect(paints).toHaveLength(2);
});
