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

## Datum normalization

`just source dgm_w` emits a **low-water-referenced** COG: raw DGM-W is orthometric NHN, and the
prep subtracts the SKN-in-NHN reference so the synced COG is depth below chart datum (SKN ≈ LAT),
needing no special handling downstream. This is the scalar lake `--offset` (Bodensee) generalized
to a spatially-varying reference **surface**.

Per pixel: `bed_chartdatum = bed_NHN − SKN_NHN(x, y)`. Result is bed elevation referenced to chart
datum (negative = depth below datum, same convention as the MLLW/LAT sources); above-datum cells
(land, drying flats) are clamped to nodata.

Pipeline steps (after `source_unzip`, before `source_normalize`):

1. `source_reference_dgmw.py` builds the SKN surface into `store/source/dgm_w/reference/`
   (a subdir, so the pipeline's `*.tif` globs never treat it as a data tile). Two pieces merged
   into one EPSG:4326 raster:
   - **Outer estuaries + open Bight** — BSH **SKN-Fläche Nordsee 2026** ("Chart datum for the
     German Bight"), a published grid of SKN in NHN. **CC-BY 4.0.** Atom
     `https://gdi.bsh.de/de/feed/Chart-datum-for-the-German-Bight-2026.xml` (also WCS / ZIP). Its
     east edge is ~9.5° E, so it covers Nordsee, Jade, Außenweser, and the outer Elbe.
   - **Inner tidal Elbe (Hamburg reach, east of the grid to the Geesthacht weir)** — no grid
     reaches here, so it is assembled from the **GDWS per-gauge SKN** values ("Aktuelles
     Seekartennull an den Tidepegeln … ab 2026") placed at each gauge's river-km/position
     (PEGELONLINE) and interpolated along the gauge polyline. SKN there is ~−1.9 m NHN tapering to
     ~−1.2 m near Zollenspieker; above the weir is non-tidal, so the profile is clamped at the
     most-upstream gauge. Both pieces are the 2026 vintage — refresh the grid URL and gauge table
     together when BSH republishes.
2. `source_datum --offset-surface reference/skn_reference.tif --clamp-positive` reprojects the
   reference onto each tile (bilinear, cross-CRS), subtracts it, and drops above-datum cells.
3. `source_normalize` (no `--crs`, keep per-tile CRS) → COG.

Validated end-to-end on two real tiles: outer Elbe km710–728 (BSH grid) and inner Elbe km620–639
(Hamburg, assembled fill — 0 % → covered), both yielding water-only depths below chart datum.

**Clamp caveat / follow-up.** `--clamp-positive` drops everything above chart datum, which removes
the surrounding land *and* intertidal drying flats a chart would show. Follow-up: instead of a
blunt `>0` clamp, reconcile against the OSM land–water mask (`landmask.py`) so genuine drying areas
inside mapped water survive while dike/land terrain is dropped.

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
