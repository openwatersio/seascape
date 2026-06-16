# Master Plan — Planet-Scale Bathymetry for a Nautical Chart

The end goal is a **planet-scale bathymetry product good enough to use in a
nautical chart** — not just a bathymetry visualizer. This document is the spine:
it states the goal, the guiding principle, and the order of work. Each workstream
links to its own detailed plan; this file does not duplicate them.

## Scope, honestly

This is a **derived, supplementary** bathymetry layer for situational awareness
and passage planning — a GEBCO/regional-DEM mosaic, not an official Electronic
Navigational Chart. It is **not a replacement for official ENCs** from national
hydrographic offices and must not be the sole basis for navigation. That framing
drives every design choice below: where it can't be authoritative, it must at
least be *conservative* and *honest about its own quality*.

> Open question: is the long-term target purely supplementary, or do we want to
> ingest official ENC soundings (S-57/S-101) and approach authoritative coverage
> in some regions? That decision changes Workstreams 3–5. Flagged, not yet decided.

## Guiding principle: conservative and self-describing

A chart is a safety instrument. Two rules separate "looks like a chart" from
"safe to glance at on the water":

1. **Bias shallow.** Where the data is uncertain or processing must round, err
   toward *less* depth. Charted depth ≤ true depth. This directly constrains
   contour smoothing (must not migrate a line into deeper water) and datum choice
   (a low-water datum, not MSL).
2. **Carry provenance and confidence to the pixel.** The mariner must be able to
   tell GEBCO-interpolated deep ocean from a surveyed 3 m CUDEM coastline. Source
   and a quality grade travel with the data into the tiles.

## Workstreams, in order

| # | Workstream | Owns | Status | Detail |
| - | ---------- | ---- | ------ | ------ |
| 1 | Build scaling | Finishing the global build on free runners (shard + cache) | **In progress** — validating on GH Actions | [SCALING.md](SCALING.md) |
| 2 | Multi-source mosaic | Data hierarchy + per-source ingest + initial datum offsets | Phase 1 done; Phase 2+ next | [MOSAIC-PLAN.md](MOSAIC-PLAN.md) |
| 3 | Chart datum correctness | Conservative low-water vertical datum (LAT/MLLW) | Not started | this doc |
| 4 | Chart data model | Soundings, safety contour, shallow-biased contours | Not started | this doc |
| 5 | Confidence & provenance | Per-source quality grade carried to tiles | Not started | this doc |
| 6 | Accuracy validation | Compare against official soundings; regression harness | Not started | this doc |

Sequencing rationale: **1 unblocks everything** (a build that can't finish can't
be improved). **2 is the substrate** the chart-specific work sits on — you need
the multi-source mosaic before per-source datum/confidence have anything to attach
to. **3 is the highest-value chart correctness fix** once data is flowing. 4–6
turn a correct bathymetry mosaic into an actual chart product and can progress in
parallel once 2–3 land.

---

## Workstream 1 — Build scaling (in progress)

Make the global terrain + contour build complete reliably on free GitHub runners
via geographic sharding + a cached smoothed DEM. Currently validating on GH
Actions. Full design and order of work in **[SCALING.md](SCALING.md)**.

Definition of done for this workstream: a green global z0–9 build, and a re-run
(tweaked color/zoom) that reuses the cached smoothed DEM instead of recomputing
hours of work.

---

## Workstream 2 — Multi-source mosaic & data hierarchy

GEBCO global base, higher-quality regional sources layered on top by priority,
extending to deeper zoom only where data supports it. This is also where the
*first cut* of vertical datum handling lives (constant per-source offsets).

Full design, source table, and phasing in **[MOSAIC-PLAN.md](MOSAIC-PLAN.md)**.
Near-term: Phase 2 (EMODnet + DDM, European coverage for seamap), then Phase 3
(CUDEM + BlueTopo multi-tile US coverage), then the Phase 4 unification.

Where it hands off to this plan: MOSAIC-PLAN treats proper vertical datum and
quality masking as Phase 5 "fidelity, as needed." For the **chart** goal those
are not optional polish — Workstreams 3 and 5 promote them to first-class.

---

## Workstream 3 — Chart datum correctness

**Problem.** MOSAIC-PLAN's ingest applies one constant `datum_offset_m` per source
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
- Sources already in a low-water datum (BlueTopo = MLLW) need no shift; the
  pipeline must *not* re-datum them. Per-source datum metadata in `sources.conf`
  drives this.

**Done when:** a known shoal reads a charted depth at-or-shallower-than its
official ENC sounding across a few test regions, with no visible seam where the
separation model meets the constant-offset fallback.

---

## Workstream 4 — Chart data model

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
   corner-cutting that moves a contour into *deeper* water violates the
   conservative principle. Either constrain smoothing to never deepen a line, or
   reduce/disable it near navigationally-relevant depths. This is a correctness
   change to an existing step, not a new feature.

These can land independently; (3) is the cheapest and most urgent because it fixes
an existing safety-relevant behavior.

---

## Workstream 5 — Confidence & provenance

The mariner must see data quality. Carry a per-source quality grade (analogous to
ENC **CATZOC** zones of confidence) and source identity from `sources.conf`
through ingest into the tiles, so the viewer can surface "surveyed 3 m" vs.
"interpolated GEBCO." Pairs with MOSAIC-PLAN Phase 5's GEBCO TID-based quality
masking (prefer measured cells over interpolated). Minimum viable version: a
source-id + coarse confidence attribute on tiles and a viewer affordance to
inspect it.

---

## Workstream 6 — Accuracy validation

Stand up a regression harness that spot-checks derived depths against an
authoritative reference (official ENC soundings / NOAA survey data) for a set of
test regions, and fails the build if error drifts or biases *deeper* (the unsafe
direction). This is what lets every later change ship with confidence and what
substantiates the "good enough for a chart" claim. Build it once Workstream 3
gives depths worth measuring.

---

## What stays the same

The core tiling pipeline (`terrain` via rio-rgbify, `contour` via gdal_contour →
tippecanoe, PMTiles distribution, the MapLibre viewer) operates on whatever DEM
and vectors the upstream stages produce. The workstreams above change *what data
flows through* and *what layers come out*, not the tiling machinery itself.
