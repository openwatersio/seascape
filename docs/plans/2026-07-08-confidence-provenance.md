# Confidence & provenance to the pixel — planning doc
*Written 2026-07-08. Point-in-time; the code is the source of truth. Reviewed by the gis
subagent; its correctness findings are folded in below.*

## Problem

CONTRIBUTING §Principles rule 2: *carry provenance and confidence to the pixel* — a
mariner must tell GEBCO-interpolated deep ocean from a surveyed 3 m coastline. The
source-**footprint** layer (`coverage.pmtiles`) already answers "which source's footprint
covers here." Three open issues remain:

- **#17** Per-source confidence grade + source identity on tiles (CATZOC-style). MVP: a
  coarse confidence attribute on the tiles + a viewer affordance to inspect it.
- **#18** GEBCO TID-based quality masking: "prefer measured over interpolated" + a per-pixel
  provenance band off the merge.
- **#19** (bug, prerequisite) The footprint layer never ships in CI builds — production tiles
  carry zero `coverage` features because `store/polygon/*.gpkg` + the covering CSVs never
  reach the `contour-bundle` job. #17 is only *visible in production* once this lands.

**Two clarifications that shrink the work:**

1. **#18's "prefer measured over interpolated" is already done by the merge order.** Sources
   merge by `(priority, maxzoom)` (`utils.py` grouping): GEBCO is priority 0 and coarsest, so
   any real regional survey already wins over it, and each GEBCO cell carries exactly one TID
   — there is no second GEBCO value to prefer against. So #18 has **no depth/blend decision to
   make**; what's left is carrying a per-pixel/region quality **label** forward. That's the
   whole of #18.
2. **Confidence is resolution-capped, not method-capped.** Real CATZOC (S-57 M_QUAL /
   S-101 QoS) encodes position + depth *accuracy* (A1 ≈ 0.5 m+1 %, B ≈ 1 m+2 %, C ≈ 2 m+5 %).
   A ~450 m GEBCO cell can't reach A/B no matter how it was measured; a source's grade is
   bounded by its grid spacing, not its survey pedigree. The rubric must bake in the cap.

TID (450 m category code) and S-102 band-2 uncertainty (per-node TVU in metres) are two very
different per-pixel signals; the flat per-source grade is the fallback. We build them
concretely, not behind a premature "provider interface" abstraction.

## Goals / Non-goals

**Goals.** Every source carries a resolution-capped CATZOC grade from `metadata.json` into
the tiles; the viewer surfaces the *winning* source's grade on click and can tint by it.

**Non-goals.**
- **No depth-changing blend, and confidence never suppresses a feature.** Display only —
  drive dashed/greyed "interpolated" styling from confidence, never gate *generation* of a
  contour/sounding. Suppressing a low-confidence *shallow* feature hides a shoal, the exact
  inverse of bias-shallow — most dangerous in coastal gaps where GEBCO is all there is.
- **Not an official CATZOC assessment.** A coarse, honest self-grade. Keep the letters for
  chart familiarity, label the legend "data confidence (CATZOC-style)", never render the
  official CATZOC star symbol.
- Datum work (Milestone 3) is separate.

## Approach

Ship **Phase 1 → 2**; they are the milestone MVP and deliver ~90 % of the safety value
(confidence where regional sources meet the coast). **Defer Phase 3** — its delta is mostly
deep-ocean polish and it carries a full-planet rebuild cost.

### Phase 1 — Footprint layer ships in CI (#19) — largely done, plus a perf fix

#19 is essentially resolved on-branch: the source job uploads `store/polygon/<id>.gpkg` →
`bathymetry/polygon/<id>.gpkg` (`build.yml:164-167`, with a rebuild-to-backfill guard
:130-135), `coverage_bundle()` fails loud on empty footprints (`contour_run.py:344`), and
**#66** moves the build to its own parallel job (off the aggregate critical path) +
BBOX-scopes previews. Phase 2 is unblocked.

**Perf (the cost #66 flags but doesn't fix).** The coverage tippecanoe keeps footprints
whole (`--no-tile-size-limit`, `contour_run.py:352`) and runs ~40 min, dominated by a few
huge intertidal-survey unions (`uk_surfzone` 76 MB, `infomar_10m` 40 MB). The tileset is only
z0-8 (overzoomed deeper), so metre-precise footprints are wasted bytes: simplify each
footprint to ≈z8 resolution (~600 m — today ogr2ogr does just `-simplify 0.001`≈100 m at
`:318`, with a min-feature floor so small sources don't collapse), which shrinks tiles enough
to **drop `--no-tile-size-limit`** and cut the build cost at the root. Do it while Phase 2 is
already editing `_coverage_geojson` — one pass, and it keeps the footprint tileset cheap for
everything built on it.

### Phase 2 — Per-source CATZOC grade (#17), resolution-capped

The MVP. Rides on the existing standalone `coverage` layer — no new tileset.

- **Rubric (respect the resolution cap).** Max grade by grid spacing, then downgrade for age/
  method: ≤~10 m modern survey → **A/B** (`noaa_s102`, `cudem*`); ~10–100 m regional DEM →
  **B/C** (`emodnet`, `ddm`, `vaklodingen`, `bodensee`); ~100–500 m compiled → **C/D**; GEBCO
  interpolated → **U**. GEBCO *measured* cells are still a 450 m grid → **C/D at best, never
  A/B**. Seed all sources by this rubric; flag for review (it's a rubric decision, not a
  guess).
- **`sources/*/metadata.json`** — add `"confidence": "A|B|C|D|U"`; document in
  `sources/README.md` (schema doc ~line 93).
- **`pipelines/contour_run.py:313-318`** — add `"source_confidence": meta.get("confidence")
  or "U"` to the coverage feature `properties` (`meta` already loaded at :308).
- **`worker/src/coverage.ts:34-40`** — declare `source_confidence: "String"`.
- **`style/index.ts`** — replace the static `flavor.coverage` (`:336`) in `source-fill`/
  `source-outline` with a `["match", ["get","source_confidence"], …]` ramp (green→grey); show
  the grade on `source-labels` (`:511`); add a legend.
- **`index.js`** click popup (`sourcesAt`, `:125-168`) — append `confidence: <grade>` for the
  **winning** source (consistent with the existing deepest-wins pick), `U` on the GEBCO
  fallback.

### Phase 3 — Per-pixel quality label (#18), deferred

Per §Problem #1 this is a *label*, not a blend. When built, deliver it by **attributing
polygons that already exist**, not a new fork/tileset/raster:

- **Cheap tier (no merge change).** Spatial-join the `depare` depth-area polygons
  (`depare_run.py`, z6+, already seam-safe + generalized) — or the `coverage` footprints —
  against `store/polygon/*.gpkg` + `metadata.confidence`, tagging each with the **worst**
  grade over its area (conservative). Confidence "to the depth-area" with zero new machinery
  and no cache impact.
- **True per-pixel tier (needs the merge, has a big cost).** To catch GEBCO-fill holes inside
  a regional footprint and intra-source quality, record a `uint8 source_index` + per-pixel
  `confidence` while priority-painting (`aggregation_merge.py:72-80`), kept **sharp** (not
  through the Gaussian feather `:87-110`; cropped with the same offsets `:112-117`). Then
  refine with the two concrete providers:
  - **GEBCO TID** — parallel `tid_*.tif` track, **nearest-neighbour** on every warp
    (categorical; the default `AGG_RESAMPLE` interpolates codes to garbage —
    `aggregation_reproject.py:36`), same buffered `-te/-tr` 3857 warp as elevation. Map codes
    → CATZOC **under the resolution cap** (measured 10–17 → C/D; interpolated/predicted
    40–46/70–72 → U; land 0 → drop).
  - **S-102 uncertainty** — carry band 2 (currently dropped by `band:1`,
    `aggregation_reproject.py:55-59`) as a parallel track through the `mixed_crs` UTM-per-tile
    warp (nearest, **not** negated — `negate_band1` must not touch it); map TVU(m) → CATZOC.
  - **Correctness gotchas (must handle):** single-source tiles skip the merge entirely
    (`aggregation_merge.py:43-45`) → `provenance` must set index = that lone source; the merge
    only knows an integer group index → also emit the `i→source` map from reproject
    (`reprojection.json` today holds only `buffer_pixels`).

GEBCO-fill holes are better fixed at the source: polygonize each source's **real valid-data
mask** into `store/polygon/<id>.gpkg` instead of the simplified *union* outline — no per-pixel
machinery needed for the common case. **Tension with Phase 1's perf fix:** a detailed
valid-data mask re-bloats the coverage tippecanoe. Keep the *served* footprint tileset
simplified (z0-8); if hole fidelity is wanted, carry the detailed mask only into the
merge/per-pixel path, not the outline that gets tiled.

## Alternatives considered

- **New `provenance_run.py` per-pixel vector fork.** Rejected: polygonizing intra-GEBCO
  measured-vs-interpolated is a fragmentation nightmare (thin sinuous multibeam ribbons across
  a 450 m field → huge MVT counts) describing deep ocean nobody navigates by this layer.
  Attributing existing depare polygons is strictly less machinery.
- **Terrarium-sibling raster** (`R=source G=grade`). Rejected: `encode.py:9` uses all three
  RGB channels; would need a new encode, a `.prov` Worker route, and a viewer decode.
- **Coarse paletted confidence raster (~z8, worst-per-cell).** Honest (confidence *is* coarse)
  and a viable fallback, but still needs a Worker route + viewer decode — depare-attribute
  wins on laziness.
- **Numeric / plain-tier grades.** Rejected — CATZOC letters per the issue's analogy.

## Validation

- Phase 1: dispatch a regional CI build; a production `z6` tile carries `coverage` features
  (the #19 repro, inverted). `test-engine` / `test-sources` pass.
- Phase 2: `just preview`; `tippecanoe-decode` shows `source_confidence`; click a footprint →
  popup shows the winning source's grade; footprints tint by grade. **Bias-shallow check:** a
  low-confidence shoal is still *drawn*, only styled as uncertain.
- Phase 3: synthetic tile with mixed GEBCO TID → depare/region grades vary (C/D measured, U
  interpolated) under the resolution cap; S-102 tile → uncertainty drives the grade; a
  GEBCO-fill hole inside a regional footprint reads as GEBCO.

## Open questions

- **Rubric resolution cap** — finalize the grid-spacing → max-grade table before seeding.
- **CI cost of the true-per-pixel tier** — adding `source_index` to the merge *output shape*
  invalidates the whole merged-DEM cache → a full-planet re-merge + re-fork (+ re-running the
  TID/S-102 source+reproject stages). Confirm that's acceptable before starting Phase 3b.
- Worst-grade-per-polygon vs letting `gdal_contour -p` split a band across confidence zones.
- Does confidence ever affect merge order? Default **no** — priority stays `(priority,
  maxzoom)` (`utils.py:248`); confidence is display-only.
- **Coverage-build perf (#66):** confirm a ≈z8 footprint simplification lets
  `--no-tile-size-limit` go without collapsing small-source footprints — #66 moved the 40-min
  cost off the critical path but left it unreduced.
