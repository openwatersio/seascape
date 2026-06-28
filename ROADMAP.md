# Roadmap — Planet-Scale Bathymetry for a Nautical Chart

The end goal is a **planet-scale bathymetry product good enough to use in a
nautical chart** — not just a bathymetry visualizer. This roadmap is the single
forward-looking doc: it states the goal, the guiding principle, and the order of
work, and carries the source/coverage and build-scaling detail that used to live
in separate plans. Each workstream below is a milestone — what it owns, where it
stands, and what's left. Cross-cutting _as-built_ architecture lives in
[CLAUDE.md](CLAUDE.md); this doc is where the work is _going_.

## Scope

This is a **derived, supplementary** bathymetry layer for situational awareness
and passage planning — a GEBCO/regional-DEM mosaic, not an official Electronic
Navigational Chart. It is **not a replacement for official ENCs** from national
hydrographic offices and must not be the sole basis for navigation. That framing
drives every design choice below: where it can't be authoritative, it must at
least be _conservative_ and _honest about its own quality_.

> Open question: is the long-term target purely supplementary, or do we want to
> ingest official ENC soundings (S-57/S-101) and approach authoritative coverage
> in some regions? That decision changes Milestones 3–5. Flagged, not yet decided.

## Guiding principle

A chart is a safety instrument. Two rules separate "looks like a chart" from
"safe to glance at on the water":

1. **Bias shallow.** Where the data is uncertain or processing must round, err
   toward _less_ depth. Charted depth ≤ true depth. This directly constrains
   contour smoothing (must not migrate a line into deeper water) and datum choice
   (a low-water datum, not MSL).
2. **Carry provenance and confidence to the pixel.** The mariner must be able to
   tell GEBCO-interpolated deep ocean from a surveyed 3 m CUDEM coastline. Source
   and a quality grade travel with the data into the tiles.

## Milestones

| #   | Milestone               | Owns                                                        | Status      |
| --- | ----------------------- | ----------------------------------------------------------- | ----------- |
| 1   | Build scaling           | Global build on free runners (shard + R2 incremental cache) | ✅ Done     |
| 2   | Multi-source mosaic     | Data hierarchy + per-source ingest + initial datum offsets  | ✅ Done     |
| 3   | Chart datum correctness | Conservative low-water vertical datum (LAT/MLLW)            | Not started |
| 4   | Chart data model        | Soundings, safety contour, shallow-biased contours          | Not started |
| 5   | Confidence & provenance | Per-source quality grade carried to tiles                   | Not started |
| 6   | Accuracy validation     | Compare against official soundings; regression harness      | Not started |

Sequencing rationale: **1 unblocks everything** (a build that can't finish can't
be improved). **2 is the substrate** the chart-specific work sits on — you need
the multi-source mosaic before per-source datum/confidence have anything to attach
to. **3 is the highest-value chart correctness fix** once data is flowing. 4–6
turn a correct bathymetry mosaic into an actual chart product and can progress in
parallel once 2–3 land.

---

## ✅ Milestone 1 — Build scaling

The [global build](.github/workflows/ci.yml) is sharded geographically and runs in parallel across GitHub's
free concurrent runners, with build state cached in R2 so a rebuild only redoes the
regions that changed and reuses the rest — and the shards stitch back together
without seams at their boundaries.

---

## ✅ Milestone 2 — Multi-source mosaic & data hierarchy

GEBCO is the global base; higher-quality regional sources layer on top and extend
to deeper zoom only where their data supports it. Each source is a directory under
[`sources/`](sources/) (`metadata.json` + a fetch/normalize recipe), GEBCO included
as just source #0 — no special-casing. Priority is **derived, not configured**:
`(maxzoom, id)`, so GEBCO (smallest maxzoom) loses wherever a finer regional source
overlaps. Zoom caps are display caps (per source via `max_zoom`), not native resolution.

| Source        | Native res | Zoom cap | Coverage           | Datum                  | `sources/` dir      |
| ------------- | ---------- | -------- | ------------------ | ---------------------- | ------------------- |
| GEBCO 2026    | ~450 m     | ~z8      | global             | MSL                    | `gebco` (#0)        |
| EMODnet 2024  | ~115 m     | z11      | European seas      | LAT (confirm)          | `emodnet`           |
| DDM (Denmark) | 50 m       | z12      | Danish EEZ         | MSL (DKMSL2022)        | `ddm`               |
| CUDEM 1/9     | ~3.4 m     | z13      | US coast           | NAVD88                 | `cudem`             |
| CUDEM 1/3     | ~10 m      | z12      | US coast (broader) | NAVD88                 | `cudem_third`       |
| NOAA S-102    | ~4–16 m    | z14      | US navigable       | MLLW (+ uncertainty)   | `noaa_s102`         |

Not yet ingested: CUDEM territory products (HI/PR/USVI/Guam/AmSam/CNMI) and NIWA NZ
— pulled in when needed.

The large US sources (CUDEM, S-102) are range-read straight off NOAA's public
buckets at build time rather than downloaded — CUDEM alone is ~188 GB. S-102 takes
the per-tile engine path: its tiles come from a GeoPackage index and span multiple
UTM zones, so the mosaic reprojects them per-tile rather than as one VRT.

Vertical datum here is only a _first cut_ — one constant offset per source. The
proper spatially-varying datum, quality masking, and provenance are promoted to
Milestones 3 and 5; opportunistic data/ops sit in the backlog below. Note streamed
sources (CUDEM, S-102) keep no local copy, so they bypass the per-file offset step
— Milestone 3 covers how the datum work reaches them.

---

## Milestone 3 — Chart datum correctness

**Problem.** The mosaic ingest applies one constant `datum_offset_m` per source
to bring everything to ~MSL. For a chart this is wrong in two ways: MSL is not a
charting datum, and a single offset can't represent a tidal datum that varies
spatially (the LAT/MLLW–MSL separation swings by metres over a coastline).

**Target.** Reference all depths to a **conservative low-water datum** — LAT
(IHO standard for ENCs) or MLLW (US convention) — applied as a spatially-varying
separation, not a constant.

**Approach (to validate with a hydrographer / the `oceanographer` agent):**

- Source a tidal-datum separation model (e.g. NOAA VDatum for US waters; a global
  LAT–MSL grid such as a FES-derived tidal model elsewhere) and apply it during
  `ingest`, replacing the constant offset where coverage exists.
- Keep the constant offset as the fallback where no separation model exists —
  but log it, and bias it conservative.
- Per-source datum metadata in each source's `metadata.json` drives this. NOAA S-102
  now supplies US navigable waters already on MLLW (it supersedes BlueTopo, whose raw
  tiles were per-tile mixed MLLW/NAVD88), so the main remaining US datum work is CUDEM
  (NAVD88 → low-water).
- **Caveat — streamed sources skip `source_datum.py`.** CUDEM is a
  `/vsicurl/` reference range-read straight off NOAA at reproject time, so there's
  no local value-transform step to swap an offset into. Two ways to reach it: (a)
  apply the separation grid on the fly in the aggregation reproject — a value-add
  pass after warp, keeps the no-download model (preferred); or (b) re-process each
  tile through `source_datum.py` into our own R2 bucket (datum-corrected COGs) and
  register _those_ `/vsicurl/` URLs. (a) keeps zero-disk; (b) costs the storage but
  reuses the per-file transform verbatim.

**Done when:** a known shoal reads a charted depth at-or-shallower-than its
official ENC sounding across a few test regions, with no visible seam where the
separation model meets the constant-offset fallback.

---

## Milestone 4 — Chart data model

A chart is more than shaded relief + decorative contours. Three additions:

1. **Soundings.** Point depths are the primary depth cue on a real chart. Derive
   a sparse, de-conflicted sounding layer from the mosaic (sample shallow-biased,
   thin by zoom, label in metres/feet/fathoms). New vector layer alongside
   contours.
2. **Safety contour.** A user-selectable depth (e.g. draft + margin) rendered
   prominently, with water shallower than it shaded as a hazard. Needs the contour
   set to include candidate safety depths and the viewer to restyle a chosen one
   client-side.
3. **Shallow-biased contours.** Revisit the Chaikin smoothing (`smooth-contours`):
   corner-cutting that moves a contour into _deeper_ water violates the
   conservative principle. Either constrain smoothing to never deepen a line, or
   reduce/disable it near navigationally-relevant depths. This is a correctness
   change to an existing step, not a new feature.

These can land independently; (3) is the cheapest and most urgent because it fixes
an existing safety-relevant behavior.

---

## Milestone 5 — Confidence & provenance

The mariner must see data quality. Three pieces, all carried from each source's
`metadata.json` through ingest into the tiles:

1. **Source identity + confidence grade.** A per-source quality grade (analogous to
   ENC **CATZOC** zones of confidence) and source id, so the viewer can surface
   "surveyed 3 m" vs. "interpolated GEBCO." MVP: a source-id + coarse confidence
   attribute on tiles and a viewer affordance to inspect it.
2. **GEBCO TID-based quality masking.** Prefer measured cells over interpolated
   when blending (also feeds a per-pixel provenance band off the merge).
3. **Source-footprint provenance layer.** Tile straight from the coverage polygons
   the source stage already generates — "which source covers here," essentially free.

---

## Milestone 6 — Accuracy validation

Stand up a regression harness that spot-checks derived depths against an
authoritative reference (official ENC soundings / NOAA survey data) for a set of
test regions, and fails the build if error drifts or biases _deeper_ (the unsafe
direction). This is what lets every later change ship with confidence and what
substantiates the "good enough for a chart" claim. Build it once Milestone 3
gives depths worth measuring.

---

## Backlog — opportunistic data & ops

Pull in only when a concrete need appears (inherited from the retired
source/coverage roadmap's "fidelity & ops" list):

- **NOAA CSB** crowdsourced bathymetry as additional fill.
- **GLOBathy** lake bathymetry — a separate inland layer, not the marine mosaic.
- **Auto-refresh** as upstream sources update (GEBCO annual, others irregular).

---

## What stays the same

The core tiling pipeline (`terrain` via rio-rgbify, `contour` via gdal*contour →
tippecanoe, PMTiles distribution, the MapLibre viewer) operates on whatever DEM
and vectors the upstream stages produce. The workstreams above change \_what data
flows through* and _what layers come out_, not the tiling machinery itself.
