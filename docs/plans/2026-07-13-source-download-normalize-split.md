# Split source preparation into download-once + normalize-many — planning doc

*Written 2026-07-13. Point-in-time; the code is the source of truth.*

Status: proposed. Motivated by iterating on the WSV DGM-W datum/masking (`sources/dgm_w`,
on `add-source-wsv-dgmw`): every tweak to `build_reference.py` / `source_datum.py` forced a
full re-download, because raw and normalized data share one directory and the raw is
destroyed mid-run.

## Problem: everything mutates one directory

A source's whole lifecycle lives in and rewrites one directory, `store/source/<id>/`:

1. [source_download.py](../../pipelines/source_download.py) writes `<id>_<i>.<ext>` there —
   **always fetches**, no skip-if-present.
2. [source_unzip.py](../../pipelines/source_unzip.py) extracts `*.tif`, then
   **`os.remove(zpath)`** — the raw archive is gone.
3. [source_datum.py](../../pipelines/source_datum.py) and
   [source_normalize.py](../../pipelines/source_normalize.py) each **mutate the tifs in place**
   (`os.replace(tmp, filepath)` at `source_datum.py:54` / `source_normalize.py:41`): raw →
   datum-referenced → COG.

The raw bytes exist only transiently, so any re-run must re-download and re-extract to get raw
back. The masking work (`--clamp-positive`, the datum-surface subtraction) is entangled with the
download only because they share the directory and the raw is deleted underneath the transform.

## Goals / Non-goals

**Goals.** Iterating on datum/masking reruns only the transform chain — seconds, zero network.
Raw source bytes are fetched once into a cache that later phases never mutate, so `source_datum`
/ `source_normalize` can be re-run to convergence against a pristine input. `just source <id>`
stays the single entry point and its CI invocation is unchanged.

**Non-goals.** Caching raw bytes across CI runs (runners are ephemeral; the payoff is local
iteration — see Gotchas). Reworking the mirrored sources (`cudem`, `cudem_third`, `noaa_s102`)
— they already fetch straight to R2 via [source_mirror.py](../../pipelines/source_mirror.py) and
map onto the download phase trivially. Changing the R2 store layout or `bounds.csv` shape.

## Approach: two directories, two phases

| Dir | Written by | Lifecycle |
| --- | --- | --- |
| `store/download/<id>/` | `download` phase | cache — fetched once, never deleted, skip-if-present |
| `store/source/<id>/` | `normalize` phase | regenerated each run from the cache → final COGs (pushed to R2) |

- **`just source-download <id>`** — writes raw archives/tifs to `store/download/<id>/` under a
  stable name. Idempotent: skip a URL whose cache file already exists (static sources);
  size/etag check for volatile ones. Does not unzip, does not delete. A `--force` flag re-fetches.
- **`just source-normalize <id>`** — clears `store/source/<id>/`, stages raw from the cache into
  it (extract zips / copy plain tifs), then runs the transform chain: `source_datum` →
  `source_normalize` → `source_bounds` → `source_polygonize` → `source_create_tarball`. Re-runnable
  with zero network. (For DGM-W the chain also carries `build_reference` ahead of `source_datum`.)
- **`just source <id>`** = `source-download` then `source-normalize` — unchanged, and still the
  shared `source_catalog` tail from the top-level [Justfile](../../Justfile) rides after it.

Win: the datum/masking loop reruns only `normalize`. CI still downloads once per run (the runner
is thrown away), so the concrete payoff is local iteration.

## Concrete changes

- **[source_download.py](../../pipelines/source_download.py)**: target `store/download/<id>/`;
  add skip-if-exists and a `--force` flag. Keep `fix_archive_ext` here so the cache holds a
  correctly-named `.zip` for the extract step to find. (~10 lines.)
- **[source_unzip.py](../../pipelines/source_unzip.py)**: read zips from `store/download/<id>/`,
  extract into `store/source/<id>/`, and **stop deleting the zip**. Moves into the normalize
  phase (extraction is cheap and keeps `download/` archive-pure). (~5 lines.)
- **Non-zip sources** (plain `.tif` / `.nc`, e.g. GEBCO members aside, DDM): stage by **copying**
  from `download/` → `source/` before the in-place mutation, so the cache stays pristine. A new
  tiny `source_stage` step, or fold the copy into the start of `source-normalize`. Copy, never
  move — `source_datum` / `source_normalize` overwrite in place.
- **Bespoke downloaders** (`source_download_greatlakes.py`, `_estuarine`, `_swissbathy`,
  `_tahoe`, `_african_lakes`, `_filelist`): each writes into `store/source/<id>/` today and some
  do more than fetch. Repoint their raw output to `store/download/<id>/` and move any
  transform-ish work into the normalize phase. Auditing these one by one is real work.
- **Every `sources/*/Justfile`**: split the ordered `default:` command list into a `download:`
  group and a `normalize:` group, with `default: download normalize` composing them. This is the
  bulk of the work — each recipe is bespoke. Mirrored sources (`cudem` et al.) are the easy case:
  `download: source_mirror`, `normalize:` empty (the shared catalog tail is all they need).
- **Top-level [Justfile](../../Justfile)**: add `source-download` and `source-normalize` (each
  delegating to the matching per-source group), keep `source` composing both + `source_catalog`,
  and update the `sources` loop.

## Alternatives considered

- **Copy-on-write / snapshot the tifs in place** (e.g. a `.raw.tif` sibling before each mutate,
  restore to re-run). Cheaper diff, but it litters `store/source/` with shadow copies, keeps the
  download coupled to the transform, and doesn't give a clean "fetched once" cache. Two dirs is
  the honest boundary.
- **Make `source_datum` / `source_normalize` non-destructive** (write to new names, chain by
  filename). Touches every downstream glob (`*.tif` everywhere) and the tarball/bounds contract;
  much larger blast radius for the same iteration win.

## Validation

- `source_download.py --check` / `source_normalize.py` self-checks keep passing; extend
  `source_download`'s to cover skip-if-exists and `--force`.
- [test_source_stage.py](../../pipelines/test_source_stage.py) already runs the transform chain
  offline from a pre-placed raster — the natural home for asserting `normalize` regenerates
  `store/source/<id>/` from a `store/download/<id>/` cache without touching the cache bytes
  (hash the cache before/after).
- Manual: `just source-download gebco` (a zip source) then `just source-normalize gebco` twice —
  second normalize is network-free and byte-identical, and `store/download/gebco/` is untouched.
- `just source <id>` in the `sources.yml` container path still produces the same
  `store/source/<id>/` artifacts (`bounds.csv`, `catalog.json`, COGs, tarball) it pushes to R2.

## Gotchas

- **CI gets no caching benefit.** [sources.yml](../../.github/workflows/sources.yml) runs
  `just source <id>` in one container and pushes only `store/source/<id>` to R2 (`aws s3 sync`,
  line ~306); `store/download/` is never persisted and the runner is destroyed after. So the
  split is purely a local-iteration win. Persisting the cache (R2 or `actions/cache`) is a
  separate follow-up, out of scope here. Keep `just source` = download + normalize precisely so
  the CI invocation line doesn't change.
- **Peak disk roughly doubles for zip sources during normalize.** Today `source_unzip` deletes
  each zip as it extracts; keeping the archive in `store/download/` *and* the extracted tifs in
  `store/source/` holds both at once. EMODnet already flags disk pressure (~7 GB zipped / tens
  unzipped) — on a standard runner this could tip it over. Mitigation where the cache doesn't
  help anyway (CI): let normalize consume-and-delete from `download/`, or keep the persistent
  cache local-only.
- **Copy, don't move, when staging.** `source_datum:54` and `source_normalize:41` both
  `os.replace(tmp, filepath)` over the staged file. If `source-normalize` *moves* raw out of the
  cache instead of copying, the first datum run destroys the cache and defeats the whole point.
- **Splitting each Justfile changes its bytes → one-time re-registration.** `sources.yml`
  short-circuits on `RECIPE_HASH = hashFiles('sources/<id>/**')`, which includes the Justfile.
  Editing every recipe moves every hash, so the next `sources.yml` run re-prepares all sources
  once. Expected and one-time; note it in the dispatch (same pattern the sources-mirror plan
  called out).
- **`store/download/` must be gitignored and stay out of the R2 push.** The existing
  `aws s3 sync store/source ...` naturally excludes it, but the `.gitignore` and any tarball/
  manifest globs should be checked so raw archives don't leak into an artifact.
- **Mirrored sources have no local `store/download/`.** Their "cache" is R2. Don't force
  `source_mirror` through the extract/stage dance — its download-phase mapping is the whole
  recipe, and its normalize phase is empty.
- **`source_datum` writes `datum.json` into `store/source/<id>/`**, not the cache — correct, it's
  a normalize-phase provenance artifact and gets regenerated each run. Staging must not clobber
  it with a stale copy from a previous run (clearing `store/source/<id>/` first handles this).

## Open questions

- One `source_stage` module vs. folding the copy/extract into `source-normalize` — implementer's
  call; `source_stage` is more testable, the fold is fewer files.
- Skip-if-present for volatile non-mirrored sources: is size/etag enough, or do any need a
  content hash? (Mirrored sources sidestep this — they diff against the previous `bounds.csv`.)
- Whether to land the CI cache-persistence follow-up now (R2-backed `store/download/`) or defer
  until local iteration proves the split's shape.
