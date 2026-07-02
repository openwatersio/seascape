// Run: node src/overlay.test.mjs   (Node ≥22.18 strips the imported .ts)
import assert from "node:assert/strict";
import { overlayFor } from "./overlay.ts";

const ov = {
  split_z: 5,
  cells: { "5-9-11": 14, "5-15-15": 11 },
};

// A deep tile routes to its z5 ancestor's archive: z14 (4919, 6087) >> 9 = (9, 11).
assert.deepEqual(overlayFor(ov, 14, 4919, 6087), {
  file: "overlay-5-9-11.pmtiles",
  maxZoom: 14,
});
// The shallowest overlay zoom routes the same way: z9 (287, 380) >> 4 = (17, 23) → miss…
assert.equal(overlayFor(ov, 9, 287, 380), null);
// …while z9 (144, 176) >> 4 = (9, 11) lands in the populated cell.
assert.deepEqual(overlayFor(ov, 9, 144, 176), {
  file: "overlay-5-9-11.pmtiles",
  maxZoom: 14,
});
// Every z14 descendant of an unpopulated cell misses (planet overzoom fallback).
assert.equal(overlayFor(ov, 14, 0, 0), null);
// A tile deeper than its cell's max still resolves (the caller overzooms within the cell).
assert.deepEqual(overlayFor(ov, 13, 15 << 8, 15 << 8), {
  file: "overlay-5-15-15.pmtiles",
  maxZoom: 11,
});
// Cell corners: the LAST tile under (5,9,11) at z14 is ((9+1)<<9 - 1, (11+1)<<9 - 1)…
assert.equal(overlayFor(ov, 14, (10 << 9) - 1, (12 << 9) - 1).file, "overlay-5-9-11.pmtiles");
// …and one step east is the next cell over (unpopulated → null).
assert.equal(overlayFor(ov, 14, 10 << 9, (12 << 9) - 1), null);

console.log("overlay.ts ok — cell routing, misses, corners, over-max resolution");
