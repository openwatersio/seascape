# Scheduled source mirroring — planning doc

*Written 2026-07-08. Point-in-time; the code is the source of truth.*

## Problem

Three sources register against live NOAA state at build time and stream their bytes from NOAA at aggregate time: `noaa_s102` (tile-scheme GeoPackage → ~4.3k `.h5` products, ~15–20 GB), `cudem` and `cudem_third` (urllists → 942 + 372 COGs, ~188 + 8.5 GB). NOAA republishes continuously and non-atomically, so builds that span an update read tiles the `sources` job registered but `aggregate` finds 404'd. Confirmed twice:

- **2026-06-24:** the S-102 tile-scheme gpkg updated *mid-build* (19:57:31Z), removing a Richmond product; 2 of 24 aggregate shards failed on it.
- **2026-07-08:** the current catalog (published 2026-07-02) still references 7 Jacksonville products that 404 — catalog↔bucket inconsistency persisting for *days*. NOAA retains only one gpkg edition under `_CATALOG/` and deletes superseded objects, so no consistent catalog edition exists to pin.

Downstream symptoms: `just preview` re-registers S-102 live and silently registered 2 of 9 Jacksonville products (`MAX_DROPS = 50` is an absolute count, meaningless for a bbox'd regional read); every build is coupled to upstream availability at dispatch time. The production requirement is the opposite: **missing upstream data must fail immediately and loudly — never silently gut coverage, never tear a build mid-flight.**

## Goals / Non-goals

**Goals.** Builds never contact NOAA: volatile sources are mirrored into the data bucket and every `bounds.csv` row references our copy. Source registration moves out of `build.yml` into a scheduled + on-demand `sources.yml`, so upstream churn surfaces as a red *sources* run (the alert channel) while the last-good mirror and bounds keep builds green. `bounds.csv`, published last, is the atomic pointer: a build can never observe a half-mirror. The S-102 GeoPackage path is deleted — enumeration is a public S3 prefix listing. A 404 against our own mirror mid-build is *our* bug and hard-fails the build (no tolerate-and-skip anywhere; that band-aid was tried and reverted).

**Non-goals.** Pruning superseded mirror objects (hoarding ~$3/month of R2 is cheaper than a delete race; revisit when it bothers us). A bounds-staleness guard (fail builds when bounds.csv is older than N weeks) — worthwhile follow-up, not this pass. HDF5→COG conversion during mirroring (would drop the reproject-time negate; future optimization — the mirror copies `.h5` verbatim). Auto-dispatching a build after a sources refresh (builds stay manual by design). The `landmask` job stays in `build.yml` — recipe-hash-gated, not volatile, cheap no-op. Other sources (GEBCO, EMODnet, AusSeabed, …) are prepared/tarballed already and only move workflows, not shape.

## Approach

### `sources.yml`: the sources work leaves `build.yml`

New workflow, `schedule:` weekly cron + `workflow_dispatch` with two inputs: `source` (optional — filter the matrix to one source) and `force` (skip the recipe-hash short-circuit and the shrink guard, for deliberate re-registration). The `discover` and `sources` matrix jobs move from `build.yml` essentially verbatim (including the `image` job copy — ~25 lines, not worth a reusable workflow), with the volatile branch reworked per below. `metadata.json`'s `volatile: true` keeps its meaning shifted one level: *re-run on every sources.yml run* (skip the recipe-hash check) instead of *re-run on every build*.

Both workflows must share one concurrency group (e.g. `group: r2-store`, `cancel-in-progress: false` in both, replacing build.yml's `group: ${{ github.workflow }}`): the sources push still uses `aws s3 sync --delete` for prepared sources, which must never run under an active build's aggregate shards.

`build.yml` drops `discover` + `sources`; `plan` needs only `image` (its `if: !cancelled()` hack — which existed precisely because "the whole build died skipped when the volatile S-102 registration hiccuped" — simplifies away), `aggregate` drops `sources` from `needs`. `plan` already pulls every `bounds.csv` from R2, so nothing else changes.

### Mirrored volatile sources: shape and flow

A mirrored source's bytes live at `bathymetry/source/<id>/objects/<upstream-key>` in the data bucket (upstream key paths preserved — zero rename logic, provenance readable). Its `bounds.csv` rows switch from absolute `/vsicurl/https://noaa…` URLs to **relative** `objects/<upstream-key>` filenames, which [config.source_path](../../pipelines/config.py) already resolves three ways: via `SOURCE_VSI_BASE` in CI/preview (`/vsicurl/https://data.openwaters.io/bathymetry/source/<id>/objects/…`) and `store/source/<id>/…` locally. Mirrored sources thereby become prepared-source-shaped; the streaming special case disappears from the read path. Cutover is safe in both directions because `source_path` passes absolute `/vsi` rows through verbatim — the old bounds.csv keeps working until the first sources.yml run publishes the new one.

Per volatile source, the sources job runs four ordered steps:

1. **Enumerate + register** (container, `just source <id>`): a new `source_mirror.py` reads `file_list.txt` entries — a prefix URL ending `/` means *list the public S3 prefix* (S-102: `ed3.0.0/`, paginated, `.h5` only, `_CATALOG/` excluded); anything else is fetched as a flat urllist (CUDEM, unchanged files). S-102 products are deduped per cell — strip the trailing issue-digits from the filename (`102US005JAXEF` ← `102US005JAXEF262287.h5`; verify the split against the real population), keep the object with the newest `LastModified` from the listing (the suffix encoding is undocumented — don't trust lexical order). Diff against the previous bounds.csv (seeded from R2, the existing pattern): rows whose object is still listed upstream are **carried forward without any network read** (the [register_tiles](../../pipelines/source_remote.py) incremental pattern); *new* keys get a header read **from the upstream URL** (it was listed seconds ago; a 404 here means upstream churned mid-refresh — fail the job, next run reconciles) for 3857 bounds + pixel dims. Output: bounds.csv with relative filenames, plus a workflow-consumable mirror manifest (the full upstream key list — full, not just new, so rclone self-heals any partial prior mirror).
   - **Crash isolation:** `.h5` header reads go through a `gdalinfo -json` subprocess, not in-process rasterio — container GDAL 3.8's S102 driver segfaults on NOAA's degenerate 1×1 stub products, and a segfault must fail one probe, not the job silently. CUDEM GeoTIFFs keep the existing in-process rasterio path.
2. **Mirror** (workflow): `rclone copy --files-from <manifest>` from the anonymous public NOAA bucket straight into R2 (S3 API, existing secrets). rclone streams object→object with no runner disk (CUDEM's 188 GB can't land on a runner), skips objects already present, verifies size/MD5-ETag per copy (the files are single-part, so ETag is a real MD5 — this replaces the gpkg's SHA-256 manifest), and never deletes.
3. **Push registration** (workflow): sync `store/source/<id>` up *excluding* bounds.csv, **no `--delete`** for volatile sources.
4. **Publish** (workflow): `aws s3 cp bounds.csv` — one PUT, the atomic pointer; then the recipe-hash marker. Every object a row references is already verified-present in R2 before any build can see the row. (Reordering bounds.csv-last is correct for prepared sources too — apply it to the shared push step.)

### Failure semantics and guards

The gpkg's HEAD sweep, probe machinery, `PROBE_UNDER_BYTES`, and `MAX_DROPS` are all deleted. Their replacements:

- **The previous bounds.csv is the manifest.** Registration prints every removed key loudly and **refuses to publish when rows shrink by more than ~5%** (fraction, not absolute count — fixing the 7-of-9 blindness) unless `force`. Last-good bounds keeps serving; the red run is the alert.
- **A header-read failure on a new object hard-fails the source job** — no silent drop. If NOAA ever parks a long-lived stub that blocks refreshes for weeks, the upgrade path is a per-source exclusion list under `sources/<id>/`; don't build it speculatively.
- **Builds:** no new skip paths. Aggregate reads only `data.openwaters.io`; any 404 there is our bug and the shard fails.

### S-102 and CUDEM specifics

`source_register_remote_geopkg.py` is deleted outright (newest-gpkg resolution, geopandas read, HEAD sweep, probes, its `_check`). `sources/noaa_s102/file_list.txt` becomes the bucket prefix URL; `metadata.json` drops `link_column`. `source_register_remote_urllist.py` folds into `source_mirror.py` if that lands naturally — one module, two enumeration shapes — or stays as the urllist front-end; implementer's call. `source_remote.py`'s helpers (`to_vsicurl`, `write_bounds`, `_prev_bounds`, `bounds_3857`) survive.

One-time costs, first sources.yml run: ~215 GB through rclone on one runner (~1–2 h at runner NIC speeds, inside the 6 h cap; S-102 alone is ~20 min) and a full header sweep for every product (~4.3k gdalinfo subprocesses + 1.3k rasterio opens; parallelize with a thread pool if it drags — subsequent runs read ~tens). After cutover the covering diff sees every S-102/CUDEM filename change → the US-coastal tile set re-aggregates once on the next build. Expected and correct; note it in the dispatch.

### Preview

Delete the `just source noaa_s102` line (and its comment) from the `preview` recipe in the root [Justfile](../../Justfile) — the bounds.csv seed loop above it already covers S-102 once rows are relative, and preview stops touching NOAA entirely (the original bug report). `preview-local` needs no change: a mirrored source without local objects behaves like any prepared source that isn't on disk.

### Docs

Update [sources/README.md](../../sources/README.md) (S-102 + CUDEM entries describe the streaming model) and CONTRIBUTING.md's CI/architecture section (it documents the build.yml job graph and the sources job). Check both for stale references to live-catalog registration.

## Alternatives considered

- **Verified atomic snapshot keyed on the gpkg's `S102V30_SHA256` manifest** (the earlier deferred plan): correct but heavier — versioned snapshot paths, a READY pointer, and gpkg parsing all survive. Publishing bounds.csv last gives identical never-see-a-half-mirror semantics using a file the pipeline already consumes, and dropping the gpkg removes the catalog-consistency problem instead of detecting it. ETag-per-copy replaces SHA-256 well enough for single-part objects.
- **Cloudflare-managed mirroring:** Super Slurper is a one-shot unverified bulk copy of the *bucket* (not the catalog's view — no failure signal); Sippy is a lazy pull-through cache, so a never-cached tile still 404s mid-aggregate at first touch. Neither is atomic or manifest-aware. Researched 2026-07-08; no managed continuous replication into R2 exists.
- **Keep streaming, harden registration only** (fractional `MAX_DROPS`, zero-tolerance in prod): doesn't close the TOCTOU — objects still vanish between registration and aggregation (confirmed 2026-06-24), and with upstream broken for 6+ days the source simply can't register.
- **Pin an older catalog edition:** impossible — NOAA retains one gpkg and deletes superseded objects (verified 2026-07-08).
- **Tolerate-and-skip at aggregate:** tried, reverted; silent chart gaps are the worst failure mode.
- **Mirror per-build instead of on a schedule:** couples build latency and success to NOAA again — exactly the coupling being removed.

## Validation

- `uv run python source_mirror.py --check` self-check covering: cell dedupe keeps newest-by-LastModified; the shrink guard refuses publish and `force` overrides; carried-forward rows do zero network reads (assert offline, like `source_remote._check`); rows are relative `objects/<key>` paths; a header-read failure raises.
- Existing suites still pass (`test_engine.py`, `test_source_stage.py`, remaining `--check`s); anything importing the deleted geopkg module is cleaned up.
- Live smoke, locally runnable: enumerate S-102 (expect ~4.3k after dedupe; the 7 missing Jacksonville products absent — they were never uploaded, so listing self-consistently excludes them) and end-to-end register a 2–3-tile subset, verifying relative rows + manifest output.
- `actionlint` on both workflows if available.
- Post-merge (not from this worktree): dispatch sources.yml once and watch it (initial mirror), spot-check bounds.csv rows resolve via `https://data.openwaters.io/…/objects/…`, then `just preview "-81.55,30.28,-81.38,30.44"` — the Jacksonville preview from the bug report — completes with zero NOAA traffic; next build dispatch re-aggregates the coastal dirty set once.
