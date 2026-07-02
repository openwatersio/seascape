// Run: node src/terrarium.test.mjs   (Node ≥22.18 strips the imported .ts)
import assert from "node:assert/strict";
import {
  unpackTerrarium,
  packTerrariumInto,
  cubicBSpline,
} from "./terrarium.ts";

// cubicBSpline: non-negative weights that sum to 1 (a convex blend), so it keeps a flat
// region flat and reproduces a linear ramp exactly — only curvature gets smoothed. It's
// approximating, so it does NOT pass through the samples (that's the deliberate blur).
assert.equal(cubicBSpline(5, 5, 5, 5, 0.37), 5); // constant stays constant
assert.ok(Math.abs(cubicBSpline(0, 1, 2, 3, 0.5) - 1.5) < 1e-12); // linear → linear
assert.ok(Math.abs(cubicBSpline(0, 1, 2, 3, 0) - 1) < 1e-12); // linear exact at the knot too

// Non-negative weights ⇒ every output stays within the min/max of its 4 taps ⇒ it can
// never overshoot ⇒ no halo. Sweep a sharp shelf edge and a bump and assert the bound.
for (let t = 0; t <= 1; t += 0.05) {
  const edge = cubicBSpline(-100, -20, -20, -20, t); // deep → flat shallow shelf
  assert.ok(edge >= -100 - 1e-9 && edge <= -20 + 1e-9, `no overshoot at shelf edge t=${t}`);
  const bump = cubicBSpline(-100, -20, -20, -100, t); // plateau between two drops
  assert.ok(bump <= -20 + 1e-9, `no overshoot above plateau t=${t}`);
}

const buf = new Uint8ClampedArray(4);
const roundtrip = (h) => {
  packTerrariumInto(buf, 0, h);
  return unpackTerrarium(buf[0], buf[1], buf[2]);
};

// pack→unpack recovers the height to within Terrarium's 1/256 m quantum
for (const h of [-10916, -200, -30.5, 0, 0.004, 4321, 8848.86]) {
  assert.ok(Math.abs(roundtrip(h) - h) <= 1 / 256, `roundtrip ${h}`);
}

// bilinear weights that re-encode equal corners must stay flat (no stair-step), and a
// midpoint must land exactly between two depths
const lerp = (a, b, w) => a * (1 - w) + b * w;
assert.equal(roundtrip(lerp(-50, -50, 0.5)), roundtrip(-50)); // flat region stays flat
const mid = roundtrip(lerp(-40, -60, 0.5));
assert.ok(Math.abs(mid - -50) <= 1 / 256, "midpoint interpolates");

// clamp: absurd heights saturate instead of wrapping the RGB
packTerrariumInto(buf, 0, 1e9);
assert.deepEqual([buf[0], buf[1], buf[2]], [255, 255, 255]);
packTerrariumInto(buf, 0, -1e9);
assert.deepEqual([buf[0], buf[1], buf[2]], [0, 0, 0]);

console.log("ok");
