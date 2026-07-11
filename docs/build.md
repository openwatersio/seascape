# The planet build

How `.github/workflows/build.yml` turns `sources/` into servable tilesets, on one ephemeral machine. Companion to [CONTRIBUTING.md](../CONTRIBUTING.md#ci--build--release), which covers the release flow; this documents the build itself and the constraints every change to it must respect.

Point-in-time. This is **phase 3** of [the production-build plan](plans/2026-07-09-production-build.md) ("hash keys"): the matrix fan-out is gone (phase 2), and content-hash keys now drive incrementality — the covering diff, its `.done` markers, and the `force`-for-correctness requirement are deleted. The store is still a mutable shared R2 prefix and the covering-retile prunes are still here; phase 4 (immutable store + GC) deletes the prunes.

## What it is

A manually-dispatched GitHub Actions workflow that boots one ephemeral Hetzner box, hydrates the incremental store from R2, runs the full `cover → aggregate → downsample → bundle → vector` pipeline through `just`, pushes each stage back to R2, and always-destroys the box. The GitHub runner only orchestrates; the compute is a pay-per-build box (~€2/hr → roughly €10 for a forced planet rebuild, €1–3 for a weekly incremental, ~$0 idle). It is dispatch-only on purpose: the incremental store is shared, so routine pushes must not mutate it. Per-commit checks live in `ci.yml`; publishing a finished build is `release.yml`.

## Inputs

**Dispatch inputs** (Actions → Build → Run workflow, from any branch):

| Input         | Default | Meaning                                                                                  |
| ------------- | ------- | ---------------------------------------------------------------------------------------- |
| `bbox`        | empty   | `"W,S,E,N"` regional build; empty = full planet                                          |
| `force`       | false   | Ignore the content-hash keys, rebuild every tile (escape hatch only — the keys already see code/config changes) |
| `server_type` | `ccx33` | Hetzner box size — `ccx33` (8 vCPU / 32 GB, today's dedicated-vCPU cap); `ccx63` later    |

**Repository state**: `sources/<id>/` recipes + `metadata.json`, `pipelines/` code, the toolchain Docker image (deps-only, keyed on `Dockerfile`/`pyproject.toml`/`uv.lock`; code mounts at runtime). The box pulls this image from GHCR and runs everything through `docker run`, exactly like `ci.yml`.

**R2 state** (the `data` bucket, public at `data.openwaters.io`, under `bathymetry/`). The box `rclone copy`s these down on boot (hydrate) and pushes deltas back up:

| Prefix                              | Contents                                                                          |
| ----------------------------------- | --------------------------------------------------------------------------------- |
| `source/<id>/`                      | Prepared/mirrored COGs + `bounds.csv` + `catalog.json` (sources.yml owns these; the catalog item carries the recipe hash + flags the tile keys read) |
| `polygon/<id>.gpkg`                 | Per-source provenance footprints                                                   |
| `landmask/`                         | `land.fgb` + `water.fgb` (sources.yml owns these)                                  |
| `aggregation/<ulid>/`               | Legacy coverings from the retired diff era (nothing pushes new ones; phase 4's GC cleans up) |
| `pmtiles/`                          | Per-tile terrain pmtiles + their `.key` sidecars (the incremental store)          |
| `contour/`, `soundings/`, `depare/` | Per-tile vector intermediates + their `.key` sidecars                              |
| `build/<sha>/`                      | This build's outputs (below)                                                       |

Planet builds hydrate **selectively**: `bounds.csv` + `catalog.json` + footprints come down before `cover` (the covering, the tile keys, and the coverage layer need them); then the masks hydrate (their content hashes enter the tile keys, so the manifest step below must see the same mask state aggregate will), and once the covering exists, `just sources-manifest` derives the exact `(source, filename)` union the key-stale tiles reference and the box `rclone copy --files-from`s those files into the local store — `aggregate` then reads everything from local disk (the preview-local path: no `SOURCE_VSI_BASE`/`LANDMASK`/`WATERMASK` env, so `config.source_path` and the landmask defaults resolve `store/…`). Two real runs that streamed sources per tile banked zero tiles in 2.5–3.9 healthy hours — a coastal macrotile re-read the same S-102/CUDEM bytes over `/vsicurl` for every tile. A legacy bounds row whose filename is already an absolute `/vsi` path is filtered out of the hydrate list, passes through `source_path` untouched, and still streams — acceptable fallback. `bbox` builds stay fully streaming (self-contained, no volume), exactly the old behavior.

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

1. **Boot** — generate a per-run SSH key, `hcloud server create` (type from `server_type`), and for a planet build attach a 400 GB volume (~50 GiB hydrated store + bundle outputs which roughly double during pmtiles finalize ≈ 100 GiB + the dirty-set source hydrate, ~100–200 GiB for the full coastal S-102+CUDEM subset, + headroom). A `bbox` build skips the volume and works on the root disk.
2. **Ship** — `rsync` the repo (excluding `.git`/`node_modules`/`data`/`pipelines/store`) and `scp` a `build.env` of secrets + config.
3. **Set up** — install Docker + the pinned rclone, log in to GHCR, `docker pull` the image, mount the volume at the store path, compute `AGG_PROCESSES` / `BUNDLE_PROCESSES` and `GDAL_CACHEMAX` from the box's cores + RAM, and export `TOOLCHAIN` = the image tag (in the workflow, not Python — pipelines stay host-agnostic).
4. **Hydrate** — `rclone copy` the store prefixes down (planet only); the `.key` sidecars ride in the same prefixes. After `cover`, hydrate the masks + the stale tiles' source files (see above).
5. **Build + push** — run each stage through `docker run … just <stage>`, pushing to R2 between stages (below).
6. **Prune** — planet only, after the build (below).
7. **Destroy** — an `if: always()` step deletes the server, the volume (with a detach wait-loop), and the SSH key.

The `build` job has `timeout-minutes: 350` — the max available, since GitHub-hosted runners hard-cap jobs at 6 h. Routine incrementals fit easily; a full forced planet rebuild on `ccx33` may exceed it — the build resumes on re-dispatch thanks to the per-stage pushes, the periodic mid-aggregate pushes, and the keys (an artifact whose `.key` sidecar landed is skipped), and `ccx63` (quota pending) makes it moot. There is no self-detaching daemon machinery.

## The incremental model

Rebuilds are cheap because every artifact carries a **content-hash key** ([pipelines/keys.py](../pipelines/keys.py)): a short hash of its inputs ‖ the pipeline modules that produce it ‖ the resolved config values the stage read ‖ the toolchain image tag, written to a `.key` sidecar next to the artifact in the same store prefix (so the existing hydrate/push carries it with zero extra plumbing). A stage recomputes each key and skips artifacts whose sidecar matches; anything else — missing artifact, changed input, changed code, changed config, bumped toolchain — rebuilds. The old covering diff, its `.done` markers, and the downsample mtime cascade are deleted; "the diff cannot see code" and "force after code changes" are historical.

- **Per-fork granularity.** Each aggregation tile carries four keys — terrain, contours, soundings, depare — sharing the merged DEM's determinants (covering row, each intersecting source's `catalog.json` recipe hash + priority/maxzoom/offset/land_clamp, the mask identity, smoothing knobs) plus each fork's own modules and config. A tile re-runs iff any fork key is stale; fresh forks within it are skipped. The merge itself is recomputed whenever any fork needs it (pre-mosaic, the merged DEM is ephemeral — phase 5 makes it durable), so what a fresh fork saves is its regenerate + rewrite. Consequence: a `CONTOUR_LEVELS` change re-merges tiles but rewrites no terrain pmtiles, so downsample and the terrain bundle skip entirely.
- **The key cascade replaces the mtime cascade.** An overview's key hashes its children's keys, so a rebuilt child propagates upward by construction, and a missing artifact self-heals inherently (no artifact → no fresh key). Bundles key off their member artifacts' keys the same way; `manifest.json` is still written whenever anything changed (it's per-sha).
- **Keys are also the resume mechanism.** Each stage is pushed as it finishes (artifact + sidecar together), and the long aggregate stage is additionally pushed every ~20 minutes by a background loop on the box — so even a stage that can't fit one orchestrator window makes monotonic progress across re-dispatches (destroy kills the volume; without the loop every retry would restart aggregate from tile 1). On re-dispatch the box hydrates whatever landed, `cover` makes a fresh covering, and only the tiles whose artifact or sidecar never got pushed come back stale.
- **`force: true` survives as an escape hatch only** (`FORCE_REBUILD` ignores every key match). It is no longer required for correctness after code or config changes — the keys see those. Reach for it when the store itself is suspect (a bad artifact pushed under a valid key).
- **Transition note:** artifacts built before phase 3 carry no sidecars, so the first phase-3 build re-runs everything once (correct — the store was built by old code paths) and stamps keys as it goes.

## The push protocol

Stages push to R2 as they finish, so an orchestrator timeout loses at most one stage (`.key` sidecars ride in the same prefixes as their artifacts):

- `coverage` → `build/<sha>/coverage.pmtiles`
- `aggregate` → `pmtiles/` + `contour/` + `soundings/` + `depare/` (also pushed every ~20 min mid-stage by a background loop; `rclone copy` is idempotent, and a file caught mid-write copies torn but is overwritten by the next pass — the final post-stage push is authoritative)
- `downsample` → `pmtiles/` (no covering publish — the retired diff was the only consumer of a previous covering)
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

A `bbox` build is otherwise **self-contained**: it skips the hydrate and pushes **nothing** to the shared store — only its window `build/<sha>/` outputs, which cannot corrupt the planet store. So `planet.pmtiles`/`vector.pmtiles` from a bbox build reflect only the window (compare a bbox build's tiles over the bbox, not against the planet); only `coverage.pmtiles` was ever window-scoped for other reasons. A bbox build never releases.

### One build at a time, globally

The workflow-level `concurrency: r2-store` group (no `cancel-in-progress`) exists because two builds mutating the incremental store concurrently corrupt it — and it's shared with `sources.yml` (whose prepared-source syncs use `--delete`), so a build never interleaves with a source refresh. Don't scope it per-ref.

### No `--delete`; markers last; prune guarded twice

- **No `--delete` on shared prefixes** (`pmtiles/`, `contour/`, `aggregation/`) — pushes are `rclone copy`, so a re-tile leaves the old tile's key behind rather than clobbering a concurrent write. Deletion is done deliberately by the prune.
- **`manifest.json` last.** It's pushed only after planet + overlays + vector + coverage have all landed, so its presence is a true "complete build" marker for `release.yml`.
- **The prune is guarded twice.** A covering re-tile (a source's footprint/maxzoom shift) orphans the old stems; the prune computes the full orphan list per prefix, **refuses to run if more than 25% of a prefix reads as orphaned** (a real re-tile orphans a slice; "most of the store" is always a bad stems list), and prunes **pmtiles before the vector FGBs** — a deleted pmtiles self-heals (missing → stale key → rebuilt, regenerating its vector FGBs), a deleted FGB does not, so a wrongful prune costs at most a rebuild, never a silent hole in `vector.pmtiles`. `.key` sidecars prune with their artifacts. Any new deletion of shared state must follow the same shape. (`bundle.py` also filters orphans at bundle time via `covering_stems`, so the prune is storage hygiene, not correctness — but it keeps the store from growing dead keys.)

### Code and config changes rebuild themselves

The historical footgun this replaces: the old covering diff only saw source coverage, so a change to what a tile _contains_ (smoothing, contour levels, encoding, depare logic) marked nothing dirty and shipped a stale planet unless someone remembered `force: true`. The content-hash keys close it — each stage's key hashes the modules and resolved config that produce it, so exactly the affected artifacts rebuild on the next dispatch. `force` remains only as the escape hatch for a corrupted store.

### Pipeline code stays R2-agnostic

`pipelines/*.py` reads and writes the local `store/` and knows nothing about R2, rclone, or the box — all cloud plumbing (hydrate/push, `/vsicurl` bases via env vars like `SOURCE_VSI_BASE`/`LANDMASK`/`WATERMASK`, `AGG_PROCESSES`/`GDAL_CACHEMAX` sizing) lives in the workflow. Keep it that way: it's what makes `just planet` / `just preview` run identically on a laptop. The box's build is just `just planet` decomposed into per-stage `docker run`s with pushes interleaved — the recipes and their order are byte-identical to a local build.

### Pinned rclone

The box downloads the **pinned, sha256-verified rclone 1.74.4** (same as `sources.yml`/`release.yml`), not apt's 1.60.1 — the old version records the `x-amz-version-id` R2 now returns on uploads and then 501s on its post-upload `HEAD ?versionId=` verification, turning every transfer into noise that buries real failures.
