# The planet build

How `.github/workflows/build.yml` turns `sources/` into servable tilesets, on one on-demand Hetzner box run as a self-hosted GitHub Actions runner. Companion to [CONTRIBUTING.md](../CONTRIBUTING.md#ci--build--release), which covers the release flow; this documents the build itself and the constraints every change to it must respect.

The store lives on a **persistent Hetzner volume** (`seascape-store`, 750 GB) that outlives every box — there is no hydrate and no per-stage push. A build is **one Snakemake invocation over one DAG** against that volume; the only R2 writes are **products**: the content-addressed mosaic publish (tiles + index + a **candidate** pointer, never the serving pointer) and the per-commit `build/<sha>/` bundle set. Deletion is a separate scheduled **GC** (`gc.yml`), the only deletion path. The phase history that led here lives in [the production-build plan](plans/2026-07-09-production-build.md).

## What it is

A manually-dispatched GitHub Actions workflow that boots one on-demand Hetzner box, **registers it as a self-hosted runner** ([`Cyclenerd/hcloud-github-runner`](https://github.com/Cyclenerd/hcloud-github-runner)), attaches the persistent store volume, and runs the build **natively on that box** as one `docker.sh snakemake` invocation (default targets `bundles publish_mosaic stage_build vector_selfcheck`) — then always-destroys the box, never the volume. Two tiny `ubuntu-latest` jobs bracket it: `create-runner` boots the box, `delete-runner` tears it down; the compute is a pay-per-build box (~€2/hr → roughly €10 for a forced planet rebuild, €1–3 for an incremental, ~$0 idle). It is dispatch-only on purpose: the store volume is shared state, so routine pushes must not mutate it. Per-commit checks live in `ci.yml`; publishing a finished build is `release.yml`.

Running the build *on* the box rather than SSH-ing into an ephemeral box from a hosted runner deletes the entire SSH surface (keygen / boot-wait / `rsync` / `scp` / `ssh … <<'REMOTE'`, the NAT keepalive tuning, and the `build.env` + `%q` quoting — secrets are native job `env:` now, so nothing is ever `source`d) and, crucially, **lifts the 6 h job cap** (below).

## Inputs

**Dispatch inputs** (Actions → Build → Run workflow, from any branch):

| Input            | Default | Meaning                                                                                  |
| ---------------- | ------- | ---------------------------------------------------------------------------------------- |
| `bbox`           | empty   | `"W,S,E,N"` regional build; empty = full planet                                          |
| `depare`         | true    | Build the DEPARE depth-area layer; uncheck for a raster-only build (`SKIP_DEPARE=1`)     |
| `force`          | false   | `-F` on the build invocation — ignore freshness (code changes are force-only by design)  |
| `server_type`    | `ccx33` | `ccx33` (8 vCPU / 32 GB) fits incremental builds; `ccx63` (48 / 192) for wide runs       |
| `snakemake_args` | empty   | Extra snakemake args, e.g. `-R publish`                                                  |
| `max_jobs`       | empty   | Scope gate: dry-run first, abort if the planned job count exceeds this or is zero        |
| `targets`        | the full build | Snakemake targets, e.g. `soundings_all` for an isolated leaf rebuild that must not cascade |

**Repository state**: `sources/<id>/` recipes + `metadata.json`, `pipelines/` code, the toolchain Docker image (deps-only, keyed on `Dockerfile`/`pyproject.toml`/`uv.lock`; code mounts at runtime). The box pulls this image from GHCR and runs everything through `docker run`, exactly like `ci.yml`.

**R2 state** (the `data` bucket, public at `data.openwaters.io`, under `bathymetry/`). What the build reads streams over `/vsicurl`; what it writes are published products:

| Prefix                              | Contents                                                                          |
| ----------------------------------- | --------------------------------------------------------------------------------- |
| `source/<id>/`                      | Processed COGs (or a raw source's mirrored objects) + `catalog.json` — the single registration artifact (sources.yml owns these; the item carries the per-file bounds, recipe hash + flags the tile keys read) |
| `polygon/<id>.gpkg`                 | Per-source provenance footprints                                                   |
| `landmask/`                         | `land.fgb` + `water.fgb` (sources.yml owns these)                                  |
| `mosaic/`                           | The published mosaic: content-addressed `tiles/<stem>-<hash12>.tif` COGs + `planet-z8-<hash12>.tif` overviews, `index/<idxhash12>.parquet` GeoParquet indexes, candidate pointers `mosaic-candidate-<idxhash12>.gti`, and the serving pointer `mosaic.gti` (written only by release.yml's promotion) |
| `build/<sha>/`                      | This build's outputs (below)                                                       |

Superseded mosaic objects (a re-tiled key, an unpromoted candidate whose build expired) linger unreferenced until `gc.yml` sweeps them.

A planet build assumes a **warm volume**: the covering, source registrations, and per-stem artifacts persist on `seascape-store` between runs, so `cover` is up-to-date and its stage-1 producers stay dormant (a step guard refuses a blank volume — that's sources.yml's job; smoke a `bbox` before the first full planet dispatch). Raw source bytes stream from the public mirror via `/vsicurl` (`SOURCE_VSI_BASE`); a file already present in the volume's store wins per file (`config.source_path`). The land/water masks are copied from R2 onto the volume and then served from box-local NVMe via per-file bind mounts — every fork's clamp rasterize range-reads them thousands of times, and the binds preserve identical paths and mtimes so provenance is untouched.

**Secrets**: `HCLOUD_TOKEN` (create/destroy the box + volume, held by `create-runner`/`delete-runner`), `RUNNER_PAT` (a fine-grained PAT with repo **Administration: read & write** — the runner action uses it to mint a registration token and register/deregister the self-hosted runner; the job's `GITHUB_TOKEN` can't do this, hence a dedicated secret), `R2_ACCOUNT_ID` + `R2_ACCESS_KEY_ID` + `R2_SECRET_ACCESS_KEY` (rclone reads them as `RCLONE_CONFIG_R2_*`; the AWS S3-API vars ride along for any `/vsis3` path). `github.token` (with `packages: read`) logs the box into GHCR to pull the image. On the box the secrets are **native job `env:`** — never written to a file and never `source`d, so there's no sourced-file injection vector to quote around (the old `build.env` + `%q` is gone).

## Outputs

Everything lands in `bathymetry/build/<sha>/` (byte-compatible with the old build's contract):

- `planet.pmtiles` — merged Terrarium raster base, z0–`MACROTILE_Z` (z8)
- `overlay-{z}-{x}-{y}.pmtiles` — one per populated `OVERLAY_SPLIT_Z` grid cell, above z8
- `vector.pmtiles` — contours + soundings + depare: a dense shallow run + one variable-depth run per stem-grid cell, `pmtiles merge`d → a sparse pyramid the Worker overzooms
- `coverage.pmtiles` — source-provenance footprints, its own small z0–8 tileset
- `manifest.json` — planet metadata + overlay cell map + `vector.max_zoom` (the covering's max child_z, which turns on the Worker's vector overzoom); **written and pushed last, its presence marks a complete build** (release.yml refuses a sha without one)

Releasing is a separate manual dispatch (`release.yml` promotes a sha a build already produced); `bbox` builds stage under `build/<sha>-bbox/` — previewable at the same Worker route, invisible to release (it promotes the bare sha).

## The box lifecycle

The workflow is four jobs: `image` (ensure the deps-keyed toolchain image is in GHCR) + `create-runner` → `build` → `delete-runner`. `image` and `create-runner` run in parallel on `ubuntu-latest`; `build` runs on the Hetzner box; `delete-runner` (`if: always()`) tears it down.

1. **Create runner** (`ubuntu-latest`) — `Cyclenerd/hcloud-github-runner@v1.4.1` (`mode: create`) boots the box (`server_type` from the dispatch input, `location: fsn1`, `image: ubuntu-24.04`) and registers it as a self-hosted runner labelled with the box name; a following step **attaches the persistent `seascape-store` volume** (750 GB, created once — every build reuses it, bbox smokes included: the build always reads the volume's registrations). The action outputs the runner **`label`** (the `build` job's `runs-on`) and the **`server_id`**; the attach step outputs the box **`ip`**, seeded into a pending `build` commit status so operators can ssh in for live diagnosis.
2. **Set up** (on the box) — the runner runs as **root** (so no `sudo`). The `build` job checks out the repo, installs Docker if missing, mounts the volume whole at the store path (`store/` and `.snakemake/` both persist; a resize grows the filesystem to match), refuses a blank volume, pulls the toolchain image from GHCR, and warms the land/water masks (R2 → volume → per-file NVMe binds).
3. **Build + publish** — **one `docker.sh snakemake` invocation** over the whole DAG: the `cover` checkpoint re-evaluates into mosaic → forks → bundles → `publish_mosaic` + `stage_build`. When `max_jobs` is set, a scope-gate dry run aborts first on a planner surprise. Budgets derive from the box (`--cores` = 2× vCPUs, `mem_gb` = RAM minus a scaling reserve, `disk_mb` from NVMe free space); `mosaic.py verify` first drops strandings a prior hard teardown left; a heartbeat loop carries the job counter into the commit status every minute.
4. **Destroy** (`delete-runner`, `ubuntu-latest`, `if: always()`) — `Cyclenerd/hcloud-github-runner` (`mode: delete`, using the `server_id`) deletes the server and deregisters the runner. **The store volume is never deleted** — it *is* the incremental state. A `collect-metrics` job snapshots the provider's CPU/disk/network series while the server still exists. The action emits `server_id` *before* its runner-registration wait, so teardown still runs even if the box booted but never finished registering.

There is **no prune step** — R2 deletion is out-of-band (`gc.yml`, below), and volume-side orphans (stems that left the covering, retired sources) are tracked in [#148](https://github.com/openwatersio/seascape/issues/148).

**No `timeout-minutes`.** A self-hosted job is **not** subject to the 6 h cap that GitHub-*hosted* runners impose — the ceiling is now the 72 h workflow limit, which is effectively non-binding, so a forced full planet rebuild runs to completion in one window regardless of size (no resume-on-re-dispatch needed). The warm volume still makes re-dispatches cheap (only missing or stale artifacts rebuild), but a build no longer *has* to fit a window. `ccx33` is the default; pick `ccx63` (48 vCPU / 192 GB) for wide runs — cold builds, big invalidations.

## Watching a build

The run log is produced by `snakemake_logger_plugin_seascape`, a logger plugin that
replaces Snakemake's default console handler (`--logger seascape`, wired into build.yml
and sources.yml; the container puts the repo on `PYTHONPATH` so Snakemake's plugin scan
finds it). Snakemake's own output costs ~16 lines per job — a field block per job, a bare
timestamp per event, `Select jobs to execute...` and `Execute N jobs...` per scheduling
round — which is ~200k lines at planet scale, past what the GitHub UI serves, and it
still never prints a job's runtime or peak RSS. `--quiet` can't trim it: `rules` also
suppresses job errors, `progress` also suppresses the counter the heartbeat reads, and
neither level reaches the two scheduler lines.

What the plugin emits instead:

- **One line per finished job** — `✓ mosaic_tile 8-70-105-15 25m08s rss=7.2G cpu=18m00s [2/113, 2%, 15 running]`. Runtime is measured by the handler (JOB_FINISHED carries only a jobid); RSS and CPU come from the job's own `benchmark:` TSV, which the job process flushes before the scheduler handles success. `--logger-seascape-starts` adds a `▸` line at job start with its `mem_gb`/`disk_mb` reservation.
- **A failure line** naming the rule, its wildcards, and its log path. Snakemake reports each failure twice (once live, once in the exit summary, the second time without wildcards); the plugin keeps the first.
- **A periodic status line** every 5 minutes (`--logger-seascape-status-interval`): elapsed, the counter, which rules are in flight, and the oldest running job — the line that says whether a build is progressing or wedged on one long-tail stem.
- **An end-of-run rollup**: per-rule count, total, mean and max runtime, plus the failure list. GitHub truncates the middle of a long log but serves the end, so this always survives.
- **A JSONL event stream** (`--logger-seascape-events`), written to `$TMP/logs/events.jsonl` and shipped in the `snakemake-bench-<run_id>` artifact alongside the benchmark TSVs. Every event carries its full record — jobid, rule, wildcards, reason, resources, duration, the whole benchmark row — so post-run analysis is `jq`, not log scraping. Long `input:`/`reason:` lists live here and never reach the console.

The heartbeat reads the counter straight off the console line, and
[`scripts/watch-build`](../scripts/watch-build) tails the box's container log filtered to
`✓`/`✗`/status lines. Benchmarks are written with `--benchmark-extended`
(`profiles/default/config.yaml`), so each TSV names its own jobid, rule, wildcards,
threads and resources rather than relying on the filename.

The scope-gate dry run deliberately keeps the default logger — it parses `^Job stats` and
the `total` row out of that output.

## The incremental model

Rebuilds are cheap because the store persists on the volume and **Snakemake engine provenance** decides freshness: a rule's inputs and params, so an artifact rebuilds when an input or the resolved config it read changed. Volume artifacts are **mutable stem-named files** (`store/mosaic/tiles/{stem}.tif`, `store/pmtiles/{stem}.pmtiles`, …) that overwrite in place — content-addressing happens only at the R2 mosaic publish.

- **Per-fork granularity.** Terrain, contours, soundings, and depare are separate jobs off the same merged DEM, so e.g. a contour-levels change re-runs the vector forks without rewriting terrain pmtiles.
- **Code is deliberately not an input to the heavy merge** — an innocuous edit must not re-merge the planet. Force it explicitly when code changes what a tile contains: `-R mosaic_tile` via `snakemake_args`, or the `force` input (`-F`).
- **Resume.** The volume carries finished artifacts across re-dispatches; `--rerun-incomplete` plus `mosaic.py verify` (which drops unreadable tiles and empty renders a hard teardown stranded) make a re-dispatch rebuild only what's missing or stale.
- **`force: true` survives as an escape hatch only.** Reach for it when the store itself is suspect.

## The publish protocol

The build writes R2 exactly twice, both products, both at the end of the DAG:

- **`publish_mosaic`** (`mosaic.py publish`) — hash the finished tile COGs, hardlink them under content names, and push only what R2 lacks (`--ignore-existing`; a content-addressed name's presence is proof of its bytes): `mosaic/tiles/<stem>-<hash12>.tif` and `mosaic/planet-z8-<hash12>.tif`, then the index `mosaic/index/<idxhash12>.parquet`, then the **candidate** pointer `mosaic-candidate-<idxhash12>.gti` **last**. The serving pointer `mosaic.gti` is never written from a build — release.yml's promotion copies the winning candidate over it.
- **`stage_build`** (`bundle.py stage-build`) — every archive to `build/<sha>/` (or `build/<sha>-bbox/`), then `manifest.json` **last**: its presence marks a complete build (release.yml refuses a sha without one), and its `mosaic_gti` field names the candidate the release will promote — which is also what holds that candidate against GC while the manifest lives.

Every push is `rclone copy`/`copyto`, **never `sync --delete`** — deletion is out-of-band (`gc.yml`). A crash mid-publish leaves the serving pointer untouched over a complete old world; the partial candidate is unpromotable by construction (pointer-last) and falls to GC.

## Changing a source's resolution cap

A source's built depth is `min(native_overzoom, max_zoom)` floored to `macrotile_z`, where `max_zoom` is the **optional** `sources/<id>/metadata.json` cap (omit it to build to native). Removing the cap lets the source build to its native grid; adding/lowering one is the ops escape hatch for a source that shouldn't be trusted at its full resolution. Either way the covering re-derives `child_z`, the mosaic key re-keys exactly the affected tiles, and the incremental build rebuilds only those cells — no manual state clearing.

**But the edit is inert until the source re-registers.** Builds read the cap from each source's published `catalog.json` (`seascape:max_zoom`), **not** `metadata.json` directly (`config.source_property`: catalog first, metadata only as fallback and only when the catalog's field is null). A planet build dispatched after the metadata edit but before registration re-runs still builds capped — benign (the next build picks it up) but a wasted build. The metadata edit *does* change the source's registration recipe hash, so `sources.yml` re-registers on its own.

Ordered dispatches to make a cap change live, **per source**:

1. **Edit** `sources/<id>/metadata.json` — remove (or change) `max_zoom`. Commit + merge.
2. **Re-register**: run `sources.yml` (weekly cron picks it up, or dispatch it with the `source` filter). It regenerates `catalog.json` and republishes it last, after the `bounds.csv` pointer.
   - Pre-req check: `sources.yml` must have completed green. A registration that shrinks a volatile source >5% refuses to publish without `force` — unrelated to the cap, but it blocks the republish, so watch the run.
3. **Verify the republished catalog dropped the cap** before building — read `seascape:max_zoom` from the published item:
   `curl -s https://data.openwaters.io/bathymetry/source/<id>/catalog.json | jq .properties.\"seascape:max_zoom\"` → must be `null` (uncap) or the new value.
4. **Dispatch `build.yml`** (planet, or a `bbox` slice over the source's footprint first to measure). Only now does the deeper `child_z` take effect. Watch planet-build wall clock + mosaic store size — each +1 `child_z` is 4× mosaic pixels + 4× terrain render in the affected stems.

Do these one source at a time when the cost is uncertain (e.g. CUDEM's z13→z15 lift covers the whole US coastal strip); small-footprint sources can be batched into one registration + one build.

## Requirements for all changes

Constraints every modification to the build must respect.

### Every step must accept BBOX

A dispatch with `bbox` set builds a regional slice — the primary way to test build changes without a multi-hour planet run. What "accept BBOX" means still splits three ways:

- **Rebuild-scoping steps** (cover, coverage, aggregate, downsample, the vector forks) honor `BBOX` (empty = planet): the box passes `-e BBOX` into every `docker run`, and the covering carries the scope transitively.
- **Shared-metadata steps** (source prep / `catalog.json`, the land + water masks) are **not in this build at all** — they moved to `sources.yml`, which is always global. A build never writes them.
- **Publish steps** stage under `build/<sha>-bbox/` when BBOX is set — release.yml promotes only the bare sha, so a regional stage can never ship. The only pointer a build writes is its own mosaic **candidate** GTI, scoped to whatever covering it built; the serving pointer is untouchable from here either way.

A `bbox` build is otherwise **self-contained**: its `build/<sha>-bbox/` outputs reflect only the window, so compare a bbox build's tiles over the bbox, not against the planet. Its regional candidate is held against GC only by its own manifest until the `build/` lifecycle rule expires it. A bbox build never releases.

### One store-mutating workflow at a time, globally

The workflow-level `concurrency: r2-store` group (no `cancel-in-progress`) exists because two writers mutating the store concurrently corrupt it — it's shared with `sources.yml` (whose prepared-source syncs use `--delete`) AND `gc.yml` (the only deletion path), so a build never interleaves with a source refresh or a GC. Don't scope it per-ref.

### No `--delete`; pointers flip last; GC is the only deletion path

- **No `--delete` anywhere but GC.** Pushes are `rclone copy`, so a re-publish leaves superseded objects behind as unreferenced garbage rather than clobbering anything concurrent. It's collected out-of-band.
- **Pointers flip last.** The candidate GTI publishes after the tiles/planet/index it names; `build/<sha>/manifest.json` flips last in the build domain (release.yml's completeness marker); the serving `mosaic.gti` is written only by release promotion, from a complete candidate.
- **GC (`gc.yml`) is the only deletion path.** No build-time prune exists. See [Garbage collection](#garbage-collection).

### Config changes rebuild themselves; code changes are force-only

Config knobs ride as rule inputs and params, so an artifact rebuilds when the resolved config it read changed. Code is deliberately **not** an input to the heavy merge — the historical footgun (an edit silently shipping a stale planet) is traded for an explicit dispatch: when pipeline code changes what a tile contains, force the affected rules (`-R mosaic_tile` via `snakemake_args`, or `force: true`).

### Pipeline code stays R2-agnostic

`pipelines/*.py` reads and writes the local `store/` and knows nothing about R2, rclone, or the box — all cloud plumbing (`/vsicurl` bases via env vars like `SOURCE_VSI_BASE`, core/memory/disk budget sizing) lives in the workflow. Keep it that way: it's what makes `just preview` run identically on a laptop. The box's build is the same Snakemake DAG a local run walks, pointed at the volume's store.

### Pinned rclone

The box downloads the **pinned, sha256-verified rclone 1.74.4** (same as `sources.yml`/`release.yml`), not apt's 1.60.1 — the old version records the `x-amz-version-id` R2 now returns on uploads and then 501s on its post-upload `HEAD ?versionId=` verification, turning every transfer into noise that buries real failures.

## Garbage collection

Deletion is out-of-band: [`.github/workflows/gc.yml`](../.github/workflows/gc.yml) runs weekly (Tuesday) + on dispatch (with a `dry_run` input, default true for a manual run; the cron always deletes), sharing the `r2-store` concurrency group so it can never run during a build or a source refresh. It is the **only deletion path anywhere**.

The referenced set is rooted on the published mosaic:

- `mosaic/mosaic.gti` — the serving pointer — plus the index parquet, `planet-z8` overview, and every tile COG the index's `location` column names. Any failure resolving this set refuses the whole run: a broken serving set is a human's problem, never a delete list.
- every candidate still named by a live `build/<sha>/manifest.json` (`mosaic_gti`), rooted the same way: a published-but-unreleased build stays promotable until the `build/` lifecycle rule (7 days) expires its manifest. A candidate whose GTI or index never landed can never be promoted, so it's skipped with a note and its debris falls to collection.

It deletes:

- everything else under `mosaic/` — superseded tiles/overviews, old indexes, and old candidate GTIs. A promoted candidate's own copy is included: `release.yml` skips promotion for an already-published sha, so a rollback re-dispatch never re-reads it;
- the retired store-hydrate prefixes **wholesale** — `pmtiles/`, `contour/`, `soundings/`, `depare/`, `aggregation/`, `store/` — dead since the store moved to the persistent build volume; nothing reads or writes them;
- retired `source/<id>/bounds.csv` registrations (`catalog.json` carries the per-file rows as `seascape:files` now).

It **never touches**: `build/<sha>/` (read-only roots here; an R2 lifecycle rule collects it after 7 days — see `release.yml` — and releases are promoted to the separate tiles bucket), source COGs / `catalog.json` / `polygon/` / `landmask/` (sources.yml owns them).

Guards — **absence is proof, errors are not**: an object genuinely absent from a cleanly-fetched mosaic listing skips (an unpublished candidate is unpromotable debris), but any *error* refuses the whole run — a failed `mosaic/` or `build/` listing, a listed manifest or index that won't read, unparsable Parquet or tile locations, or a referenced object missing from the listing. Treating a transient backend error as absence would silently drop a live root and delete objects the next release needs. It logs a full inventory before deleting, and deletes in bounded batches. The Collect arithmetic + every refusal guard live in one script, [`scripts/gc-collect.sh`](../scripts/gc-collect.sh) — gc.yml invokes it with the rclone backend, and its test [`pipelines/test_gc.sh`](../pipelines/test_gc.sh) (`just test-gc`, run by ci.yml on every push) invokes the same script with the local backend against a synthetic tree, covering the happy path and each refusal — so the workflow and its test cannot drift.

Operationally: before the cron's first live deletion, run a manual dispatch with `dry_run=true` (the default) and eyeball the inventory — a healthy store flags a small superseded slice of `mosaic/`; "most of the prefix" flagged means stop and investigate, though the guards should have refused first.
