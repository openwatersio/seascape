# Chart datum correctness: VDatum separation grids — planning doc

*Written 2026-08-03. Point-in-time; the code is the source of truth. Covers #16 (spatially-varying LAT/MLLW separation), with #26 (datum verification) as a prerequisite and #20 (accuracy harness) as the downstream consumer.*

## Problem

Ingest applies one constant `datum_offset_m` per source ([source_datum.py](../../pipelines/source_datum.py)) to bring everything to ~MSL. For a chart this is wrong twice: MSL is not a charting datum, and a constant can't represent a tidal separation that swings by metres along a coastline. The mosaic today is actually a *mix*: S-102, NOAA estuarine, INFOMAR, and the Great Lakes are already on a local low-water datum; GEBCO, CUDEM, and the other regional DEMs are on ~MSL or a national vertical datum. The seams between them are undocumented datum steps.

The concrete, quantified case ([inland-water plan §Part 4](2026-07-07-inland-water.md)): CUDEM is NAVD88, and NAVD88 zero sits ~0.95 m **above** MLLW at Jacksonville (Mayport 8720211: NAVD88 = +0.947 m re MLLW). Un-shifted CUDEM reads ~0.95 m too deep relative to chart datum — drying flats classify as navigable water and shallow depths overstate clearance, the unsafe direction. The separation is spatially varying — measured span +0.13 m (San Diego) to +1.68 m (Boston) and beyond +3 m in Alaska, with the sign actually flipping in the Columbia River estuary (−0.53 m at Skamokawa) — so no scalar fixes it. Correcting it means transforming the pixels, which makes CUDEM a **prepared** source: `raw` means stored verbatim from upstream, and corrected bytes are not verbatim.

## Target datum

**0 = local chart datum**: the nationally-defined low-water datum where tidal, the local surface for lakes. "Low water" is not one datum: LAT is the IHO default, but member states diverge — MLLW (US, Bahamas, Philippines, Pacific territories), LLWLT (Canada), NLLW (Japan), ALLW (Korea), TLT (China), MLWS (Brazil, Italy, Chile), and BSCD2000 ≈ MSL in the non-tidal Baltic. The authoritative in-house mapping is the tide-database's [`CHART_DATUMS` table](https://github.com/openwatersio/tide-database/blob/main/tools/station.ts) (built from the IHO TWCWG vertical-datums list); each source's reference surface targets *its* national chart datum, which is what national separation products ship anyway. This is already the de-facto contract downstream (landmask: "0 = chart datum at the shoreline"; drying = elevation in (0, cap]) and already true for the low-water sources and the offset-corrected lakes. The work is pulling the MSL-ish tidal sources **down to local low water**, not normalizing anything to MSL. Consequences:

- Sources already on low water (S-102, `noaa_estuarine`, `infomar_*`, `great_lakes` LWD, `emodnet` LAT, `gsc_pacific` LLWLT — the latter two verified against product docs 2026-08-03, closing #26's checklist) are **untouched**. The stale "MLLW→MSL is a future VDatum job" comments (`aggregation_reproject.py`, `source_datum.py` "~MSL" docstring) flip direction and get rewritten.
- Mixing national chart datums in one mosaic is acceptable — that is what paper charts of adjacent countries already do: each is a conservative local low-water reference, tiles never blend across national regions except through the GEBCO fallback, and per-source datum is recorded in the manifest (`seascape:datum`) for provenance. The Baltic is the instructive extreme: BSCD2000 ≈ MSL *is* the chart datum there, so Baltic sources are already correct and no LW correction may ever be applied to them.
- Lakes and non-tidal sources never get a separation grid — the flag is per-source, so the gate is explicit.
- Depths get *shallower* where the correction applies (bed elevation rises by the MSL/NAVD88−LW separation). That is the bias-shallow-safe direction; drying flats currently reading as submerged cross to positive and start rendering as foreshore.

## Approach

The core operation is: correct each source raster in its **native frame, before warp**, in the prep step. Two mechanism candidates for that operation:

- **`source_datum --offset-surface`**: resample a reference raster (chart datum height expressed in the source's own vertical frame) bilinearly onto the source grid and subtract per pixel — `bed − reference = elevation re chart datum`. A few dozen self-checked lines that compose with the existing negate/offset/clamp knobs and the `datum.json` sidecar; the reference surface is ours to build and sign-verify. (PR #76, DGM-W, proposes the same flag shape for a different source.)
- **PROJ vertical transforms** (`gdalwarp` with compound CRSs through EPSG-registered operations): **ruled out, checked 2026-08-03** — PROJ 9.8.1's database contains zero MLLW operations and no tidal grids (the sole NAVD88 height→MLLW depth candidate is a "ballpark" zero-shift placeholder). VDatum's tidal grids never entered EPSG/proj-data; that gap is why VDatum exists as a separate NOAA tool. So the grids must be sourced and composed by us regardless, and the reference-subtract mechanism wins by default.

The invariants either way: an explicit per-source flag gates the correction; out-of-coverage behavior is documented; the sign is validated executably against published benchmark separations; and the applied transform lands in the catalog provenance. What remains to build is the reference surfaces themselves — VDatum for the US, the national grids for Phase 2.

### Phase 0 — verify what we have (#26) — done 2026-08-03

All three verified against product documentation and recorded in `metadata.json` + the `sources/README.md` table:

- **EMODnet 2024: LAT confirmed** (the GeoTIFF tiles are the LAT product; the MSL variant is ESRI-ASCII-only, which we don't harvest). Stays untouched. Caveat carried in its metadata: the GEBCO/IBCAO background fill baked inside the tiles remains ~MSL — same fallback tier GEBCO already occupies.
- **GSC Pacific: CHS Chart Datum (LLWLT)** per Open File 8963 Table 1 — joins the already-on-low-water list; datum-authoritative for its region like S-102.
- **GSC Atlantic: no vertical datum is declared anywhere** (Open File 9064 + FGP metadata) — inputs blend CHS chart datum inshore into ~MSL offshore. No source frame exists to build a separation against, so it is *not* a Phase-2 candidate: treat as fallback-tier, label via the confidence work (#17).

### Phase 1 — VDatum grid for CUDEM (the MVP)

**Prep: one composed separation COG.** New `pipelines/datum_grid.py` (a support artifact like the landmask, NOT a `sources/` entry — everything under `sources/` enters the merge):

The bundle inventory and every formula below were established empirically 2026-08-03 against the extracted bundle + the CO-OPS datums API — full ground truth in the companion [2026-08-03-vdatum-inventory.md](2026-08-03-vdatum-inventory.md).

1. Harvest the NOAA VDatum grid bundle (public domain): a single dated zip, `vdatum.noaa.gov/download/data/vdatum_all_20250917.zip` (3.09 GB → 21 GB, 52 tidal regions; the URL date is the version pin). Per region: `<REGION>_{tss,mllw,…}.gtx` surfaces (Float32, nodata −88.8888, **0–360 longitude** — shift at harvest), a `.met` sidecar (authoritative bbox + `horz` frame), and a `.bnd` validity polygon; skip `vdatum/lib/` (~300 MB bundled JRE).
2. Compose `S = NAVD88 − MLLW` per region — **the formula branches on the `.met` `horz` frame** (every tidal `.gtx` is that surface's height re LMSL, but what `tss` is referenced to differs): `horz=NAD83` → `S = tss − mllw`; `horz=IGS14/IGS08` (whole West Coast, most Chesapeake/Delaware, SE Alaska) → `S = (N_hybrid + δ − N_xgeoid20b) + tss − mllw` using the bundle's `core/` geoids (GEOID18, GEOID12B in Alaska) and the NAD83(2011)→ITRF2014 ellipsoid shift δ; PRVI (PRVD02/VIVD09 ≡ LMSL) → `S = −mllw`. Getting the branch wrong is 0.4–1.2 m of error, part of it in the unsafe deep direction. Mosaic smallest-bbox-first with nodata fallthrough (validated at all stations; `.bnd`-polygon selection is the upgrade if seams appear), then nearest-fill **~10–20 km for interior holes between adjacent regions' boundaries** (the real gaps — 5 km off Panama City/Neah Bay, 20–30 km off Sitka; CUDEM sits well inside the 100–150 km Atlantic/Gulf outer edge). Beyond coverage the correction is a no-op (status quo bias, logged) — not DGM-W's drop-to-nodata: an un-referenced NAVD88 coastal pixel is today's shipped data, and deleting it would trade a bounded ~1 m bias for a hole.
3. **Sign verification is executable**: `--check` asserts the composed reference at the 14 verified benchmark stations (inventory §benchmark table: Mayport +0.948 … Skamokawa −0.530, spanning all three formula branches and both signs) reproduces the published CO-OPS separations within 5 cm (observed residuals ≤3.4 cm; VDatum's own uncertainty is ~9 cm).
4. Output `store/datum/navd88_mllw.tif` holding the reference on the subtract convention (`−S`, i.e. MLLW height in NAVD88; Mayport ≈ −0.947), a 4326 coastal ribbon. It is a **DAG-cached prep input, not a published artifact**: the `datum_surface` rule builds it on the box from the pinned bundle URL and the persistent store volume keeps it (and the 3.2 GB bundle zip) warm, keyed on `datum_grid.py` so a formula fix can never ship under the old grid. Nothing downstream of the source lane reads it, so there is nothing to publish.
5. Known limits, deliberate for the MVP: **Hawaii, Guam, CNMI, American Samoa, and Alaska outside the SE panhandle have no VDatum grid at all** — those CUDEM tiles stay uncorrected (no-op) for now; per-island CO-OPS scalars (conservative minimum across an island's stations) are the follow-up. The **Columbia River estuary** corrects to MLLW like everywhere else even though NOAA charts there use CRD — the bundle's `CRD/crd.gtx` is the natural later override.

**Apply: CUDEM as a prepared source.** `raw` means the store holds the upstream bytes verbatim; a datum-corrected tile is not verbatim, so `cudem` and `cudem_third` drop `raw: true` and take the ordinary prepared-source path every other transformed source uses — fetch each enumerated tile into `raw/`, then `stage → datum → normalize → polygon → catalog`, with the correction sitting in the datum step:

- `metadata.json` declares `offset_surface: "navd88_mllw"` beside the existing `datum_offset_m`/`negate`/`clamp_positive` knobs. `source_prep` resolves it to `store/datum/navd88_mllw.tif` and `source_datum.transform_file` subtracts it per pixel, in the tile's own NAD83 grid, before any warp. It also drops the vertical half of a compound source CRS: a corrected tile is no longer on NAVD88, and EPSG has no compound code for MLLW.
- The applied reference lands in the `datum.json` sidecar and from there in `catalog.json` as `seascape:datum_surface` — the correction is invisible in the pixels, so the name of what was subtracted is the provenance.
- Where the reference has no coverage the pixel passes through **unchanged** (Hawaii, the Pacific territories, Alaska off the SE panhandle). Not drop-to-nodata: an un-referenced NAVD88 coastal pixel is today's shipped data, and deleting it would trade a bounded ~1 m bias for a hole. `metadata.json`'s `datum` records the split, and prep logs the count of files it could not correct.
- Both sources also declare `mixed_crs: true` — CUDEM territory tiles carry their own local CRS and nodata, so the registration must not advertise one EPSG code for the collection.

Properties this buys:

- **Staleness tracking is the engine's, not a manual state clear.** The reference is a declared input of `prep_source`, so a rebuilt grid re-preps the source and re-registers it; the recipe hash moves and the covering re-aggregates exactly the affected tiles. The code-only-change blind spot (nothing marks dirty, forced re-registration, cleared R2 state) never arises.
- **Transform cost is paid once per tile ever** (~1.3k tiles, ~197 GB through the box one time; only new/changed tiles on later refreshes) instead of per aggregation tile per rebuild, and the aggregate/merge code is untouched.
- **One materialization model, not two.** CUDEM needs no special case in the publish/mirror/check paths, and it gains the coverage footprint every prepared source publishes.

**Rollout.** The first prepared run re-registers both sources with new filenames and a new recipe hash → the US-coastal tile set re-aggregates once on the next build dispatch. No force, no state surgery.

**Cost.** Storage roughly doubles for these two sources on the store volume: `raw/` keeps the upstream bytes (~197 GB) and the prepared COGs sit beside them (~190 GB). The 750 GB volume holds it, but it is the tightest thing on there — the follow-up is dropping `raw/` once a tile is prepared (`temp()`-style), which costs a refetch on any re-prep.

### Phase 2 — national grids through the prep recipe

These are prepared sources, so once the mechanism lands the per-source cost is data work only: build the reference surface, add the correction to the recipe. Note the frame requirement — each reference must be expressed in that source's own vertical datum (LAT-in-NAP for vaklodingen, etc.), which is how the national products ship anyway. Ordered by data availability:

- `vaklodingen` NAP → LAT: Netherlands NLLAT2018 separation grids (RWS/Hydrografie, open).
- `ausbathytopo` / `gbr30` AHD(~MSL) → LAT: Geoscience Australia AusCoastVDT surfaces (CC-BY).
- `nz_coastal` NZVD2016 → CD: LINZ tidal datum surfaces, if openly gridded.
- `uk_surfzone` ODN → CD: VORF is UKHO-licensed — likely ruled out; keep the documented ~ODN status and its recorded bias instead.

### Phase 3 — global MSL→LAT fallback (deferred)

GEBCO (and any MSL fallback water) stays ~MSL for now: the separation is navigationally irrelevant in deep water, and coastal GEBCO is already the low-confidence tier the confidence work (#17) labels. A global LW−MSL surface (FES2014/FES2022-derived; computing LAT from the constituents over a 19-yr epoch is the realistic path, but the AVISO license needs the same gate as any source) can slot into the identical `offset_surface` mechanism later — masked per national chart-datum region (LAT is only the IHO default; the Baltic's ≈MSL datum, for one, must be excluded, and the per-country table above drives the mask). Not in scope; recorded so the phase pointer is real.

## Alternatives considered

- **A derive step beside the verbatim mirror**: keep CUDEM `raw`, and after the object mirror correct each object R2→R2 into a reference-versioned `derived-<refhash>/<key>` prefix that the registration rows point at. It was designed for the streaming/mirror era, when CUDEM had no prep recipe to hook and the runner had no disk to stage 197 GB on; the box plus the persistent volume made staging the ordinary thing, and the prepared convention gets the same exact-scope invalidation with one materialization model instead of two, no derived-prefix versioning scheme, and no orphaned-prefix GC question.
- **Mosaic-time: a post-warp per-block subtract in `aggregation_reproject`** (#16 option (a), viable when CUDEM still streamed from NOAA): it is a second implementation of `--offset-surface` semantics (per-block, against multi-GB warped macrotiles), re-pays a reference warp on every aggregation rebuild forever, and — decisive — its value changes ride on code/config alone, so nothing marks dirty: every rollout and every future grid update needs manual R2 state clears plus forced re-registration. Correcting at prep gets exact-scope invalidation from the recipe hash for free.
- **PROJ vertical transforms inside the aggregation warp** (as opposed to at prep time — see Approach): rejected for the warp itself — the chain differs per region/source, and per-source datum policy doesn't belong in the one shared warp command that already fights "several coordinate operations" ambiguity.
- **Scalar per-region offsets as a stopgap** (e.g. −0.95 m CONUS-east for CUDEM): the real separation spans +0.13 to +3 m and flips sign in the Columbia estuary, seams at region edges, and it burns the same rollout cost as doing it right.

## Validation

- `datum_grid.py --check`: composed grid matches NOAA benchmark separations at stations spanning both signs (worst residual 3.4 cm at Yakutat).
- `source_datum.py --check`: the subtract, the reference-nodata pass-through, the source's own nodata staying nodata, the compound-CRS reduction, and stripe-invariance of the values.
- End to end on one real tile: `ncei19_n30X50_w081X50_2018v1.tif` (8112², EPSG:4269, nodata −9999) reads +1.54197 m at Mayport before prep and +2.48953 m after — a +0.94756 m shift against the published +0.948 — and survives normalize as a ZSTD/PREDICTOR=3 COG with its CRS and nodata intact.
- The [inland-water plan's regression guard](2026-07-07-inland-water.md) inverts: at the ICW/Jacksonville point the S-102↔CUDEM step, currently bounded by the ~0.95 m documented separation, should collapse to ≈0 after correction.
- #16's done-when: a known shoal reads at-or-shallower-than its official ENC sounding across a few test regions (feeds the #20 harness); no visible seam where grid coverage ends (the nearest-fill + 0-fallback edge).
- Drying check: a flat that dries to +0.5 m MLLW in CUDEM territory renders as foreshore, not water.

## Open questions

- ~~Exact GTX composition per VDatum region~~ — resolved 2026-08-03, see the inventory doc: three formula branches keyed on `.met` `horz`, verified at 14 stations.
- ~~The exact implementation of the reference subtract~~ — resolved: `source_datum --offset-surface`, striped so peak memory tracks the stripe (382 MB measured on an 8112² tile) rather than the tile.
- How far offshore to nearest-fill before falling back to 0 — pick from CUDEM's actual footprint overlap with VDatum coverage.
- ~~Whether `cudem_third`'s territory files need separate handling~~ — resolved: PR/USVI correct via the `S = −mllw` branch (PRVD02/VIVD09 ≡ LMSL); Pacific islands and non-SE Alaska have no grid and stay no-op until the per-island-scalar follow-up.
- Whether `raw/` survives prep for these two sources. Keeping it costs ~197 GB of the 750 GB volume and buys a re-prep with no refetch (which a grid rebuild needs); dropping it inverts both. Keep for now, revisit when the volume tightens.
- Whether the store's copy of the reference should be publishable at all. Nothing outside the source lane reads it, so a laptop `--config stream=1` preview never needs it — but a *local* prep of a CUDEM tile does, and composing it locally means the 3.2 GB bundle.
