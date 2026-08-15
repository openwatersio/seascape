# Data Sources

The worldwide bathymetry-source survey behind the mosaic: what's built, what's open but not yet ingested, and — just as important — what was researched and ruled out, so nobody re-researches it.

Selection rule: **resolution sets the zoom cap, an openly-redistributable license is the gate.** Data is baked into served tiles, so viewer-only, encrypted, non-commercial, and request-by-email sources are unusable. GEBCO stays the fallback under everything. Vertical datum matters because a chart wants low water ([#16](https://github.com/openwatersio/seascape/issues/16)): sources already on LAT/MLLW/Chart Datum are the cleanest fit; MSL/elevation ones need an offset.

## Access patterns

A source is described declaratively in `metadata.json`, along two independent axes the engine's one Snakemake chain executes:

- **Enumerate** — how the file list is discovered. *Static* (default): the committed `file_list.txt` is the list, and only a change to it re-fetches. *Listed*: a `filter` (an fnmatch glob) turns a `file_list.txt` bucket-prefix (trailing `/`) or urllist line into a live listing, re-enumerated on the weekly sources cron — refresh cadence is derived from listedness, not declared (nz_coastal, CUDEM, S-102).
- **Materialize** — what happens to the bytes. *Processed* (default): fetch → `unpack` → datum → normalize to a 4326 COG → R2, streamed from there (EMODnet, DDM, CUDEM, most others). *Raw* (`raw: true`): the bytes are registered 1:1 from header reads and copied object-for-object into the data bucket (S-102), so builds range-read our copy and NOAA churn reddens a sources refresh but never a build. Raw means *verbatim* — a source needing any value transform is processed.

The axes are orthogonal: most sources are static + processed, nz_coastal and CUDEM are listed + processed, S-102 is listed + raw. Unpacking an archived asset is declarative too — `unpack: "format[:glob][!N]"` (`"zip:*.tif"`, `"tar.gz:*_lld.tif!1"`, `"7z:*_ras.tif"`, `"asc-mosaic"`, `"e00"`, `"netcdf"`, `"gldb"`; `!N` asserts exactly N matches per archive), absent for a bare raster.

(`raw` was called `volatile`, and raw sources "mirrored", before the model split enumeration from materialization.)

## Built sources

| Source                                                          | Native res  | Zoom cap  | Coverage                            | Datum                   |
| --------------------------------------------------------------- | ----------- | --------- | ----------------------------------- | ----------------------- |
| [GEBCO 2026](gebco/)                                            | ~450 m      | ~z8       | global                              | MSL                     |
| [EMODnet 2024](emodnet/)                                        | ~115 m      | z11       | European seas                       | **LAT**                 |
| [DDM (Denmark)](ddm/)                                           | 50 m        | z12       | Danish EEZ                          | MSL (DKMSL2022)         |
| [Kartverket 50 m](kartverket_50m/)                              | 50 m        | ~z11      | Norway coast + Svalbard + Barents   | **LAT** inshore, MSL offshore |
| [CUDEM](cudem/)                                                 | 3.4 / 10 m  | z13       | US coast + PR/USVI                  | **MLLW** (VDatum grid)  |
| CUDEM [Hawaii](cudem_pacific_hawaii/)                           | 3.4 / 10 m  | z13       | 8 main Hawaiian islands             | **MLLW** (+0.351 m scalar) |
| CUDEM [Guam](cudem_pacific_guam/)                               | 3.4 / 10 m  | z13       | Guam                                | **MLLW** (+0.418 m scalar) |
| CUDEM [CNMI](cudem_pacific_cnmi/)                               | 3.4 / 10 m  | z13       | Saipan, Tinian, Rota                | **MLLW** (+0.393 m scalar) |
| CUDEM [Am. Samoa](cudem_pacific_samoa/)                         | 3.4 / 10 m  | z13       | Tutuila, Manu'a                     | ASVD02, uncorrected     |
| [NOAA S-102](noaa_s102/)                                        | ~4–16 m     | z15       | US navigable                        | MLLW (+ uncertainty)    |
| [Vaklodingen](vaklodingen/)                                     | 20 m        | z12       | Netherlands                         | NAP (~MSL)              |
| INFOMAR ([10 m](infomar_10m/), [25 m](infomar_25m/))            | 10 m / 25 m | z13 / z11 | Ireland inshore + shelf             | **LAT**                 |
| [UK SurfZone](uk_surfzone/)                                     | 2 m         | z15       | England intertidal                  | ODN (~MSL)              |
| [EA Multibeam](uk_multibeam/)                                   | 0.5 m       | z15       | England nearshore (mostly east coast) | ODN (~MSL)            |
| [CCO Multibeam](uk_cco/)                                        | 0.25–4 m    | z15       | England & Wales nearshore           | ODN (~MSL)              |
| [DORIS](uk_cco_doris/)                                          | 1 m         | z15       | Dorset coast                        | ODN (~MSL)              |
| [Litto3D Bretagne](litto3d_bretagne/)                           | 5 m         | z14       | Brittany coast                      | **ZH** (IGN69 − `ign69_zh` surface) |
| [GSC Atlantic](gsc_atlantic/)                                   | 100 m       | ~z10      | Scotian Shelf + NL                  | undocumented (mixed)    |
| [GSC Pacific](gsc_pacific/)                                     | 10 m        | z13       | BC coast + Salish Sea               | **Chart Datum (LLWLT)** |
| [gbr30](gbr30/)                                                 | 30 m        | z12       | GBR + Coral Sea                     | MSL                     |
| [AusBathyTopo](ausbathytopo/)                                   | 250 m       | z9        | Australia EEZ                       | MSL                     |
| [BATNAS](batnas/)                                               | ~180 m      | z10       | Indonesia                           | MSL                     |
| [swIOBC](swiobc/)                                               | 250 m       | z9        | SW Indian Ocean                     | ~MSL                    |
| [NZ Coastal LiDAR](nz_coastal/)                                 | 1 m         | z15       | NZ coastal clusters                 | NZVD2016 (~MSL)         |
| [NOS Estuarine](noaa_estuarine/)                                | 30 m        | z11       | 70 US estuaries                     | **MLLW**                |
| [Great Lakes (NCEI)](great_lakes/)                              | ~90 m       | z10       | Great Lakes (incl. Canadian halves) | **LWD**                 |
| [African Great Lakes](african_great_lakes/)                     | 50–100 m    | z13       | Victoria/Albert/Edward/George       | lake surface            |
| swissBATHY3D ([Léman](lac_leman/), [Neuchâtel](lac_neuchatel/)) | 1–2 m       | z15 / z16 | Léman, Neuchâtel                    | LN02 − surface offset   |
| [Bodensee](bodensee/)                                           | 3 m         | z15       | Lake Constance                      | DHHN92 − surface offset |
| [Lake Tahoe](lake_tahoe/)                                       | 10 m        | z13       | Lake Tahoe                          | MSL − surface offset    |
| [GLDB v2](gldb/)                                                | ~930 m      | ~z7       | 25 large lakes worldwide            | lake surface            |

Priority is derived, not configured: `(maxzoom, id)`, so GEBCO (smallest maxzoom) loses wherever a finer regional source overlaps — except a source can set `priority` in metadata to win regardless of zoom: datum-authoritative (S-102 over CUDEM, INFOMAR over EMODnet), or carrying a bed where the finer source has only a flat lake surface (GLDB over GEBCO). Zoom caps are display caps (`max_zoom`), not native resolution. Inland lakes are pure GEBCO gap-fill: hydraulically isolated, so no seam against the ocean base. A freshwater grid storing lakebed _elevation_ carries a "subtract surface level" offset; one already stored as depth below its own surface (African Great Lakes, GLDB) only negates.

S-102's ~4.3k products are copied object-for-object into the data bucket on a schedule rather than downloaded to a runner, and aggregation range-reads our copy, so NOAA churn or outages can redden a sources refresh but never a build. It takes the per-tile engine path too: its products span multiple UTM zones, so the mosaic reprojects them per-tile rather than as one VRT. CUDEM's ~1.5k tiles (~197 GB) are prepared on the box against the persistent store volume, because each tile is corrected onto local chart datum before it registers ([datum plan](../docs/plans/2026-08-03-datum-vdatum.md)).

CUDEM is five sources, split on **datum treatment alone**. Resolution is not a split axis: build depth is derived per file from its own pixel size, so each source carries both published bands — the 1/9″ nearshore band (~3.4 m, z15 native) and the 1/3″ band that telescopes out beyond it (~10 m, z13 native, and most of Alaska's coverage) — and the merge orders the finer files first where the covering separates them. `max_zoom: 13` caps that derivation as a build budget, not as a claim about resolution; dropping it would build the whole US coastal strip at z15, which is the [native-resolution plan](../docs/plans/2026-07-14-native-resolution.md)'s Part 3, not a per-source decision.

What is left is the datum, which is a property of the NCEI dataset and cannot be expressed per file. CONUS, Alaska, Puerto Rico and the USVI are all correctable from one VDatum grid, so they are `cudem`'s 1,382 tiles. Each Pacific territory sits on its own island datum that no VDatum grid reaches, so each is its own source with its own `datum_offset_m` — and American Samoa, whose datum cannot be tied to MLLW at all, is its own source precisely so it can say so.

Horizontal frames differ by dataset — NAD83 (EPSG:4269) nationally and in PR/USVI, the CNMI and American Samoa, WGS 84 (EPSG:4326) in Hawaii, NAD83(MA11) (EPSG:6325) on Guam's 1/9″ band (all 1,545 tile headers read) — but that no longer costs anything: heterogeneous CRS inside a source is served by lazy per-tile warped VRTs, measured cheaper than the single-VRT path. Only Guam needs it, because its two bands are in different frames *and* share a build depth, so they would otherwise meet in one VRT, which drops off-frame inputs silently; it carries `mixed_crs` and the rest do not. Vertical datums play no part in the partitioning — every staged raster loses the vertical half of a compound CRS at prep, so no warp can apply a geoid shift to the pixel values.

The Pacific scalars come from CO-OPS station datums (`mdapi/prod/webapi/stations/{id}/datums.json`, epoch 1983–2001), since the local island datums are MSL realizations: GUVD04 sits 3 mm above MSL at Apra and NMVD03 3 mm above MSL at Saipan, each at its own origin station, and Hawaii's tiles declare local MSL outright (EPSG:5714). Where an island group's stations disagree, the applied value is the **largest** MSL−MLLW across them, not the smallest — too small a scalar leaves part of the original deep bias in place, while too large charts shallower than truth, and only the second is the safe direction.

| source | MSL−MLLW range across stations | applied | governing station | worst over-shallowing |
| --- | --- | --- | --- | --- |
| `cudem_pacific_hawaii` | +0.250 … +0.351 m (13 stations) | **+0.351** | Hilo 1617760 | 0.101 m (Honolulu, Nawiliwili) |
| `cudem_pacific_guam` | +0.308 … +0.418 m (2 stations) | **+0.418** | Apra Harbor 1630000 | 0.110 m (Pago Bay) |
| `cudem_pacific_cnmi` | +0.393 m (1 station) | **+0.393** | Saipan 1633227 | not measurable |
| `cudem_pacific_samoa` | +0.415 m (1 station) | **none** | — | — |

Per-island granularity would not buy accuracy the stations can support: within one island the spread is already as wide as between islands (Oahu alone runs +0.250 leeward to +0.329 windward, Guam +0.308 to +0.418 over 15 km), so keying constants to island polygons would cut Hawaii's worst residual from 0.101 m only to 0.079 m — well inside both CUDEM's own vertical accuracy and the ~9 cm uncertainty NOAA states for this class of datum transform.

American Samoa is the one territory left uncorrected. Pago Pago 1770000 is its only station, it publishes no ASVD02 row to tie the DEM's declared datum to MLLW, and its datums sit on a modified 2011–2019 epoch rather than the 1983–2001 NTDE — the signature of the post-2009-earthquake re-determination, whose subsidence offsets ASVD02 from present-day MSL by an amount nobody publishes. A scalar there would be a guess whose sign cannot be bounded, so the ~0.4 m deep bias stays documented instead. The Manu'a group, 100 km east of Tutuila, has no station at all.

## Open candidates

Every open candidate is a GitHub issue labeled [`source`](https://github.com/openwatersio/seascape/issues?q=is%3Aissue%20state%3Aopen%20label%3Asource), each carrying the verified website, license, access path, and ingest notes. Headlines:

- [#29](https://github.com/openwatersio/seascape/issues/29) **AusSeabed per-survey COGs** — the AU z12–13 tier, the biggest open win; access verified (public WFS index + anonymous zips).
- [#36](https://github.com/openwatersio/seascape/issues/36) **Great Salt Lake** — the last unbuilt surveyed lake.
- [#38](https://github.com/openwatersio/seascape/issues/38) **IBCAO** — best Arctic resolution, blocked on a licence-ambiguity question, not build work.
- [#86](https://github.com/openwatersio/seascape/issues/86) **Berlin Tiefenlinienkarte** + [#87](https://github.com/openwatersio/seascape/issues/87) **Brandenburg Seenvermessung** — the Berlin/Potsdam waterways (Müggelsee, Wannsee, Templiner See, 253 BB lakes); both vector isobaths, need a contour→grid step; low-water offsets per impoundment pool from PEGELONLINE/BWu (in the issues).

## Ruled out (don't re-research)

License is the real filter, not data existence — whole regions surveyed their waters but lock the result. For these coasts GEBCO stays the only option:

| Source / region                                                                                              | Why skipped                                                                                                                                                              |
| ------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| IBCSO v2 (Southern Ocean)                                                                                    | ≈GEBCO resolution _and_ already folded into GEBCO via Seabed 2030 — no new coverage; <85°S untileable                                                                    |
| SRTM15+                                                                                                      | same resolution as GEBCO, already folded in                                                                                                                              |
| ArcticDEM                                                                                                    | topographic land, not bathymetry                                                                                                                                         |
| NIWA NZ 250 m                                                                                                | CC BY-**NC**-SA — offshore NZ stays GEBCO-only (the coast is now [nz_coastal](nz_coastal/), 1 m open)                                                                    |
| LINZ hydro (NZ)                                                                                              | S-63 encrypted / request-by-email — superseded inshore by the open [nz_coastal](nz_coastal/) LiDAR                                                                       |
| SPC Pacific islands 5 m lidar                                                                                | sovereignty-gated, country-owned; public entries are GEBCO-derived                                                                                                       |
| HELCOM BSBD (Baltic)                                                                                         | 250 m — coarser than the EMODnet 115 m already ingested                                                                                                                  |
| Sweden, Spain, Portugal, Italy, Greece                                                                       | defense-restricted, viewer-only, or ≤EMODnet                                                                                                                             |
| Japan (JODC J-EGG500, JHA M7000)                                                                             | no-redistribute / paid; soundings reach us via GEBCO anyway                                                                                                              |
| India (INCOIS, NHO)                                                                                          | nationals-only / S-63                                                                                                                                                    |
| Philippines, China, Taiwan                                                                                   | priced / state-secret / gated                                                                                                                                            |
| Vietnam, Thailand, Malaysia, Singapore                                                                       | ENC/chart products only, no open grid                                                                                                                                    |
| Brazil LEPLAC, de Wet SA shelf, Lesser Antilles, EOMAP, Israel, Mexico IBCCA                                 | study-only / NC / no-license / commercial                                                                                                                                |
| Brazil DHN, Chile SHOA, Argentina SHN, Peru/Colombia/Ecuador, Caribbean HOs, SANHO, W/E Africa, Arabian Gulf | closed / request-only — no open hi-res source exists                                                                                                                     |
| HydroLAKES                                                                                                   | vector + scalar depth only — useful as a free lake mask, not bathymetry                                                                                                  |
| Caspian Sea                                                                                                  | already inside GEBCO                                                                                                                                                     |
| Baikal, Tanganyika, Malawi, Great Bear/Slave, Titicaca, MN-DNR, Champlain, Salton, TWDB, Mekong/Yangtze      | NC / no-license / points-only / closed — the dedicated surveys. Great Bear and Champlain carry [GLDB](gldb/)'s coarse digitised bed; Great Slave only its western basin (to ~100 m — the 614 m East Arm was never digitised) |
| LUNG M-V Seenkataster Tiefenkarten (Mecklenburg lakes incl. Müritz)                                          | the data exists (~900 lakes, 1 m isobaths) but is private-use-only, no redistribution (INSPIRE Art. 13(1)(e)); a LUNG/ministry contract is the only path to Müritz depth |
| German Inland ENCs (ELWIS IENC)                                                                              | open (GeoNutzV) but carry **no depth values** (DEPARE 0/0, no SOUNDG — verified on Potsdamer Havel cells); no cells at all for the Mecklenburg waterways                 |
| WSV Peildaten / 3D-Datenarchiv BWaStr                                                                        | the real waterway-depth archive is intranet-only; public DGM-W Atom feed ([#53](https://github.com/openwatersio/seascape/issues/53)) has no Berlin/Mecklenburg tiles     |

Shelved with a revisit path (kept as issues, not re-research):

- **CHS NONNA** — sparse multibeam survey coverage, wrong fit for a continuous-DEM mosaic (removed at git `9f93ad3`); revisit as a soundings ingest, [#44](https://github.com/openwatersio/seascape/issues/44). Its licence is also non-navigational-use-only — see the issue.
- **Allen Coral Atlas SDB** (Bahamas/N. Caribbean) — recipe complete (PR #5, unmerged) but satellite-derived bathymetry proved too noisy for a chart; revisit via ATL24 if ever.

Two coverage notes that look like gaps but aren't: EMODnet's 58 tiles are the full product and include the **N. African Mediterranean shelf** (the Med is enclosed, so its tiles carry the African shore); nothing European reaches the Caribbean.

## Cross-cutting

- **Datum is the recurring wrinkle.** Already low-water (plug into the chart-datum work cleanly): EMODnet (LAT), INFOMAR, S-102, NOS Estuarine, GSC Pacific (CHS Chart Datum, LLWLT), NCEI Great Lakes (LWD), Kartverket (LAT inshore; its MSL half is deep MAREANO water where the datum bias is immaterial), and among candidates UKHO-EEZ, BSH. CUDEM gets there in prep: `offset_surface` subtracts a composed VDatum separation grid per pixel (a scalar can't — the NAVD88−MLLW span is +0.13 to +3.3 m across US waters), charting the Columbia River against CRD rather than MLLW, and leaving Alaska off the SE panhandle on NAVD88. Litto3D takes the same route on the other side of the Atlantic: Shom ships it as an altitude in each zone's legal system, not on a chart datum, and `ign69_zh` (`datum_grid_fr.py`, composed from Shom's BATHYELLI and IGN's geoid grids, then centred on the RAM per-port ties) carries the 1.3–6.8 m separation a scalar cannot. The Pacific territories have no VDatum grid at all and reach MLLW through a per-island-group CO-OPS scalar instead, American Samoa excepted. Everything else MSL/NAP/ODN/elevation needs an offset. GSC Atlantic declares no vertical datum at all — CHS chart datum inshore blended into ~MSL offshore, so no single offset is correct. USACE eHydro mixes MLLW vs LWRP _per district_ — its single biggest ingest risk ([#50](https://github.com/openwatersio/seascape/issues/50)).
- **Modeled ≠ surveyed.** GLOBathy/3D-LAKES are interpolated depth, not measurement — fine as a labeled low-zoom fill, never as authoritative depth (violates the "honest about quality" principle if shown un-flagged; blocked on [#17](https://github.com/openwatersio/seascape/issues/17)).
- **The one open global inland compilation is coarse and old.** [GLDB](gldb/) digitises 36 large lakes at 30 arcsec from navy charts, the ILEC data books and topographic atlases of 1960s–2000s vintage — real bathymetry, but built for atmospheric lake modeling and no substitute for a survey. Every other global lake product is modeled; surveyed lakes are still ingested one by one (see the built table).

## Adding a source

Check the catalog above first — the candidate may already be cataloged (with verified license/datum/access notes in its `source` issue) or ruled out.

1. Create `sources/<id>/` — two files, no recipe (the Snakemake lane discovers the directory and routes it by metadata):
   - `metadata.json` — `name`, `producer`, `website`, `license`, and an optional `max_zoom` cap (omit to use the source's native resolution; cap it for high-res lidar like CUDEM). Prep knobs as needed: `crs` (assigned at normalize), `nodata`, `negate` (positive-down depths), `datum_offset_m`, `offset_surface` (a reference raster in `store/datum/` subtracted per pixel, for a datum separation that varies across the source — CUDEM's NAVD88→chart datum), `clamp_positive` (drop a lake DEM's land fringe), `valid_range` (`[min, max]` in the source's final frame — voids everything outside the depths it can physically contain, for an upstream that ships impossible values as ordinary data rather than as its nodata: EA multibeam carries -999/-9910/+99/+999 among soundings that really span -101..+3.2), `unpack` (how to materialize each asset — `format[:glob][!N]`, e.g. `"zip:*.tif"`, `"tar.gz:*_lld.tif!1"`, `"7z:*_ras.tif"`, `"asc-mosaic"`, `"asc-tile[:glob]"`, `"e00"`, `"netcdf"`, `"gldb"`; absent = a bare raster). Enumeration/materialization: `filter` (an fnmatch glob — turns file_list.txt's bucket-prefix/urllist into a live listing, re-enumerated weekly), `raw: true` (register bytes 1:1 and copy the objects into the data bucket instead of preparing them — a public tile collection like S-102 whose upstream catalog drifts and whose bytes need no transform; builds range-read our copy, never the upstream). Build flags as needed: `priority` (outrank a finer source, e.g. datum-authoritative), `mixed_crs`, `band`, `land_clamp` (coarse sources with no land/water concept).
   - `file_list.txt` — the enumeration input. Static: one upstream URL per line. Listed (`filter` set): a single bucket-prefix (trailing `/`) or urllist line the engine lists at build time. An archived/wrapped asset (zip / 7z / tar.gz / ARC-INFO e00 / netCDF / ESRI ASCII mosaic / the GLDB archive) declares its shape via `unpack` above; a bare raster needs no key. Only an API-gated enumeration (a login token, a key sweep) still needs a `harvest.py` to regenerate `file_list.txt` (human-run, committed — see batnas, uk_surfzone).
2. `just source <id>` — runs the lane for that source (verify it lands in `pipelines/store/source/<id>/`); equivalently `uv run snakemake sources --config source=<id>` from the repo root.
3. `just planet` — its tiles fold into the grid-cell overlays + manifest automatically (priority is derived from `(maxzoom, id)`). `just preview` over its bbox to eyeball depths and seams.
4. Nothing to wire in CI — the sources workflow discovers `sources/<id>/` directories automatically; dispatch it (optionally filtered to the new source), then dispatch a build.
