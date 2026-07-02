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

The [global build](.github/workflows/build.yml) is sharded geographically and runs in parallel across GitHub's
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

Not yet ingested: CUDEM territory products (HI/PR/USVI/Guam/AmSam/CNMI) — pulled in
when needed. The worldwide expansion candidates (Canada, Australia, Ireland,
Indonesia, the lakes, …) are catalogued under [Source expansion](#source-expansion--worldwide-coverage-candidates) below.

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

A chart is more than shaded relief + decorative contours. Landed on branch
`milestone-4-chart-data-model` (PR #8) — the three additions plus feet/fathom units:

1. **Soundings.** ✅ New `soundings` vector layer (`pipelines/soundings_run.py`),
   forked off each aggregation tile's merged DEM and folded into `contours.pmtiles`.
   The shoalest wet pixel per grid cell (floored toward shallower — never charts a
   depth deeper than reality), placed on a jittered quincunx that restaggers per zoom
   (a shoalest-per-block pyramid via tippecanoe `minzoom==maxzoom`) so every zoom is an
   even, chart-like field that densifies inward. Labels in metres/feet/fathoms.
   _Deferred:_ terrain-adaptive density (thin flat bottom, keep irregular — the
   Zoraster prime/background split) so uniform-depth plains go quiet.
2. **Safety depth.** ✅ Viewer-only (the contour levels already carry 1 m steps to
   −15 m, so no pipeline change). A user-set safety depth (default 2 m) shades water
   shallower than it as a hazard — folded into the single depth-shading `color-relief`
   ramp (two color-relief layers on one DEM source don't composite; crisp edge at any
   value) — and turns soundings ≤ safety hazard-red (S-52). The bold safety-_contour_
   line was built then dropped; the DEM shading reads better. _Dropped:_ a red stipple
   over the hazard (needs depth-area polygons; `gdal_contour -p` on the native z14 DEM
   times out — not viable per-tile).
3. **Shallow-biased contours.** ✅ Chaikin corner-cutting could bow a contour into
   deeper water; gated off in the navigable band (≤ `CONTOUR_NAV_SMOOTH_MAX` = 30 m,
   the ECDIS safety band) so smoothing never understates a shoal.

**Feet / fathom.** ✅ A second contour set at the classic fathom curves (tagged
`sys=ft`, labelled feet or fathoms) shares the `contours` layer; the viewer's Units
selector flips one MapLibre `global-state` variable — no per-layer restyle. _Deferred:_
safety input in the active unit (currently metres).

Chart-cartography standards + the sounding-selection literature grounding all of the
above: [docs/nautical-chart-references.md](docs/nautical-chart-references.md).

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

## Source expansion — worldwide coverage candidates

Feeds Milestone 2. Today the mosaic is sharp only where we've ingested: Europe
(EMODnet), US (CUDEM, S-102), Denmark (DDM). Everywhere else is GEBCO's ~450 m.
This is the researched catalog of sources that would extend higher-than-GEBCO
coverage worldwide — pick by the same rule as everywhere: **resolution sets the
zoom cap, an openly-redistributable license is the gate** (data is baked into
served tiles, so viewer-only / encrypted / non-commercial / request-by-email
sources are unusable — listed as SKIP so nobody re-researches them). GEBCO stays
the fallback under all of them. Datum is noted because the chart wants low-water
(Milestone 3): sources already on **LAT / MLLW / Chart Datum** are the cleanest
fit; MSL/elevation ones need an offset.

**Access legend:** **A** = streamed COG, range-read at build via `/vsicurl`/`/vsis3`,
no download (CUDEM/S-102 path). **B** = prepared download → normalize to 4326 COG →
R2 → stream (EMODnet/DDM path). **C** = viewer/encrypted/request-only (unusable).

### Build-next shortlist (open + clear coverage win)

Roughly in coverage-per-effort order:

1. **[Vaklodingen 20m](https://downloads.rijkswaterstaatdata.nl/bodemhoogte_20mtr/bodemhoogte_20mtr.tif)** (Netherlands) — 20 m, **CC0**, a single ~97 MB GeoTIFF (EPSG:28992). Cleanest ingest in the catalog. z12. ✅ built (`sources/vaklodingen`).
2. **[gbr30](https://files.ausseabed.gov.au/survey/Great%20Barrier%20Reef%20Bathymetry%202020%2030m.zip)** (Australia) — 30 m, CC-BY 4.0, one range-readable 3.8 GB zip of 4 COG tiles over the Great Barrier Reef + Coral Sea. z12. ✅ built (`sources/gbr30`).
3. **GSC (Canada)** — the **GSC Atlantic Bathymetric Compilation** (NRCan OF 9064) is the Canadian source: a 100 m **continuous** compiled GeoTIFF (Scotian Shelf + Newfoundland-Labrador), OGL-Canada, LCC. ✅ built (`sources/gsc_atlantic`; CI fetches it — the GSC FTP throttles too slow for a local pull). The **GSC Pacific** 10 m DEM (OF 8963, BC + Salish Sea) is a follow-up — a 7.9 GB FileGDB / ~32 GB raster needing subdataset-extract + tiling. *(CHS NONNA was built here too then **shelved** — sparse multibeam survey coverage, not a continuous grid, so it blows up `source_polygonize` and a national harvest is impractical; better for the Milestone-4 soundings layer. Removed from the tree, recoverable at git `9f93ad3`.)*
4. **[AusBathyTopo 250m](https://www.ausseabed.gov.au/data/bathymetry)** (Australia) — national 250 m bathy/topo compilation, CC-BY 4.0, MSL, EPSG:4326. One ~2.8 GB COG zip; fills the AU EEZ at z9 (gbr30 wins the GBR/Coral Sea overlap). ✅ built (`sources/ausbathytopo`). *The per-survey 2–10 m AusSeabed COGs — the z12–13 prize — are **deferred**: served via portal/WCS + a survey coverage DB, not a clean static urllist, so they need a custom coverage-DB fetch.*
5. **[INFOMAR](https://www.infomar.ie/)** (Ireland) — **10 m inshore** (`sources/infomar_10m`, z13) + **25 m shelf** (`sources/infomar_25m`, z11), **LAT**, CC-BY 4.0. Two **sibling sources** (cudem/cudem_third pattern). Both set `priority: 1` to outrank EMODnet regardless of any zoom tie (decoupling precedence from zoom, like S-102 vs CUDEM); `max_zoom` stays the honest native (z13 / z11) and the 10 m wins inshore via finer maxzoom within the tier. WGS84/LAT, no embedded CRS → assign EPSG:4326. 100 m offshore omitted (≈EMODnet). ✅ built.
6. **UK [SurfZone 2m](https://environment.data.gov.uk/dataset/77e6f743-d708-4909-a80f-9510b7dbaa16) + [CCO swath](https://maps.coastalmonitoring.org/cco/)** (England) — 1–2 m, OGL v3, EPSG:27700, **ODN datum** (topographic, not chart). No static tile URLs — download is the interactive DefraDataDownload tool or a WCS endpoint, and coverage is the narrow intertidal strip. Belongs with the **awkward-fetch sources** (mirror-to-R2 / WCS), not a clean file_list — deferred to P4. z13–14.
7. **[BATNAS](https://tanahair.indonesia.go.id/demnas/)** (Indonesia) — 6″ (~180 m), MSL, open w/ attribution (no resale), whole archipelago. ✅ **built** (`sources/batnas`). Its download is reCAPTCHA-login-gated (not CI-fetchable), so the **53 surveyed 5° sheets are mirrored to R2** (`data/bathymetry/mirror/batnas/`) and the recipe is a **standard prepared source** fetching that mirror over public HTTPS — CI-reproducible, no token. `sources/batnas/harvest.py` (stdlib-only, standalone) refreshes the mirror via a one-time login token when BIG ships a new version. EPSG:4326 (assign — not embedded), elevation/no-negate, z10.
8. **[swIOBC](https://doi.pangaea.de/10.1594/PANGAEA.880618)** (SW Indian Ocean) — 250 m, CC-BY 3.0, EPSG:4326 topobathy off Kenya/Tanzania/Mozambique/Madagascar; ~2× GEBCO. One ~711 MB GeoTIFF, z9. ✅ built (`sources/swiobc`).
9. **Inland lakes** (separate layer, pure GEBCO gap-fill): **[African Great Lakes CC0 bundle](https://dataverse.harvard.edu/dataset.xhtml?persistentId=doi:10.7910/DVN/ITCOGT)** (Victoria/Albert/Edward/George, one .7z), **[swisstopo Alpine lakes](https://data.geo.admin.ch/api/stac/v1/collections/ch.swisstopo.swissbathy3d/items)** + **[Bodensee](https://doi.org/10.1594/PANGAEA.855987)**, **[Great Salt Lake](https://doi.org/10.5066/P9DGG75W)** (0.5 m, stream the 34 GB), **[Lake Tahoe](https://pubs.usgs.gov/dds/dds-55/pacmaps/exports/lt_bathy.e00.gz)**, and **[NOAA NOS Estuarine DEMs](https://www.ncei.noaa.gov/products/estuarine-bathymetric-digital-elevation-models)** (70 US estuaries, already MLLW).

EMODnet already covers European seas **including the N. African Med shelf** (the 58 tiles we ingest are the full product; the Mediterranean is enclosed, so its tiles carry the African shore) — no extension needed. IBCSO v2 (Southern Ocean) is omitted on purpose: at 500 m it matches GEBCO's resolution and is already folded into GEBCO via Seabed 2030, so it adds no coverage (see the regional tables below).

### Canada, Arctic, Antarctic & global compilations

| Source | Res | Coverage | Datum | License | Cap | Verdict (access) |
| ------ | --- | -------- | ----- | ------- | --- | ---------------- |
| **GSC Atlantic 100 m** (NRCan OF 9064) | 100 m | Scotian Shelf + Newfoundland-Labrador | unverified (confirm) | OGL-Canada ✓ | z10 | **BUILT** (recipe `sources/gsc_atlantic`; CI-fetched — GSC FTP too slow locally) — continuous compiled GeoTIFF, LCC; sign/datum verify on first build. The Canadian win. |
| GSC Pacific 10 m (NRCan OF 8963) | 10 m | BC coast/PNCIMA + Salish Sea | unverified | OGL-Canada ✓ | z13 | FOLLOW-UP — 7.9 GB FileGDB, `WEST_COAST_DEM` raster 86k×94k (~32 GB uncompressed); needs OpenFileGDB subdataset extract + tiling |
| CHS NONNA-10/100 | 10 m / 100 m | Cdn coasts + Great Lakes + Arctic | **Chart Datum** ✓ | open CHS licence ✓ | — | **SHELVED** — sparse survey coverage (not gridded): polygonize explodes, national harvest impractical; → Milestone-4 soundings (harvester in git `9f93ad3`) |
| IBCSO v2 | 500 m (≈GEBCO) | Southern Ocean, N→50°S | MSL | CC-BY 4.0 ✓ | — | **SKIP** — ≈GEBCO res AND already folded into GEBCO via Seabed 2030 (no new coverage); <85°S untileable |
| IBCAO v5.2 | 100 m | Arctic, S→64°N | MSL | ⚠ disclaimer-gated, ambiguous | z11 | OPPORTUNISTIC — **verify redistribution first**; EPSG:3996 |
| GMRT v4.x | ~100 m (multibeam only) | global swaths | mixed | CC-BY 4.0 ✓ | z9–12 | OPPORTUNISTIC — dynamic GridServer, targeted fill only |
| SRTM15+ V2.7 | ~450 m | global | MSL | public domain | — | SKIP — same res as GEBCO, already folded in |
| ArcticDEM | 2 m | Arctic **land** | — | — | — | SKIP — topographic, not bathymetry |

### Australia, New Zealand & Pacific

| Source | Res | Coverage | Datum | License | Cap | Verdict (access) |
| ------ | --- | -------- | ----- | ------- | --- | ---------------- |
| gbr30 | 30 m | GBR + Coral Sea + QLD coast | MSL | CC-BY 4.0 ✓ | z12 | **BUILD** (B/A; one range-readable file) |
| AusBathyTopo 250m 2024 | 250 m | Australia EEZ | MSL | CC-BY 4.0 ✓ | z9 | **BUILT** (B; national fill, one step above GEBCO; gbr30 wins the GBR overlap) |
| AusSeabed survey COGs | 2–10 m | AU EEZ survey footprints (patchy) | MSL | CC-BY 4.0 ✓ | z12–13 | DEFER — served via portal/WCS + a coverage DB, not a clean COG bucket; needs a custom enumeration |
| gbr100 | 100 m | GBR + deeper Coral Sea | MSL | CC-BY 2.5-AU ✓ | z11 | OPPORTUNISTIC (B) — only the deep strip gbr30 misses |
| NIWA NZ 250m | 250 m | NZ EEZ | — | **CC BY-NC-SA ✗** | — | SKIP — non-commercial; NZ stays GEBCO-only |
| LINZ hydro | vector/grid | NZ | Approx LAT | **S-63 encrypted / email ✗** | — | SKIP — not obtainable as an open grid |
| SPC Pacific islands | 5 m lidar | Pacific islands | local | **gated / country-owned ✗** | — | SKIP — sovereignty-restricted; public entries are GEBCO-derived |

### Europe — national, beyond EMODnet (z11)

| Source | Res | Coverage | Datum | License | Cap | Verdict (access) |
| ------ | --- | -------- | ----- | ------- | --- | ---------------- |
| INFOMAR (10 m + 25 m) | 10 m / 25 m | Ireland inshore + shelf | **LAT** ✓ | CC-BY 4.0 ✓ | z13 / z11 | **BUILT** (B; sibling sources `infomar_10m`/`infomar_25m`, cudem/cudem_third pattern; both `priority:1` to outrank EMODnet; assign EPSG:4326; 100 m skipped) |
| Vaklodingen 20m | 20 m | Netherlands | NAP (MSL) | **CC0** ✓ | z12 | **BUILD** (B; single file, EPSG:28992) |
| UK SurfZone DEM 2m | 2 m | England intertidal | ODN | OGL v3 ✓ | z13 | DEFER (P4) — WCS/interactive only, no static URLs; EPSG:27700; ODN not chart datum |
| UK CCO swath | 1–2 m | England nearshore | ODN/CD | OGL v3 ✓ | z13–14 | DEFER (P4) — per-survey/interactive; pairs with SurfZone fetch |
| UKHO ADMIRALTY EEZ surfaces | 1–5 m | UK EEZ | **Chart Datum** ✓ | OGL v3 (per-survey varies) | z13–14 | OPPORTUNISTIC (B) — vet OGL per survey, skip fee-bearing |
| Kartverket 50m | 50 m | Norway coast + Svalbard | **LAT** ✓ | NLOD ✓ | z11–12 | OPPORTUNISTIC (B) — 2024-declassified; gappy, marginal |
| BSH DGM 50m | 50 m | German N. Sea + Baltic | **LAT/SKN** ✓ | open (GeoNutzV) ✓ | z12 | OPPORTUNISTIC (B) — only modestly beats EMODnet |
| Iceland MFRI | 10 m | Iceland survey patches | ISN2004 | open (cite) ✓ | z12 | OPPORTUNISTIC (B) — discrete patches |
| SHOM Litto3D | 1–5 m | French coastal ribbon | LAT per-zone | Licence Ouverte ✓ | z13–14 | OPPORTUNISTIC (B) — thin coverage, ASC→COG, per-zone datum |
| HELCOM BSBD | 250 m | Baltic | — | CC-BY 3.0 | — | SKIP — coarser than EMODnet (115 m) already there |
| Sweden / Spain / Portugal / Italy / Greece | — | — | — | **restricted / NC / no gain ✗** | — | SKIP — defense-restricted, viewer-only, or ≤EMODnet |

### Asia (mostly restricted — the key finding)

| Source | Res | Coverage | Datum | License | Cap | Verdict (access) |
| ------ | --- | -------- | ----- | ------- | --- | ---------------- |
| BATNAS (Indonesia, BIG) | ~180 m | Indonesian archipelago | MSL ✓ | open, attrib, no resale ✓ | z10 | **BUILT** — 53 sheets mirrored to R2, standard prepared recipe; `harvest.py` refreshes the mirror |
| KHOA BADA2024 (Korea) | ~150 m | Korean coast/EEZ | unknown — **verify** | KOGL Type 1 ✓ | z11 | OPPORTUNISTIC (B) — confirm datum + KOGL badge |
| HHU24SWDSCS (S. China Sea) | 10 m | scattered SCS reefs | SDB | CC-BY 4.0 ✓ | z12 | OPPORTUNISTIC (B) — sparse, contested waters |
| Japan (JODC J-EGG500, JHA M7000) | 500 m | Japan | — | **no-redistribute / paid ✗** | — | SKIP — soundings already reach us via GEBCO |
| India (INCOIS, NHO) | — | India | — | **nationals-only / S-63 ✗** | — | SKIP |
| Philippines (NAMRIA), China, Taiwan ODB | — | — | — | **priced / state-secret / gated ✗** | — | SKIP |
| Vietnam / Thailand / Malaysia / Singapore | — | — | — | **ENC/chart only ✗** | — | SKIP — no open grid |

### Latin America, Caribbean, Africa & Middle East (sparse)

| Source | Res | Coverage | Datum | License | Cap | Verdict (access) |
| ------ | --- | -------- | ----- | ------- | --- | ---------------- |
| EMODnet DTM 2024 _(already ingested)_ | 115 m | European seas + **N. African Med shelf** | **LAT** ✓ | CC-BY 4.0 ✓ | z11 | covers the N-African Med coast as part of the existing product; EMODnet is European/NE-Atlantic so nothing reaches the Caribbean |
| swIOBC | 250 m | SW Indian Ocean / E. Africa | ~MSL | CC-BY 3.0 ✓ | z9 | **BUILT** (B) — one ~711 MB GeoTIFF |
| GMRT | ~100 m swaths | global multibeam | mixed | CC-BY 4.0 ✓ | z9–11 | OPPORTUNISTIC — patchy; "not for navigation" |
| Red Sea / Strait of Tiran patches | 10–30 m | Red Sea rift + Tiran | unstated | CC-BY 4.0 ✓ | z12 | OPPORTUNISTIC (B) — Tiran coastal; rest deep curiosities |
| Chilean fjord grids | 10–50 m | S. Chile fjords | SHOA-CD | per-record — **verify** | z12 | OPPORTUNISTIC (B) — license-gated scattered patches |
| Brazil LEPLAC / de Wet SA shelf / Lesser Antilles / EOMAP / Israel / Mexico IBCCA | varies | — | — | **study-only / NC / no-license / commercial ✗** | — | SKIP |
| Brazil DHN, Chile SHOA, Argentina SHN, Peru/Colombia/Ecuador, Caribbean states, SANHO, W/E Africa, Arabian Gulf | — | populated coasts | — | **closed / request-only ✗** | — | **GEBCO-only** — no open hi-res source exists |

### Inland waters — lakes & rivers (separate layer; Great Lakes covered elsewhere)

Pure GEBCO gap-fill (lakes are hydraulically isolated → no seam against the ocean
base). Freshwater grids store **lakebed _elevation_** in a national/geoid datum, so
each needs a per-lake "subtract surface level" step. **No open _surveyed_ global
inland compilation exists** — the global products are modeled, usable only as a
labeled cosmetic low-zoom fill.

| Source | Model | Res | Coverage | License | Cap | Verdict (access) |
| ------ | ----- | --- | -------- | ------- | --- | ---------------- |
| GLOBathy | **modeled** raster | ~30 m | all 1.4M lakes, global | CC-BY 4.0 ✓ | z6–8 | OPPORTUNISTIC (B) — synthetic cones; label "modeled" |
| 3D-LAKES | altimetry-hybrid | per-lake | 510k lakes (98.9% storage) | CC-BY 4.0 ✓ | z9–11 | OPPORTUNISTIC (B) — awkward format, less proven |
| HydroLAKES | vector + scalar | — | global | CC-BY 4.0 ✓ | — | SKIP as bathy — keep as a free lake mask |
| African Great Lakes (GLWNB-2020) | surveyed DEM | 50–100 m | Victoria/Albert/Edward/George | **CC0** ✓ | z11 | **BUILD** (B) — one .7z, per-lake CRS |
| swisstopo swissBATHY3D | surveyed DEM | 1–2 m | Geneva/Neuchâtel/Maggiore(CH) | OGD ✓ | z13–14 | **BUILD** (B) — elevation→subtract level (LN02) |
| Bodensee (IGKB) | surveyed DEM | 3 m | Lake Constance | CC-BY 3.0 ✓ | z14 | **BUILD** (B) — EPSG:25832, DHHN92 |
| Great Salt Lake (USGS TBDEM) | surveyed DEM | 0.5 m | whole lake | **CC0** ✓ | z13–14 | **BUILD** (A) — stream 34 GB, mask dry playa |
| Lake Tahoe (USGS DDS-55) | surveyed DEM | 10 m | whole lake | public domain ✓ | z13 | **BUILD** (B) — use the .e00 grid |
| NOAA NOS Estuarine DEMs | raster | 30 m | 70 US estuaries (Chesapeake, SF Bay, Puget Sound…) | public domain ✓ | z11–12 | **BUILD** (A/B) — already **MLLW**, netCDF→COG |
| Caspian Sea | — | — | — | (in GEBCO) | — | SKIP — already free inside GEBCO_2026 |
| USACE eHydro | XYZ/TIN points | dense | 61 US federal channels | public domain ✓ | z11–13 | OPPORTUNISTIC (A) — grid it yourself; per-district datum chaos |
| Amazon estuary / NL+German rivers / USGS reservoir+CoNED | raster/points | 1–30 m | scattered reaches | CC-BY/CC0/PD ✓ | z12–14 | OPPORTUNISTIC — high-zoom regional overlays only |
| Baikal / Tanganyika / Malawi / Great Bear+Slave / Titicaca / MN-DNR / Champlain / Salton / TWDB / Mekong+Yangtze | varies | — | — | **NC / no-license / points-only / closed ✗** | — | SKIP |

### Cross-cutting notes

- **Datum is the recurring wrinkle.** Already low-water (ideal, plug into Milestone 3
  cleanly): INFOMAR, UKHO-EEZ, Kartverket, BSH, SHOM, NOS-Estuarine (MLLW).
  Need an offset: everything MSL/NAP/ODN/elevation (AusBathyTopo, gbr30, swIOBC, Vaklodingen,
  the lakes). eHydro mixes MLLW vs LWRP **per district** — its single biggest ingest risk.
- **Access-A (no-download) streams beyond CUDEM/S-102:** Great Salt Lake's single GeoTIFF.
  (AusSeabed's per-survey COGs were expected to be one, but they're served via portal/WCS +
  a coverage DB, not a clean public bucket — deferred.) Everything else is access-B
  (download → R2), the EMODnet/DDM path. Restricted national HOs are access-C and excluded.
- **License is the real filter, not data existence.** Whole regions surveyed their
  waters but lock the result: NZ (NIWA non-commercial), most of Asia, Brazil (LEPLAC
  "study only"), much of the Mediterranean and the Arabian Gulf. For those coasts GEBCO
  stays the only option — recorded here so it isn't re-litigated.
- **Modeled ≠ surveyed.** GLOBathy/3D-LAKES are interpolated depth, not measurement —
  fine as a labeled low-zoom fill, never as authoritative depth (violates the "honest
  about quality" principle if shown un-flagged).

### Plan of action — ingesting the shortlist

Every source is the same unit of work: a `sources/<id>/` dir cloning an existing
recipe — **no engine changes for any of the shortlist**, only `cover`/`aggregate`
already handle a new source by priority.

1. `metadata.json` — name/producer/website/license, `max_zoom` (sets the display cap
   _and_ priority `(maxzoom, id)`), the vertical datum, and any flags (`negate`,
   `mixed_crs`, `band`, `priority`).
2. `file_list.txt` — fetch URLs (download tiles, or S3/HTTP COG refs for streamed).
3. `Justfile` — clone the closest existing source; set the source `--crs`, and the
   datum step: `source_datum --negate` for positive-down depth, `--offset <m>` for a
   constant shift (e.g. lakebed elevation → lake-surface-as-zero).
4. `just source <id>` → inspect the overlay; `just preview` over its bbox → eyeball
   depths + seams; add `<id>` to the `sources` matrix in `build.yml`.

Reuse map — which recipe each clones, and the params that change:

| Source | Clones | source `--crs` | datum step | `max_zoom` | fetch note |
| ------ | ------ | -------------- | ---------- | ---------- | ---------- |
| Vaklodingen ✅ | `ddm` | EPSG:28992 | — (NAP bed elev) | 12 | one file |
| gbr30 ✅ | `emodnet` | EPSG:4326 | — (MSL) | 12 | one zip of 4 COG tiles |
| swIOBC ✅ | `gebco` | EPSG:4326 | — | 9 | one ~711 MB GeoTIFF |
| INFOMAR ✅ | `emodnet` | EPSG:4326 (assign) | — (LAT) | 13 | 10 m inshore zip (no embedded CRS) |
| INFOMAR 25m ✅ | `emodnet` | EPSG:4326 (assign) | — (LAT) | 11 | 25 m shelf zip; sibling source; `priority:1` (both) to beat EMODnet |
| UK SurfZone | _(deferred → P4)_ | EPSG:27700 | — (ODN) | 13 | WCS/interactive only — no static tile URLs |
| AusBathyTopo ✅ | `emodnet` | EPSG:4326 | — (MSL) | 9 | one ~2.8 GB national COG zip |
| AusSeabed COGs | _(deferred)_ | — | — | 12–13 | per-survey; portal/WCS + coverage DB, not a urllist |
| BATNAS ✅ | `emodnet`-like | EPSG:4326 (assign) | — (MSL) | 10 | R2 mirror of 53 sheets; `sources/batnas/harvest.py` refreshes it |
| African Great Lakes | `ddm` | per-lake UTM | `--offset` per lake | 11 | un-.7z |
| swisstopo + Bodensee | `ddm` | 2056 / 25832 | `--offset` (LN02/DHHN92→0) | 14 | STAC / PANGAEA |
| Lake Tahoe | `ddm` | e00 grid | `--offset` (lake level) | 13 | `.e00` → tif |
| Great Salt Lake | `ddm` | — | `--offset` (NAVD88→level) | 13 | 34 GB once; mask dry playa |
| NOS Estuarine DEMs | `emodnet` | EPSG:4326 | — (MLLW) | 12 | netCDF→COG, 70 estuaries |

Sequenced to prove the cheap path before the awkward ones:

- **P0 — pilot:** Vaklodingen (CC0, single file). Proves a non-US prepared overlay
  flows source→cover→aggregate→bundle→preview end-to-end. Smallest possible diff.
- **P1 — single-file wins:** gbr30 ✅, swIOBC (one PANGAEA fetch).
- **P2 — prepared grids:** INFOMAR ✅ (one merged 10 m zip — turned out single-file, not
  per-tile). UK SurfZone moved to P4 (no static URLs; WCS/interactive only).
- **P3 — Australia:** AusBathyTopo 250 m national grid ✅ (clean single-file fill). The
  per-survey 2–10 m AusSeabed COGs are deferred — served via portal/WCS + a coverage DB,
  not a clean urllist, so they need a custom coverage-DB fetch when the detail is worth it.
- **P4 — gated/awkward fetch:** BATNAS ✅ (reCAPTCHA-gated → 53 sheets mirrored to R2, standard
  prepared recipe; `sources/batnas/harvest.py` refreshes the mirror). UK SurfZone + CCO (England,
  WCS/interactive) is the last one.
  *(NONNA was built here too, then shelved — sparse survey coverage, wrong fit for the DEM
  mosaic; see the catalog. Revisit for Milestone-4 soundings.)*
- **P5 — inland lakes layer:** confirm a lake overlay bundles with no false land,
  then African Great Lakes / swisstopo+Bodensee / Tahoe / Great Salt Lake / NOS
  estuaries. Shared mechanic = lakebed elevation→depth via `source_datum --offset`.

Carried per source (feeds Milestone 3): record the vertical datum in `metadata.json`
even though only the constant-offset first cut is applied today. **One blocker, not a
build task:** IBCAO's redistribution rights are ambiguous — resolve the licence before
building the Arctic source.

---

## Backlog — opportunistic data & ops

Pull in only when a concrete need appears (inherited from the retired
source/coverage roadmap's "fidelity & ops" list):

- **NOAA CSB** crowdsourced bathymetry as additional fill.
- **Auto-refresh** as upstream sources update (GEBCO annual, others irregular).

**Parked during the source build-out** (deferred with a reason — revisit when the
need/effort justifies; the inline notes above point here):

- **AusSeabed per-survey 2–10 m COGs** — the AU high-res (z12–13) tier. Needs a custom
  coverage-DB/WCS enumeration (served via the portal + a survey coverage DB, not a clean
  static urllist like CUDEM). AusBathyTopo 250 m covers AU at z9 in the meantime; gbr30
  covers the GBR/Coral Sea at z12.
- **UK SurfZone 2 m + CCO swath** (England intertidal) — no static URLs (WCS / interactive
  DefraDataDownload only), EPSG:27700, ODN datum (not chart). Mirror-to-R2 fetch — tracked
  as a P4 awkward-fetch item.
- **IBCAO v5.2 100 m** (Arctic) — best Arctic resolution, but redistribution rights are
  ambiguous (disclaimer-gated). Resolve the licence before building.
- **INFOMAR 100 m offshore** — only if a deep-offshore Irish gap appears; ≈EMODnet res and
  likely redundant with the 25 m IE-Waters grid. One-line `file_list` add.
- **NONNA (shelved)** — sparse multibeam *survey coverage* doesn't fit the continuous-DEM
  mosaic: pixel-exact `source_polygonize` blows up (~1 M vertices for 15 tiles) and a national
  10 m harvest is ~65 k tiles / hundreds of GB (CI died mid-download, no retry/resume). Removed
  from the tree (recoverable at git `9f93ad3`). Revisit for the Milestone-4 **soundings** layer,
  where sparse Chart-Datum points fit naturally — that, plus a resilient harvester and tile-bbox
  (not pixel-exact) coverage, is what it would need.

(Concrete source candidates — marine and inland, with resolution/license/datum and
BUILD/SKIP verdicts — live in [Source expansion](#source-expansion--worldwide-coverage-candidates) above. GLOBathy and the
surveyed lakes are catalogued there as the inland layer.)

---

## What stays the same

The core tiling pipeline (`terrain` via rio-rgbify, `contour` via gdal*contour →
tippecanoe, PMTiles distribution, the MapLibre viewer) operates on whatever DEM
and vectors the upstream stages produce. The workstreams above change \_what data
flows through* and _what layers come out_, not the tiling machinery itself.
