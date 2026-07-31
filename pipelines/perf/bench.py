#!/usr/bin/env -S uv run --script
"""Run a real pipeline stage against a local fixture and record wall, peak RSS, per-phase timing,
and the domain metrics — so a change can be gated before it costs a production build.

Two peak-RSS numbers are recorded and they mean different things. `rss_tree` samples
self + all descendants, which is the definition snakemake's `max_rss` uses on the build box, so
it is the one comparable to a production row. `rss_time` comes from /usr/bin/time -l, which
reports max(self, max-over-reaped-children) and so reads LOW whenever Python and a GDAL child are
resident together. A large gap between them is itself a finding, not noise.

Snakemake's own benchmark TSVs are not usable locally: its sampler needs USS/PSS, which macOS
denies unprivileged, so every local `max_rss` column is 0.

    bench.py run <stage> <stem> --label <name>     stage: window|depare|contour|soundings
    bench.py compare <labelA> <labelB>
    bench.py show [--label <name>]
    bench.py --check
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# PERF_ROOT selects the store a run reads: the default fixture root, or a root holding real
# production stems (symlinked tiles + a covering listing only what is present) when a
# production-scale number is the point.
ROOT = os.environ.get("PERF_ROOT", f"{REPO}/pipelines/store/profile/root")
RESULTS = f"{REPO}/pipelines/store/profile/results.jsonl"
CONTOUR_P = f"{REPO}/pipelines/store/profile/bin/contour-p"
SAMPLE_S = 0.25

STAGES = {
    "window": ("smooth.py", lambda s: ["prepare-window", s, f"store/window/{s}.tif"],
               lambda s: f"store/window/{s}.tif", "raster"),
    "depare": ("depare_run.py", lambda s: ["tile", s], lambda s: f"store/depare/{s}.fgb", "vector"),
    "contour": ("contour_run.py", lambda s: ["tile", s], lambda s: f"store/contour/{s}.fgb",
                "vector"),
    "soundings": ("soundings_run.py", lambda s: ["tile", s],
                  lambda s: f"store/soundings/{s}.geojson", "vector"),
}


def _sample_rss(proc, out):
    """Peak of (self + descendants) RSS. Same definition as snakemake's max_rss on the box."""
    import psutil
    try:
        p = psutil.Process(proc.pid)
    except Exception:
        return
    peak = 0
    while proc.poll() is None:
        try:
            tot = p.memory_info().rss
            for c in p.children(recursive=True):
                try:
                    tot += c.memory_info().rss
                except Exception:
                    pass
            peak = max(peak, tot)
        except Exception:
            break
        time.sleep(SAMPLE_S)
    out["rss_tree"] = peak


def provenance():
    def sh(*c):
        try:
            return subprocess.run(c, capture_output=True, text=True).stdout.strip()
        except Exception:
            return ""
    return {
        "git_rev": sh("git", "-C", REPO, "rev-parse", "--short", "HEAD"),
        "git_dirty": bool(sh("git", "-C", REPO, "status", "--porcelain")),
        "gdal": sh("gdal-config", "--version"),
        # DEPARE_TIMEOUT must be empty: a timeout silently reroutes through _uniform_coarsen,
        # which is a different algorithm, so a timed-out run is not comparable to a clean one.
        "env": {k: os.environ.get(k, "") for k in
                ("DEPARE_CONTOUR_BIN", "DEPARE_TIMEOUT", "SLIVER_MIN_PX", "NODATA_SIMPLIFY_PX",
                 "LANDMASK", "WATERMASK", "GDAL_CACHEMAX", "SKIP_SMOOTH")},
    }


def run(stage, stem, label):
    import threading
    script, args, artifact, kind = STAGES[stage]
    if os.environ.get("DEPARE_TIMEOUT"):
        sys.exit("refusing to measure with DEPARE_TIMEOUT set — the retry path is a different "
                 "algorithm (see _uniform_coarsen)")
    env = {**os.environ}
    env.setdefault("DEPARE_CONTOUR_BIN", CONTOUR_P)
    env["DEPARE_TIMING"] = "1"
    cmd = ["uv", "run", "--project", REPO, "python", f"{REPO}/pipelines/{script}"] + args(stem)
    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    box = {}
    th = threading.Thread(target=_sample_rss, args=(proc, box), daemon=True)
    th.start()
    out, _ = proc.communicate()
    wall = time.monotonic() - t0
    th.join(timeout=2)
    if proc.returncode != 0:
        print(out)
        sys.exit(f"{stage} {stem} failed (exit {proc.returncode})")
    phases = {}
    for line in out.splitlines():
        if line.startswith("depare-timing "):
            k, _, v = line[len("depare-timing "):].rpartition(": ")
            phases[k] = float(v.rstrip("s"))
    rec = {"label": label, "stage": stage, "stem": stem, "wall_s": round(wall, 2),
           "rss_tree": box.get("rss_tree", 0), "phases": phases,
           "returncode": proc.returncode, **provenance()}
    art = f"{ROOT}/{artifact(stem)}"
    if os.path.exists(art):
        import metrics
        rec["metrics"] = metrics.raster(art) if kind == "raster" else metrics.vector(art)
    os.makedirs(os.path.dirname(RESULTS), exist_ok=True)
    with open(RESULTS, "a") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"{label} {stage} {stem}: {wall:.1f}s  peak_rss_tree {rec['rss_tree']/1e6:.0f} MB")
    if rec.get("metrics"):
        m = rec["metrics"]
        keys = ("parts", "vertices", "fgb_bytes") if kind == "vector" else (
            "crossings@0", "parts@0", "water_components", "largest_share")
        print("  " + "  ".join(f"{k}={m[k]}" for k in keys if k in m))
    if phases:
        top = sorted(phases.items(), key=lambda kv: -kv[1])[:4]
        print("  slowest phases: " + ", ".join(f"{k} {v}s" for k, v in top))
    return rec


def _load():
    if not os.path.exists(RESULTS):
        return []
    return [json.loads(l) for l in open(RESULTS) if l.strip()]


# Regression thresholds: wall/RSS are noisy on a laptop, geometry counts are deterministic.
LIMITS = {"wall_s": 0.10, "rss_tree": 0.10, "parts": 0.01, "vertices": 0.01, "fgb_bytes": 0.01}


def compare(a, b):
    rows = _load()
    idx = {}
    for r in rows:
        idx[(r["label"], r["stage"], r["stem"])] = r  # last write wins
    keys = sorted({(s, t) for (l, s, t) in idx if l in (a, b)})
    bad, missing = [], []
    for stage, stem in keys:
        ra, rb = idx.get((a, stage, stem)), idx.get((b, stage, stem))
        if not ra or not rb:
            missing.append(f"{stage} {stem} (no {a if not ra else b})")
            continue
        print(f"\n{stage} {stem}")
        for k, lim in LIMITS.items():
            va = ra.get(k, ra.get("metrics", {}).get(k))
            vb = rb.get(k, rb.get("metrics", {}).get(k))
            if va is None or vb is None or not va:
                continue
            d = (vb - va) / va
            flag = "  REGRESSED" if d > lim else ""
            if flag:
                bad.append((stage, stem, k))
            print(f"  {k:12s} {va:>14,.0f} -> {vb:>14,.0f}  {d:+7.1%}"
                  f"  ({'x%.2f' % (va / vb) if vb else '-'} better){flag}")
    if missing:
        # Not a regression, but an incomplete comparison is how you fool yourself, so it still
        # fails: re-run the missing pairs before trusting the verdict.
        print("\nincomplete — not compared:\n  " + "\n  ".join(missing))
    if bad:
        print(f"\nFAIL: {len(bad)} regression(s) past threshold: "
              + ", ".join(f"{s}/{t}:{k}" for s, t, k in bad))
    elif not missing:
        print("\nOK: no regressions past thresholds")
    if bad or missing:
        sys.exit(1)


def show(label=None):
    for r in _load():
        if label and r["label"] != label:
            continue
        m = r.get("metrics", {})
        print(f"{r['label']:14s} {r['stage']:9s} {r['stem']:18s} {r['wall_s']:7.1f}s "
              f"{r['rss_tree']/1e6:7.0f} MB  parts={m.get('parts', m.get('parts@0', '-'))} "
              f"vtx={m.get('vertices', '-')}")


def _check():
    """RSS sampling sees a child's allocation, and the compare gate fails on a real regression."""
    import psutil  # noqa: F401
    code = ("import time;"
            "x = bytearray(200*1024*1024);"
            "x[::4096] = b'\\x01' * (len(x)//4096);"
            "time.sleep(1.5)")
    p = subprocess.Popen([sys.executable, "-c", code])
    box = {}
    _sample_rss(p, box)
    p.wait()
    mb = box.get("rss_tree", 0) / 1e6
    assert mb > 150, f"tree RSS sampling saw only {mb:.0f} MB for a 200 MB child"
    # the compare gate's arithmetic: a 2% vertex growth must trip the 1% limit
    d = (102000 - 100000) / 100000
    assert d > LIMITS["vertices"]
    assert (100500 - 100000) / 100000 < LIMITS["vertices"]
    print(f"bench self-check ok (sampled {mb:.0f} MB of a 200 MB child)")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["--check"]:
        _check()
    elif a[:1] == ["run"] and len(a) >= 3:
        lbl = a[a.index("--label") + 1] if "--label" in a else "adhoc"
        run(a[1], a[2], lbl)
    elif a[:1] == ["compare"] and len(a) == 3:
        compare(a[1], a[2])
    elif a[:1] == ["show"]:
        show(a[a.index("--label") + 1] if "--label" in a else None)
    else:
        sys.exit(__doc__.strip().split("\n\n")[-1])
