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

Each dataset is a `.zip` containing one GeoTIFF tile (`unpack: zip:*.tif!1`); `file_list.txt`
holds the direct URLs. The feed also carries 19 `Höhenlinien` (contour) shapefile zips at a fixed
2.5 m interval — **deliberately excluded**; they are isolines of the same NHN surface and Seascape
generates its own contours from the merged DEM.

The full feed is 93 DEM tiles across 15 waterways: Elbe (35), Rhein (19), Grenzoder (8), Saar (5),
Nordsee (4), Ems (4), Main (4), Havel-Oder (3), Lahn (3), Dortmund-Ems-Kanal (2), Main-2 (2), and
one each for Jade, Mosel, Ober-/Mittelweser, and Unter-/Außenweser.

## The datum problem, and why only tidal reaches are active

DGM-W stores **orthometric NHN elevation** (DHHN2016, height above ~mean sea level), not depth.
NHN is a geodetic datum, not a chart datum: up is positive, and only points below sea level go
negative. That splits the waterways in two:

- **Tidal / estuary reaches** (Nordsee, Außenelbe below the Geesthacht weir at Elbe-km 586, Jade,
  Unter-/Außenweser) sit near or below MSL, so their beds read as genuine depth. **These 16 tiles
  are active** in `file_list.txt`, referenced to SKN chart datum by the step below.
- **Inland reaches** (Rhein, Main, Mosel, Saar, Lahn, Grenzoder, Havel-Oder, upper Elbe above the
  weir, Ems, Dortmund-Ems-Kanal, Ober-/Mittelweser) have beds tens of metres above MSL (the Saar
  bed is ~+130 m). As trusted data they keep their true elevation and render as **land, not
  water**. There is no single offset that fixes this — a river's water surface slopes downstream —
  so each reach needs its own low-water surface (GlW / Stauziel / MNW profiles) subtracted the same
  way. Those reaches stay **commented out** in `file_list.txt` until their surfaces land; the
  machinery below (corridor fill along a gauge spine) is built to take them.

Not for navigation: NHN/SKN depths are approximate and unreduced to chart standards. This source
is visualization-only, consistent with Seascape's non-navigational disclaimer.

## Vintage

The Atom feed is the baseline national set; some tiles are older (the Saar tile is dated 2014,
the hillshade layer caps at 2015). Newer high-resolution project DEMs ship separately with DOIs —
e.g. **DGM-W Elbe 2022** on Zenodo (`10.5281/zenodo.17378778`) and via GovData/BfG — and could
supersede the estuary tiles here if desired.

## Datum normalization

The prep emits a **low-water-referenced** COG: raw DGM-W is orthometric NHN, and the prep
subtracts a **low-water reference surface** — the local low-water datum expressed in NHN — so the
synced COG is depth below chart datum, needing no special handling downstream. This is the scalar
lake `--offset` (Bodensee) generalized to a spatially-varying surface, the same
`offset_surface` mechanism the CUDEM/VDatum correction uses.

Per pixel: `bed_depth = bed_NHN − datum_NHN(x, y)`. Result is bed elevation referenced to the
local low water (negative = depth below datum, same convention as the MLLW/LAT sources);
above-datum cells (land, drying flats) are clamped to nodata (`clamp_positive`).

The surface: `build_reference.py` (bespoke, lives in this source dir; the Snakefile's
`datum_surface_dgm_w` rule runs it) composes `store/datum/dgm_w_lowwater.tif` — **SKN**
(Seekartennull ≈ LAT) in NHN. Outer estuaries + open Bight come from the BSH **SKN-Fläche
Nordsee 2026** ("Chart datum for the German Bight") grid of SKN in NHN, fetched at build time
(**CC-BY 4.0**; Atom `https://gdi.bsh.de/de/feed/Chart-datum-for-the-German-Bight-2026.xml`,
also WCS / ZIP; east edge ~9.5° E covering Nordsee, Jade, Außenweser, outer Elbe). East of the
grid to the Geesthacht weir, the **inner tidal Elbe** is assembled from the per-gauge SKN values
in `tideelbe_skn.csv` (transcribed from the GDWS "Seekartennull an den Tidepegeln … ab 2026"
table; SKN ~−1.9 m NHN tapering to ~−1.2 m near Zollenspieker, clamped at the most-upstream gauge
above the weir), interpolated along the gauge polyline and painted into a corridor around it.
Refresh the BSH grid edition and the CSV together.

Validated end-to-end on real tiles: outer Elbe km710–728 (BSH grid) and inner Elbe km620–639
(Hamburg, assembled fill), both yielding water-only depths below chart datum with land clamped off.

**Clamp caveat / follow-up.** `clamp_positive` drops everything above chart datum, which removes
the surrounding land *and* intertidal drying flats a chart would show. Follow-up: instead of a
blunt `>0` clamp, reconcile against the OSM land–water mask (`landmask.py`) so genuine drying
areas inside mapped water survive while dike/land terrain is dropped.

**Inland low-water sourcing.** PEGELONLINE (`pegelonline.wsv.de/webservices/rest-api/v2`)
exposes each gauge's `gaugeZero` (PNP in NHN) and characteristic values (**GlW**, **ZS_I**), so a
per-gauge low-water datum in NHN is derivable for most free-flowing and impounded reaches; where
gauges are too sparse for the pool structure, checked-in barrage-level tables fill the gaps. The
Elbe-upper / Oder / Weser / Ems reaches publish no GlW/Stauziel (MNW/MW only) and need an MNW
proxy or a state-sourced datum. Tracked separately.

## Pipeline

Prepared path modeled on `noaa_estuarine` (the other `mixed_crs` source): per-tile CRS preserved
(no `--crs`), with the datum step driven by `offset_surface`/`clamp_positive` in `metadata.json`
so the synced COG is already referenced to chart datum.
