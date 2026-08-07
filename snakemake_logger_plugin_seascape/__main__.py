"""Self-check: uv run python -m snakemake_logger_plugin_seascape --check

Drives the handler with the exact record shapes snakemake emits (snakemake/jobs.py
log_info, scheduling/job_scheduler.py) and asserts the console stays one line per job,
the JSONL carries the structure, and the noise is gone.
"""

import io
import json
import logging
import os
import sys
import tempfile

from snakemake_interface_logger_plugins.common import LogEvent

from snakemake_logger_plugin_seascape import LogHandler, LogHandlerSettings, read_benchmark

BENCHMARK = (
    "s\th:m:s\tmax_rss\tmax_vms\tmax_uss\tmax_pss\tio_in\tio_out\tmean_load\tcpu_time\n"
    "1508.2\t0:25:08\t7372.80\t9000.0\t7000.0\t7100.0\t120.0\t80.0\t95.0\t1080.0\n"
)


def record(event=None, msg="", level=logging.INFO, **extra):
    rec = logging.LogRecord("snakemake", level, __file__, 0, msg, None, None)
    if event is not None:
        rec.event = event
    for key, value in extra.items():
        setattr(rec, key, value)
    return rec


def check():
    tmp = tempfile.mkdtemp()
    bench = os.path.join(tmp, "mosaic.tsv")
    with open(bench, "w") as f:
        f.write(BENCHMARK)
    events = os.path.join(tmp, "events.jsonl")

    handler = LogHandler(
        common_settings=None,
        settings=LogHandlerSettings(events=events, starts=True, status_interval=0),
    )
    handler.stream = io.StringIO()

    # RUN_INFO's job-stats table is where the DAG size comes from; PROGRESS only lands
    # after the first job finishes.
    handler.emit(record(LogEvent.RUN_INFO, msg="Job stats:\njob    count\nmosaic_tile  113\ntotal  113\n"))
    assert handler.total == 113, handler.total

    handler.emit(
        record(
            LogEvent.JOB_INFO,
            jobid=3603,
            rule_name="mosaic_tile",
            wildcards={"stem": "8-70-105-15"},
            resources={"mem_gb": 9, "disk_mb": 26624, "tmpdir": "/tmp"},
            benchmark=bench,
            log=["/app/tmp/logs/mosaic/8-70-105-15.log"],
            reason="Updated input files: store/source/cudem/catalog.json",
            input=["a.csv", "b.json"],
            output=["store/mosaic/tiles/8-70-105-15.tif"],
        )
    )
    # Noise snakemake emits between JOB_INFO and JOB_FINISHED — none of it may print.
    handler.emit(record(msg="Select jobs to execute..."))
    handler.emit(record(LogEvent.JOB_STARTED, msg="Execute 16 jobs...", jobs=[3603]))
    # Snakemake's order: JOB_FINISHED, then PROGRESS. The finish line must already count
    # itself rather than lag a job behind.
    handler.emit(record(LogEvent.JOB_FINISHED, msg="", job_id=3603))
    handler.emit(record(LogEvent.PROGRESS, done=1, total=113))

    out = handler.stream.getvalue()
    lines = [line for line in out.splitlines() if line.strip()]
    # The job-stats table passes through; everything between the two job lines is dropped.
    assert "Select jobs" not in out and "Execute 16 jobs" not in out, out
    start, finish = lines[-2:]
    assert "▸ mosaic_tile" in start and "8-70-105-15" in start, start
    assert "mem=9G" in start and "disk=26G" in start, start
    for want in ("✓ mosaic_tile", "8-70-105-15", "rss=7.2G", "cpu=18m00s", "[1/113, 0%, 0 running]"):
        assert want in finish, f"missing {want!r} in {finish!r}"

    # A failure names its rule, its wildcards, and where to look.
    handler.emit(
        record(
            LogEvent.JOB_ERROR,
            level=logging.ERROR,
            jobid=42,
            rule_name="depare_tile",
            wildcards={"stem": "9-1-2-12"},
            log=["/app/tmp/logs/depare/9-1-2-12.log"],
            shellcmd="depare_run.py tile 9-1-2-12",
        )
    )
    failure = handler.stream.getvalue().splitlines()[-1]
    assert "✗ depare_tile 9-1-2-12" in failure and "depare/9-1-2-12.log" in failure, failure

    # Snakemake re-reports every failure in its exit summary, without the wildcards.
    before = len(handler.stream.getvalue().splitlines())
    handler.emit(record(LogEvent.JOB_ERROR, level=logging.ERROR, jobid=42, rule_name="depare_tile"))
    assert len(handler.stream.getvalue().splitlines()) == before, "failure reported twice"

    # Warnings reach the console and the JSONL both; a plain info line only the console.
    handler.emit(record(msg="WARNING: only 12G free", level=logging.WARNING))
    assert "only 12G free" in handler.stream.getvalue()
    handler.emit(record(msg="Nothing to be done (all requested files are present)."))
    assert "Nothing to be done" in handler.stream.getvalue()

    status = handler.status_line()
    assert "1/113" in status and "1 failed" in status, status

    # Snakemake closes its handlers and logging.shutdown() closes them again.
    handler.close()
    handler.close()
    assert handler.stream.getvalue().count("build summary") == 1, "rollup printed twice"
    assert "mosaic_tile" in handler.stream.getvalue()

    with open(events) as f:
        rows = [json.loads(line) for line in f]
    kinds = [row["event"] for row in rows]
    assert kinds == ["job_started", "job_finished", "job_error", "message"], kinds
    assert rows[0]["reason"].startswith("Updated input files"), rows[0]
    assert rows[1]["benchmark"]["max_rss"] == "7372.80", rows[1]
    assert rows[1]["duration"] >= 0, rows[1]

    # No measurement is better than a fabricated one: a missing file, an empty file, and
    # the header-with-no-row a killed job leaves behind all read as "nothing recorded".
    assert read_benchmark(os.path.join(tmp, "nope.tsv")) == {}
    for content in ("", BENCHMARK.splitlines()[0] + "\n", "s\th:m:s\n0.5\n"):
        partial = os.path.join(tmp, "partial.tsv")
        with open(partial, "w") as f:
            f.write(content)
        assert read_benchmark(partial) == {}, content

    print("snakemake_logger_plugin_seascape: ok")


if __name__ == "__main__":
    if "--check" not in sys.argv:
        sys.exit("usage: python -m snakemake_logger_plugin_seascape --check")
    check()
