# The planet build

How `.github/workflows/build.yml` turns `sources/` into servable tilesets, on one ephemeral machine. Companion to [CONTRIBUTING.md](../CONTRIBUTING.md#ci--build--release), which covers the release flow; this documents the build itself and the constraints every change to it must respect.

Point-in-time. This is **phase 4** of [the production-build plan](plans/2026-07-09-production-build.md) ("immutable store + GC"): the matrix fan-out is gone (phase 2), content-hash keys drive incrementality (phase 3), and now the store is **immutable + content-addressed** — every artifact carries its key in its filename, each planet build publishes a **store manifest** and flips a one-object **pointer** last, hydrate reads exactly the referenced artifacts, and deletion is a separate scheduled **GC** (`gc.yml`), the only deletion path. The covering-retile prunes and their guards are deleted, not ported.

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
| `pmtiles/`                          | Per-tile terrain + overview pmtiles, **content-addressed** `<stem>-<key12>.pmtiles` |
| `contour/`, `soundings/`, `depare/` | Per-tile vector intermediates, `<stem>-<key12>.{fgb,geojson}` (+ a `<stem>-<key12>.empty` marker when a fork legitimately produced no features) |
| `store/manifests/<ulid>.json`       | One per planet build — the content name of every artifact that build's covering left |
| `store/manifest.json`               | The **pointer**: names the current manifest; one atomic PUT, flipped last          |
| `build/<sha>/`                      | This build's outputs (below)                                                       |

The `pmtiles/`/`contour/`/… prefixes carry **no `.key` sidecars** — the key is in the filename, so freshness is "the named file exists". Superseded keys (a re-tile / re-key / retired diff-era `aggregation/<ulid>/` coverings / legacy mutable names) linger unreferenced until `gc.yml` sweeps them.

Planet builds hydrate **selectively**: the store artifacts the manifest names, plus `bounds.csv` + `catalog.json` + footprints, come down before `cover` (the covering, the tile keys, and the coverage layer need them); then the masks hydrate (their content hashes enter the tile keys, so the manifest step below must see the same mask state aggregate will), and once the covering exists, `just sources-manifest` derives the exact `(source, filename)` union the key-stale tiles reference and the box `rclone copy --files-from`s those files into the local store — `aggregate` then reads everything from local disk (the preview-local path: no `SOURCE_VSI_BASE`/`LANDMASK`/`WATERMASK` env, so `config.source_path` and the landmask defaults resolve `store/…`). Two real runs that streamed sources per tile banked zero tiles in 2.5–3.9 healthy hours — a coastal macrotile re-read the same S-102/CUDEM bytes over `/vsicurl` for every tile. A legacy bounds row whose filename is already an absolute `/vsi` path is filtered out of the hydrate list, passes through `source_path` untouched, and still streams — acceptable fallback. `bbox` builds stay fully streaming (self-contained, no volume), exactly the old behavior.

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
4. **Hydrate** — **manifest-driven** (planet only): read the store pointer, fetch the manifest it names, `rclone copy --files-from` exactly the referenced artifacts. Unreferenced garbage costs no hydrate bytes. No pointer = the first immutable build → clean rebuild (below). After `cover`, hydrate the masks + the stale tiles' source files (see above).
5. **Build + push + publish the store** — run each stage through `docker run … just <stage>`, pushing to R2 between stages (below); after `downsample`, assemble the store manifest and flip the pointer.
6. **Destroy** — an `if: always()` step deletes the server, the volume (with a detach wait-loop), and the SSH key.

There is **no prune step** — deletion is out-of-band (`gc.yml`, below).

The `build` job has `timeout-minutes: 350` — the max available, since GitHub-hosted runners hard-cap jobs at 6 h. Routine incrementals fit easily; a full forced planet rebuild on `ccx33` may exceed it — the build resumes on re-dispatch by hydrating the **last completed build's** manifest (its artifacts skip as fresh) and rebuilding only what changed. A build interrupted before its pointer flips redoes its own new work (it's in no manifest yet), so resume granularity is "the last completed build"; `ccx63` (quota pending) makes a forced full rebuild fit one window regardless.

## The incremental model

Rebuilds are cheap because every store artifact is **content-addressed** ([pipelines/keys.py](../pipelines/keys.py)): its key — a short hash of its inputs ‖ the pipeline modules that produce it ‖ the resolved config the stage read ‖ the toolchain image tag — rides IN its filename (`<stem>-<key12>.<ext>`). Freshness is "the named file exists"; there is no sidecar to match. Anything that moves the key — changed input, code, config, bumped toolchain — writes a NEW name, and a stage rebuilds a name that isn't present. The old covering diff, its `.done` markers, the `.key` sidecars, and the downsample mtime cascade are all gone.

- **Per-fork granularity.** Each aggregation tile carries four keys — terrain, contours, soundings, depare — sharing the merged DEM's determinants (covering row, each intersecting source's `catalog.json` recipe hash + priority/maxzoom/offset/land_clamp, the mask identity, smoothing knobs) plus each fork's own modules and config. A tile re-runs iff any fork's content name (or its `.empty` marker) is absent; fresh forks are skipped. A `CONTOUR_LEVELS` change re-merges tiles and writes new contour/depare names but rewrites no terrain pmtiles, so downsample and the terrain bundle skip entirely.
- **Write discipline.** A fork **supersedes** its stem's other-key siblings (all keys + the `.empty` marker), then **publishes** atomically (write a temp, `os.replace` into the content name), or writes a `.empty` marker when it produced nothing. So a crash mid-rebuild leaves nothing at the current key → reads stale, never a torn artifact vouching as fresh. The per-sha bundle outputs (`store/bundle/*` + `manifest.json`) stay keyed by a `.key` sidecar (they're never hydrated) and follow the same invalidate-before-write / atomic-write rule.
- **The key cascade replaces the mtime cascade.** An overview's key hashes its children's keys (read off their content filenames), so a rebuilt child yields a new overview name that isn't present → stale, cascading up by construction; a missing artifact self-heals inherently.
- **Resume.** Each stage is pushed as it finishes, and the long aggregate stage is additionally pushed every ~20 minutes by a background loop. On re-dispatch the box hydrates the **last completed build's** manifest — its artifacts skip as fresh — and rebuilds only what changed (a build interrupted before its pointer flips redoes its own new work, since that new work is in no manifest yet).
- **`force: true` survives as an escape hatch only** (`FORCE_REBUILD` ignores freshness). Not required for correctness after code/config changes — the keys see those. Reach for it when the store itself is suspect.
- **Bootstrap:** the first phase-4 build finds no store pointer → clean rebuild (hydrate nothing). The phase-4 content-address rename makes every phase-3 (logical-named + sidecar) artifact stale anyway, so a full re-key rebuild happens regardless; adopting the old names by renaming isn't worth the fragility. Once this build's manifest lands, the legacy names + sidecars become GC debris.

## The push protocol

Stages push to R2 as they finish (one `rclone copy` pass — the content name self-marks, so there's no artifact-before-sidecar ordering to get right):

- `coverage` → `build/<sha>/coverage.pmtiles`
- `aggregate` → `pmtiles/` + `contour/` + `soundings/` + `depare/` (also pushed every ~20 min mid-stage by a background loop; `rclone copy` is idempotent and skips unchanged keys)
- `downsample` → `pmtiles/` (overviews)
- **store manifest + pointer** → `store/manifests/<ulid>.json`, then the pointer `store/manifest.json` **last** (one atomic PUT). This is the store's completeness marker: the next build's hydrate and the GC see the whole old world or the whole new one.
- `bundle` → `build/<sha>/planet.pmtiles` + `overlay-*.pmtiles`
- vector (`soundings`, `depare`, `contours`) → `build/<sha>/vector.pmtiles`
- `build/<sha>/manifest.json` → **last** in the build domain, `release.yml`'s completeness marker

Every push is `rclone copy`, **never `sync --delete`** — deletion is out-of-band (`gc.yml`). A crash mid-push leaves the old pointer over a complete old store; this build's pushed-but-unreferenced objects are GC debris.

## Requirements for all changes

Constraints every modification to the build must respect.

### Every step must accept BBOX

A dispatch with `bbox` set builds a regional slice — the primary way to test build changes without a multi-hour planet run. What "accept BBOX" means still splits three ways:

- **Rebuild-scoping steps** (cover, coverage, aggregate, downsample, the vector forks) honor `BBOX` (empty = planet): the box passes `-e BBOX` into every `docker run`, and the covering carries the scope transitively.
- **Shared-metadata steps** (source prep / `bounds.csv`, the land + water masks) are **not in this build at all** — they moved to `sources.yml`, which is always global. A build never writes them.
- **Planet-scoped-pointer steps** (hydrate, the store manifest + pointer flip) **skip** when BBOX is set — a regional run never writes a planet-scoped pointer (`store/manifest.json`), and content-addressed artifacts from a window can't corrupt the planet store (worst case they add unreferenced keys for the GC). The manifest/pointer block is guarded `if [ -z "$BBOX" ]`.

A `bbox` build is otherwise **self-contained**: it skips the hydrate and flips no pointer — it may push content-addressed store artifacts (harmless, unreferenced garbage) but its `build/<sha>/` outputs reflect only the window. So `planet.pmtiles`/`vector.pmtiles` from a bbox build reflect only the window (compare a bbox build's tiles over the bbox, not against the planet). A bbox build never releases.

### One store-mutating workflow at a time, globally

The workflow-level `concurrency: r2-store` group (no `cancel-in-progress`) exists because two writers mutating the store concurrently corrupt it — it's shared with `sources.yml` (whose prepared-source syncs use `--delete`) AND `gc.yml` (the only deletion path), so a build never interleaves with a source refresh or a GC. Don't scope it per-ref.

### No `--delete`; the pointer is the completeness marker; GC is the only deletion path

- **No `--delete` anywhere but GC.** Pushes are `rclone copy`, so a re-tile / re-key leaves the old object behind as unreferenced garbage rather than clobbering a concurrent write. It's collected out-of-band.
- **`store/manifest.json` (the store pointer) flips last** — after every artifact it references is up — so the next build's hydrate and the GC always see a complete store. **`build/<sha>/manifest.json`** likewise flips last in the build domain, `release.yml`'s completeness marker.
- **GC (`gc.yml`) is the only deletion path.** A re-tile is now purely additive (new content names + a new manifest, pointer flips to it), so no build-time prune exists — the old 25%-guard / pmtiles-before-FGB ordering is deleted, not ported. See [Garbage collection](#garbage-collection).

### Code and config changes rebuild themselves

The historical footgun this replaces: the old covering diff only saw source coverage, so a change to what a tile _contains_ (smoothing, contour levels, encoding, depare logic) marked nothing dirty and shipped a stale planet unless someone remembered `force: true`. The content-hash keys close it — each stage's key hashes the modules and resolved config that produce it, so exactly the affected artifacts rebuild on the next dispatch. `force` remains only as the escape hatch for a corrupted store.

### Pipeline code stays R2-agnostic

`pipelines/*.py` reads and writes the local `store/` and knows nothing about R2, rclone, or the box — all cloud plumbing (hydrate/push, `/vsicurl` bases via env vars like `SOURCE_VSI_BASE`/`LANDMASK`/`WATERMASK`, `AGG_PROCESSES`/`GDAL_CACHEMAX` sizing) lives in the workflow. Keep it that way: it's what makes `just planet` / `just preview` run identically on a laptop. The box's build is just `just planet` decomposed into per-stage `docker run`s with pushes interleaved — the recipes and their order are byte-identical to a local build.

### Pinned rclone

The box downloads the **pinned, sha256-verified rclone 1.74.4** (same as `sources.yml`/`release.yml`), not apt's 1.60.1 — the old version records the `x-amz-version-id` R2 now returns on uploads and then 501s on its post-upload `HEAD ?versionId=` verification, turning every transfer into noise that buries real failures.

## Garbage collection

Deletion is out-of-band: [`.github/workflows/gc.yml`](../.github/workflows/gc.yml) runs weekly (Tuesday) + on dispatch (with a `dry_run` input, default true for a manual run; the cron always deletes), sharing the `r2-store` concurrency group so it can never run during a build or a source refresh. It is the **only deletion path anywhere**.

It deletes:

- store artifacts under `pmtiles/`/`contour/`/`soundings/`/`depare/` **not referenced by the union of the last N = 3 store manifests** (keeps a couple of builds of hydrate/rollback headroom). Pre-phase-4 mutable-named artifacts + their `.key` sidecars fall out here for free — they sit in those prefixes and no manifest names them;
- the retired diff-era `aggregation/<ulid>/` coverings (phase 4 hydrates from the manifest — nothing reads a covering from R2);
- volatile sources' retired `source/<id>/.recipe-hash` markers (their hash lives in `catalog.json` now).

It **never touches**: `build/<sha>/` (an R2 lifecycle rule collects it after 7 days — see `release.yml` — and releases are promoted to the separate tiles bucket, so keeping `build/<sha>/` out of GC scope is the conservative choice), source COGs / `bounds.csv` / `catalog.json`, the **live** `landmask/.recipe-hash`, the store manifests, or the pointer.

Guards: it refuses to delete anything unless the pointer **and** every one of the last N manifests fetch as valid JSON, and unless the referenced set actually matches objects present in the store (a path/listing mismatch must never delete the world); it logs a full per-prefix inventory (kept/deleted) before deleting; and it deletes in bounded batches. The Collect arithmetic + every refusal guard live in one script, [`scripts/gc-collect.sh`](../scripts/gc-collect.sh) — gc.yml invokes it with the rclone backend, and its test [`pipelines/test_gc.sh`](../pipelines/test_gc.sh) (`just test-gc`, run by ci.yml on every push) invokes the same script with the local backend against a synthetic tree, covering the happy path and each refusal — so the workflow and its test cannot drift.

Operationally: before the cron's first live deletion, run a manual dispatch with `dry_run=true` (the default) and eyeball the inventory — kept/deleted counts per prefix should match expectations (a healthy store deletes a small superseded slice; "most of a prefix" flagged means stop and investigate, though the guards should have refused first).

Accepted debt: `store/manifests/*.json` grow unbounded — one small JSON per planet build, so years of weekly builds cost megabytes, not money. If it ever matters, teach the GC to keep the newest ~20 manifests (they're ULID-named, so retention is one `tail -n`); deliberately not built now to keep the first GC's delete surface minimal.
