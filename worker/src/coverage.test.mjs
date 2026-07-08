// Run: node src/coverage.test.mjs   (Node ≥22.18 strips the imported .ts)
import assert from "node:assert/strict";
import { coverageTileJSON } from "./coverage.ts";

// Shape with a real header (what /coverage.json serves for a coverage-bearing release).
const h = {
  minZoom: 0,
  maxZoom: 8,
  minLon: -180,
  minLat: -66,
  maxLon: 180,
  maxLat: 84,
};
const tj = coverageTileJSON(h, "https://tiles.example/seascape", "credit");
assert.equal(tj.tilejson, "3.0.0");
assert.deepEqual(tj.tiles, [
  "https://tiles.example/seascape/coverage/{z}/{x}/{y}.pbf",
]);
assert.equal(tj.minzoom, 0);
assert.equal(tj.maxzoom, 8);
assert.deepEqual(tj.bounds, [-180, -66, 180, 84]);
assert.equal(tj.vector_layers.length, 1);
assert.equal(tj.vector_layers[0].id, "coverage");
assert.deepEqual(Object.keys(tj.vector_layers[0].fields).sort(), [
  "source_id",
  "source_maxzoom",
  "source_name",
]);
assert.equal(tj.attribution, "credit");

// Absent coverage.pmtiles (a pre-coverage release under a new Worker) degrades
// to a valid empty document — same tiles URL (its requests 204), never a throw.
const empty = coverageTileJSON(null, "http://localhost:8787", "");
assert.equal(empty.minzoom, 0);
assert.equal(empty.maxzoom, 0);
assert.equal(empty.tiles[0], "http://localhost:8787/coverage/{z}/{x}/{y}.pbf");
assert.equal(empty.bounds.length, 4);

console.log("coverage.ts ok — TileJSON shape, absent-archive degradation");
