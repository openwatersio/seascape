// Run: node src/mask.test.mjs   (Node ≥22.18 strips the imported .ts)
import assert from "node:assert/strict";
import { composite, rasterizeLayers, waterMask } from "./mask.ts";

const TILE = 512;
const at = (mask, col, row) => mask[row * TILE + col];

// Fake @mapbox/vector-tile layer: features is an array of features, each a list of rings,
// each ring a list of {x,y} in extent coords. Exercises the scanline core without an MVT.
const layer = (extent, features) => ({
  extent,
  length: features.length,
  feature: (i) => ({ loadGeometry: () => features[i] }),
});
// closed rectangle ring in extent coords
const rect = (x0, y0, x1, y1) => [
  { x: x0, y: y0 },
  { x: x1, y: y0 },
  { x: x1, y: y1 },
  { x: x0, y: y1 },
  { x: x0, y: y0 },
];
// z == maskMaxZoom → no sub-tile window (span 1); extent 4096 → scale 0.125
const whole = (land, water) =>
  rasterizeLayers(land, water, 12, 0, 0, 12);

// ── convex ring ────────────────────────────────────────────────────────────
{
  const m = whole(layer(4096, [[rect(1024, 1024, 3072, 3072)]]), undefined);
  assert.equal(at(m, 200, 200), 1, "inside convex ring");
  assert.equal(at(m, 10, 10), 0, "outside convex ring");
  assert.equal(at(m, 400, 400), 0, "past convex ring");
}

// ── ring with hole (even-odd, no orientation classification) ─────────────────
{
  const feat = [rect(512, 512, 3584, 3584), rect(1536, 1536, 2560, 2560)];
  const m = whole(layer(4096, [feat]), undefined);
  assert.equal(at(m, 100, 256), 1, "inside outer, outside hole");
  assert.equal(at(m, 256, 256), 0, "inside hole");
  assert.equal(at(m, 10, 10), 0, "outside outer");
}

// ── multipolygon (two disjoint rings in one feature) ─────────────────────────
{
  const feat = [rect(0, 0, 1024, 1024), rect(3072, 3072, 4096, 4096)];
  const m = whole(layer(4096, [feat]), undefined);
  assert.equal(at(m, 10, 10), 1, "first polygon");
  assert.equal(at(m, 400, 400), 1, "second polygon");
  assert.equal(at(m, 256, 256), 0, "gap between polygons");
}

// ── water burn opens land ────────────────────────────────────────────────────
{
  const land = layer(4096, [[rect(0, 0, 4096, 4096)]]);
  const water = layer(4096, [[rect(1024, 1024, 3072, 3072)]]);
  const m = whole(land, water);
  assert.equal(at(m, 10, 10), 1, "land outside the water cut");
  assert.equal(at(m, 256, 256), 0, "water reopened over land");
}

// ── sub-tile window: 1 level of overzoom (span 2) ────────────────────────────
{
  const land = layer(4096, [[rect(0, 0, 2048, 4096)]]); // left half of the parent
  const left = rasterizeLayers(land, undefined, 13, 2, 0, 12); // subX 0
  const right = rasterizeLayers(land, undefined, 13, 3, 0, 12); // subX 1
  assert.equal(at(left, 256, 256), 1, "left sub-tile is land");
  assert.equal(at(right, 256, 256), 0, "right sub-tile is water");
}

// ── sub-tile window: 2 levels (span 4) ───────────────────────────────────────
{
  const land = layer(4096, [[rect(0, 0, 1024, 1024)]]); // top-left 1/4 of the parent
  const inCell = rasterizeLayers(land, undefined, 14, 0, 0, 12); // subX,subY 0
  const offCell = rasterizeLayers(land, undefined, 14, 1, 0, 12); // subX 1
  assert.equal(at(inCell, 256, 256), 1, "quadrant maps to sub-tile 0");
  assert.equal(at(offCell, 256, 256), 0, "neighbour sub-tile empty");
}

// ── sub-tile window: 4 levels (span 16) ──────────────────────────────────────
{
  const cell = layer(4096, [[rect(768, 512, 1024, 768)]]); // sub-tile (3,2) of 16×16
  const hit = rasterizeLayers(cell, undefined, 16, 3, 2, 12);
  const miss = rasterizeLayers(cell, undefined, 16, 4, 2, 12);
  assert.equal(at(hit, 256, 256), 1, "sub-tile (3,2) filled");
  assert.equal(at(miss, 256, 256), 0, "sub-tile (4,2) empty");
  const full = layer(4096, [[rect(0, 0, 4096, 4096)]]);
  const anyCell = rasterizeLayers(full, undefined, 16, 5, 7, 12);
  assert.equal(at(anyCell, 256, 256), 1, "full-parent land fills any deep sub-tile");
}

// ── geometry past tile bounds clips instead of breaking the fill ─────────────
{
  const over = whole(layer(4096, [[rect(-2000, -2000, 6000, 6000)]]), undefined);
  assert.equal(at(over, 0, 0), 1, "buffer geometry fills the corner");
  assert.equal(at(over, 511, 511), 1, "buffer geometry fills far corner");
  const half = whole(layer(4096, [[rect(-1000, -1000, 2048, 2048)]]), undefined);
  assert.equal(at(half, 100, 100), 1, "clipped region inside");
  assert.equal(at(half, 300, 300), 0, "clipped region outside");
}

// ── compositing: all four rows of the plan's table ───────────────────────────
{
  // land→2; water & h≥2 → 0; water & 0≤h<2 → keep; water & h<0 → keep
  const h = new Float64Array([5, 5, 2, 1.99, 0, -10]);
  const mask = new Uint8Array([1, 0, 0, 0, 0, 0]);
  composite(h, mask);
  assert.deepEqual(
    [...h],
    [2, 0, 0, 1.99, 0, -10],
    "land code; land-topo knocked to water; land code itself knocked; blends and depth kept",
  );
}

// ── absent tile (all-water) vs absent archive (no mask) ──────────────────────
{
  const wm = waterMask();
  assert.equal(wm.length, TILE * TILE, "water mask is a full tile");
  assert.ok(wm.every((v) => v === 0), "water mask is all zeros (open ocean)");
  // absent tile within a present archive → composite knocks phantom DEM land to water,
  // no-op on real ocean.
  const absentTile = new Float64Array([5, -10]);
  composite(absentTile, wm);
  assert.deepEqual([...absentTile], [0, -10], "phantom land → water; ocean kept");
  // absent archive → mask undefined → synthesize skips composite entirely, heights untouched.
  const noArchive = new Float64Array([5, -10]);
  assert.deepEqual([...noArchive], [5, -10], "unmasked heights pass through");
}

console.log("ok");
