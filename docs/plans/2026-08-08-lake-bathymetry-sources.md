# Lake bathymetry sources — planning doc

_Written 2026-08-08. Point-in-time; the code is the source of truth._

Status: draft

## Problem

Green and blue stripes across southern Lake Ladoga ([viewer](https://openwatersio.github.io/seascape/#9.58/60.1285/31.2645)) trace to two independent defects and one source gap.

**GEBCO is inconsistent across lakes**, and the difference decides what any fix may do. Probed against `gebco2020` (opentopodata), the GEBCO 2024/2026 WMS, and our published tiles:

| Lake       | GEBCO value    | What it is                                            |
| ---------- | -------------- | ----------------------------------------------------- |
| Baikal     | −117 to −366 m | real lakebed, referenced to sea level (surface +456 m) |
| Caspian    | −423 m         | real lakebed                                          |
| Superior   | +24 / −74 m    | real lakebed, referenced to sea level (surface +183 m) |
| Tanganyika | flat +767 m    | lake surface only, no bed                             |
| Ladoga     | flat +1 m      | lake surface only, no bed                             |

Where GEBCO carries bed it is elevation relative to sea level, and the pipeline reads it as depth below chart datum: we serve Baikal at 297 m against a true ~820 m, an error equal to the lake's surface elevation. Shoal-safe but wrong. Superior escapes only because `great_lakes` outranks GEBCO there. Where GEBCO carries no bed, the flat positive surface is cleared by the #24 inverse clamp and the lake renders as unknown-depth water, which is correct.

**No global product carries Ladoga's bed.** GEBCO 2024/2026, ETOPO1, NOAA's global DEM mosaic and EMODnet 2024 all return the flat surface, including over the 200 m deep northern basin. EMODnet is `NaN` across the whole lake in both the published 2024 grid (ERDDAP `bathymetry_dtm_2024`) and the `D7_2024.tif` tile we ingest; the coverage layer naming EMODnet there reflects the coarsened footprint, not the value's origin.

**GEBCO 2026 adds a junk patch** between roughly 60.078 and 60.146 N whose values vary only with latitude — constant across 50 km of longitude, confirmed on three columns 25 km apart in our own tiles. GEBCO 2024 is flat there. Its values straddle zero, which split it into the two visible artefacts: below 0 entered the depth-band ladder, `[0, DRYING_CAP]` entered the drying bucket and painted foreshore green inside a lake.

Status of the three pieces:

- **Drying in lakes** — fixed by [#113](https://github.com/openwatersio/seascape/issues/113)'s tidal-water gate; the green clears on the next rebuild of the affected stems. (That issue describes Ladoga's remaining bands as "about 5 m off" pending a surface correction. They are not bathymetry at all, and no offset makes them correct.)
- **The datum error** — open; recommendation 6 of [2026-07-16-depth-below-water.md](2026-07-16-depth-below-water.md).
- **The junk patch** — open, and it survives the datum fix. HydroLAKES `Elevation` is the majority EarthEnv-DEM90 pixel inside the lake (GTOPO30 substitution above 60°N applies only where the value came out negative — v1.0 tech doc). Ladoga's is positive, so it stays EarthEnv-DEM90 and should agree with GEBCO's ~+1 m, making the subtraction a near-no-op. The patch keeps straddling zero.

## Goals / Non-goals

Serve real depth for Ladoga and the other large lakes we currently render as unknown-depth water, without a datum transform and without disturbing lakes where GEBCO already carries bed.

Not in scope: the GEBCO sea-level datum error on Baikal and the Caspian (independent, and GLDB covers neither lake's bathymetry); modeled lake fills; chart digitisation.

## Approach

Ingest GLDB (Global Lake Database, Kourzeneva & Choulga), which carries digitised bathymetry for 36 large lakes, Ladoga among them. Read directly from [`gldbv2.tar.gz`](http://www.flake.igb-berlin.de/data/gldbv2.tar.gz) (10 MB; investigated archive SHA-256 `283e7b9ceaf7bec522a80ed80a55010bb03d361574e74b02e2ac8ecf9e318ef0`):

```
lat\lon     30.40   30.80   31.20   31.60   32.00
61.30        79.2   144.1   127.6    19.9     0.0
61.10       132.7    61.7    57.7    72.4    66.6
60.70         0.0    20.9    60.4    56.6    57.7
60.30         0.0     5.0    16.4    15.2     6.2
```

The real shape — deep northern basin, shallow southern shelf, 230 m maximum in the inventory and 205.5 m in the gridded product. The grid is 30 arcsec (~460 m E–W at this latitude, ~930 m N–S), depth below the lake surface, positive-down: `negate: true` and **no datum offset**. Like `african_great_lakes`, it already stores depth below the local lake surface and needs negation but no surface correction.

Derived priority ranks GLDB **below** GEBCO: maxzoom derives from Mercator-meter resolution (`get_smallest_overzoom`), where 30 arcsec of longitude is ~927 m at every latitude — native z7, one zoom coarser than GEBCO's 15 arcsec; the ~460 m ground resolution at Ladoga's latitude never enters. Left there, GEBCO's land-clamped flat surface would nodata-fill from GLDB across most of the lake while its sub-zero junk patch stayed on top — the stripes survive at exactly the pixels this ingest targets, invisibly to a casual depth check. So GLDB sets `priority` in metadata, the override S-102 and INFOMAR use. Because that override also beats finer default-priority sources, the ingest excludes every component already covered by a dedicated source. A future dedicated lake source must either outrank GLDB or remove that component from GLDB's retained set.

Four constraints:

- **Extract only documented bathymetry components.** Every other lake in the grid holds a flat *mean* depth (Baikal reads a uniform 744 m), a modeled estimate, or a 10 m default. `GlobalLakeStatus.dat` cannot isolate bathymetry: status `3` combines local bathymetry with measured per-lake means. Seed the 36 entries in `LargeLakesWithBathymetry_v2.txt` and retain their connected nonzero components. They resolve to 33 components because Huron/Michigan, Bratsk's two entries, and Chudskoe/Pskovskoe each share one. Exclude the five Great Lakes components — six inventory entries: the ETOPO1 section includes Lake St. Clair, which `great_lakes` already covers inside the NCEI Erie grid — plus Victoria, Albert and Edward, all superseded by dedicated sources. The result is exactly 25 components: Ladoga plus the 24 additions listed below. Without this restriction the ingest paints flat plates over lakes worldwide and makes Baikal worse than today.
- **Cut the mean-depth plates inside the retained lakes.** Seeding from the inventory is not enough: GLDB fills the part of a bathymetry lake's mask it never digitised with that lake's measured mean, under the same status code as the digitised cells, so nothing but geometry separates them. Four components carry such a plate — Great Slave (13,682 cells at 41.0 m, the entire East Arm), Winnipeg (7,906 at 12.0 m, bridging across the Waterhen and Dauphin waterways into Winnipegosis, Manitoba and Cedar Lake, none of them documented), Ust-Ilim (1,317 at 32.0 m) and Beloe (777 at 5.5 m) — and two more carry a flat digitisation fill at some other value: Onega (877 at 50.0 m) and a second Winnipeg blob (537 at 16.2 m). So within each retained component, remove every 8-connected constant-value blob of 0.2 m or deeper whose size reaches 400 cells or a tenth of the component, then re-flood from the seed, dropping whatever a plate was bridging. Measured basis: removed blobs run 537–11,039 cells, the largest surviving constant blob is 330 (Vänern at 50.0 m), and the 0.1 m exemption protects genuine digitised shore rims reaching 2,148 cells (Winnipeg; Taymyr 1,508, Rybinsk 1,326). The relative arm covers the small lakes, where a proportionally large plate never reaches 400 cells. Ladoga is untouched — its largest constant blob is 15 cells. An area-vs-inventory guard is not a substitute: the plates lie inside the real lake mask, so Great Slave matches its documented area while 22% of it is plate.
- **Fail closed on archive changes.** Add a bounded `gldb` converter to the source-preparation format registry. It validates the archive members and global raster dimensions, reads the little-endian Int16 decimetre grid, resolves and deduplicates the documented components (8-connected — the asserted counts depend on the connectivity choice), applies the eight-component exclusion, and writes one georeferenced Float32 GeoTIFF per retained component — metres positive-down, everything else nodata, because 0.1 m precision at a 205.5 m maximum does not survive Int16 metres and gdalwarp ignores a band scale factor. Assert 36 seeds, 33 distinct components, the three documented merges by name, eight superseded components, 25 outputs, more than one depth in each, and no removable plate left after the cut. This is a dataset-format converter, not general-purpose lake masking machinery.
- **Licence verified.** The Data Availability section of [Toptunova, Choulga & Kurzeneva 2019](https://asr.copernicus.org/articles/16/57/2019/) explicitly says the GLDB dataset is available under CC-BY. Preserve dataset attribution and the bundled per-lake references in source documentation; the download page's "please cite" instruction is consistent with that licence.
- **Clamp the coarse shoreline.** Set `land_clamp: true` so aggregation clears negative GLDB cells where the effective OSM/Overture mask says land. At native GLDB resolution, 3.16% of Ladoga's component lies outside the current water polygons. Do not bake the changing Overture mask into source preparation: aggregation already owns the versioned shoreline contract. Missing or incorrect water polygons remain the ceiling.

Provenance is uneven and belongs under [#17](https://github.com/openwatersio/seascape/issues/17)'s grading: Ladoga's entry cites "Andreev P., Hydrometcentre of St Petersburg, 2009, personal communication" (unpublished); others cite GUNiO navy charts and the ILEC data books, 1988–1993 vintage. GLDB was built for atmospheric lake modeling, not navigation, and its digitised grids must not present as equivalent to modern hydrographic surveys. Build and preview may proceed independently, but publication requires the lowest applicable source-level confidence grade. Preserve the per-lake reference number in the generated inventory even if the first confidence field is conservative at source level.

The win is global. Exactly 25 distinct components currently served as unknown or incorrect depth gain real depth: Ladoga, Onega, Peipus, Rybinsk, Kuybyshev, Bratsk, Ust-Ilim, Beloe, Taymyr, Balkhash, Great Bear, Great Slave, Winnipeg, Athabasca, Saint-Jean, Vänern, Vättern, Mälaren, Hjälmaren, Sevan, Biwa, Champlain, Lough Neagh, Pyramid and Skadar. The Great Lakes entries derive from ETOPO1 and Victoria/Albert/Edward are coarser than the sources already built; all eight components are excluded before registration rather than relying on merge priority.

## Alternatives considered

- **Naumenko 2020 digital bathymetric model.** 0.5 km kriged grid from 70,190 soundings, RMSE 5.6 m, Institute of Limnology RAS ([Limnological Review 20(2):65–80](https://doi.org/10.2478/limre-2020-0008)). Twice GLDB's resolution and properly documented, but no repository deposit or data availability statement — contact the institute, with the friction that implies.
- **Russian GUNiO / Rosmorrechflot charts.** The survey source behind everything above. Not open.
- **Pre-1940 Finnish charts.** Northern Ladoga was Finnish territory and was charted; likely out of copyright by age, held in Finnish national collections. Needs scanning, georeferencing and manual sounding extraction — only worth it if a chart-digitisation path ever exists.
- **GLOBathy ([#34](https://github.com/openwatersio/seascape/issues/34)) and 3D-LAKES.** Cover Ladoga, but modeled from shoreline geometry and a maximum-depth prior. Ruled out as authoritative; blocked on #17 as a labeled low-zoom fill.
- **Per-lake presence test** — compare a source's implied maximum depth against HydroLAKES `Depth_avg` per lake, clear the lake where the ratio is low. Separation is wide enough to be mechanical (Ladoga 0.2, Baikal 2.2, Superior 1.7), but GLDB makes it unnecessary for Ladoga. Worth building only if other lakes turn out to need it.
- Nothing on Zenodo or PANGAEA.

## Validation

- Validate the archive checksum, expected members, binary dimensions, little-endian Int16 encoding and decimetre units.
- All 36 documented seeds resolve; deduplication yields 33 components; the eight superseded components are excluded; exactly 25 per-lake rasters are emitted.
- Every emitted component has more than one distinct positive depth, and no constant-value blob large enough to be a mean-depth fill survives in it. No default 10 m lake, default 3 m river, modeled estimate or flat measured-mean component or plate enters the output.
- The Ladoga component contains 42,785 valid native cells, 1,844 distinct decimetre values and a 0.1–205.5 m range. Its northern basin decodes near 140 m and its southern shelf near 15 m, with the latitude-banded GEBCO patch gone.
- The priority test proves GLDB wins over GEBCO despite GLDB's native z7, while the Great Lakes and Victoria/Albert/Edward remain sourced from their current dedicated products.
- Baikal, the Caspian, Tanganyika and a sampled unknown lake are untouched; the unknown lake still decodes exactly 0 and retains its `nodata` depth-area.
- For every retained lake, report gridded mean/max against `LargeLakesWithBathymetry_v2.txt`. Differences are expected from 1 km sampling and digitisation, but gross mismatches fail review. Measured after the plate cut, 23 of the 25 come in shallower than their documented maximum — the safe direction, and widest on Great Slave (100.2 m gridded against 614 documented, the East Arm never digitised), Great Bear (291.1 vs 446), Mälaren (21.7 vs 61), Beloe (6.5 vs 33), Kuybyshev (22.8 vs 44) and Rybinsk (12.4 vs 30.4). Two come in deeper, the unsafe direction: Sevan (90.3 vs 80.2) and Skadar (11.1 vs 8.3). Both belong at the lowest confidence grade, and neither is a lake this layer should be trusted on.
- The land clamp removes GLDB cells outside mapped water before they can generate terrain depth, contours or soundings. Preview shorelines and islands on Ladoga plus one reservoir and one highly articulated lake.
- Confidence/provenance output identifies GLDB at its conservative grade and preserves the bundled per-lake reference in the source inventory.

## Open questions

- Whether source-level confidence is sufficient for the first release or the uneven per-lake references require per-file confidence before publication. Use the lowest grade across GLDB if the schema remains source-level.
- Whether an explicit priority should remain a scalar override or grow a narrower "dedicated inland source over global fallback" tier. For this ingest the exclusions make the scalar safe; do not expand the priority model speculatively.

## Follow Ups

- Per-lake surface datums for the GEBCO sea-level error on Baikal and the Caspian ([2026-07-16-depth-below-water.md](2026-07-16-depth-below-water.md), recommendation 6). Independent of this work.
- Per-file/per-lake confidence after the conservative source-level GLDB grade under #17.
- GLOBathy (#34) as a labeled low-zoom fill for lakes outside the 36, once #17 lands.
- Remove a GLDB component whenever a better dedicated source is ingested; never rely on a future source accidentally winning the priority override.
