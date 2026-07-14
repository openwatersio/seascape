"""Memory-budget admission for the raster process pools (aggregate + terrain).

The problem it solves: both the aggregate merge and the terrain render hold a multi-GB raster per
worker, and the pools run HEAVIEST-FIRST (deepest child_z), so a fixed-size pool starts its first N
workers on the N densest coastal macrotiles at once — each peaking >12 GB during the multi-source
feather-merge (a z14 macrotile DEM is 32768²×4 B ≈ 4.3 GB at rest, ×~3 holding the merged array +
reprojected sources + masks). No fixed process count survives: safe-for-the-peak wastes cores on
cheap ocean tiles, and core-count OOM-kills on the coast (the exit-137 the two ccx63 builds hit).

The fix decouples the two limits:
  * POOL SIZE = cores  — cheap tiles (weight 1) fill every core.
  * A shared GB BUDGET — each task, inside its worker, reserves weight(tile) GB across its
    memory-peak section (blocking until the budget frees) and releases after, so the concurrent
    heavy-tile peak can never exceed the budget however many cores are busy.

weight(stem) is a deterministic estimate from the tile geometry (below); the budget is a
cross-process counter (a multiprocessing.Manager Value + Condition) the parent creates and hands to
every worker through the Pool initializer — works under both fork (aggregate) and spawn (terrain).

Deadlock-freedom: a worker acquires its FULL weight atomically under the condition lock (decrement
only when avail ≥ weight — never a partial hold-and-wait), and every weight is clamped ≤ budget, so
when the budget is idle any single waiter can always proceed. No two tasks can each hold part and
wait for the rest. Crash-safety: the reservation is released in a try/finally, so a normal exception
frees it. A HARD kill (SIGKILL / OOM) can't run finally and would leak that weight for the run's
lifetime — accepted, because correct budgeting is precisely what prevents OOM-kills; we don't build
elaborate recovery for a case the budget exists to make impossible.

Host-agnostic: the pool size and the budget come from env (the workflow computes them from the box's
RAM); this module reads them but never mentions R2 / CI / any host.
"""

import math
import os
from contextlib import contextmanager

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
# Tune DOWN once the per-tile peak_rss log (run()/_render()) gives real RSS data.
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


# ── the shared budget: a cross-process Value + Condition ─────────────────────────────────────────

# Per-worker globals, set by init_worker (the Pool initializer). None when no budget is configured
# (local / small runs) — then reserve() is a no-op and the pool is a plain core-bound pool.
_COND = None
_AVAIL = None       # Manager Value('i'): GB currently available
_BUDGET_GB = 0
_FACTOR = DEFAULT_FACTOR


def make_budget(budget_gb):
    """Create the shared budget in the PARENT. Returns (manager, initargs) — keep `manager` alive
    for the Pool's lifetime (it owns the server process); pass `initargs` as the Pool initializer's
    args. Returns (None, (None, None, 0, factor)) when budget_gb is 0 (no budget: reserve() no-ops).

    A Manager (not raw multiprocessing primitives) so the proxies pickle cleanly into BOTH a fork
    and a spawn Pool's workers; the per-tile acquire is rare (seconds-to-minutes tiles), so the
    proxy round-trip is negligible next to a merge."""
    factor = float(os.environ.get("AGG_MEM_FACTOR", str(DEFAULT_FACTOR)))
    if not budget_gb:
        return None, (None, None, 0, factor)
    import multiprocessing as mp
    mgr = mp.Manager()
    cond = mgr.Condition()
    avail = mgr.Value("i", budget_gb)
    return mgr, (cond, avail, budget_gb, factor)


def init_worker(cond, avail, budget_gb, factor):
    global _COND, _AVAIL, _BUDGET_GB, _FACTOR
    _COND, _AVAIL, _BUDGET_GB, _FACTOR = cond, avail, budget_gb, factor


@contextmanager
def reserve(stem):
    """Hold weight(stem) GB of the shared budget across the wrapped memory-peak section. Blocks
    until the budget can satisfy the FULL weight (atomic acquire — never a partial hold), then
    releases in a try/finally so a normal exception frees it. No-op when no budget is configured."""
    if _COND is None or not _BUDGET_GB:
        yield
        return
    w = weight(stem, _BUDGET_GB, _FACTOR)
    with _COND:
        while _AVAIL.value < w:
            _COND.wait()
        _AVAIL.value -= w
    try:
        yield
    finally:
        with _COND:
            _AVAIL.value += w
            _COND.notify_all()


def peak_rss_gb():
    """This worker process's peak resident set, in GB. ru_maxrss is KB on Linux (the build box) and
    BYTES on macOS — normalise both. Captures the WHOLE run's peak, so it covers the vector forks'
    own peaks (depare's shapely union + the gdal_contour subprocess) on top of the merge, not just
    the modelled merge — the number the factor should ultimately be tuned against."""
    import resource
    import sys
    ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return ru / 1e9 if sys.platform == "darwin" else ru / 1e6  # macOS: bytes→GB; Linux: KB→GB


_WORKER_PEAK = 0.0  # this worker PROCESS's last-seen ru_maxrss high-water (GB); module global per worker


def log_peak(stem):
    """Log peak RSS for factor tuning. ru_maxrss is the worker PROCESS's MONOTONIC lifetime high-water,
    not this tile's peak — a Pool worker reuses across many tiles, so a light tile would otherwise
    inherit an earlier heavy tile's number. So log the PID and flag only a tile that DROVE a NEW
    high-water (that tile caused the peak). The max `drove … peak` across all workers is the heaviest
    tile's real peak, correctly attributed — the number to tune the factor against. (Structured
    logging is a separate task.)"""
    global _WORKER_PEAK
    import os
    cz = int(stem.split("-")[3])
    w = weight(stem, _BUDGET_GB, _FACTOR) if _BUDGET_GB else weight(stem)
    rss = peak_rss_gb()
    pid = os.getpid()
    if rss > _WORKER_PEAK + 0.05:
        print(f"tile {stem} z{cz} weight={w} drove worker[{pid}] peak {rss:.1f}GB "
              f"(was {_WORKER_PEAK:.1f})", flush=True)
        _WORKER_PEAK = rss
    else:
        print(f"tile {stem} z{cz} weight={w} worker[{pid}] peak {rss:.1f}GB", flush=True)


def pool_kwargs(budget_gb):
    """(manager, kwargs) for a Pool: `kwargs` carries initializer/initargs wiring the budget into
    every worker. Spread `**kwargs` into Pool(...); keep `manager` alive until the pool closes."""
    mgr, initargs = make_budget(budget_gb)
    return mgr, {"initializer": init_worker, "initargs": initargs}


# ── self-check ───────────────────────────────────────────────────────────────────────────────────

def _sim_task(args):
    """A synthetic task: reserve the budget for a tile, then (independently of the mechanism) bump a
    shared held-GB counter and record the peak, sleep to force overlap, and release. If the budget
    mechanism is correct the peak held over ALL workers never exceeds the budget."""
    import time
    stem, budget, factor, held, peak, lock = args
    w = weight(stem, budget, factor)
    with reserve(stem):
        with lock:
            held.value += w
            if held.value > peak.value:
                peak.value = held.value
        time.sleep(0.02)
        with lock:
            held.value -= w
    return w


def _check():
    import multiprocessing as mp

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

    # (b) a mixed-weight run through the real budget mechanism, low budget, short sleeps to force
    # overlap: the concurrently-admitted weight must NEVER exceed the budget, and every task must
    # complete (no deadlock — including the clamped over-budget tiles that run alone). Run it under
    # BOTH the default Pool AND an explicit SPAWN context: aggregate uses fork, terrain uses spawn,
    # and the budget's Manager proxies must pickle into a spawn worker via the initializer — a future
    # refactor that breaks that must fail here, not in the migration.
    factor = DEFAULT_FACTOR
    # a spread of cheap (cz=8 → 1), medium (cz=13 → ~4) and over-budget (cz=14 → clamp 8) tiles
    stems = ([f"{z}-{i}-0-8" for i in range(20)]
             + [f"{z}-{i}-0-13" for i in range(8)]
             + [f"{z}-{i}-0-14" for i in range(3)])

    def _run_sim(ctx, label):
        mgr = ctx.Manager()
        held = mgr.Value("i", 0)
        peak = mgr.Value("i", 0)
        lock = mgr.Lock()
        _mgr2, kwargs = pool_kwargs(budget)
        tasks = [(s, budget, factor, held, peak, lock) for s in stems]
        with ctx.Pool(4, **kwargs) as pool:
            results = list(pool.imap_unordered(_sim_task, tasks, chunksize=1))
        assert len(results) == len(tasks), f"[{label}] all tasks must complete, got {len(results)}/{len(tasks)}"
        assert peak.value <= budget, f"[{label}] concurrent held weight {peak.value} exceeded budget {budget}"
        assert peak.value >= budget - 1, f"[{label}] expected the budget to fill (peak {peak.value})"
        mgr.shutdown()
        _mgr2.shutdown()

    _run_sim(mp, "default-context")
    _run_sim(mp.get_context("spawn"), "spawn-context")
    print("scheduler.py self-check ok")


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["--check"]:
        _check()
    else:
        sys.exit("scheduler.py is a library; run with --check for the self-check")
