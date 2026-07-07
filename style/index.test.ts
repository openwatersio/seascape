import { expect, test } from "vitest";
import { validateStyleMin } from "@maplibre/maplibre-gl-style-spec";
import {
  applyState,
  day,
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

test("unit/safety reach the layers as literals", () => {
  const s = style({ tilesBase: "https://t.example", unit: "fm", safety: 3 });
  // The compat contract: every expression is a literal, so the style runs on
  // any GL JS with color-relief.
  expect(JSON.stringify(s)).not.toContain("global-state");
  expect(s).not.toHaveProperty("state");
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
  expect(on[hz + 1]).toBe(-1 / 256); // …hazard up to the encoder's water floor
  expect(on[on.length - 1]).toBe(day.land); // land wash still terminates the ramp
});

test("depthRelief stops stay strictly ascending for any safety depth", () => {
  // Tiny values used to emit out-of-order/duplicate stops, which MapLibre
  // rejects (breaking the whole depth-shading layer); they floor to just
  // above the crisp-edge width.
  for (const safety of [0.001, 0.015, 0.02, 0.03, 2, 10000]) {
    const expr = raw(depthRelief(day, { unit: "m", safety }));
    const stops = expr.slice(3).filter((_, i) => i % 2 === 0) as number[];
    for (let i = 1; i < stops.length; i++)
      expect(stops[i]).toBeGreaterThan(stops[i - 1]);
  }
});

test("layers reference only the caller's source names", () => {
  const named = layers(day, { dem: "bathy-dem", vector: "bathy" });
  expect(
    [...new Set(named.map((l) => (l as { source: string }).source))].sort(),
  ).toEqual(["bathy", "bathy-dem"]);
});

test("contour lines floor at z6 — depth shading carries lower zooms", () => {
  const lines = layers().find((l) => l.id === "contour-lines");
  expect((lines as { minzoom?: number }).minzoom).toBe(6);
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

test("applyState re-derives every unit/safety-dependent property", () => {
  type Call = { fn: string; layer: string; prop?: string; value: unknown };
  const calls: Call[] = [];
  const map: ChartMap = {
    setFilter: (layer, value) => calls.push({ fn: "filter", layer, value }),
    setLayoutProperty: (layer, prop, value) =>
      calls.push({ fn: "layout", layer, prop, value }),
    setPaintProperty: (layer, prop, value) =>
      calls.push({ fn: "paint", layer, prop, value }),
    getLayer: () => ({}),
  };

  applyState(map, { unit: "fm", safety: 5 });
  // Ramp: fathom-curve band edges active, hazard band folded.
  const ramp = raw(
    calls.find((c) => c.fn === "paint" && c.layer === "depth-shading")!.value,
  );
  expect(ramp).toContain(-30 * 1.8288);
  expect(ramp).toContain(day.hazard);
  // Isobath filters flip to the fathom-curve set — lines and labels together.
  for (const id of ["contour-lines", "contour-labels"])
    expect(calls.find((c) => c.fn === "filter" && c.layer === id)!.value)
      .toEqual(["==", ["get", "sys"], "ft"]);
  // Label text follows the unit.
  expect(
    JSON.stringify(
      calls.find((c) => c.fn === "layout" && c.layer === "soundings")!.value,
    ),
  ).toContain("depth_fm");
  // Unsafe-sounding emphasis carries the safety literal.
  expect(
    JSON.stringify(
      calls.find((c) => c.fn === "paint" && c.layer === "soundings")!.value,
    ),
  ).toContain("5");

  // Layers absent from the map (composed subsets) are skipped entirely.
  const before = calls.length;
  const bare: ChartMap = { ...map, getLayer: () => undefined };
  applyState(bare, { unit: "m", safety: 0 });
  expect(calls).toHaveLength(before);
});
