"""Runtime patches to the installed Snakemake, applied at parse time (imported from common.smk).

snakemake 9.23.1 benchmark timers leak psutil monitor threads. Two of the three defects
fire on NORMAL job completion, so at planet scale (~16k benchmarked jobs) leaked DaemonTimers
accumulate until the scheduler starves — throughput decays monotonically (observed: 11 min/1%
early, 131 min/1% at 63%) and the run stalls. Reproduced against run 30116206060.

The defects, all in snakemake/benchmark.py:
  1. ScheduledPeriodicTimer._action reschedules a fresh DaemonTimer unconditionally, never
     checking the stop flag — so a cancel() that lands mid-interval is undone by the very next
     _action fire, making the timer immortal. This is the primary leak (hits the happy path).
  2. benchmarked() calls cancel() after `yield`, not in a finally — a job body that raises
     skips it entirely, leaking the whole timer.
  3. BenchmarkTimer.work swallows NoSuchProcess but _action reschedules regardless, so a timer
     whose observed process has exited keeps firing forever (the "process no longer exists" spam).

Fix, upstreamable: honor the stop flag in _action (set it first in cancel, re-check before the
reschedule), self-terminate when the observed process is gone, and cancel in a finally.
"""

import functools


def apply():
    import snakemake.benchmark as bench

    if getattr(bench, "_openwaters_leak_patched", False):
        return
    bench._openwaters_leak_patched = True

    def _action(self):
        # Honor cancel(): don't re-arm once stopped (defect 1 — the immortal-timer race).
        if self._stopped:
            return
        self.work()
        self._times_called += 1
        if self._stopped:  # work() may have stopped us (observed process gone — defect 3)
            return
        interval = (self._interval if self._times_called > self._interval
                    else bench.BENCHMARK_INTERVAL_SHORT)
        self._timer = bench.DaemonTimer(interval, self._action)
        self._timer.start()

    def cancel(self):
        self._stopped = True  # set BEFORE cancelling so a racing _action sees it and stops
        if self._timer is not None:
            self._timer.cancel()

    bench.ScheduledPeriodicTimer._action = _action
    bench.ScheduledPeriodicTimer.cancel = cancel

    # Self-terminate when the observed process has exited, so an exception in the benchmarked
    # body (which skips cancel — defect 2) can't leave an immortal monitor thread.
    _orig_work = bench.BenchmarkTimer.work

    def work(self):
        if not self.main.is_running():
            self._stopped = True
            return
        _orig_work(self)

    bench.BenchmarkTimer.work = work

    # Belt-and-suspenders for defect 2: cancel even when the job body raises.
    _orig_benchmarked = bench.benchmarked.__wrapped__  # unwrap the @contextmanager

    @functools.wraps(_orig_benchmarked)
    def benchmarked(pid=None, benchmark_record=None, interval=bench.BENCHMARK_INTERVAL):
        import os
        import time
        result = benchmark_record or bench.BenchmarkRecord()
        if pid is False:
            yield result
            return
        start_time = time.time()
        thread = bench.BenchmarkTimer(int(pid or os.getpid()), result, interval)
        thread.start()
        try:
            yield result
        finally:
            thread.cancel()
            result.running_time = time.time() - start_time

    import contextlib
    bench.benchmarked = contextlib.contextmanager(benchmarked)


if __name__ == "__main__":
    # Self-check: the leak is a timer that keeps re-arming after cancel / on a dead process.
    # Assert the patched timer stops re-arming in both cases.
    apply()
    import time
    import snakemake.benchmark as bench

    class Counter(bench.ScheduledPeriodicTimer):
        def __init__(self):
            super().__init__(interval=1)
            self._interval = 1
            self.fires = 0

        def work(self):
            self.fires += 1

    # 1. cancel() must stop re-arming: no fires accrue after cancel.
    t = Counter()
    bench.BENCHMARK_INTERVAL_SHORT = 0.01
    t.start()
    time.sleep(0.05)
    t.cancel()
    fired = t.fires
    time.sleep(0.1)
    assert t.fires == fired, f"timer kept firing after cancel: {fired} -> {t.fires}"
    assert t._stopped and (t._timer is None or not t._timer.is_alive())

    # 2. _action must not re-arm once _stopped is set (the race that made timers immortal).
    t2 = Counter()
    t2._stopped = True
    t2._timer = None
    t2._action()  # must be a no-op, must not create a live timer
    assert t2.fires == 0 and t2._timer is None

    print("snakemake_patches self-check passed")
