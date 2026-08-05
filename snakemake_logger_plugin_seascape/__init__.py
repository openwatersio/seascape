"""Snakemake logger plugin: one line per job event, plus a JSONL event stream.

Snakemake's default handler prints ~16 scaffolding lines per job (a field block per
JOB_INFO, a bare timestamp line per event, `Select jobs to execute...`/`Execute N
jobs...` per scheduling round). At planet scale that is ~200k lines, past what the
GitHub Actions UI serves, and it still omits the numbers that decide whether a build
is healthy: per-job wall time and peak RSS, and how many jobs are actually in flight.

`--quiet` cannot express this: `rules` also suppresses JOB_ERROR, `progress` also
suppresses the step counter the CI heartbeat reads, and neither level touches
`Select jobs to execute...` (a plain logger.info with no event) or `Execute N jobs...`
(JOB_STARTED, absent from snakemake's quietness map). Replacing the handler is the
only lever.

Declaring writes_to_stream makes snakemake skip its own stream handler entirely
(snakemake/logging.py: _setup_plugins), so this is a replacement, not a filter.
"""

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from logging import LogRecord
from typing import Any, Optional

from snakemake_interface_logger_plugins.base import LogHandlerBase
from snakemake_interface_logger_plugins.common import LogEvent
from snakemake_interface_logger_plugins.settings import LogHandlerSettingsBase

# Wall seconds between `──` status lines. Long enough that an hour-long build prints a
# dozen, short enough to notice a stall.
STATUS_INTERVAL = 300

# Rule name column; wide enough for the longest rule (terrain_planet_bundle).
RULE_WIDTH = 21
# Wildcard column; a stem is `8-70-105-15`, a cell `8-70-105`.
WILDCARD_WIDTH = 14

# Rules the status line reports individually; more than this and the tail is summarized.
STATUS_RULE_LIMIT = 4


# Field annotations must stay real type objects — snakemake's plugin registry hands each
# one to argparse, so `from __future__ import annotations` here breaks the CLI.
@dataclass
class LogHandlerSettings(LogHandlerSettingsBase):
    events: Optional[str] = field(
        default=None,
        metadata={
            "help": "Write the JSONL event stream to this path (default: no JSONL).",
            "env_var": True,
        },
    )
    starts: bool = field(
        default=False,
        metadata={
            "help": "Also print a line when each job starts, not only when it finishes.",
            "env_var": True,
        },
    )
    status_interval: int = field(
        default=STATUS_INTERVAL,
        metadata={"help": "Seconds between periodic status lines (0 disables).", "env_var": True},
    )


def _hms(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _gb(value: Any, per_gb: float = 1) -> Optional[str]:
    """A snakemake resource value -> a whole-gigabyte `26G`. Reservations are coarse by
    nature, so decimals here only add width. Non-numeric resources are skipped."""
    try:
        n = float(value) / per_gb
    except (TypeError, ValueError):
        return None
    return f"{n:.0f}G"


def _mb_to_gb(value: str) -> Optional[str]:
    """A benchmark TSV megabyte field -> `7.2G`. Sub-0.1G reads as unmeasured, not as a fact."""
    try:
        gb = float(value) / 1024
    except (TypeError, ValueError):
        return None
    return f"{gb:.1f}G" if gb >= 0.1 else None


def _seconds(value: str) -> Optional[float]:
    """A benchmark TSV seconds field. Benchmarks write `-` for jobs too short to sample."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 1 else None


def read_benchmark(path: str) -> dict[str, str]:
    """The header/value pair a snakemake `benchmark:` TSV holds for a finished job.

    The job process flushes this before exiting, and JOB_FINISHED fires after the
    scheduler has handled success, so it is on disk by the time this is called.
    Anything unreadable (no benchmark: directive, a killed job) yields {}.
    """
    try:
        with open(path) as f:
            header = f.readline().strip().split("\t")
            values = f.readline().strip().split("\t")
    except OSError:
        return {}
    return dict(zip(header, values))


def format_wildcards(wildcards: Any) -> str:
    if not wildcards:
        return ""
    if isinstance(wildcards, dict):
        return ",".join(str(v) for v in wildcards.values())
    return str(wildcards)


class Job:
    __slots__ = ("jobid", "rule", "wildcards", "started", "resources", "benchmark", "log")

    def __init__(self, record: LogRecord) -> None:
        self.jobid = getattr(record, "jobid", None)
        self.rule = getattr(record, "rule_name", "?")
        self.wildcards = format_wildcards(getattr(record, "wildcards", None))
        self.resources = dict(getattr(record, "resources", {}) or {})
        self.benchmark = getattr(record, "benchmark", None)
        self.log = list(getattr(record, "log", None) or [])
        self.started = time.monotonic()

    @property
    def label(self) -> str:
        return f"{self.rule:<{RULE_WIDTH}} {self.wildcards:<{WILDCARD_WIDTH}}"

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started


class LogHandler(LogHandlerBase):
    settings: LogHandlerSettings

    def __post_init__(self) -> None:
        # Snakemake sets stdout when the caller needs stderr kept clean.
        self.stream = sys.stdout if getattr(self.common_settings, "stdout", False) else sys.stderr
        self.printshellcmds = getattr(self.common_settings, "printshellcmds", False)
        # In a dry run the per-job line IS the output — that's the plan being inspected.
        self.starts = getattr(self.settings, "starts", False) or getattr(
            self.common_settings, "dryrun", False
        )
        self.running: dict[Any, Job] = {}
        self.done = 0
        self.total = 0
        self.failures: list[str] = []
        self.failed_ids: set[Any] = set()
        # Per-rule (count, total_seconds, max_seconds) for the end-of-run rollup.
        self.stats: dict[str, list[float]] = {}
        self.started = time.monotonic()
        self.last_status = self.started
        self.closed = False
        self.events = None
        path = getattr(self.settings, "events", None) if self.settings else None
        if path:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            self.events = open(path, "a", buffering=1)

    @property
    def writes_to_stream(self) -> bool:
        return True

    @property
    def writes_to_file(self) -> bool:
        return False

    @property
    def has_filter(self) -> bool:
        return True

    @property
    def has_formatter(self) -> bool:
        return True

    @property
    def needs_rulegraph(self) -> bool:
        return False

    def emit(self, record: LogRecord) -> None:
        try:
            # logging.Handler's own RLock, reused deliberately: logging.shutdown() holds it
            # across close(), so a non-reentrant lock of our own deadlocks at exit.
            with self.lock:
                self._emit(record)
        except (KeyboardInterrupt, SystemExit, BrokenPipeError):
            raise
        except Exception:  # a logging handler must never take the build down
            self.handleError(record)

    def _emit(self, record: LogRecord) -> None:
        event = getattr(record, "event", None)
        handler = {
            LogEvent.JOB_INFO: self._job_info,
            LogEvent.JOB_FINISHED: self._job_finished,
            LogEvent.JOB_ERROR: self._job_error,
            LogEvent.PROGRESS: self._progress,
            LogEvent.RUN_INFO: self._passthrough,
            LogEvent.WORKFLOW_STARTED: self._workflow_started,
        }.get(event, self._other)
        handler(record)
        self._maybe_status()

    # Everything snakemake says that isn't a job event passes through — dropping by default
    # would hide "Nothing to be done", the dry-run plan, and anything added upstream later.
    # Only the two content-free scheduler lines are filtered.
    def _other(self, record: LogRecord) -> None:
        event = getattr(record, "event", None)
        # `Execute N jobs...` — the count is already on every job line.
        if event == LogEvent.JOB_STARTED:
            return
        # has_filter means snakemake attaches no filter of its own, so -p is ours to honor.
        if event == LogEvent.SHELLCMD:
            cmd = getattr(record, "cmd", None)
            # Rules with no shell (a target rule) still emit the event, with cmd None.
            if self.printshellcmds and cmd:
                self._write(cmd)
            return
        message = record.getMessage()
        if not message or message == "Select jobs to execute...":
            return
        self._write(message)
        if record.levelno >= 30 or event in (LogEvent.ERROR, LogEvent.GROUP_ERROR):
            self._event("message", level=record.levelname, msg=message)

    def _passthrough(self, record: LogRecord) -> None:
        message = record.getMessage()
        if not message:
            return
        # RUN_INFO carries the job-stats table; its `total` row is the only place the DAG
        # size appears before the first PROGRESS event, which lands after job one finishes.
        match = re.search(r"^total\s+(\d+)\s*$", message, re.MULTILINE)
        if match and not self.total:
            self.total = int(match.group(1))
        # Raw, not timestamped: the job-stats table is a block, and build.yml's scope gate
        # matches `^Job stats` in it.
        self.stream.write(message + "\n")
        self.stream.flush()

    def _workflow_started(self, record: LogRecord) -> None:
        """The default banner is 16 lines; provenance fits on one, and the rest is in JSONL."""
        version = getattr(record, "snakemake_version", "?")
        workflow_id = str(getattr(record, "workflow_id", ""))
        self._write(f"snakemake {version} · run {workflow_id} · {getattr(record, 'cmd', '')}")
        self._event(
            "workflow_started",
            workflow_id=workflow_id,
            snakemake_version=version,
            cmd=getattr(record, "cmd", None),
            config_md5=getattr(record, "config_md5", None),
        )

    def _job_info(self, record: LogRecord) -> None:
        job = Job(record)
        if job.jobid is None:
            return
        self.running[job.jobid] = job
        self._event(
            "job_started",
            jobid=job.jobid,
            rule=job.rule,
            wildcards=job.wildcards,
            resources={k: str(v) for k, v in job.resources.items()},
            reason=getattr(record, "reason", None),
            input=list(getattr(record, "input", None) or []),
            output=list(getattr(record, "output", None) or []),
        )
        if self.starts:
            self._write(f"▸ {job.label} {self._reservation(job)}".rstrip())

    def _job_finished(self, record: LogRecord) -> None:
        job = self.running.pop(getattr(record, "job_id", None), None)
        if job is None:
            return
        elapsed = job.elapsed
        bench = read_benchmark(job.benchmark) if job.benchmark else {}

        # PROGRESS lands after this event, so counting here keeps the line self-consistent.
        self.done += 1
        stat = self.stats.setdefault(job.rule, [0, 0.0, 0.0])
        stat[0] += 1
        stat[1] += elapsed
        stat[2] = max(stat[2], elapsed)

        parts = [f"✓ {job.label} {_hms(elapsed):>7}"]
        rss = _mb_to_gb(bench.get("max_rss", ""))
        if rss:
            parts.append(f"rss={rss}")
        cpu = _seconds(bench.get("cpu_time", ""))
        if cpu:
            parts.append(f"cpu={_hms(cpu)}")
        parts.append(self._counter())
        self._write("  ".join(parts))
        self._event(
            "job_finished",
            jobid=job.jobid,
            rule=job.rule,
            wildcards=job.wildcards,
            duration=round(elapsed, 1),
            benchmark=bench,
        )

    def _job_error(self, record: LogRecord) -> None:
        jobid = getattr(record, "jobid", None)
        # Snakemake reports each failure twice — once when it happens, once in the exit
        # summary (which no longer carries the wildcards). Keep the first, richer report.
        if jobid in self.failed_ids:
            return
        self.failed_ids.add(jobid)
        job = self.running.pop(jobid, None)
        rule = getattr(record, "rule_name", job.rule if job else "?")
        wildcards = format_wildcards(getattr(record, "wildcards", None)) or (
            job.wildcards if job else ""
        )
        logs = list(getattr(record, "log", None) or (job.log if job else []))
        label = f"{rule} {wildcards}".strip()
        self.failures.append(label)
        self._write(f"✗ {label}  jobid={jobid}" + (f"  log={logs[0]}" if logs else ""))
        self._event(
            "job_error",
            jobid=jobid,
            rule=rule,
            wildcards=wildcards,
            log=logs,
            shellcmd=getattr(record, "shellcmd", None),
        )

    def _progress(self, record: LogRecord) -> None:
        self.done = max(self.done, getattr(record, "done", 0) or 0)
        self.total = getattr(record, "total", self.total) or self.total

    def _reservation(self, job: Job) -> str:
        """The two resources that decide admission — the rest of the dict is scheduler noise."""
        out = []
        for label, key, per_gb in (("mem", "mem_gb", 1), ("disk", "disk_mb", 1024)):
            text = _gb(job.resources.get(key), per_gb)
            if text:
                out.append(f"{label}={text}")
        return " ".join(out)

    def _counter(self) -> str:
        running = len(self.running)
        pct = f", {self.done * 100 // self.total}%" if self.total else ""
        return f"[{self.done}/{self.total or '?'}{pct}, {running} running]"

    def _maybe_status(self) -> None:
        interval = getattr(self.settings, "status_interval", STATUS_INTERVAL) or 0
        if not interval or time.monotonic() - self.last_status < interval:
            return
        self.last_status = time.monotonic()
        self._write(self.status_line())

    def status_line(self) -> str:
        by_rule: dict[str, int] = {}
        for job in self.running.values():
            by_rule[job.rule] = by_rule.get(job.rule, 0) + 1
        ranked = sorted(by_rule.items(), key=lambda kv: -kv[1])
        shown = " ".join(f"{rule}×{n}" for rule, n in ranked[:STATUS_RULE_LIMIT])
        if len(ranked) > STATUS_RULE_LIMIT:
            shown += f" +{len(ranked) - STATUS_RULE_LIMIT} more"
        parts = [
            f"{_hms(time.monotonic() - self.started)} elapsed",
            self._counter().strip("[]"),
        ]
        if shown:
            parts.append(shown)
        if self.running:
            oldest = max(self.running.values(), key=lambda j: j.elapsed)
            parts.append(f"oldest {oldest.rule} {oldest.wildcards} {_hms(oldest.elapsed)}")
        if self.failures:
            parts.append(f"{len(self.failures)} failed")
        return "── " + " · ".join(parts) + " ──"

    def rollup(self) -> list[str]:
        """Per-rule timing summary, printed once at close.

        GitHub truncates the middle of a long log but reliably serves the end, so this
        is the part of a planet build that always survives.
        """
        if not self.stats:
            return []
        lines = [f"── build summary · {_hms(time.monotonic() - self.started)} elapsed ──"]
        for rule, (count, total, worst) in sorted(self.stats.items(), key=lambda kv: -kv[1][1]):
            lines.append(
                f"   {rule:<{RULE_WIDTH}} n={count:<6} "
                f"total={_hms(total):>7}  mean={_hms(total / count):>7}  max={_hms(worst):>7}"
            )
        if self.failures:
            lines.append(f"   failed: {', '.join(self.failures)}")
        return lines

    def _write(self, text: str) -> None:
        self.stream.write(f"{time.strftime('%H:%M:%S')} {text}\n")
        self.stream.flush()

    def _event(self, kind: str, **fields: Any) -> None:
        if self.events is None:
            return
        record = {"t": round(time.time(), 3), "event": kind, **fields}
        self.events.write(json.dumps(record, default=str) + "\n")

    def close(self) -> None:
        # Snakemake closes its handlers, then logging.shutdown() closes them again — the
        # rollup must print once.
        with self.lock:
            if not self.closed:
                self.closed = True
                for line in self.rollup():
                    self.stream.write(line + "\n")
                self.stream.flush()
                if self.events is not None:
                    self.events.close()
                    self.events = None
        super().close()
