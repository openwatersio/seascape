// Run: node src/vector.test.mjs   (Node ≥22.18 strips the imported .ts)
//
// Exercises the tippecanoe-overzoom port in vector.ts against hand-crafted
// fixture tiles: line/point/polygon overzoom, edge-adjacent children at large Δz,
// the multi-parent merge (neighbour sources leafed at differing depths), tile-edge
// polygons with buffer, the depare partition staying pairwise-disjoint and
// seam-exact through synthesis AND through the leaf clean/simplify pass, the
// buffer-arithmetic invariant, and property-TYPE / id / layer-name / rank
// preservation through the wire.
import assert from "node:assert/strict";
import {
  decodeTile,
  encodeTile,
  synthesizeLayers,
  featureCount,
  serveMaxZoom,
  POINT,
  LINE,
  POLYGON,
} from "./vector.ts";

// ── fixture builders (decoded representation: op-lists, polygon rings closed) ──
const M = 1,
  L = 2;
function polyFeature(id, rings, properties) {
  const ops = [];
  for (const ring of rings) {
    const closed =
      ring[0][0] === ring[ring.length - 1][0] && ring[0][1] === ring[ring.length - 1][1]
        ? ring
        : [...ring, ring[0]];
    for (let i = 0; i < closed.length; i++) ops.push({ op: i ? L : M, x: closed[i][0], y: closed[i][1] });
  }
  return { id, type: POLYGON, properties, ops };
}
function lineFeature(id, pts, properties) {
  return { id, type: LINE, properties, ops: pts.map((p, i) => ({ op: i ? L : M, x: p[0], y: p[1] })) };
}
function pointFeature(id, x, y, properties) {
  return { id, type: POINT, properties, ops: [{ op: M, x, y }] };
}
const layer = (name, features, extent = 4096, version = 2) => ({ name, version, extent, features });
const srcTile = (z, x, y, layers) => ({ z, x, y, layers });
function bbox(f) {
  let mnx = Infinity,
    mny = Infinity,
    mxx = -Infinity,
    mxy = -Infinity;
  for (const p of f.ops) {
    mnx = Math.min(mnx, p.x);
    mny = Math.min(mny, p.y);
    mxx = Math.max(mxx, p.x);
    mxy = Math.max(mxy, p.y);
  }
  return { mnx, mny, mxx, mxy };
}
const hasVertexX = (f, x) => f.ops.some((p) => p.x === x);
const FULL = [
  [0, 0],
  [4096, 0],
  [4096, 4096],
  [0, 4096],
];

// ── A: basic polygon overzoom — a full parent tile fills the child + buffer ────
{
  const parent = srcTile(10, 0, 0, [layer("depare", [polyFeature(1, [FULL], { drval1: 0, drval2: 5, sys: "m", rank: 3 })])]);
  const out = synthesizeLayers([parent], 12, 0, 0); // top-left quarter, Δz=2
  assert.equal(out.length, 1);
  assert.equal(out[0].name, "depare");
  assert.equal(out[0].extent, 4096);
  assert.equal(out[0].features.length, 1);
  const f = out[0].features[0];
  assert.equal(f.id, 1, "feature id preserved");
  assert.equal(f.type, POLYGON);
  // the child covers its whole extent plus the 80-unit (4096*5/256) buffer margin
  assert.deepEqual(bbox(f), { mnx: 0, mny: 0, mxx: 4176, mxy: 4176 });
  // property types survive (numeric stays numeric, string stays string)
  assert.equal(f.properties.drval1, 0);
  assert.equal(typeof f.properties.drval1, "number");
  assert.equal(typeof f.properties.rank, "number");
  assert.equal(f.properties.sys, "m");
  assert.equal(typeof f.properties.sys, "string");
}
console.log("vector.ts A ok — polygon overzoom: full tile → child + buffer, id/types/rank preserved");

// ── B: edge-adjacent children at large Δz partition the parent ────────────────
{
  const parent = srcTile(10, 0, 0, [layer("depare", [polyFeature(1, [FULL], { rank: 1 })])]);
  // z15 from z10 is Δz=5 — the "deepest intended zoom has no baked leaf" case the
  // manifest vector.max_zoom governs; synthesis still produces a correct tile.
  const px = 7,
    py = 11; // an arbitrary z15 child under (10,0,0): 7>>5==0, 11>>5==0
  const deep = synthesizeLayers([parent], 15, px, py);
  assert.equal(featureCount(deep), 1, "deep (Δz=5) child still has the band");
  // an interior child buffers on all four sides → full extent + buffer both ways
  assert.deepEqual(bbox(deep[0].features[0]), { mnx: -80, mny: -80, mxx: 4176, mxy: 4176 });
  // all four z11 children each cover their FULL extent (buffer clamps to the parent
  // boundary on the outward sides) → together they tile the parent with no gap
  for (const [cx, cy] of [
    [0, 0],
    [1, 0],
    [0, 1],
    [1, 1],
  ]) {
    const b = bbox(synthesizeLayers([parent], 11, cx, cy)[0].features[0]);
    assert.ok(b.mnx <= 0 && b.mny <= 0 && b.mxx >= 4096 && b.mxy >= 4096, `child ${cx},${cy} covers its full extent`);
  }
}
console.log("vector.ts B ok — edge-adjacent children, large Δz (no-leaf zoom) synthesize correctly");

// ── C: point overzoom clips to the child that contains it ─────────────────────
{
  const parent = srcTile(10, 0, 0, [layer("soundings", [pointFeature(9, 2560, 2560, { depth_m: 12.5 })])]);
  // parent fraction 2560/4096 = 0.625 → z12 child index 2; local = (0.625*4-2)*4096 = 2048
  const inChild = synthesizeLayers([parent], 12, 2, 2);
  assert.equal(featureCount(inChild), 1);
  assert.deepEqual(bbox(inChild[0].features[0]), { mnx: 2048, mny: 2048, mxx: 2048, mxy: 2048 });
  assert.equal(inChild[0].features[0].properties.depth_m, 12.5, "float property kept as float");
  // the top-left child does not contain the point (clipped away → empty layer set)
  const outChild = synthesizeLayers([parent], 12, 0, 0);
  assert.equal(featureCount(outChild), 0, "point clipped out of the non-containing child");
}
console.log("vector.ts C ok — point overzoom: kept in its child, clipped from others");

// ── D: depare partition — pairwise-disjoint & seam-exact, synthesis + leaf pass ─
{
  // two bands sharing a seam at parent x=1536 (off every power-of-2 child edge, so
  // a child straddles it)
  const A = polyFeature(1, [[[0, 0], [1536, 0], [1536, 4096], [0, 4096]]], { drval1: 0, drval2: 5, rank: 1 });
  const B = polyFeature(2, [[[1536, 0], [4096, 0], [4096, 4096], [1536, 4096]]], { drval1: 5, drval2: 10, rank: 2 });
  const parent = srcTile(10, 0, 0, [layer("depare", [A, B])]);

  // synthesis: z11 child (0,0) straddles the seam; both bands survive, meeting at x=3072
  const child = synthesizeLayers([parent], 11, 0, 0);
  assert.equal(child[0].features.length, 2, "both bands present in the straddling child");
  const [ca, cb] = child[0].features[0].properties.rank === 1 ? child[0].features : [child[0].features[1], child[0].features[0]];
  assert.equal(ca.properties.rank, 1);
  assert.equal(cb.properties.rank, 2, "rank preserved (orders the fills)");
  const ba = bbox(ca),
    bb = bbox(cb);
  assert.equal(ba.mxx, 3072, "band A right edge at the seam");
  assert.equal(bb.mnx, 3072, "band B left edge at the seam");
  assert.ok(hasVertexX(ca, 3072) && hasVertexX(cb, 3072), "seam vertices coincide (seam-exact)");
  assert.ok(ba.mxx <= bb.mnx, "interiors disjoint — they meet only at the seam");
  assert.equal(ba.mnx, 0);
  assert.equal(bb.mxx, 4176);

  // leaf clean/simplify pass: synthesize the parent at its OWN z/x/y (Δz=0). The
  // partition must survive cleaning too — seam now at parent-scale x=1536.
  const leaf = synthesizeLayers([parent], 10, 0, 0);
  assert.equal(leaf[0].features.length, 2, "both bands survive the leaf clean pass");
  const [la, lb] = leaf[0].features[0].properties.rank === 1 ? leaf[0].features : [leaf[0].features[1], leaf[0].features[0]];
  assert.equal(bbox(la).mxx, 1536, "band A right edge at seam (leaf)");
  assert.equal(bbox(lb).mnx, 1536, "band B left edge at seam (leaf)");
  assert.ok(hasVertexX(la, 1536) && hasVertexX(lb, 1536), "leaf seam vertices coincide");
}
console.log("vector.ts D ok — depare partition stays disjoint + seam-exact through synthesis and the leaf pass");

// ── E: tile-edge polygon + buffer arithmetic invariant ────────────────────────
{
  // a parent whose baked geometry fills its buffer too: [-80, 4176]^2
  const withBuffer = [
    [-80, -80],
    [4176, -80],
    [4176, 4176],
    [-80, 4176],
  ];
  const parent = srcTile(10, 0, 0, [layer("depare", [polyFeature(1, [withBuffer], { rank: 0 })])]);
  // rightmost z12 child: its own right buffer reaches into the parent's baked
  // buffer, and the SINGLE parent feeds it (no neighbour tile needed)
  const c = synthesizeLayers([parent], 12, 3, 0);
  const b = bbox(c[0].features[0]);
  assert.equal(b.mxx, 4176, "child right buffer fed from the parent's baked buffer");
  assert.equal(b.mnx, -80, "child left buffer present");

  // the invariant itself: child buffer need in world units = b/2^Δz ≤ parent's b.
  const bWorld = (z) => Math.trunc((5 * 2 ** (32 - z)) / 256);
  for (let pz = 8; pz <= 14; pz++)
    for (let dz = 0; dz <= 6; dz++) {
      const nz = pz + dz;
      assert.ok(bWorld(nz) <= bWorld(pz), `buffer invariant b(${nz}) ≤ b(${pz})`);
      assert.equal(bWorld(nz), bWorld(pz) >> dz || bWorld(nz), "child buffer = parent buffer / 2^Δz");
    }
}
console.log("vector.ts E ok — tile-edge buffer fed by single parent; b/2^Δz ⊆ b invariant holds");

// ── F: multi-parent merge — sources leafed at differing depths ────────────────
{
  // child (12,1,0) has both (10,0,0) and (11,0,0) as ancestors: 1>>2==0, 1>>1==0.
  const s1 = srcTile(10, 0, 0, [layer("depare", [polyFeature(1, [FULL], { rank: 1 })])]);
  const s2 = srcTile(11, 0, 0, [
    layer("depare", [polyFeature(2, [FULL], { rank: 2 })]),
    layer("soundings", [pointFeature(3, 3072, 100, { depth_m: 7 })]),
  ]);
  const out = synthesizeLayers([s1, s2], 12, 1, 0);
  const depare = out.find((l) => l.name === "depare");
  const soundings = out.find((l) => l.name === "soundings");
  assert.ok(depare && soundings, "both layer names present");
  const ids = depare.features.map((f) => f.id).sort();
  assert.deepEqual(ids, [1, 2], "depare merges features from both parents (differing depths)");
  assert.equal(soundings.features.length, 1, "soundings from the deeper parent contributes");
}
console.log("vector.ts F ok — multi-parent merge across sources at differing depths");

// ── G: property TYPES / id / layer name survive the wire (encode → decode) ─────
{
  const parent = srcTile(10, 0, 0, [
    layer("contours", [
      lineFeature(42, [[0, 512], [4096, 512]], { depth_m: 10, depth_ft: 32.8, sys: "m", label: "10 m" }),
    ]),
  ]);
  // round-trip the parent through the wire, then synthesize a child and re-encode
  const parentBytes = encodeTile(synthesizeLayers([parent], 10, 0, 0));
  const decoded = decodeTile(parentBytes);
  const childLayers = synthesizeLayers([{ z: 10, x: 0, y: 0, layers: decoded }], 12, 0, 0);
  const childBytes = encodeTile(childLayers);
  const child = decodeTile(childBytes);
  assert.equal(child.length, 1);
  assert.equal(child[0].name, "contours", "layer name preserved through the wire");
  const f = child[0].features[0];
  assert.equal(f.id, 42, "feature id preserved through the wire");
  assert.equal(f.type, LINE);
  assert.equal(f.properties.depth_m, 10);
  assert.equal(typeof f.properties.depth_m, "number", "integer property stays numeric");
  assert.equal(f.properties.depth_ft, 32.8);
  assert.equal(typeof f.properties.depth_ft, "number", "float property stays numeric (not stringified)");
  assert.equal(typeof f.properties.sys, "string");
  assert.equal(f.properties.label, "10 m");
}
console.log("vector.ts G ok — layer name, feature id, and property types survive encode/decode");

// ── H: line overzoom clips and scales ─────────────────────────────────────────
{
  // horizontal line across the parent at y=1024 (top quarter band)
  const parent = srcTile(10, 0, 0, [layer("contours", [lineFeature(5, [[0, 1024], [4096, 1024]], { depth_m: 3 })])]);
  const child = synthesizeLayers([parent], 12, 0, 0); // top-left quarter: parent [0,1024]×[0,1024]
  assert.equal(featureCount(child), 1, "line present in the child it crosses");
  const f = child[0].features[0];
  assert.equal(f.type, LINE);
  // parent y=1024 is the bottom edge of the top-left quarter → child y=4096; the
  // line spans the full child width (0..4096) plus buffer clipping to ±80
  const b = bbox(f);
  assert.equal(b.mny, 4096);
  assert.equal(b.mxy, 4096, "line stays horizontal at child y=4096");
  assert.ok(b.mnx <= 0 && b.mxx >= 4096, "line spans the child width");
  // a child the line does not reach (bottom quarter) has no line
  const below = synthesizeLayers([parent], 12, 0, 3);
  assert.equal(featureCount(below), 0, "line absent where it doesn't reach");
}
console.log("vector.ts H ok — line overzoom: clip + scale, present only where it crosses");

// ── I: serveMaxZoom — manifest wins, header is the fallback ───────────────────
{
  assert.equal(serveMaxZoom(16, 14), 16, "manifest covering max_zoom wins over the header leaf depth");
  assert.equal(serveMaxZoom(undefined, 14), 14, "absent manifest value falls back to the header");
  assert.equal(serveMaxZoom(0, 14), 0, "an explicit 0 is honored (not treated as absent)");
}
console.log("vector.ts I ok — serveMaxZoom: manifest wins, header fallback");

console.log("vector.test.mjs: all assertions passed");
