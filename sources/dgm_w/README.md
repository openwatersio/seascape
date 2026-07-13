# WSV DGM-W — German Federal Waterway Bathymetry (2 m)

Digitales Geländemodell des Wasserlaufs (DGM-W): the WSV's 2 m terrain-plus-riverbed
model for the German federal waterways (Bundeswasserstraßen). A multi-purpose model — it
merges the surveyed river/estuary bed with the surrounding land terrain, not just soundings.

- **Producer:** Wasserstraßen- und Schifffahrtsverwaltung des Bundes (WSV) / Bundesanstalt für Gewässerkunde (BfG)
- **License:** GeoNutzV (Geodatennutzungsverordnung), tagged `geonutz/20130319` = free use on the
  federal open-data portals; INSPIRE access flagged `noLimitations`. Attribution required:
  **© Wasserstraßen- und Schifffahrtsverwaltung des Bundes (WSV)**.
- **Resolution / type:** 2 m, Float32 GeoTIFF, nodata −32768.
- **CRS:** varies per tile (DHDN / Gauss-Krüger and UTM/ETRS89 zones) → `mixed_crs`.

## Access

Published through the WSV INSPIRE "Höhe" (Elevation) pre-defined **Atom download service**:

- Browse: <https://via.bund.de/wsv/inspire/> → "INSPIRE Höhe – Höhenmodell Bundeswasserstraßen"
- Machine-readable tile list (all datasets, direct links):
  `https://via.bund.de/wsv/inspire/resttpl/client?request=getTableData&serviceId=1c4c7ffc-cdd8-11eb-8fb7-005056a877b6`
- View-only WMS: `https://via.bund.de/wsv/inspire/el/wms` (plus a separate hillshade / Schummerung BWaStr WMS).

Each dataset is a `.zip` containing one GeoTIFF tile. `file_list.txt` holds the direct URLs;
`source_download` + `source_unzip` fetch and extract them. The feed also carries 19 `Höhenlinien`
(contour) shapefile zips at a fixed 2.5 m interval — **deliberately excluded**; they are isolines
of the same NHN surface and Seascape generates its own contours from the merged DEM.

The full feed is 93 DEM tiles across 15 waterways: Elbe (35), Rhein (19), Grenzoder (8), Saar (5),
Nordsee (4), Ems (4), Main (4), Havel-Oder (3), Lahn (3), Dortmund-Ems-Kanal (2), Main-2 (2), and
one each for Jade, Mosel, Ober-/Mittelweser, and Unter-/Außenweser.

## The datum problem, and why only tidal reaches are active

DGM-W stores **orthometric NHN elevation** (DHHN2016, height above ~mean sea level), not depth.
NHN is a geodetic datum, not a chart datum: up is positive, and only points below sea level go
negative. That splits the waterways in two:

- **Tidal / estuary reaches** (Nordsee, Außenelbe below the Geesthacht weir at Elbe-km 586, Jade,
  Unter-/Außenweser) sit near or below MSL, so their beds read as genuine depth. **These 16 tiles
  are active** in `file_list.txt`. Interim they ingest at NHN (≈ MSL), which reads ~1–3 m shallow
  versus chart datum in these macrotidal estuaries — the same MSL-vs-chart-datum bias Seascape
  already tolerates for GEBCO. The datum step below upgrades them to true chart datum.
- **Inland reaches** (Rhein, Main, Mosel, Saar, Lahn, Grenzoder, Havel-Oder, upper Elbe above the
  weir, Ems, Dortmund-Ems-Kanal, Ober-/Mittelweser) have beds tens of metres above MSL (the Saar
  bed is ~+130 m). As trusted data they keep their true elevation and render as **land, not
  water**. There is no single offset that fixes this — a river's water surface slopes downstream —
  so these tiles are **commented out** in `file_list.txt` pending the inland datum work below.

Not for navigation: NHN/SKN depths are approximate and unreduced to chart standards. This source
is visualization-only, consistent with Seascape's non-navigational disclaimer.

## Vintage

The Atom feed is the baseline national set; some tiles are older (the Saar tile is dated 2014,
the hillshade layer caps at 2015). Newer high-resolution project DEMs ship separately with DOIs —
e.g. **DGM-W Elbe 2022** on Zenodo (`10.5281/zenodo.17378778`) and via GovData/BfG — and could
supersede the estuary tiles here if desired.

## Datum normalization (planned)

Goal: make `just source dgm_w` emit a **low-water-referenced** COG, so the source synced to R2 is
already depth-below-chart-datum and needs no special handling downstream. This generalizes the
scalar lake `--offset` (Bodensee) to a spatially-varying reference **surface**.

Core transform, per pixel: `bed_chartdatum = bed_NHN − reference_NHN(x, y)`, where
`reference_NHN` is the low-water surface height in NHN at that location. Result is bed elevation
referenced to low water (negative = depth below datum, same convention as MLLW/LAT sources);
clamp values above the reference to nodata to drop the terrain fringe.

Proposed step (extends `source_datum` with an `--offset-surface <ref>` mode, or a small
`source_datum_lowwater.py`), inserted **after `source_unzip`, before `source_normalize`**:

1. **Fetch + cache the reference surface** into `store/source/dgm_w/` alongside the processed COGs:
   - **Tidal — ready today.** BSH **SKN-Fläche Nordsee 2026** ("Chart datum for the German
     Bight"), a gridded surface of Seekartennull (≈ LAT) in NHN covering the sea, Watten, and
     estuaries. **CC-BY 4.0.** Atom `https://gdi.bsh.de/de/feed/Chart-datum-for-the-German-Bight-2026.xml`,
     also a WCS and a direct ZIP. One fetch covers every active tidal tile.
2. **Reproject/resample** the reference onto each DEM tile's grid and CRS (bilinear — it's a smooth
   continuous surface, unlike categorical data), giving a per-tile `reference_NHN` raster.
3. **Subtract** (`bed_NHN − reference_NHN`), set nodata where the reference doesn't cover the tile,
   clamp positives to nodata. Record the applied datum in the catalog sidecar so `metadata.datum`
   reflects "SKN (LAT) via BSH SKN-Fläche subtraction."
4. `source_normalize` (no `--crs`, keep per-tile CRS) → COG, as today.

Open items for the tidal step:
- Confirm the SKN-Fläche grid encodes SKN **height in NHN** (expected) and its eastern extent —
  the published envelope stops near 9.5° E, so the inner tidal Elbe toward Hamburg/Geesthacht
  (~10° E) may fall outside it; those pixels fall back to nodata or ~MSL until confirmed via the
  WCS `DescribeCoverage`.

**Inland (deferred).** The reference is the WSV low-water longitudinal profile: **GlW**
(Gleichwertiger Wasserstand) for free-flowing rivers, **Stauziel** for impounded reaches — NHN vs
river-km. Unlike the tidal surface, there is no packaged download: GlW is published as ELWIS PDF
tables and computed in BfG's FLYS web tool, and per-gauge values sit in gauge datasheets.
PEGELONLINE's REST API (`pegelonline.wsv.de/webservices/rest-api/v2`) gives every gauge's river-km
and coordinates but not the GlW value. So the inland step must **assemble** a longitudinal profile
from published GlW/Stauziel gauge values, interpolate along km, project across the channel width,
then apply the same subtraction. Bounded (gauges are finite and km-referenced) but real work —
tracked separately from this PR.

## Pipeline

Run from `pipelines/`: `just ../sources/dgm_w/`. Standard prepared path, modeled on
`noaa_estuarine` (the other `mixed_crs` source). The datum step above is not yet wired in; until it
lands the active tidal tiles ingest at NHN (≈ MSL).
