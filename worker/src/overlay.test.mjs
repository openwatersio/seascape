// Run: node src/overlay.test.mjs   (Node ≥22.18 strips the imported .ts)
import assert from "node:assert/strict";
import { overlayFor, previewRoute } from "./overlay.ts";

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

// ── previewRoute: leading sha segment → build prefix + rewritten rel/mount ────
const B = "/bathymetry/build";
const sha = "a".repeat(40);
assert.deepEqual(previewRoute(`/${sha}/3/1/2.webp`, B), {
  prefix: `bathymetry/build/${sha}/`,
  rel: "/3/1/2.webp",
  mount: `${B}/${sha}`,
});
// A bare sha strips to "/"; a JSON endpoint keeps its path.
assert.equal(previewRoute(`/${sha}`, B).rel, "/");
assert.equal(previewRoute(`/${sha}/manifest.json`, B).rel, "/manifest.json");
// A 7-char short sha is accepted; the 7-char floor is what stops a bare z/x/y
// path (single-digit zoom) from ever being read as a build id.
assert.equal(
  previewRoute("/abc1234/0/0/0.pbf", B).prefix,
  "bathymetry/build/abc1234/",
);
assert.equal(previewRoute("/3/1/2.webp", B), null); // no sha → 404
assert.equal(previewRoute("/NOTHEX0/0/0/0.pbf", B), null); // non-hex → 404

console.log("previewRoute ok — sha peel, rel/mount rewrite, hex+length guard");
