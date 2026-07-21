import { expect, test } from "vitest";
import { validateStyleMin } from "@maplibre/maplibre-gl-style-spec";
import {
  applyState,
  day,
  depthAreasColor,
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
    style({ tilesBase: "https://t.example", shading: "bands", safety: 5 }),
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
  expect(src["seascape-coverage"].url).toBe("https://t.example/coverage.json");
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
  // …then 0 → unknown-water tint, +LSB → land terminates the ramp.
  expect(on[on.length - 4]).toBe(0);
  expect(on[on.length - 3]).toBe(day.nodata);
  expect(on[on.length - 2]).toBe(1 / 256);
  expect(on[on.length - 1]).toBe(day.land);
});

test("depthRelief renders decoded 0 as unknown water and +LSB as land", () => {
  const ramp = raw(depthRelief(day, { unit: "m", safety: 0 }));
  const zero = ramp.indexOf(0);
  expect(zero).toBeGreaterThan(0);
  expect(ramp[zero - 2]).toBe(-1 / 256); // -LSB → shoalest band
  expect(ramp[zero - 1]).toBe(day.bandColors[5]);
  expect(ramp[zero + 1]).toBe(day.nodata); // 0 → unknown-water tint
  expect(ramp[zero + 2]).toBe(1 / 256); // +LSB → land
  expect(ramp[zero + 3]).toBe(day.land);
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
  const named = layers(day, {
    dem: "bathy-dem",
    vector: "bathy",
    coverage: "bathy-coverage",
  });
  expect(
    [...new Set(named.map((l) => (l as { source: string }).source))].sort(),
  ).toEqual(["bathy", "bathy-coverage", "bathy-dem"]);
  // The source-* provenance layers read the coverage source, not vector.
  for (const l of named.filter((l) => l.id.startsWith("source-")))
    expect((l as { source: string }).source).toBe("bathy-coverage");
});

test("contour lines floor at z6 — depth shading carries lower zooms", () => {
  const lines = layers().find((l) => l.id === "contour-lines");
  expect((lines as { minzoom?: number }).minzoom).toBe(6);
});

test("layer ids are stable — consumers key toggles/queries off them", () => {
  expect(layers().map((l) => l.id)).toEqual([
    "depth-shading",
    "depth-areas",
    "hillshade",
    "contour-lines",
    "contour-labels",
    "soundings",
    "source-fill",
    "source-highlight",
    "source-outline",
    "source-labels",
  ]);
});

test("depare fill unifies bands, drying, and unknown-depth water in one layer", () => {
  // The standalone drying-areas / unsurveyed-areas layers are gone; the depare source-layer
  // now carries all three, distinguished by attribute presence and ordered by `rank`.
  const ls = layers(day, { shading: "bands", unit: "m", safety: 0 });
  expect(ls.find((l) => l.id === "drying-areas")).toBeUndefined();
  expect(ls.find((l) => l.id === "unsurveyed-areas")).toBeUndefined();
  const da = ls.find((l) => l.id === "depth-areas") as {
    type: string;
    source: string;
    "source-layer": string;
    layout: Record<string, unknown>;
    paint: Record<string, unknown>;
  };
  expect(da.type).toBe("fill");
  expect(da["source-layer"]).toBe("depare");
  // rank orders within-layer overlaps deterministically (nodata < bands < drying).
  expect(da.layout["fill-sort-key"]).toEqual(["get", "rank"]);
  // fill-color is a case: nodata (no drval1) → the provisional flat tint, drying
  // (drval1 < 0) → foreshore green, else the band ramp keyed off drval1.
  const color = raw(da.paint["fill-color"]);
  expect(color[0]).toBe("case");
  expect(color[1]).toEqual(["!", ["has", "drval1"]]);
  expect(color[2]).toBe(day.nodata);
  expect(color[3]).toEqual(["<", ["get", "drval1"], 0]);
  expect(color[4]).toBe(day.drying);
  // The band ramp is the case fallback — the same expression depthAreasColor emits.
  expect(JSON.stringify(color[color.length - 1])).toBe(
    JSON.stringify(depthAreasColor(day, { unit: "m", safety: 0 })),
  );
  // nodata keeps a lighter provisional wash; bands + drying at the depth-fill opacity.
  expect(da.paint["fill-opacity"]).toEqual([
    "case",
    ["!", ["has", "drval1"]],
    0.55,
    0.85,
  ]);
});

test("shading gates the depare bands via filter, never compounding with the relief", () => {
  const get = (ls: ReturnType<typeof layers>, id: string) =>
    ls.find((l) => l.id === id) as {
      filter?: unknown;
      maxzoom?: number;
      minzoom?: number;
      layout?: { visibility?: string };
    };
  const relief = layers(); // default (unit m)
  // Relief mode: the raster ramp carries depth, so the depare fill drops the bands (they
  // carry `sys`) and keeps only the unit-less drying/nodata features — no double 0.85 fill.
  expect(get(relief, "depth-areas").filter).toEqual(["!", ["has", "sys"]]);
  expect(get(relief, "depth-shading").maxzoom).toBeUndefined();
  // Bands mode: the fill adds the active-sys ladder to the unit-less features, and the raster
  // hands off at the z6 floor (relief below, bands above).
  const bands = layers(day, { shading: "bands", unit: "m" });
  expect(get(bands, "depth-areas").filter).toEqual([
    "any",
    ["!", ["has", "sys"]],
    ["==", ["get", "sys"], "m"],
  ]);
  expect(get(bands, "depth-shading").maxzoom).toBe(6);
  expect(get(bands, "depth-areas").minzoom).toBe(6);
  // The fill is always visible in both modes — the filter, not visibility, gates the bands.
  expect(get(relief, "depth-areas").layout?.visibility).toBeUndefined();
  expect(get(bands, "depth-areas").layout?.visibility).toBeUndefined();
  // ft/fm mode selects the fathom-curve band ladder.
  const ftBands = layers(day, { shading: "bands", unit: "ft" });
  expect(get(ftBands, "depth-areas").filter).toEqual([
    "any",
    ["!", ["has", "sys"]],
    ["==", ["get", "sys"], "ft"],
  ]);
});

test("depthAreasColor tints bands off drval1 and snaps safety deeper", () => {
  // No safety: a step from shoalest to deepest band colour, stops just under
  // the band edges (float32 drval fuzz guard).
  const off = raw(depthAreasColor(day, { unit: "m", safety: 0 }));
  expect(off[0]).toBe("step");
  expect(off[2]).toBe(day.bandColors[5]); // < 2 m → shoalest
  expect(off[3]).toBe(2 - 0.01);
  expect(off[off.length - 2]).toBe(50 - 0.01);
  expect(off[off.length - 1]).toBe(day.bandColors[0]); // ≥ 50 m → deepest
  expect(off).not.toContain(day.hazard);
  // safety 15 m snaps to the 20 m rung: bands with drval1 < 20 go hazard.
  const on = depthAreasColor(day, { unit: "m", safety: 15 }) as unknown[];
  expect(on[0]).toBe("case");
  expect(JSON.stringify(on[1])).toContain(String(20 - 0.01));
  expect(on[2]).toBe(day.hazard);
  // Fathom mode snaps up the fathom-curve ladder (safety 3 m → the 2 fm rung).
  const fm = depthAreasColor(day, { unit: "fm", safety: 3 }) as unknown[];
  expect(JSON.stringify(fm[1])).toContain(String(2 * 1.8288 - 0.01));
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

  applyState(map, { unit: "fm", safety: 5, shading: "bands" });
  // Ramp: fathom-curve band edges active, hazard band folded.
  const ramp = raw(
    calls.find((c) => c.fn === "paint" && c.layer === "depth-shading")!.value,
  );
  expect(ramp).toContain(-30 * 1.8288);
  expect(ramp).toContain(day.hazard);
  // Isobath filters flip to the fathom-curve set — the contour lines and labels.
  for (const id of ["contour-lines", "contour-labels"])
    expect(calls.find((c) => c.fn === "filter" && c.layer === id)!.value)
      .toEqual(["==", ["get", "sys"], "ft"]);
  // The depare fill adds the ft band ladder alongside the unit-less drying/nodata features.
  expect(
    calls.find((c) => c.fn === "filter" && c.layer === "depth-areas")!.value,
  ).toEqual(["any", ["!", ["has", "sys"]], ["==", ["get", "sys"], "ft"]]);
  // Band fill recolours with the snapped safety contour (5 m is a rung).
  expect(
    JSON.stringify(
      calls.find(
        (c) =>
          c.fn === "paint" && c.layer === "depth-areas" && c.prop === "fill-color",
      )!.value,
    ),
  ).toContain(day.hazard);
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
