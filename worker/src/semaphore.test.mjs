// Run: node src/semaphore.test.mjs   (Node ≥22.18 strips the imported .ts)
import assert from "node:assert/strict";
import { limiter } from "./semaphore.ts";

// Never more than `max` fns run at once, and every fn still completes.
const run = limiter(2);
let active = 0,
  peak = 0,
  done = 0;
const defer = () => {
  let resolve;
  const p = new Promise((r) => (resolve = r));
  return { p, resolve };
};
const gates = Array.from({ length: 6 }, defer);

const tasks = gates.map((g, i) =>
  run(async () => {
    active++;
    peak = Math.max(peak, active);
    await g.p; // hold the slot until released, so overlap is observable
    active--;
    done++;
    return i;
  }),
);

// Release in waves; the limiter must keep ≤2 in flight throughout.
await Promise.resolve();
gates[0].resolve();
gates[1].resolve();
await new Promise((r) => setTimeout(r, 0));
gates[2].resolve();
gates[3].resolve();
gates[4].resolve();
gates[5].resolve();
const out = await Promise.all(tasks);

assert.equal(peak, 2, `peak concurrency ${peak}, want 2`);
assert.equal(done, 6, "all tasks completed");
assert.deepEqual(out, [0, 1, 2, 3, 4, 5]);

// A throwing fn releases its slot (finally), so later work isn't starved.
const run1 = limiter(1);
await assert.rejects(
  run1(async () => {
    throw new Error("boom");
  }),
);
assert.equal(await run1(async () => "ok"), "ok");

console.log("semaphore.test.mjs ok");
