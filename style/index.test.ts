import { assert, expect, test } from "vitest";
import {
  createExpression,
  validateStyleMin,
} from "@maplibre/maplibre-gl-style-spec";
import {
  applyState,
  day,
  depthAreasColor,
  depthRelief,
  sources,
  layers,
  style,
  SCHEMA,
  type ChartMap,
} from "./index";

// Expressions are opaque tuple unions; tests poke at their raw stops.
const raw = (e: unknown) => e as (number | string)[];

test("SCHEMA is the integer tile-contract version", () => {
  expect(Number.isInteger(SCHEMA)).toBe(true);
  expect(SCHEMA).toBeGreaterThanOrEqual(1);
});

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
  // No safety: the hazard hex appears ONLY as the unknown-water tint at the 0
  // stop (nodata deliberately shares the hazard colour) — never as a depth stop.
  const off = raw(depthRelief(day, { unit: "m", safety: 0 }));
  const hazardStops = off.filter((v) => v === day.hazard);
  expect(hazardStops).toHaveLength(1);
  expect(off[off.indexOf(day.hazard) - 1]).toBe(0);
  const on = raw(depthRelief(day, { unit: "m", safety: 2 }));
  const hz = on.indexOf(day.hazard);
  expect(hz).toBeGreaterThan(0);
  expect(on[hz - 1]).toBe(-2 + 0.01); // normal colour pinned just below…
  expect(on[hz + 1]).toBe(-1 / 256); // …hazard up to the encoder's water floor
  // …then the category codes terminate the ramp: 0 unknown (knife-edge — drying
  // pinned at +LSB keeps overzoom fractions out of the slate), drying flat
  // through 2-LSB (knife-edge at land), 2 land.
  expect(on[on.length - 10]).toBe(0);
  expect(on[on.length - 9]).toBe(day.nodata);
  expect(on[on.length - 8]).toBe(1 / 256);
  expect(on[on.length - 7]).toBe(day.drying);
  expect(on[on.length - 6]).toBe(1);
  expect(on[on.length - 5]).toBe(day.drying);
  expect(on[on.length - 4]).toBe(2 - 1 / 256);
  expect(on[on.length - 3]).toBe(day.drying);
  expect(on[on.length - 2]).toBe(2);
  expect(on[on.length - 1]).toBe(day.land);
});

test("depthRelief renders the category codes: 0 unknown, 1 drying, 2 land", () => {
  const ramp = raw(depthRelief(day, { unit: "m", safety: 0 }));
  const zero = ramp.indexOf(0);
  expect(zero).toBeGreaterThan(0);
  expect(ramp[zero - 2]).toBe(-1 / 256); // -LSB → shoalest band
  expect(ramp[zero - 1]).toBe(day.bandColors[5]);
  expect(ramp[zero + 1]).toBe(day.nodata); // 0 → unknown-water tint (knife-edge)
  expect(ramp[zero + 2]).toBe(1 / 256); // +LSB → drying green, so wet/dry
  expect(ramp[zero + 3]).toBe(day.drying); // overzoom fractions skip the slate
  expect(ramp[zero + 4]).toBe(1); // 1 → drying foreshore green
  expect(ramp[zero + 5]).toBe(day.drying);
  expect(ramp[zero + 6]).toBe(2 - 1 / 256); // drying holds to 2-LSB — the
  expect(ramp[zero + 7]).toBe(day.drying); // drying/land seam is a knife-edge too
  expect(ramp[zero + 8]).toBe(2); // 2 → land wash
  expect(ramp[zero + 9]).toBe(day.land);
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

test("the 0 m drying line is unit-less — every isobath filter admits it", () => {
  // The chart-datum shoreline is the same curve in metres, feet and fathoms, so the pipeline
  // ships it once with no `sys` (like depare's drying/nodata) instead of once per ladder.
  const filterOf = (unit: "m" | "ft" | "fm", id: string) =>
    (layers(day, { unit }).find((l) => l.id === id) as { filter?: unknown })
      .filter;
  for (const id of ["contour-lines", "contour-labels"]) {
    expect(filterOf("m", id)).toEqual(["!=", ["get", "sys"], "ft"]); // missing sys reads null
    for (const unit of ["ft", "fm"] as const)
      expect(filterOf(unit, id)).toEqual([
        "any",
        ["!", ["has", "sys"]],
        ["==", ["get", "sys"], "ft"],
      ]);
  }
});

test("layer ids are stable — consumers key toggles/queries off them", () => {
  expect(layers().map((l) => l.id)).toEqual([
    "depth-shading",
    "depth-areas",
    "depth-hillshade",
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
  // Isobath filters flip to the fathom-curve set plus the unit-less 0 m drying line — the
  // contour lines and labels.
  for (const id of ["contour-lines", "contour-labels"])
    expect(calls.find((c) => c.fn === "filter" && c.layer === id)!.value).toEqual([
      "any",
      ["!", ["has", "sys"]],
      ["==", ["get", "sys"], "ft"],
    ]);
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
  // Label text follows the unit — fathoms read off whole feet, six to the fathom.
  expect(
    JSON.stringify(
      calls.find((c) => c.fn === "layout" && c.layer === "soundings")!.value,
    ),
  ).toContain("depth_ft");
  // Sounding colour is uniform (paper-chart practice; hazard lives in the tint): it must not
  // vary with the safety depth or any per-feature property.
  const soundPaint = JSON.stringify(
    calls.find((c) => c.fn === "paint" && c.layer === "soundings")!.value,
  );
  expect(soundPaint).toBe(JSON.stringify(day.label));

  // Layers absent from the map (composed subsets) are skipped entirely.
  const before = calls.length;
  const bare: ChartMap = { ...map, getLayer: () => undefined };
  applyState(bare, { unit: "m", safety: 0 });
  expect(calls).toHaveLength(before);
});

test("contour labels print the depth number only — no unit suffix", () => {
  for (const unit of ["m", "ft", "fm"] as const) {
    const labels = layers(day, { unit }).find((l) => l.id === "contour-labels");
    const text = (labels as { layout: Record<string, unknown> }).layout["text-field"];
    expect((text as unknown[])[0]).toBe("to-string");
  }
});

test("soundings drop the sub-unit digit, and never show a zero one", () => {
  const field = (unit: "m" | "ft" | "fm") =>
    (
      layers(day, { unit }).find((l) => l.id === "soundings") as {
        layout: Record<string, unknown>;
      }
    ).layout["text-field"];

  // Feet have no sub-division on a chart (US Chart No. 1 §I), so they just print.
  expect((field("ft") as unknown[])[0]).toBe("to-string");
  expect(JSON.stringify(field("ft"))).toContain("depth_ft");

  const print = (unit: "m" | "fm", props: Record<string, number>, zoom = 12) => {
    const compiled = createExpression(field(unit), {
      type: "formatted",
      "property-type": "data-driven",
      expression: { interpolated: false, parameters: ["zoom", "feature"] },
    });
    assert(compiled.result === "success");
    return (
      compiled.value.evaluate({ zoom }, { properties: props }) as {
        sections: { text: string; scale: number | null }[];
      }
    ).sections.map((s) => [s.text, s.scale]);
  };
  const m = (depth_m: number, zoom?: number) => print("m", { depth_m }, zoom);
  const fm = (depth_ft: number) => print("fm", { depth_ft });
  // The sub-unit scale is a tuning knob; assert the structure it produces, not its value.
  const sub = m(3.9)[1][1] as number;
  expect(sub).toBeGreaterThan(0).toBeLessThan(1);

  // Metres: decimetres to 21 m, half metres 21-31 (a .5 residual), whole metres beyond.
  expect(m(3.9)).toEqual([["3", null], ["9", sub]]);
  expect(m(23.5)).toEqual([["23", null], ["5", sub]]);
  expect(m(5.0)).toEqual([["5", null], ["", sub]]);
  expect(m(137)).toEqual([["137", null], ["", sub]]);
  // Sub-units are a function of depth (S-4 B-412), never of scale.
  expect(m(3.9, 8)).toEqual([["3", null], ["9", sub]]);

  // Fathoms derive both digits from whole feet: 22 ft is 3 fathoms 4 feet.
  expect(fm(22)).toEqual([["3", null], ["4", sub]]);
  expect(fm(18)).toEqual([["3", null], ["", sub]]); // exactly 3 fathoms — no feet digit
  // From 11 fathoms the chart drops feet entirely (Canada CHS Chart 1, 2022).
  expect(fm(11 * 6)).toEqual([["11", null], ["", sub]]);
  expect(fm(11 * 6 + 5)).toEqual([["11", null], ["", sub]]);
  expect(fm(10 * 6 + 5)).toEqual([["10", null], ["5", sub]]); // …but not one fathom earlier
});

test("a prime sounding outranks the field for collisions, in uniform ink", () => {
  const snd = layers(day, { unit: "m", safety: 2 }).find(
    (l) => l.id === "soundings",
  ) as { layout: Record<string, unknown>; paint: Record<string, unknown> };

  // S-4 B-410b's "must always be shown" fails if a deeper neighbour can displace the least
  // depth over a shoal, so prime sorts ahead of every ordinary sounding (lower wins).
  const key = createExpression(snd.layout["symbol-sort-key"], {
    type: "number",
    "property-type": "data-driven",
    expression: { interpolated: false, parameters: ["zoom", "feature"] },
  });
  assert(key.result === "success");
  const sort = (p: Record<string, number>) =>
    key.value.evaluate({ zoom: 12 }, { properties: p }) as number;
  expect(sort({ depth_m: 40, prime: 1 })).toBeLessThan(sort({ depth_m: 0.2 }));
  expect(sort({ depth_m: 3 })).toBeLessThan(sort({ depth_m: 9 }));

  // …but it gets no ink of its own: prime is a topological fact of the build window (a shelf
  // whose contour closes past the window edge is never prime), not a hazard ranking, and
  // painting it black read as one. One colour for the whole field, like paper (S-4 B-412).
  expect(snd.paint["text-color"]).toBe(day.label);
});

test("glyphs are self-hosted, and soundings alone are set sloping", () => {
  const s = style({ tilesBase: "https://t.example/seascape" });
  // The MapLibre demo stack ships 3 of the 10 subscript digits, and a missing glyph does not
  // error — it just leaves the decimetre off the chart. Never point back at it.
  expect(s.glyphs).toBe(
    "https://tiles.openwaters.io/fonts/{fontstack}/{range}.pbf",
  );
  expect(s.glyphs).not.toContain("demotiles");

  const fontOf = (id: string) =>
    (s.layers.find((l) => l.id === id) as { layout: Record<string, unknown> })
      .layout["text-font"];
  // S-4 B-412.1 sets soundings in sloping numerals; B-412.4 reserves upright for soundings of
  // lower reliability, so nothing else on the chart borrows the italic face.
  expect(fontOf("soundings")).toEqual(["Noto Sans Italic"]);
  expect(fontOf("contour-labels")).toEqual(["Noto Sans Regular"]);
  expect(fontOf("source-labels")).toEqual(["Noto Sans Regular"]);
});

test("the safety contour is the one emphasized isobath", () => {
  const lines = (opts: Parameters<typeof layers>[1]) =>
    layers(day, opts).find((l) => l.id === "contour-lines") as {
      paint: Record<string, unknown>;
    };
  // 4 m snaps up to the charted 5 m level, matched by the integer depth prop.
  const metric = lines({ safety: 4 });
  expect(metric.paint["line-width"]).toEqual([
    "case",
    ["==", ["get", "depth_abs_m"], 5],
    1.5,
    0.8,
  ]);
  expect(JSON.stringify(metric.paint["line-color"])).toContain(
    day.contourEmphasis,
  );
  // Fathom mode matches on the fathom prop: 4 m snaps to the 3 fm curve.
  expect(lines({ safety: 4, unit: "fm" }).paint["line-width"]).toEqual([
    "case",
    ["==", ["get", "depth_fm"], 3],
    1.5,
    0.8,
  ]);
  // safety 0 turns the emphasis off entirely.
  expect(lines({ safety: 0 }).paint["line-width"]).toBe(0.8);
});

test("hillshade option controls the depth-hillshade visibility", () => {
  const vis = (opts?: Parameters<typeof layers>[1]) =>
    (layers(day, opts).find((l) => l.id === "depth-hillshade") as {
      layout: Record<string, unknown>;
    }).layout.visibility;
  expect(vis()).toBe("visible");
  expect(vis({ hillshade: false })).toBe("none");
});
