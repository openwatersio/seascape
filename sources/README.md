# Data Sources

The worldwide bathymetry-source survey behind the mosaic: what's built, what's open but not yet ingested, and — just as important — what was researched and ruled out, so nobody re-researches it.

Selection rule: **resolution sets the zoom cap, an openly-redistributable license is the gate.** Data is baked into served tiles, so viewer-only, encrypted, non-commercial, and request-by-email sources are unusable. GEBCO stays the fallback under everything. Vertical datum matters because a chart wants low water ([#16](https://github.com/openwatersio/seascape/issues/16)): sources already on LAT/MLLW/Chart Datum are the cleanest fit; MSL/elevation ones need an offset.

## Access patterns

- **Mirrored** — volatile upstream tile collections (CUDEM, S-102): the scheduled sources workflow enumerates the public bucket, mirrors every object into the data bucket, and registers relative `objects/<key>` rows — builds range-read only our mirror, never the upstream. Marked `volatile: true` in `metadata.json` so every sources run re-registers them as the upstream catalog drifts.
- **Prepared** — download → normalize to a 4326 COG → R2 → stream from there (EMODnet, DDM, and most others). Gated upstreams get a `harvest.py` that refreshes an R2 mirror (BATNAS, UK SurfZone).

## Built sources

| Source                                                             | Native res  | Zoom cap  | Coverage                            | Datum                   |
| ------------------------------------------------------------------ | ----------- | --------- | ----------------------------------- | ----------------------- |
| [GEBCO 2026](gebco/)                                               | ~450 m      | ~z8       | global                              | MSL                     |
| [EMODnet 2024](emodnet/)                                           | ~115 m      | z11       | European seas                       | LAT (confirm)           |
| [DDM (Denmark)](ddm/)                                              | 50 m        | z12       | Danish EEZ                          | MSL (DKMSL2022)         |
| [CUDEM 1/9](cudem/)                                                | ~3.4 m      | z13       | US coast + territories              | NAVD88 / local (terr.)  |
| [CUDEM 1/3](cudem_third/)                                          | ~10 m       | z12       | US coast + territories (broader)    | NAVD88 / local (terr.)  |
| [NOAA S-102](noaa_s102/)                                           | ~4–16 m     | z14       | US navigable                        | MLLW (+ uncertainty)    |
| [Vaklodingen](vaklodingen/)                                        | 20 m        | z12       | Netherlands                         | NAP (~MSL)              |
| INFOMAR ([10 m](infomar_10m/), [25 m](infomar_25m/))               | 10 m / 25 m | z13 / z11 | Ireland inshore + shelf             | **LAT**                 |
| [UK SurfZone](uk_surfzone/)                                        | 2 m         | z14       | England intertidal                  | ODN (~MSL)              |
| [GSC Atlantic](gsc_atlantic/)                                      | 100 m       | ~z10      | Scotian Shelf + NL                  | unverified              |
| [GSC Pacific](gsc_pacific/)                                        | 10 m        | z13       | BC coast + Salish Sea               | unverified              |
| [gbr30](gbr30/)                                                    | 30 m        | z12       | GBR + Coral Sea                     | MSL                     |
| [AusBathyTopo](ausbathytopo/)                                      | 250 m       | z9        | Australia EEZ                       | MSL                     |
| [BATNAS](batnas/)                                                  | ~180 m      | z10       | Indonesia                           | MSL                     |
| [swIOBC](swiobc/)                                                  | 250 m       | z9        | SW Indian Ocean                     | ~MSL                    |
| [NOS Estuarine](noaa_estuarine/)                                   | 30 m        | z11       | 70 US estuaries                     | **MLLW**                |
| [Great Lakes (NCEI)](great_lakes/)                                 | ~90 m       | z10       | Great Lakes (incl. Canadian halves) | **LWD**                 |
| [African Great Lakes](african_great_lakes/)                        | 50–100 m    | z13       | Victoria/Albert/Edward/George       | lake surface            |
| swissBATHY3D ([Léman](lac_leman/), [Neuchâtel](lac_neuchatel/))    | 1–2 m       | z14       | Léman, Neuchâtel                    | LN02 − surface offset   |
| [Bodensee](bodensee/)                                              | 3 m         | z14       | Lake Constance                      | DHHN92 − surface offset |
| [Lake Tahoe](lake_tahoe/)                                          | 10 m        | z13       | Lake Tahoe                          | MSL − surface offset    |

Priority is derived, not configured: `(maxzoom, id)`, so GEBCO (smallest maxzoom) loses wherever a finer regional source overlaps — except a datum-authoritative source can set `priority` in metadata (S-102 over CUDEM, INFOMAR over EMODnet) to win regardless of zoom. Zoom caps are display caps (`max_zoom`), not native resolution. Inland lakes are pure GEBCO gap-fill: hydraulically isolated, so no seam against the ocean base; freshwater grids store lakebed _elevation_, so each carries a "subtract surface level" offset.

The large US sources (CUDEM, S-102) are mirrored object-for-object into the data bucket on a schedule rather than downloaded to a runner — CUDEM alone is ~188 GB, streamed straight from NOAA's bucket into ours — and aggregation range-reads the mirror, so NOAA churn or outages can redden a sources refresh but never a build. S-102 takes the per-tile engine path: its products span multiple UTM zones, so the mosaic reprojects them per-tile rather than as one VRT.

## Open candidates

Every open candidate is a GitHub issue labeled [`source`](https://github.com/openwatersio/seascape/issues?q=is%3Aissue%20state%3Aopen%20label%3Asource), each carrying the verified website, license, access path, and ingest notes. Headlines:

- [#29](https://github.com/openwatersio/seascape/issues/29) **AusSeabed per-survey COGs** — the AU z12–13 tier, the biggest open win; access verified (public WFS index + anonymous zips).
- [#36](https://github.com/openwatersio/seascape/issues/36) **Great Salt Lake** — the last unbuilt surveyed lake.
- [#32](https://github.com/openwatersio/seascape/issues/32) **CUDEM territories** (HI/PR/USVI/Guam/AmSam/CNMI) — extend the existing streamed source.
- [#38](https://github.com/openwatersio/seascape/issues/38) **IBCAO** — best Arctic resolution, blocked on a licence-ambiguity question, not build work.

## Ruled out (don't re-research)

License is the real filter, not data existence — whole regions surveyed their waters but lock the result. For these coasts GEBCO stays the only option:

| Source / region                                                                                              | Why skipped                                                                                           |
| ------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------- |
| IBCSO v2 (Southern Ocean)                                                                                    | ≈GEBCO resolution _and_ already folded into GEBCO via Seabed 2030 — no new coverage; <85°S untileable |
| SRTM15+                                                                                                      | same resolution as GEBCO, already folded in                                                           |
| ArcticDEM                                                                                                    | topographic land, not bathymetry                                                                      |
| NIWA NZ 250 m                                                                                                | CC BY-**NC**-SA — NZ stays GEBCO-only                                                                 |
| LINZ hydro (NZ)                                                                                              | S-63 encrypted / request-by-email                                                                     |
| SPC Pacific islands 5 m lidar                                                                                | sovereignty-gated, country-owned; public entries are GEBCO-derived                                    |
| HELCOM BSBD (Baltic)                                                                                         | 250 m — coarser than the EMODnet 115 m already ingested                                               |
| Sweden, Spain, Portugal, Italy, Greece                                                                       | defense-restricted, viewer-only, or ≤EMODnet                                                          |
| Japan (JODC J-EGG500, JHA M7000)                                                                             | no-redistribute / paid; soundings reach us via GEBCO anyway                                           |
| India (INCOIS, NHO)                                                                                          | nationals-only / S-63                                                                                 |
| Philippines, China, Taiwan                                                                                   | priced / state-secret / gated                                                                         |
| Vietnam, Thailand, Malaysia, Singapore                                                                       | ENC/chart products only, no open grid                                                                 |
| Brazil LEPLAC, de Wet SA shelf, Lesser Antilles, EOMAP, Israel, Mexico IBCCA                                 | study-only / NC / no-license / commercial                                                             |
| Brazil DHN, Chile SHOA, Argentina SHN, Peru/Colombia/Ecuador, Caribbean HOs, SANHO, W/E Africa, Arabian Gulf | closed / request-only — no open hi-res source exists                                                  |
| HydroLAKES                                                                                                   | vector + scalar depth only — useful as a free lake mask, not bathymetry                               |
| Caspian Sea                                                                                                  | already inside GEBCO                                                                                  |
| Baikal, Tanganyika, Malawi, Great Bear/Slave, Titicaca, MN-DNR, Champlain, Salton, TWDB, Mekong/Yangtze      | NC / no-license / points-only / closed                                                                |

Shelved with a revisit path (kept as issues, not re-research):

- **CHS NONNA** — sparse multibeam survey coverage, wrong fit for a continuous-DEM mosaic (removed at git `9f93ad3`); revisit as a soundings ingest, [#44](https://github.com/openwatersio/seascape/issues/44). Its licence is also non-navigational-use-only — see the issue.
- **Allen Coral Atlas SDB** (Bahamas/N. Caribbean) — recipe complete (PR #5, unmerged) but satellite-derived bathymetry proved too noisy for a chart; revisit via ATL24 if ever.

Two coverage notes that look like gaps but aren't: EMODnet's 58 tiles are the full product and include the **N. African Mediterranean shelf** (the Med is enclosed, so its tiles carry the African shore); nothing European reaches the Caribbean.

## Cross-cutting

- **Datum is the recurring wrinkle.** Already low-water (plug into the chart-datum work cleanly): INFOMAR, S-102, NOS Estuarine, NCEI Great Lakes (LWD), and among candidates UKHO-EEZ, Kartverket, BSH, SHOM. Everything MSL/NAP/ODN/elevation needs an offset. USACE eHydro mixes MLLW vs LWRP _per district_ — its single biggest ingest risk ([#50](https://github.com/openwatersio/seascape/issues/50)).
- **Modeled ≠ surveyed.** GLOBathy/3D-LAKES are interpolated depth, not measurement — fine as a labeled low-zoom fill, never as authoritative depth (violates the "honest about quality" principle if shown un-flagged; blocked on [#17](https://github.com/openwatersio/seascape/issues/17)).
- **No open surveyed global inland compilation exists.** The global lake products are modeled; surveyed lakes are ingested one by one (see the built table).

## Adding a source

Check the catalog above first — the candidate may already be cataloged (with verified license/datum/access notes in its `source` issue) or ruled out.

1. Create `sources/<id>/` — two files, no recipe (the Snakemake lane discovers the directory and routes it by metadata):
   - `metadata.json` — `name`, `producer`, `website`, `license`, and an optional `max_zoom` cap (omit to use the source's native resolution; cap it for high-res lidar like CUDEM). Prep knobs as needed: `crs` (assigned at normalize), `nodata`, `negate` (positive-down depths), `datum_offset_m`, `clamp_positive` (drop a lake DEM's land fringe), `archive_members` (fnmatch glob selecting archive members, e.g. `"*_lld.tif"`). Build flags as needed: `priority` (outrank a finer source, e.g. datum-authoritative), `mixed_crs`, `band`, `land_clamp` (coarse sources with no land/water concept), `volatile` (mirrored public tile collections whose upstream catalog drifts — a bucket-prefix listing like S-102 or a flat urllist like CUDEM; registered from header reads on every scheduled sources run, bytes mirrored, builds never touch the upstream).
   - `file_list.txt` — one upstream URL per line. Formats are detected from the bytes (zip / 7z / gzip / tar / ARC-INFO e00 / netCDF / ESRI ASCII mosaic / GeoTIFF) — no per-format wiring. A source whose URL list comes from an API/index gets a `harvest.py` that regenerates `file_list.txt` (human-run, committed — see batnas, uk_surfzone).
2. `just source <id>` — runs the lane for that source (verify it lands in `pipelines/store/source/<id>/`); equivalently `uv run snakemake sources --config source=<id>` from the repo root.
3. `just planet` — its tiles fold into the grid-cell overlays + manifest automatically (priority is derived from `(maxzoom, id)`). `just preview` over its bbox to eyeball depths and seams.
4. Nothing to wire in CI — the sources workflow discovers `sources/<id>/` directories automatically; dispatch it (optionally filtered to the new source), then dispatch a build.

Transform params live in the recipe (CLI args); `metadata.json` is attribution +
the optional `max_zoom` cap only.
