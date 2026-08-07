# Dispatching and monitoring planet builds

The two failure classes this runbook exists to prevent: dispatching a run whose actual DAG is wildly bigger than intended (a staleness-trigger surprise burning box money), and a dispatched run going unwatched until someone notices it stuck or failed.

## Before dispatching: know your DAG size

Snakemake job identity is **params + inputs + code**, and the planner is the only authority on what a dispatch will run. Predicting scope by reasoning about "what changed" fails in practice; these are the traps that have each caused a full 16k-job planet rebuild:

- **Params are identity.** Adding, removing, or renaming a `params:` entry re-keys every job of that rule — including restoring a param that was previously removed. The value changing is not required.
- **Renames cascade.** Renaming a rule's output (`.geojson` → `.geojsons`) makes every instance "missing output", and every downstream consumer stale behind it.
- **`temp()` regeneration cascades to siblings.** A `temp()` intermediate (the fork window) is deleted after use. If ANY consumer goes stale, the producer reruns, and snakemake then reruns **every** consumer of the regenerated file — including ones whose outputs are current. `ancient()` does NOT prevent this: it only suppresses the on-disk mtime comparison, not the "input files updated by another job" rule that fires when the producer is scheduled in the same DAG (verified by minimal test).
- **A banked output is only banked if its whole input closure is stable.** "contour/depare don't rerun" requires that nothing upstream of them runs either.
- **bbox ↔ planet flips re-key `cover`.** The bbox is a param of the covering checkpoint; switching between a regional and planet dispatch replans the covering.
- **Every smoothing dial must live in `SMOOTH_CFG`.** A dial missing from that hash leaves windows fresh and silently re-measures the old surface.

**The gate:** every dispatch states its expected DAG size in the `max_jobs` workflow input. The build then dry-runs the exact invocation first and aborts — printing the job-stats table and a reason census — if the planner disagrees. Rough sizes for calibration: full planet rebuild ≈ 16,400; one stage across the covering ≈ 3,300; vector tail (cells + shallow + join + bundles + stage) ≈ 3,300; incremental after a code-only change ≈ single digits.

`max_jobs` scopes the **wide** phase (`prebundle`) — everything above except the serial finish. The wide phase tolerates a zero plan, because a change that only touches the bundle rules legitimately leaves it nothing to do. The `build-tail` phase states no number: its size moves with the overlay-cell count, so it is gated on rule *identity* instead — the dry run aborts if the plan names any rule outside the tail set. `cover` is deliberately not in that set: a tail plan that reschedules the checkpoint hides its real size behind it, which is exactly the case to refuse.

To predict scope before dispatching, dry-run locally against a representative store (the bbox root in `pipelines/`, with the same `BBOX` its provenance records) and read the job stats **and** the `reason:` lines per rule — the reasons, not the counts, tell you whether the plan matches your intent.

## Rebuilding a leaf product without the cascade

To rebuild one stage-3 product (e.g. all soundings) without re-paying its `temp()` window and everything downstream of the window, split the dispatch in two:

1. **Phase 1:** dispatch with `targets: soundings_all` (an aggregate rule whose closure is exactly the product files + their windows — sibling window consumers are never in the plan). Windows are re-made, consumed, and temp-deleted. Set `max_jobs` to the expected count (~2 jobs per covering stem).
2. **Phase 2:** a normal dispatch. Planned only after phase 1 finished, its DAG never schedules the window producer, so nothing cascades. Verified: the phase-2 planner reports the sibling products current.

One dispatch at a time — planning phase 2 while phase 1 runs would see its half-finished state.

**Do not use `--until` for this.** It works on checkpoint-free DAGs but prunes to **zero jobs** under the `cover` checkpoint (reproduced on the real store: the same dry-run plans the full set without `--until` and `total 0` with it) — the run reports "Nothing to be done" and silently does nothing. An isolation target also keeps the covering-derived priority functions working, which bare file targets do not. The scope gate's zero-plan check catches this class.

## Monitoring a dispatched run

Arm all of this at dispatch time, not when something looks wrong:

- **`scripts/watch-build`** — resolves the box from the `build` commit status and tails the build container's log (filtered to progress + failure signatures; `-f` for the full stream).
- **The `build` commit status** is the heartbeat: `root@<ip> · <phase> N of M steps (P%), ~R running`, updated every minute. `M` is the live check that the planned DAG matches `max_jobs` intent. The status re-seeds at `booting (tail)` on a new IP when the wide phase hands off, and only the last phase flips it green. A status that stops updating means the run died or the heartbeat did — either way, look.
- The status may briefly show the **previous run's last heartbeat** (same commit, same context) — trust it only once it has changed after your dispatch.
- Watch for the run's terminal states, not just successes: a watcher that only matches the happy path is silent through a crash.

When a run fails or must be canceled: **pull evidence first** — the job-stats table, the `reason:` census, per-rule logs and benchmarks from `/var/tmp/seascape-tmp` (each phase has its own box and ships its own `snakemake-bench-<run-id>-<phase>` artifact, but the box copy dies with the box), and whatever the failing rule wrote — the box teardown destroys everything not on the store volume.
