# Scaling the Global Build

How to make the global terrain + contour build complete reliably on free
GitHub-hosted runners. This is purely about **build execution** — fitting the
work under a single runner's limits. Combining multiple data sources is a
separate concern; see [MOSAIC-PLAN.md](MOSAIC-PLAN.md).

## The two walls we hit

A single monolithic global build at z0–9 exceeds one standard runner on both
axes (observed on run 27520665564, branch `gebco-2026-publish`):

| Job     | Failure | Cause |
| ------- | ------- | ----- |
| terrain | cancelled at **exactly 6h00m** | GitHub's hard per-job ceiling. rio-rgbify resamples the global DEM per tile; z9 alone is 262k tiles. No in-job optimization fits >6h under 6h. |
| contour | **disk full** (61 MB left of 119 GB) | 3.9M global contour features × 3 FlatGeobuf copies + the >4 GB smoothed DEM. Chaikin (5 iterations ≈ 32× vertices) blows `_smooth.fgb` to tens of GB. Freeing more runner disk only delayed it. |

Both point at the same fix: **stop processing the whole globe in one job.**

## Lever 1 — Shard by region

GitHub gives public repos ~20 concurrent runners free. Use horizontal scale
instead of one fat (paid) runner.

Shard the **expensive high-zoom tail** geographically; keep the cheap low
zooms global:

1. **One global low-zoom job** — z0–7 (~22k tiles, minutes) from the full DEM.
   Cannot be sharded: a z0 tile *is* the whole globe.
2. **Cell matrix for z8–9** — partition the globe into N tile-aligned bboxes,
   one matrix job per cell building **both** terrain + contour for its cell.
   z9 is 262k tiles total → 16 cells ≈ 16k z9 tiles each, comfortably under 6h,
   up to 20 running at once.
3. **Merge job** (`needs:` all cells + low-zoom) — download artifacts,
   `scripts/merge-tiles`, `pmtiles convert`, deploy.

Fixes both walls at once: terrain wall-clock drops ~N×; each contour cell
handles ~1/N of the features, so the Chaikin blow-up and disk stay small.

### Two rules that keep the merge trivial

- **Cell boundaries must be tile-aligned.** Snap each cell bbox to the z8 tile
  grid. Then every output tile belongs to exactly one cell → `merge-tiles`
  never sees a collision, no double-drawn contours.
- **Buffer the DEM input, not the tile output.** Clip each cell's source DEM
  with a small margin beyond its bbox so edge tiles get correct bilinear
  resampling and contour lines don't die at the seam — but restrict *emitted*
  tiles to the cell's tile range. Input overlaps; output is disjoint.

### Terrain and contour shard differently

`gdal_contour` generates **every** contour feature from the DEM in a single
pass — the expensive, disk-filling step — and that pass is independent of tile
zoom. So a "global low-zoom contour job" would regenerate all ~3.9M features
and fill the disk exactly like the failure we're fixing. Contour cost can only
be sharded *geographically* (clip the DEM before contouring). Terrain has no
such pass — rio resamples per tile, cheap at any zoom.

So the two pipelines split the work differently:

- **Terrain** — low-zoom global job (z0–7, full DEM, cheap) + geographic cells
  for z8–9. Raster MBTiles have a real `tiles` table and the tiles are disjoint
  → `merge-tiles` sqlite `INSERT OR IGNORE` union.
- **Contour** — low-zoom job contours a **downsampled** global DEM (coarse →
  few features → small disk → fine for z0–7) + geographic cells (clip → contour
  → tile z8–9) for detail. tippecanoe writes `tiles` as a **VIEW** (can't be
  sqlite-indexed), so the bands are merged with **`tile-join`** (straight to
  PMTiles), not the sqlite union.

The only contour-specific additions: one `gdalwarp -tr <coarse>` to decimate the
DEM for the low-zoom band, and `tile-join` for the merge. (Alternative
considered: cells own full z0–9 and merge with `tile-join` over overlapping
low-zoom tiles — rejected as less uniform and it simplifies low zoom unevenly
per cell.)

### Already built

Most of the machinery exists on the `mosaic` branch: `scripts/build` does
bbox + zoom-band builds via `OUT_MBTILES`; `terrain`/`contour` honor
`BBOX`/`MIN_ZOOM`/`MAX_ZOOM`; `scripts/merge-tiles` unions bands
(`INSERT OR IGNORE` / `tile-join`). The new work is the **CI matrix** + a
`build` tweak to split the global base into low-zoom-global + high-zoom-cells.

### Cell count is not capped at 20

The ~20-runner limit is a *concurrency* cap, not a job cap — define as many
cells as you like and GitHub queues the overflow, draining them through ~20
runners in waves. So oversharding is free to *schedule*, and it
**load-balances uneven density for free**: a trench/coastline-dense cell can
occupy one runner for its full duration while another runner churns through
several near-empty ocean cells. Uniform geographic cells have wildly uneven
feature counts, so more, smaller cells smooth out stragglers better than a
hand-tuned 16.

The ceiling: each cell re-pays fixed per-job cost (checkout + image pull + DEM
clip/pull), and the merge job's fan-in grows with cell count. So finer isn't
free — past some point the overhead dominates the work. `# ponytail: start at
16, then try 32–64 and compare wall-clock; stop when per-job overhead eats the
gains.`

Tuning knobs: the low/high split zoom and the cell count.

## Lever 2 — Cache the smoothed DEM (native first)

So a re-run (tweaked contour color, new zoom ceiling) doesn't recompute hours
of unchanged work. **Cache the one artifact that's expensive *and* shared: the
global smoothed DEM.** Computed once (~hours, >4 GB), pulled by every cell.
Once sharded, the per-cell contour/tile work is cheap enough not to be worth
caching — so there's only this one thing to cache, which is what makes the
native tools viable.

Climb the ladder; stop at the first rung that holds:

### Rung 1 — `actions/cache` (try this first)

The native cache caps at 10 GB/repo, but post-sharding the smoothed DEM is the
*only* entry — one ~5–8 GB artifact fits. Key it by content so it
regenerates only when an input changes:

```yaml
- uses: actions/cache@v4
  with:
    path: work/*_smoothed.tif
    key: smoothdem-${{ env.GEBCO_YEAR }}-${{ hashFiles('scripts/smooth-dem','scripts/blur') }}
```

`hashFiles` over the producing scripts + the immutable `GEBCO_YEAR` *is* the
content key — change either and the key misses → regenerate. No bash helper,
no extra creds. The build just checks `cached "$SMOOTHED"` as it already does;
the cache step restores the file before the job runs.

Caveats to measure: confirm the compressed DEM lands under 10 GB, and that
other caches don't evict it (LRU across the 10 GB pool).

### Rung 2 — fall back to R2 only if Rung 1 doesn't fit

Move here only if the DEM exceeds 10 GB compressed, eviction churns it, or you
later need to cache several per-cell intermediates too. Reuses the existing R2
access (`aws s3 cp --endpoint-url` + `R2_*` secrets from `publish-r2`) and the
local `cached()` guard — pull-before, push-after. Helper in `config.sh`:

```bash
# Content-addressable R2 cache. Empty R2_CACHE (local dev) → no-op.
R2_CACHE="${R2_CACHE:-}"          # e.g. s3://bucket/cache, set in CI
ckey() { printf '%s\0' "$@" | sha256sum | cut -c1-16; }
cache_pull() { [[ -n "$R2_CACHE" && -z "$FORCE" ]] && \
  aws s3 cp "$R2_CACHE/$2/${1##*/}" "$1" --endpoint-url "$R2_ENDPOINT" --quiet 2>/dev/null; }
cache_push() { [[ -n "$R2_CACHE" ]] && \
  aws s3 cp "$1" "$R2_CACHE/$2/${1##*/}" --endpoint-url "$R2_ENDPOINT" --quiet 2>/dev/null || true; }
```

```bash
KEY=$(ckey "$(cat "$SCRIPT_DIR"/smooth-dem "$SCRIPT_DIR"/blur)" "$GEBCO_YEAR" "$DEM_BLUR$MASK_BLUR$SLOPE_LOW$SLOPE_HIGH")
if cached "$SMOOTHED" || cache_pull "$SMOOTHED" "$KEY"; then log "smoothed DEM: cache hit"
else "$SCRIPT_DIR/smooth-dem" "$DEM" "$SMOOTHED"; cache_push "$SMOOTHED" "$KEY"; fi
```

Same content key, just self-managed: `hash(producing scripts + source
identifier + params)`. Get right if you go here:
- **Don't hash the 7 GB input** — key the root on `GEBCO_YEAR` (immutable per
  year). Hash only the scripts.
- **Chain keys** only if caching downstream stages: a stage folds its input's
  key into its own so changes cascade. Skip until needed.
- **R2 lifecycle rule** (expire `cache/` after ~30 days) so stale content-keys
  don't pile up — a bucket rule, not GC code.

> `upload-artifact` is for the *intra-run* cell→merge handoff (needed by
> sharding anyway), not cross-run caching — artifacts aren't content-keyed and
> expire. Different job, don't conflate it with the DEM cache.

## Order of work

1. Shard first — it's the prerequisite (caching a job that can't finish doesn't
   help). Get a green global build.
2. Add the smoothed-DEM cache second — turns the green build fast on re-runs.
3. Everything else (per-stage caching, key-chaining) is deferred until the DEM
   cache alone proves insufficient.
