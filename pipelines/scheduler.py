"""Memory-budget admission for the raster process pools (aggregate + terrain).

The problem it solves: both the aggregate merge and the terrain render hold a multi-GB raster per
worker, and the pools run HEAVIEST-FIRST (deepest child_z), so a fixed-size pool starts its first N
workers on the N densest coastal macrotiles at once — each peaking >12 GB during the multi-source
feather-merge (a z14 macrotile DEM is 32768²×4 B ≈ 4.3 GB at rest, ×~3 holding the merged array +
reprojected sources + masks). No fixed process count survives: safe-for-the-peak wastes cores on
cheap ocean tiles, and core-count OOM-kills on the coast (the exit-137 the two ccx63 builds hit).

The fix decouples the two limits:
  * POOL SIZE = cores  — cheap tiles (weight 1) fill every core.
  * A shared GB BUDGET — enforced in the PARENT, at dispatch time (map_budgeted): a task enters
    the pool only once its weight(tile) GB fits, and the weight is released when it completes, so
    the concurrent heavy-tile peak can never exceed the budget however many cores are busy.

Admission is parent-side because a Pool worker dequeues a task BEFORE the task can wait on
anything: with worker-side reservations (the first design), every worker could end up holding a
blocked heavy tile while cheap tiles sat undequeued behind them — the pool starved regardless of
queue order (heaviest-first put 40 of 48 workers to sleep; the md5 shuffle merely spread the same
collapse across the whole run, run 29371657768). Parent-side, a tile that doesn't fit waits in a
list, never in a worker.

weight(stem) is a deterministic estimate from the tile geometry (below). Dispatch policy: heaviest
admissible tile first (the backlog of dense coastal tiles is the phase's critical path), light
tiles backfill while heavies wait, and lane_floor keeps heavies from draining the budget below
what a light tile needs. Deadlock-freedom is trivial: one thread admits, weights are clamped ≤
budget and lane_floor ≤ budget − weight, so on an idle budget the heaviest pending tile always
fits. Crash-safety: a task exception releases its weight via error_callback and re-raises after
in-flight work drains. A HARD kill (SIGKILL / OOM) loses the task and its callback — the pool
itself cannot complete then (Pool semantics, budget or not); accepted, because correct budgeting
is precisely what prevents OOM-kills.

Host-agnostic: the pool size and the budget come from env (the workflow computes them from the box's
RAM); this module reads them but never mentions R2 / CI / any host.
"""

import math
import os

# ── weight: the estimated peak GB of a tile, from its geometry ───────────────────────────────────

# The halo (buffer) each read carries for smoothing continuity is a small additive px term next to
# the 2**(child_z-macrotile_z)*512 core; a single constant is close enough for both pools (the
# aggregate merge buffer and the terrain smooth halo are both ~O(64 px)). The estimate only needs to
# ORDER and BOUND tiles, not be exact.
HALO_PX = 64

# Peak-over-rest multiplier: a merge/render holds roughly the merged array + reprojected sources +
# masks at once, plus the vector forks' own peaks (depare's shapely union + gdal_contour subprocess
# can rival the merge). CONSERVATIVE default 4, pending measurement:
#   factor 3 → weight(z14)=13 → floor(152/13)=11 concurrent heavies, but the real per-tile peak is
#     only known to be >12 GB (a LOWER bound), so 11×(>13) swap-thrashes the hot merge arrays.
#   factor 4 → weight(z14)=18 → floor(152/18)=8 concurrent heavies → 8×18 + 40 reserve = 184/192,
#     fits in RAM with ZERO swap even if the real peak is 18 GB. Trades some heavy-tile concurrency
#     (other cores still saturate on light tiles) for guaranteed-in-RAM completion — an OOM under a
#     marginal budget is worse than a clean exit-137 (it leaks the reservation AND can hang the pool).
# Tune DOWN once the per-tile peak_rss log (run()/_render()) gives real RSS data. Run 29371657768
# measured z14 SERIAL-fork peaks of median 12.4 / max 14.4 GB against weight 18 — but the vector
# forks now run concurrently (aggregation_run.run), which changes the peak shape, so that data no
# longer prices this factor. Re-fit from the first parallel-fork run's log_peak lines (the env
# knob AGG_MEM_FACTOR needs no code change).
DEFAULT_FACTOR = float(os.environ.get("AGG_MEM_FACTOR", "4"))


def weight(stem, budget_gb=0, factor=DEFAULT_FACTOR):
    """Estimated peak GB for a tile `z-x-y-cz` (z = macrotile_z, cz = child_z), rounded up to a
    whole GB and floored at 1. Monotonic in child_z (each level quadruples the pixel area). When a
    budget is given, the weight is CLAMPED to it (with a warning): a tile heavier than the whole
    budget must still be admittable alone — running it one-at-a-time beats deadlocking on a
    reservation the budget can never satisfy."""
    z, _x, _y, cz = (int(a) for a in stem.split("-"))
    side_px = (2 ** (cz - z)) * 512 + 2 * HALO_PX
    base_gb = side_px * side_px * 4 / 1e9
    w = max(1, math.ceil(base_gb * factor))
    if budget_gb and w > budget_gb:
        print(f"scheduler: tile {stem} weight {w} GB exceeds budget {budget_gb} GB — clamping to "
              f"{budget_gb} (runs alone; better one-at-a-time than deadlock)", flush=True)
        w = budget_gb
    return w


# The cheap lane: heavy tiles may never drain the budget below this floor, so weight-1/2 tiles
# always have GB to draw on and the spare cores stay busy. Without it, heavies can consume the
# budget to the last GB — 8 × weight-18 z14 tiles == the whole 144 GB budget — and light tiles
# stall behind them (run 29371657768's 6 h at cpu 17/48 was this plus worker-side blocking).
# The floor shrinks to keep any single heavy admissible on an idle budget (deadlock-freedom:
# effective floor ≤ budget − w always, so the heaviest pending tile always fits eventually;
# clamped tiles still run alone).
CHEAP_LANE_GB = 16
CHEAP_MAX_W = 2  # weights ≤ this are "cheap": they ignore the floor and only need their own GB


def lane_floor(w, budget_gb):
    """GB a task of weight w must leave unclaimed. 0 for cheap tiles; for heavies, the cheap
    lane — shrunk so the task itself stays admissible on an idle budget (never > budget − w)."""
    if w <= CHEAP_MAX_W:
        return 0
    return max(0, min(CHEAP_LANE_GB, budget_gb - w))


def map_budgeted(pool, fn, items, budget_gb, procs, stem_of=None, on_done=None):
    """Run fn(item) for every item across `pool`, admitting work in THIS process before dispatch
    (see the module docstring for why worker-side blocking starves the pool).

    Heaviest admissible first: items are weighed and sorted here, so callers need no particular
    order. In-flight is capped at `procs` so reservations mirror what can actually run. On a task
    exception, dispatching stops, in-flight tasks drain, and the first error re-raises (pool.map
    semantics). budget_gb 0 = no admission control (local / small runs): every item is admissible,
    through the SAME dispatch loop — one code path, and on_done keeps one contract.
    `stem_of(item)` extracts the `z-x-y-cz` stem weight() reads (default: item IS the stem).
    `on_done(item)` (optional) fires in the parent per successful completion with the ITEM (never
    fn's return value), serialized on the pool's result-handler thread — progress reporting."""
    stem_of = stem_of or (lambda it: it)
    import threading
    # Weigh once (weight() warns on clamp — don't re-warn every scan), heaviest first; the sort
    # key is the weight alone so items never need to be comparable. Unbudgeted: weights are moot
    # (everything is admissible) — skip them so weight() never warns, and keep the given order.
    if budget_gb:
        pending = sorted([(weight(stem_of(it), budget_gb), it) for it in items],
                         key=lambda p: -p[0])
    else:
        pending = [(0, it) for it in items]
    cond = threading.Condition()
    state = {"avail": budget_gb, "inflight": 0, "err": None}

    def _release(w, item, err=None):
        # Runs in the pool's single result-handler thread — on_done calls are serialized.
        with cond:
            state["avail"] += w
            state["inflight"] -= 1
            if err is not None and state["err"] is None:
                state["err"] = err
            cond.notify_all()
        if err is None and on_done:
            on_done(item)

    with cond:
        while pending and state["err"] is None:
            picked = None
            if state["inflight"] < procs:
                # Reserve the cheap lane only while cheap work is actually waiting. Once the
                # cheap queue is drained, continuing to reserve 16 GB serializes an all-heavy
                # tail for no beneficiary (four weight-5 regional tiles ran one-at-a-time on a
                # 23 GB budget in run 29578999167).
                cheap_pending = any(pw <= CHEAP_MAX_W for pw, _ in pending)
                # First fit in a heaviest-first list = heaviest admissible; light tiles pass a
                # full-of-heavies budget via the cheap lane (lane_floor 0). Unbudgeted: the first
                # pending item always fits (w == 0).
                for i, (w, it) in enumerate(pending):
                    floor = lane_floor(w, budget_gb) if cheap_pending else 0
                    if not budget_gb or state["avail"] - w >= floor:
                        picked = i
                        break
            if picked is None:
                cond.wait()
                continue
            w, it = pending.pop(picked)
            state["avail"] -= w
            state["inflight"] += 1
            pool.apply_async(fn, (it,),
                             callback=lambda _r, w=w, it=it: _release(w, it),
                             error_callback=lambda e, w=w, it=it: _release(w, it, e))
        while state["inflight"]:
            cond.wait()
    if state["err"] is not None:
        raise state["err"]


def peak_rss_gb():
    """(self_gb, children_gb): this worker process's peak resident set AND its reaped subprocesses'
    (gdal_contour — RUSAGE_SELF is blind to children, which hid multi-GB per tile from two runs'
    factor tuning). ru_maxrss is KB on Linux (the build box) and BYTES on macOS — normalise both.
    self covers the whole run incl. the in-process forks (depare's shapely union); self+children is
    the conservative per-tile footprint the factor is tuned against (they can peak concurrently)."""
    import resource
    import sys
    div = 1e9 if sys.platform == "darwin" else 1e6  # macOS: bytes→GB; Linux: KB→GB
    return (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / div,
            resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / div)


_WORKER_PEAK = 0.0  # this worker PROCESS's last-seen ru_maxrss high-water (GB); module global per worker


def log_peak(stem, sources=None):
    """Log peak RSS for factor tuning. ru_maxrss is the worker PROCESS's MONOTONIC lifetime high-water,
    not this tile's peak — a Pool worker reuses across many tiles, so a light tile would otherwise
    inherit an earlier heavy tile's number. So log the PID and flag only a tile that DROVE a NEW
    high-water (that tile caused the peak). The max `drove … peak` across all workers is the heaviest
    tile's real peak, correctly attributed — the number to tune the factor against.

    `sources` (the tile's overlapping source-file count, from the covering CSV) is the density signal
    the geometry weight can't see: at one child_z the peak spans ~11× (a 1-source ocean tile vs a
    ~50-source coastal one), so logging (sources, peak) pairs turns the density-aware weight from a
    guess into a fit. Omitted (None) where the caller doesn't have the covering handy (terrain).
    (Structured logging is a separate task.)"""
    global _WORKER_PEAK
    import os
    cz = int(stem.split("-")[3])
    w = weight(stem)  # unclamped estimate — display only; admission happens in the parent
    rss, child = peak_rss_gb()
    pid = os.getpid()
    src = f" src={sources}" if sources is not None else ""
    ch = f" child={child:.1f}GB" if child >= 0.05 else ""
    if rss > _WORKER_PEAK + 0.05:
        print(f"tile {stem} z{cz} weight={w}{src} drove worker[{pid}] peak {rss:.1f}GB{ch} "
              f"(was {_WORKER_PEAK:.1f})", flush=True)
        _WORKER_PEAK = rss
    else:
        print(f"tile {stem} z{cz} weight={w}{src} worker[{pid}] peak {rss:.1f}GB{ch}", flush=True)


def pool_kwargs():
    """kwargs for a Pool (spread `**pool_kwargs()` into Pool(...)).

    maxtasksperchild recycles each worker after N tiles so its multi-GB peak is RETURNED to the OS.
    glibc keeps freed arenas otherwise, so a long-lived worker's RSS ratchets up to its high-water and
    stays there — the pool then OOMs even though the budget correctly bounds concurrent RESERVATIONS
    (the reserved weight is released, but the memory isn't). A fresh worker per tile makes the actual
    RSS match what the budget models. Respawn is ~free (fork) or a re-import (spawn), negligible vs a
    minutes-long tile. POOL_MAXTASKSPERCHILD unset/1 = fresh per tile; 0 = never recycle (old)."""
    maxtasks = int(os.environ.get("POOL_MAXTASKSPERCHILD", "1")) or None
    return {"maxtasksperchild": maxtasks} if maxtasks is not None else {}


# ── self-check ───────────────────────────────────────────────────────────────────────────────────

def _sim_task(args):
    """A synthetic task: bump a shared held-GB counter by this tile's weight, record the peak,
    sleep (heavies longer, so light tasks can only finish early if admission lets them through),
    release, and log (weight, completion time). The ADMISSION happens in the parent
    (map_budgeted) — this task just proves what actually ran concurrently."""
    import time
    stem, budget, held, peak, lock, log, t0 = args
    w = weight(stem, budget)
    with lock:
        held.value += w
        if held.value > peak.value:
            peak.value = held.value
    time.sleep(0.3 if w > CHEAP_MAX_W else 0.03)
    with lock:
        held.value -= w
    log.append((w, time.monotonic() - t0))
    return stem


def _noop(_item):
    """Self-check worker for the unbudgeted path — returns None so a broken on_done contract
    (passing fn's return instead of the item) fails loudly."""
    return None


def _check():
    import multiprocessing as mp
    import time

    # (a) weight is monotonic in child_z and clamped ≤ budget.
    z = 8
    ws = [weight(f"{z}-0-0-{cz}") for cz in range(z, z + 7)]
    assert ws == sorted(ws), f"weight must be monotonic in child_z, got {ws}"
    assert ws[0] == 1, f"a macrotile-zoom ocean tile must weigh 1 GB, got {ws[0]}"
    assert weight(f"{z}-0-0-14") >= 12, "a z14 coastal macrotile must weigh ~13 GB"
    budget = 8
    for cz in range(z, z + 8):
        w = weight(f"{z}-0-0-{cz}", budget)
        assert 1 <= w <= budget, f"clamped weight must stay in [1, budget], got {w} for cz={cz}"
    assert weight(f"{z}-0-0-20", budget) == budget, "an over-budget tile must clamp to the budget"

    # (a2) the cheap lane: heavies leave the floor, cheap tiles ignore it, and the floor never
    # makes a task inadmissible on an idle budget (lane_floor ≤ budget − w).
    assert lane_floor(1, 160) == 0 and lane_floor(2, 160) == 0, "cheap tiles must ignore the floor"
    assert lane_floor(18, 160) == CHEAP_LANE_GB, "a z14 heavy must leave the full cheap lane"
    assert lane_floor(150, 160) == 10, "the floor must shrink to keep a huge tile admissible"
    assert lane_floor(160, 160) == 0, "a budget-sized (clamped) tile must still run alone"
    for w_, b_ in ((1, 4), (3, 4), (18, 160), (5, 8)):
        assert lane_floor(w_, b_) + w_ <= b_ or w_ <= CHEAP_MAX_W, f"floor+w must fit budget ({w_},{b_})"

    # (b) the ADVERSARIAL mix through the real mechanism (map_budgeted on a real Pool), heavies
    # listed FIRST — the exact shape that starved the worker-side design (workers dequeued blocked
    # heavies before any cheap task; run 29371657768). Asserts, per context (fork = aggregate,
    # spawn = terrain):
    #   * every task completes (no deadlock, clamped tiles included)
    #   * the concurrently-held weight never exceeds the budget
    #   * the budget actually fills (heavy + backfilled cheap)
    #   * ANTI-STARVATION: heavies serialize on this tiny budget (~8 × 0.3 s back-to-back), so if
    #     admission is fair ALL cheap tasks finish while heavies are still draining — assert the
    #     slowest cheap task beats the MEDIAN heavy completion (~4 × 0.3 s ≈ 10× margin; the
    #     worker-side design fails this by construction: cheap tasks couldn't start until the
    #     heavies ahead of them in the queue had run).
    stems = ([f"{z}-{i}-0-13" for i in range(6)]        # heavy: w=5, one at a time under floor 3
             + [f"{z}-{i}-0-14" for i in range(2)]      # over-budget: clamp 8, runs alone
             + [f"{z}-{i}-0-8" for i in range(20)])     # cheap: w=1, must flow around the heavies

    def _run_sim(ctx, label):
        mgr = ctx.Manager()
        held = mgr.Value("i", 0)
        peak = mgr.Value("i", 0)
        lock = mgr.Lock()
        log = mgr.list()
        t0 = time.monotonic()
        tasks = [(s, budget, held, peak, lock, log, t0) for s in stems]
        with ctx.Pool(4, **pool_kwargs()) as pool:
            map_budgeted(pool, _sim_task, tasks, budget, 4, stem_of=lambda t: t[0])
        entries = list(log)
        assert len(entries) == len(tasks), f"[{label}] all tasks must complete, got {len(entries)}/{len(tasks)}"
        assert peak.value <= budget, f"[{label}] concurrent held weight {peak.value} exceeded budget {budget}"
        assert peak.value >= budget - 1, f"[{label}] expected the budget to fill (peak {peak.value})"
        cheap_done = [t for w, t in entries if w <= CHEAP_MAX_W]
        heavy_done = sorted(t for w, t in entries if w > CHEAP_MAX_W)
        assert max(cheap_done) < heavy_done[len(heavy_done) // 2], (
            f"[{label}] cheap tasks starved behind heavies: slowest cheap {max(cheap_done):.2f}s vs "
            f"median heavy {heavy_done[len(heavy_done) // 2]:.2f}s")
        mgr.shutdown()

    _run_sim(mp, "default-context")
    _run_sim(mp.get_context("spawn"), "spawn-context")

    # Heavy-only work must use the budget once there is no cheap task waiting for the reserved
    # lane. Four z13 weight-5 tiles fit concurrently in a 23 GB small-box budget.
    heavy_only = [f"{z}-{i}-0-13" for i in range(4)]
    mgr = mp.Manager()
    held, peak, lock, log = mgr.Value("i", 0), mgr.Value("i", 0), mgr.Lock(), mgr.list()
    t0 = time.monotonic()
    tasks = [(s, 23, held, peak, lock, log, t0) for s in heavy_only]
    with mp.Pool(4, **pool_kwargs()) as pool:
        map_budgeted(pool, _sim_task, tasks, 23, 4, stem_of=lambda t: t[0])
    assert peak.value == 20, f"heavy-only tail should fill 20/23 GB, got {peak.value}"
    mgr.shutdown()

    # (c) the UNBUDGETED path shares the dispatch loop and the on_done contract: every item
    # completes, and on_done receives the ITEMS (never fn's return value — _noop returns None).
    seen = []
    with mp.Pool(4) as pool:
        map_budgeted(pool, _noop, [f"{z}-{i}-0-8" for i in range(9)], 0, 4,
                     on_done=seen.append)
    assert sorted(seen) == sorted(f"{z}-{i}-0-8" for i in range(9)), (
        f"unbudgeted on_done must receive every ITEM, got {seen}")
    print("scheduler.py self-check ok")


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["--check"]:
        _check()
    else:
        sys.exit("scheduler.py is a library; run with --check for the self-check")
