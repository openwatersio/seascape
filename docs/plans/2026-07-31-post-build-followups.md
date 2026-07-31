# Post-build follow-ups — run 30641774632

_Written 2026-07-31 while the run's final singleton chain executes. Two halves: the analysis owed from this run's evidence, then the fix batch for the next PR. Evidence lands in `pipelines/store/profile/run-30641774632/` (pull bench + logs + container log off the box at completion, before teardown — the volume is unreadable without a runner)._

## A. Analysis — from this run's logs and TSVs

1. **Log-pattern census across every depare log + the container stream.** Counts, per-stem attribution, and distributions for each recurring line:
   - `did not simplify as a coverage (…) - keeping raw geometry` — which stems ship RAW vs SIMPLIFIED geometry, per ladder, and why: area-drift rejection vs invalid result vs exception, with the drift magnitude (the Arctic cz12 case rejected at 0.19% against the budget). Output: a per-stem ledger `stem × ladder × {simplified | raw:reason} × bands-read/simplify/clip walls × FGB bytes`.
   - `not a valid polygonal coverage (…) - falling back to unary_union` — ~35 trips this run. Per-stem, part count, and the cost each paid. Cross-check: `re-noded from` count is apparently **zero** — the second-chance path never fired, which needs explaining before it earns its keep (see B2).
   - `repaired N row(s) the 4326 write folded` — N's distribution fleet-wide. Production carried 6 total before; the Arctic stem alone repaired 2,070 (m) + 2,261 (ft). Correlate N with latitude; if it is polar-longitude quantization at `COORD_DECIMALS`, that is a targeted fix, not noise.
   - `ContourTimeout` / `Trying to restart` / `MemoryError` — expected zero; verify zero.
2. **Phase corpus, fleet-wide.** First run ever with `DEPARE_TIMING` on the box: aggregate every stem's phase splits into a planet-scale phase ranking. Specifically hunt the `bands-read-*` distribution for more members of the read_bucket wedge class (coarse stems × giant rings × per-level scans) beyond `8-131-84-12`.
3. **Reservation validation, round 2.** The median-floor retune (`996d857`) against this run's full actuals: exceedance rate, worst overshoot, achieved depare concurrency over time, and whether swap was ever touched. The philosophy is over-admission — the check is that the box stayed busy, not that reservations were never exceeded.
4. **Old-vs-new matched comparison, full fleet.** The cancelled-run corpus plus the historical runs against this run's TSVs — now with the heavy classes present on both sides (Gulf, Antarctic, coarse continents). Separate ambient contention from code wins using the unchanged-rule (window/contour) ratios as the baseline, as before.
5. **The singletons' first-ever numbers.** `vector_shallow` and `vector_join` wall + RSS vs the 20 GB protective reservation; join output size vs the 25–35 GB projection; completeness self-check outcome.
6. **Artifact accounting.** Final `vector.pmtiles` size; depare FGB total vs the 1.04 GB single-stem era; drying/nodata byte shares vs the ~¾-nodata measurement that motivated pre-simplification.

## B. Fix batch — next PR

1. **~~`read_bucket` single-scan refactor~~ — REFUTED, replaced by the shipped structure-mode repair.** Measured: the raw partition holds one MultiPolygon per bucket, all 20 per-level scans cost 2.41 s total, and the FGB driver skips geometry for filtered-out rows — a single-read refactor saves 0.04%. The real wedge was `make_valid` in linework mode (n^1.9–2.7) on a 5.7 M-vertex / 350,800-hole Dutch-coast drying part inside `repaired_parts`; `method="structure"` (n^1.34) takes the whole bucket read to 78 s at 2.5 GB and is landed. Follow-through items: (a) `shapely.is_valid` on the *repaired* 15,539-part bucket costs 106 s — the next bottleneck in that path; (b) the deeper cause is `DRYING_CAP=16` making low-coast buckets carry every pixel below +16 m (19,670 km² of polder here) that the land cut later deletes — cutting land *before* repair/simplify shrinks the problem by orders of magnitude but changes what the ladder simplifies as a coverage; belongs with the shallow-coarsening plan.
2. **Instrument the re-node guard.** 0-for-~35 means its premise is wrong somewhere: log WHY the re-noded result fails (validity vs area, and the magnitude) so the next run's data settles whether to fix or delete the path.
3. **§6a/§6b/§6d** (deferred from the shallow-coarsening plan): `gdalwarp -r max` rescue, the shallow-band `max(out, dem)` clamp, scale-derived `SLIVER_MIN_PX`.
4. **4326-fold repairs at high latitude** — root-cause if A1 confirms the latitude correlation.
5. **Simplify-rejection follow-through.** Stems that kept raw geometry still ship S-58-dense vertices. Decide: widen `AREA_GUARD` with a justified budget, add a degenerate-ladder carve-out, or accept raw for the (few?) rejecting stems — sized by A1's ledger.
6. **Timeout re-fit.** `DEPARE_TIMEOUT=3600` bounds contour passes that now run in seconds-to-minutes, and the 8× SIGALRM allows an 8 h wedge. Re-fit both to post-fix phase distributions so a future wedge fails in tens of minutes, not hours.
7. **Extend the live-log pattern** (`> {log} 2>&1` + heartbeat) from depare to the other fork rules if A2 shows any long unattributable phases outside depare.
8. **GEOS upstream filings** — pending explicit go: the `MakeValid` MultiPolygon-dispatch issue (55× penalty, `repro.py` ready) and the `CoverageValidator` per-target-rebuild issue + partial patch (163 lines, builds clean). Artifacts in the session scratchpad; copy to durable storage before filing.
9. **`terrain.py` window divergence** (shallow-coarsening open question 8) — the published DEM now disagrees with depare polygons on 0.245% of production water pixels (pond fill); decide render-side adoption or document.
10. **Backlog carry-overs:** the bbox→planet rerun tax (`216e719`/`684d9af`), fork_window content guard, and the marsh/plan memory + `docs/plans/2026-07-30-shallow-coarsening.md` §10 refresh with this run's box actuals.

## C. Standing process notes

- Pull evidence off the box **before** cancel/teardown, always.
- Per-trip guard events are tracked silently and summarized; only novel classes get surfaced.
- Reservations tune to the median; the box being idle is the failure mode (see `reservations-median-not-tail` memory).
