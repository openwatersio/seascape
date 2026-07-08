# Coverage as its own tileset (coverage.pmtiles)

## Goal

Ship the source-provenance layer for real. Today it doesn't exist in any published
build: the planet bundle silently drops `coverage` because the footprint polygons
never reach the CI bundle job, and the viewer's "Source coverage" checkbox renders
nothing — click-to-identify names every point "GEBCO (global)". Fixing the supply
alone isn't enough, because coverage can't live in `vector.pmtiles` at any zoom
range without breaking something (below). Serve it as its own small tileset with
its own low maxzoom and let MapLibre's per-source overzoom carry it to every zoom.

## Why it can't stay in vector.pmtiles — the zoom-range trap

A tile-joined archive makes every layer share one zoom range, and coverage is the
one layer that fits at neither end:

- **Tiled to the shared max (z14, today's `_coverage_pmtiles(maxz)`):** footprints
  are sea-sized *fill* polygons (EMODnet = all European waters). tippecanoe emits a
  tile for every zoom across a feature's whole extent and `tile-join` unions tile
  sets, so the archive gains millions of deep-ocean tiles containing nothing but a
  polygon slice — tiles contours/soundings/drying never create. Only survivable
  today because the layer is being dropped.
- **Tiled low (z8):** above z8 the joined tiles still exist (contours), just without
  a coverage layer — and MapLibre only overzooms a missing *tile*, never a missing
  layer. Click-to-identify silently dies above z8. This is the drying/soundings
  vanishing-layer bug again, but the fix used there ("tile to the shared max") is
  the first bullet.

A separate tileset dissolves the trap: `coverage.pmtiles` ends at z8, MapLibre
overzooms it independently of the vector source, and the fat, rarely-toggled
provenance bytes leave the hot vector path entirely.

## Part 1 — get the footprints (and maxzooms) into CI

The source stage builds `store/polygon/<id>.gpkg` (the per-source union footprint
`contour_run._coverage_geojson()` reads), but the sources job syncs only
`store/source/<id>` to R2 and no later job pulls polygons — `build.yml` mentions
"polygon" only in comments. `_coverage_geojson()` then globs an empty dir, returns
`None`, and `_finalize_contours` takes the shard-job fallback ("coverage is dropped
from the join when no footprints are present locally") on every planet build.

- **sources job:** after the store sync, `aws s3 cp store/polygon/<id>.gpkg
  s3://$DATA_BUCKET/bathymetry/polygon/<id>.gpkg` (guarded: streaming-only sources
  like CUDEM may have no polygon). Same `.recipe-hash` skip as the rest of the
  recipe — the polygon only changes when the source rebuilds.
- **Build coverage in the `plan` job**, not contour-bundle: plan has the covering
  locally, so `_source_maxzooms()` works unchanged (it reads the newest covering's
  aggregation CSVs). contour-bundle has no covering, and `metadata.json` `max_zoom`
  is only an optional cap — absent for DDM/EMODnet/vaklodingen/AusBathyTopo/both
  GSCs — so building there would need a new maxzoom sidecar for no benefit.
  The plan job pulls `bathymetry/polygon/*.gpkg` into `store/polygon/`, runs
  `just coverage` (below) in the container, and pushes
  `build/<sha>/coverage.pmtiles`. Coverage depends on footprints + covering only —
  nothing from aggregate — so plan is also the earliest it can build.
- **Make the empty case loud where it's wrong:** `_coverage_geojson()` returning
  `None` stays valid for contour shard jobs, but the new `coverage` recipe must
  fail (non-zero) when it finds no polygons — a planet build without footprints is
  a broken build, not a fallback. The silent drop is how this bug shipped.

## Part 2 — bundle standalone

In `contour_run.py` (the footprint code already lives there):

- `_coverage_pmtiles(maxz)` becomes `coverage_bundle()` → `store/bundle/coverage.pmtiles`,
  CLI `contour_run.py coverage`, Justfile recipe `coverage`, wired into the local
  `just planet` flow. `_finalize_contours` loses its coverage input and the
  "dropped from the join" branch — the single tile-join no longer sees coverage.
- tippecanoe: `-l coverage -Z 0 -z 8 --no-tile-size-limit` (keep footprints whole;
  `COVERAGE_MAX_ZOOM` env knob). z8 loses nothing: footprints are already
  simplified to 0.001° (~100 m) and a z8 tile's 4096 MVT grid resolves ~37 m —
  geometry fidelity is simplify-bound, not zoom-bound. Global tile count at z8 is
  bounded at 65k and footprints cover a fraction of it.
- `bundle_maxz` deliberately NOT used — that helper exists to keep layers *inside*
  the joined tileset alive to its max; coverage's whole point is to leave the join.
- Ships automatically: `worker/seed.sh` seeds `store/bundle/*.pmtiles` and
  release.yml's rclone promotes everything except build scratch
  (contour-shards/bundle-frags), manifest last. No manifest change: the Worker
  reads zooms/bounds from the pmtiles header, like `/vector.json` does;
  `source_ids` (viewer palette) is already there.

## Part 3 — Worker

Two additions in `worker/src/index.ts`, both reusing the existing `pm()`/`tile()`/
`sendTile` machinery and the content-ETag + colo-cache path (release-scoped cache
keys make it self-invalidating; nothing new to purge):

- **`GET /coverage.json`** — TileJSON like `/vector.json`: header-derived
  minzoom/maxzoom/bounds from `coverage.pmtiles`, tiles
  `${tilesBase}/coverage/{z}/{x}/{y}.pbf`, the `coverage` vector_layer entry
  (`source_id`, `source_name`, `source_maxzoom`), manifest attribution.
- **`GET /coverage/{z}/{x}/{y}.pbf`** — match a `/coverage/...` prefix before the
  existing tile regex; serve from `coverage.pmtiles`, 204 on a miss (same `noTile`
  contract as vector). Out-of-range x/y → 204, uncached.
- Remove the `coverage` entry from `/vector.json`'s hardcoded `vector_layers`.
- A missing `coverage.pmtiles` (old release being served by a new Worker) must
  degrade to 204s/an empty TileJSON, not 500s — same tolerance the manifest code
  extends to pre-grid releases.
- Tests: `coverage.json` shape, a tile round-trip, miss → 204, absent archive →
  degraded not thrown (alongside the existing `*.test.mjs`).

## Part 4 — style package + viewer

- `style/index.ts` `sources()` gains
  `"seascape-coverage": { type: "vector", url: `${tilesBase}/coverage.json` }`;
  the four `source-*` layers move `source:` to it (a `coverage` source-name param
  next to `dem`/`vector`). `style()` output — which the Worker serves at
  `/style.json` — picks the change up from the same function.
- Versioning: by the letter of the package's policy (semver against the tile
  schema) removing `coverage` from the vector tileset is a major. In practice no
  published build has ever contained the layer, so nothing can depend on it;
  recommend shipping as a minor with a CHANGELOG note saying exactly that, and
  reserving the major for schema changes that can actually strand a consumer.
- Viewer (`index.js`): no logic change — `sourceAt()` queries by layer id
  (`source-fill`) and the checkbox toggles visibility; both follow the layer to
  its new source. Rebuild `style/dist` (wrangler dev and `vite build` read dist).

## Rejected alternatives

- **Coverage in vector.pmtiles at full depth, boundaries-only:** outlines tile
  cheaply but click-to-identify needs fills; `queryRenderedFeatures` on lines
  can't answer "which footprint contains this point".
- **Worker-side merge (inject coverage into vector tiles at request time by
  overzooming coverage.pmtiles and splicing layers):** re-encoding MVT per request
  on the hot path to avoid one extra source declaration — all cost, no benefit.
- **Sidecar maxzoom file for contour-bundle** instead of building in plan: works,
  but adds a build artifact and a second home for "which zoom is this source" when
  the covering — the authoritative answer — is already sitting in the plan job.

## Verification

1. `just preview` (Æbelø/Odense bbox): `store/bundle/coverage.pmtiles` exists;
   `pmtiles show` says z0–8; the z8 tile at Æbelø has DDM + EMODnet footprints
   with real `source_maxzoom` (10, not 0).
2. `vector.pmtiles` no longer contains a coverage layer at any zoom.
3. Dev worker: `/coverage.json` sane; `/coverage/8/135/80.pbf` → 200,
   `/coverage/8/0/0.pbf` (open ocean) → 204; `/vector.json` no longer lists it.
4. Viewer at z13 over Æbelø: checkbox shows footprints (overzoomed z8 fills),
   click names DDM — not "GEBCO (global)" — and deepest-wins ordering is exercised
   where DDM overlaps EMODnet.
5. CI dry-run: plan job fails loudly if `bathymetry/polygon/` is empty.
