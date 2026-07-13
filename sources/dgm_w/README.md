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
  so a per-reach low-water surface is subtracted instead (see below). The **free-flowing Rhein**
  below the Iffezheim barrage (km 336–865, 3 tiles) is now referenced to **GlW** and active; the
  remaining inland tiles stay **commented out** in `file_list.txt` pending their datum work.

Not for navigation: NHN/SKN depths are approximate and unreduced to chart standards. This source
is visualization-only, consistent with Seascape's non-navigational disclaimer.

## Vintage

The Atom feed is the baseline national set; some tiles are older (the Saar tile is dated 2014,
the hillshade layer caps at 2015). Newer high-resolution project DEMs ship separately with DOIs —
e.g. **DGM-W Elbe 2022** on Zenodo (`10.5281/zenodo.17378778`) and via GovData/BfG — and could
supersede the estuary tiles here if desired.

## Datum normalization

`just source dgm_w` emits a **low-water-referenced** COG: raw DGM-W is orthometric NHN, and the
prep subtracts a **low-water reference surface** — the local low-water datum expressed in NHN — so
the synced COG is depth below that datum, needing no special handling downstream. The datum differs
by reach: **SKN** (Seekartennull ≈ LAT) for the tidal estuaries, **GlW** (gleichwertiger
Wasserstand) for the free-flowing Rhein. This is the scalar lake `--offset` (Bodensee) generalized
to a spatially-varying surface.

Per pixel: `bed_depth = bed_NHN − datum_NHN(x, y)`. Result is bed elevation referenced to the local
low water (negative = depth below datum, same convention as the MLLW/LAT sources); above-datum cells
(land, drying flats) are clamped to nodata.

Pipeline steps (after `source_unzip`, before `source_normalize`):

1. `build_reference.py` (bespoke, lives in this source dir) builds the low-water surface into
   `store/source/dgm_w/reference/` (a subdir, so the pipeline's `*.tif` globs never treat it as a
   data tile). Two reaches, each its own EPSG:4326 GeoTIFF, stitched into `reference.vrt` (a VRT so
   each keeps its own extent/resolution and warp reads whichever overlaps a tile):
   - **Tidal — `skn_reference.tif`.** Outer estuaries + open Bight from the BSH **SKN-Fläche
     Nordsee 2026** ("Chart datum for the German Bight") grid of SKN in NHN, fetched at build time
     (**CC-BY 4.0**; Atom `https://gdi.bsh.de/de/feed/Chart-datum-for-the-German-Bight-2026.xml`,
     also WCS / ZIP; east edge ~9.5° E covering Nordsee, Jade, Außenweser, outer Elbe). East of the
     grid to the Geesthacht weir, the **inner tidal Elbe** is assembled from the per-gauge SKN
     values in `tideelbe_skn.csv` (transcribed from the GDWS "Seekartennull an den Tidepegeln … ab
     2026" table; SKN ~−1.9 m NHN tapering to ~−1.2 m near Zollenspieker, clamped at the most-
     upstream gauge above the weir). Refresh the BSH grid edition and the CSV together.
   - **Free-flowing Rhein — `glw_rhein.tif`.** Below the Iffezheim barrage (km 336) the Rhein runs
     free, so its datum is GlW. No packaged grid: the low-water longitudinal profile is assembled
     from the per-gauge GlW-in-NHN values in `rhein_glw.csv` (harvested from PEGELONLINE by
     `harvest_rhein_glw.py` — see below), interpolated along the gauge polyline the same way as the
     inner Elbe. 17 gauges, Maxau (km 362) → Emmerich (km 852); GlW-in-NHN ~101 m tapering to ~9 m.
2. `source_datum --offset-surface reference/reference.vrt --clamp-positive` reprojects the
   reference onto each tile (bilinear, cross-CRS), subtracts it, and drops above-datum cells.
3. `source_normalize` (no `--crs`, keep per-tile CRS) → COG.

Validated end-to-end on real tiles: outer Elbe km710–728 (BSH grid) and inner Elbe km620–639
(Hamburg, assembled fill) for SKN, plus a Rhein bed at Köln through the VRT for GlW — all yielding
water-only depths below their datum with land clamped off.

**Clamp caveat / follow-up.** `--clamp-positive` drops everything above chart datum, which removes
the surrounding land *and* intertidal drying flats a chart would show. Follow-up: instead of a
blunt `>0` clamp, reconcile against the OSM land–water mask (`landmask.py`) so genuine drying areas
inside mapped water survive while dike/land terrain is dropped.

**Remaining inland reaches (deferred).** The free-flowing Rhein above uses **GlW**; the reaches
still commented out in `file_list.txt` need **Stauziel** (impoundment target level) per barrage
pool — a step function, constant within a pool and jumping at each weir, not a smooth ramp. Good
news for sourcing: PEGELONLINE (`pegelonline.wsv.de/webservices/rest-api/v2`) exposes each gauge's
`gaugeZero` (PNP in NHN) and its characteristic values including GlW, so the Rhein needed no PDF
tables — `harvest_rhein_glw.py` reads it straight from the API. Stauziel is not a PEGELONLINE
characteristic value, so the impounded reaches (Rhein km 164–337, Main, Mosel, Saar, Lahn) still
need the per-pool target levels + barrage locations from WSV/ELWIS; the Elbe-upper/Oder/Weser/Ems
reaches publish no GlW/Stauziel at all (MNW/MW only) and need another source or an MNW proxy.
Each follow-up reuses the same `build_reference.py` corridor machinery — assemble per-gauge
low-water NHN values, interpolate along the gauge line, subtract — tracked separately from this PR.

## Pipeline

Run from `pipelines/`: `just ../sources/dgm_w/`. Prepared path modeled on `noaa_estuarine` (the
other `mixed_crs` source), with the datum step (above) inserted after unzip so the synced COG is
already referenced to chart datum.
