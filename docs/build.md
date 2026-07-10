# The planet build

How `.github/workflows/build.yml` turns `sources/` into servable tilesets, on one ephemeral machine. Companion to [CONTRIBUTING.md](../CONTRIBUTING.md#ci--build--release), which covers the release flow; this documents the build itself and the constraints every change to it must respect.

Point-in-time. This is **phase 2** of [the production-build plan](plans/2026-07-09-production-build.md) ("one box"): the matrix fan-out is gone, but the pipeline semantics are unchanged — the covering-diff still drives incrementality, the store is still a mutable shared R2 prefix, and the covering-retile prunes are still here. Phase 3 (content-hash keys) and phase 4 (immutable store + GC) later delete the covering diff, the `force`-for-correctness requirement, and the prunes; until then everything below holds.

## What it is

A manually-dispatched GitHub Actions workflow that boots one ephemeral Hetzner box, hydrates the incremental store from R2, runs the full `cover → aggregate → downsample → bundle → vector` pipeline through `just`, pushes each stage back to R2, and always-destroys the box. The GitHub runner only orchestrates; the compute is a pay-per-build box (~€2/hr → roughly €10 for a forced planet rebuild, €1–3 for a weekly incremental, ~$0 idle). It is dispatch-only on purpose: the incremental store is shared, so routine pushes must not mutate it. Per-commit checks live in `ci.yml`; publishing a finished build is `release.yml`.

## Inputs

**Dispatch inputs** (Actions → Build → Run workflow, from any branch):

| Input         | Default | Meaning                                                                                  |
| ------------- | ------- | ---------------------------------------------------------------------------------------- |
| `bbox`        | empty   | `"W,S,E,N"` regional build; empty = full planet                                          |
| `force`       | false   | Ignore the incremental diff, rebuild every tile (use after pipeline-code changes)        |
| `server_type` | `ccx33` | Hetzner box size — `ccx33` (8 vCPU / 32 GB, today's dedicated-vCPU cap); `ccx63` later    |

**Repository state**: `sources/<id>/` recipes + `metadata.json`, `pipelines/` code, the toolchain Docker image (deps-only, keyed on `Dockerfile`/`pyproject.toml`/`uv.lock`; code mounts at runtime). The box pulls this image from GHCR and runs everything through `docker run`, exactly like `ci.yml`.

**R2 state** (the `data` bucket, public at `data.openwaters.io`, under `bathymetry/`). The box `rclone copy`s these down on boot (hydrate) and pushes deltas back up:

| Prefix                              | Contents                                                                          |
| ----------------------------------- | --------------------------------------------------------------------------------- |
| `source/<id>/`                      | Prepared/mirrored COGs + `bounds.csv` + `catalog.json` (sources.yml owns these)    |
| `polygon/<id>.gpkg`                 | Per-source provenance footprints                                                   |
| `landmask/`                         | `land.fgb` + `water.fgb` (sources.yml owns these)                                  |
| `aggregation/<ulid>/`               | Coverings (one `covering.tar.gz` of CSVs per build), newest 3 kept                 |
| `pmtiles/`                          | Per-tile terrain pmtiles (the incremental store)                                  |
| `contour/`, `soundings/`, `depare/` | Per-tile vector intermediates                                                      |
| `build/<sha>/`                      | This build's outputs (below)                                                       |

The box does **not** hydrate source COGs — `aggregate` range-reads them via `/vsicurl` from public R2 (`SOURCE_VSI_BASE`), exactly as the old matrix aggregate did. The land/water masks are read the same way (`LANDMASK`/`WATERMASK`), never hydrated. Only `bounds.csv` + the footprint gpkgs come down (the covering diff and coverage layer need them).

**Secrets**: `HCLOUD_TOKEN` (boot the box), `R2_ACCOUNT_ID` + `R2_ACCESS_KEY_ID` + `R2_SECRET_ACCESS_KEY` (rclone reads them as `RCLONE_CONFIG_R2_*`; the AWS S3-API vars ride along for any `/vsis3` path). `github.token` (with `packages: read`) logs the box into GHCR to pull the image. Secrets are written into a `build.env` and `scp`'d to the box, never placed on a command line.

## Outputs

Everything lands in `bathymetry/build/<sha>/` (byte-compatible with the old build's contract):

- `planet.pmtiles` — merged Terrarium raster base, z0–`MACROTILE_Z` (z8)
- `overlay-{z}-{x}-{y}.pmtiles` — one per populated `OVERLAY_SPLIT_Z` grid cell, above z8
- `vector.pmtiles` — contours + soundings + depare, tile-joined in one pass
- `coverage.pmtiles` — source-provenance footprints, its own small z0–8 tileset
- `manifest.json` — planet metadata + overlay cell map; **written and pushed last, its presence marks a complete build** (release.yml refuses a sha without one)

A build from `main` auto-dispatches `release.yml` for its sha; feature-branch and `bbox` builds write `build/<sha>/` but don't ship.

## The box lifecycle

The workflow is one `image` job (ensure the deps-keyed toolchain image is in GHCR) + one `build` job that follows the seamap `build-planet.yml` shape:

1. **Boot** — generate a per-run SSH key, `hcloud server create` (type from `server_type`), and for a planet build attach a 250 GB volume (hydrate ~50 GiB store + bundle outputs, which roughly double during pmtiles finalize). A `bbox` build skips the volume and works on the root disk.
2. **Ship** — `rsync` the repo (excluding `.git`/`node_modules`/`data`/`pipelines/store`) and `scp` a `build.env` of secrets + config.
3. **Set up** — install Docker + the pinned rclone, log in to GHCR, `docker pull` the image, mount the volume at the store path, compute `AGG_PROCESSES = min(cores, RAM_GB/4)` and a scaled `GDAL_CACHEMAX` (in the workflow, not Python — pipelines stay host-agnostic).
4. **Hydrate** — `rclone copy` the store prefixes down (planet only), untar the coverings.
5. **Build + push** — run each stage through `docker run … just <stage>`, pushing to R2 between stages (below).
6. **Prune** — planet only, after the build (below).
7. **Destroy** — an `if: always()` step deletes the server, the volume (with a detach wait-loop), and the SSH key.

The `build` job has `timeout-minutes: 350` — the max available, since GitHub-hosted runners hard-cap jobs at 6 h. Routine incrementals fit easily; a full forced planet rebuild on `ccx33` may exceed it — the build resumes on re-dispatch thanks to the per-stage pushes, the periodic mid-aggregate pushes, and the covering diff's self-heal (below), and `ccx63` (quota pending) makes it moot. There is no self-detaching daemon machinery.

## The incremental model

Rebuilds are cheap because nothing clean is redone — identical to the old build, just scanned from the hydrated local store instead of an R2 listing:

- `cover` diffs the new covering against the previous (hydrated) one; only tiles whose source coverage changed (or whose pmtiles are missing — **self-heal** — or whose overview is older than a child — the mtime cascade) are dirty.
- Clean tiles' pmtiles/contours are reused straight from the hydrated store.
- **Self-heal is also the resume mechanism.** Each stage is pushed as it finishes, and the long aggregate stage is additionally pushed every ~20 minutes by a background loop on the box — so even a stage that can't fit one orchestrator window makes monotonic progress across re-dispatches (destroy kills the volume; without the loop every retry would restart aggregate from tile 1). On re-dispatch the box hydrates the partial store, `cover` makes a fresh covering that matches the last one (no source change), and the covering diff's self-heal picks up from whatever landed — only the tiles whose pmtiles never got pushed come back dirty. (`.done` markers are within-covering state and aren't pushed — a re-dispatch's fresh covering wouldn't see them anyway.)
- **The diff cannot see code.** A change to `pipelines/*.py` or config (contour levels, encode quantization) marks nothing dirty — dispatch with `force: true` after pipeline-code changes, or the store keeps serving output built by the old code. (Phase 3's content-hash keys retire this footgun; until then it holds.)

## The push protocol

Stages push to R2 as they finish, so an orchestrator timeout loses at most one stage:

- `coverage` → `build/<sha>/coverage.pmtiles`
- `aggregate` → `pmtiles/` + `contour/` + `soundings/` + `depare/` (also pushed every ~20 min mid-stage by a background loop; `rclone copy` is idempotent, and a file caught mid-write copies torn but is overwritten by the next pass — the final post-stage push is authoritative)
- `downsample` → `pmtiles/` + the covering (`aggregation/<ulid>/covering.tar.gz`, both aggregate + downsample CSVs — the next build's baseline)
- `bundle` → `build/<sha>/planet.pmtiles` + `overlay-*.pmtiles`
- vector (`soundings`, `depare`, `contours`) → `build/<sha>/vector.pmtiles`
- `manifest.json` → **last**, after everything it references is up

Every push is `rclone copy`, **never `sync --delete`** — the only deletion path is the covering-retile prune (below). A crash mid-push leaves the old outputs and pointers intact; half-pushed objects are harmless until the next successful build overwrites or the prune removes them.

## Requirements for all changes

Constraints every modification to the build must respect.

### Every step must accept BBOX

A dispatch with `bbox` set builds a regional slice — the primary way to test build changes without a multi-hour planet run. What "accept BBOX" means still splits three ways:

- **Rebuild-scoping steps** (cover, coverage, aggregate, downsample, the vector forks) honor `BBOX` (empty = planet): the box passes `-e BBOX` into every `docker run`, and the covering carries the scope transitively.
- **Shared-metadata steps** (source prep / `bounds.csv`, the land + water masks) are **not in this build at all** — they moved to `sources.yml`, which is always global. A build never writes them.
- **Store-reconciling steps** (the covering-retile prunes) **skip** when BBOX is set — a regional covering holds only the window's tiles, so every out-of-window artifact reads as orphaned. The prune block is guarded `if [ -z "$BBOX" ]`.

A `bbox` build is otherwise **self-contained**: it skips the hydrate and pushes **nothing** to the shared store or the covering — only its window `build/<sha>/` outputs, which cannot corrupt the planet store. So `planet.pmtiles`/`vector.pmtiles` from a bbox build reflect only the window (compare a bbox build's tiles over the bbox, not against the planet); only `coverage.pmtiles` was ever window-scoped for other reasons. A bbox build never releases.

### One build at a time, globally

The workflow-level `concurrency: r2-store` group (no `cancel-in-progress`) exists because two builds mutating the incremental store concurrently corrupt it — and it's shared with `sources.yml` (whose prepared-source syncs use `--delete`), so a build never interleaves with a source refresh. Don't scope it per-ref.

### No `--delete`; markers last; prune guarded twice

- **No `--delete` on shared prefixes** (`pmtiles/`, `contour/`, `aggregation/`) — pushes are `rclone copy`, so a re-tile leaves the old tile's key behind rather than clobbering a concurrent write. Deletion is done deliberately by the prune.
- **`manifest.json` last.** It's pushed only after planet + overlays + vector + coverage have all landed, so its presence is a true "complete build" marker for `release.yml`.
- **The prune is guarded twice.** A covering re-tile (a source's footprint/maxzoom shift) orphans the old stems; the prune computes the full orphan list per prefix, **refuses to run if more than 25% of a prefix reads as orphaned** (a real re-tile orphans a slice; "most of the store" is always a bad stems list), and prunes **pmtiles before the vector FGBs** — a deleted pmtiles self-heals (missing → dirty → rebuilt, regenerating its vector FGBs), a deleted FGB does not, so a wrongful prune costs at most a rebuild, never a silent hole in `vector.pmtiles`. Any new deletion of shared state must follow the same shape. (`bundle.py` also filters orphans at bundle time via `covering_stems`, so the prune is storage hygiene, not correctness — but it keeps the store from growing dead keys.)

### Force after code changes

Restated because it's the most common way to ship a stale planet: the covering diff only sees source coverage. If your change alters what a tile _contains_ (smoothing, contour levels, encoding, depare logic) rather than _which tiles exist_, the next build reuses every clean tile unless dispatched with `force: true`. (True until phase 3's content-hash keys land.)

### Pipeline code stays R2-agnostic

`pipelines/*.py` reads and writes the local `store/` and knows nothing about R2, rclone, or the box — all cloud plumbing (hydrate/push, `/vsicurl` bases via env vars like `SOURCE_VSI_BASE`/`LANDMASK`/`WATERMASK`, `AGG_PROCESSES`/`GDAL_CACHEMAX` sizing) lives in the workflow. Keep it that way: it's what makes `just planet` / `just preview` run identically on a laptop. The box's build is just `just planet` decomposed into per-stage `docker run`s with pushes interleaved — the recipes and their order are byte-identical to a local build.

### Pinned rclone

The box downloads the **pinned, sha256-verified rclone 1.74.4** (same as `sources.yml`/`release.yml`), not apt's 1.60.1 — the old version records the `x-amz-version-id` R2 now returns on uploads and then 501s on its post-upload `HEAD ?versionId=` verification, turning every transfer into noise that buries real failures.
