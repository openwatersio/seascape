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

Two prep scripts refresh the checked-in reference inputs (neither runs in the build — the build reads
the committed files):
- `harvest_gauges.py` — per-gauge low-water datums (GlW / ZS_I / MNW) in NHN from PEGELONLINE →
  `<reach>_{glw,zs,mnw}.csv`.
- `build_geometry.py` — river corridors + centerlines from **Overture Maps** (`base/water`, the same
  anonymous S3 parquet `landmask.py` reads) → `<river>_river.wkt` / `<river>_centerline.wkt`. Overture
  over Overpass: name-filterable, bbox pushdown, no timeouts. Its `base/water` theme has no weirs or
  CEMT navigation locks, so the Main's weir lines and the upper Rhein's lock coords stay as the
  one-off OSM snapshots checked into their `*_stau.csv` (the build never re-extracts those).

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
  so a per-reach low-water surface is subtracted instead (see below). Active so far: the
  **free-flowing** reaches on a low-water ramp — the **Rhein** below Iffezheim (km 336–865, 3 tiles,
  **GlW**) and the **upper Elbe** (km 0–586, 25 tiles, **MNW** proxy) — and the impounded canalised
  rivers on **Stauziel** — the **Main** (km 0–102, 6 tiles), the **upper Rhein** (Basel→Iffezheim,
  km 164–334, 16 tiles), the **Mosel** (km 1.9–229.9, 1 tile), the **Saar** (km 0–94, 5 tiles), and
  the **Lahn** (km 80–136, 3 tiles). The remaining inland tiles (Oder, Weser, Ems, the canals) stay
  **commented out** in `file_list.txt` — they publish no GlW/Stauziel, so they need an MNW proxy or a
  state-sourced low-water datum first.

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
Wasserstand, or **MNW** where GlW is un-published) for the free-flowing Rhein and upper Elbe, and
**Stauziel** (per-pool retention level) for the impounded canalised rivers (Main, upper Rhein, Mosel,
Saar, Lahn). This is the scalar lake `--offset` (Bodensee) generalized to a spatially-varying surface.

Per pixel: `bed_depth = bed_NHN − datum_NHN(x, y)`. Result is bed elevation referenced to the local
low water (negative = depth below datum, same convention as the MLLW/LAT sources); above-datum cells
(land, drying flats) are clamped to nodata.

Pipeline steps (after `source_unzip`, before `source_normalize`):

1. `build_reference.py` (bespoke, lives in this source dir) builds the low-water surface into
   `store/source/dgm_w/reference/` (a subdir, so the pipeline's `*.tif` globs never treat it as a
   data tile). Eight reaches, each its own EPSG:4326 GeoTIFF, stitched into `reference.vrt` (a VRT so
   each keeps its own extent/resolution and warp reads whichever overlaps a tile):
   - **Tidal — `skn_reference.tif`.** Outer estuaries + open Bight from the BSH **SKN-Fläche
     Nordsee 2026** ("Chart datum for the German Bight") grid of SKN in NHN, fetched at build time
     (**CC-BY 4.0**; Atom `https://gdi.bsh.de/de/feed/Chart-datum-for-the-German-Bight-2026.xml`,
     also WCS / ZIP; east edge ~9.5° E covering Nordsee, Jade, Außenweser, outer Elbe). East of the
     grid to the Geesthacht weir, the **inner tidal Elbe** is assembled from the per-gauge SKN
     values in `tideelbe_skn.csv` (transcribed from the GDWS "Seekartennull an den Tidepegeln … ab
     2026" table; SKN ~−1.9 m NHN tapering to ~−1.2 m near Zollenspieker, clamped at the most-
     upstream gauge above the weir). Refresh the BSH grid edition and the CSV together.
   - **Free-flowing Rhein / upper Elbe — `ramp_rhein.tif` / `ramp_elbe.tif`** (`build_freeflowing`).
     Un-impounded rivers have no pools, so low water is a smooth downstream **ramp**, not a step. The
     Rhein below Iffezheim uses **GlW** (`rhein_glw.csv`); the un-canalised Elbe (Czech border →
     Geesthacht km 586) has no openly-published per-gauge GlW, so it uses **MNW** (`elbe_mnw.csv`) — a
     few cm–dm below GlW, the conservative direction, and reproducible from PEGELONLINE. Both harvested
     by `harvest_gauges.py`; the datum is interpolated along the gauge line by arc-length (low water
     drifts ~0.02 m/km, so the coarse gauge chord is fine for the *value*). The **corridor is the
     river union** (`rhein_river.wkt` / `elbe_river.wkt`, from Overture — see `build_geometry.py`),
     NOT the gauge chord: gauges ~30 km apart
     make a chord that misses the wide outer channel at big meanders (the Rhein's Düsseldorf bends),
     so the river there fell outside the mask — the fill needed the real river, only the value didn't.
   - **Impounded Main — `zs_main.tif`.** The canalised Main (km 0–102 here) is a staircase of
     pools, each held at a fixed **Stauziel** by its barrage, so the datum is a **step function**,
     not a ramp: flat within a pool, jumping at each weir. Three data files, each addressing a
     different gap:
     - **Levels — `main_stau.csv`** (transcribed from Wikipedia "Liste der Mainstaustufen"): the
       DEM reach has more pools than PEGELONLINE gauges (the Eddersheim/Offenbach pools have none),
       so one Stauziel per barrage gives a complete set. `main_zs.csv` (harvested ZS_I gauges) is an
       independent cross-check `build_reference` asserts — each gauge's ZS_I equals its pool's
       Stauziel to within a few cm.
     - **Dividers — the OSM `man_made=weir` line per barrage** (endpoints in `main_stau.csv`,
       fetched from the OSM /map API): a pixel takes the Stauziel of the barrage whose weir line is
       immediately downstream of it (how many weir lines it lies upstream of). So each pool step
       lands *exactly on its dam*, not on an interpolated point.
     - **Corridor — `main_centerline.wkt`** (the real OSM Main centerline): the reference is filled
       only within ~1.5 km of it, so the band hugs every meander instead of a coarse gauge chord the
       river escapes at bends. Stauziel 83.9 m (Kostheim pool) → 116.5 m (Wallstadt).
   - **Impounded upper Rhein — `zs_rhein.tif`.** The Rhein above Iffezheim (km 164–334) is another
     staircase — ten pools from Kembs to Iffezheim (Basel→Iffezheim, mostly French EDF barrages).
     Same step-function idea as the Main, but the divider is simpler: this reach flows due **north**,
     so pool boundaries fall cleanly on lines of **latitude** and no weir geometry is needed — a pixel
     takes the retention level of the first barrage at or north of it (`np.searchsorted` on the
     barrage latitudes). Two files:
     - **Levels — `rhein_stau.csv`**: per-barrage normal retention level (Stauziel / cote de retenue),
       244.26 m (Kembs pool) → 123.68 m (Iffezheim). PEGELONLINE publishes no ZS_I here (the barrages
       are French; German gauges report MNW/MW only), so unlike the Main there is no gauge cross-check.
       Values come from the French Wikipedia "Schéma détaillé du Grand canal d'Alsace" level profile,
       anchored on Iffezheim's measured NHN Stauziel; the eight French values are NGF (Lallemand), so
       ±0.3 m in NHN — fine for a non-navigational render. Barrage lat/lon are the OSM CEMT-VIb locks.
     - **Corridor — `rhein_centerline.wkt`** (OSM navigation line: Grand Canal d'Alsace + canalised
       Rhine, `boat=yes`): filled within ~2 km, which keeps the band on the impounded navigation
       channel and off the low Restrhein running alongside it in the Grand Canal reach. (Confirmed on
       a km173–190 tile: the DGM-W only models the navigation corridor — one channel, ~14 % valid —
       so there is no second channel at a different level to mis-reference.)
   - **Impounded Mosel / Saar / Lahn — `zs_mosel.tif` / `zs_saar.tif` / `zs_lahn.tif`** (`build_impounded`,
     the general canalised-river builder). Each is a Stauziel staircase like the Main, but these
     rivers meander in every direction, so the pool divider is **arc-length along a proper
     centerline**, not latitude or a coarse chord: a pixel projected onto `<river>_centerline.wkt`
     (a single LineString built by shortest-path through the river graph — big meanders like the
     Cochem loop followed faithfully) lands in a pool, and takes the retention level of the barrage
     bounding it downstream. Flow direction is read off the falling levels, so river-km may run
     either way (the Lahn's runs downstream, the Mosel/Saar's upstream). The **corridor** is the
     river union (`<river>_river.wkt`); both geometries come from Overture (`build_geometry.py`).
     Levels + km + coords in
     `<river>_stau.csv` (Mosel: Wikipedia "Liste der Moselstaustufen"; Saar: Wikipedia + PEGELONLINE
     headwater gauges; Lahn: the WSV/WSA-Koblenz "Steckbriefe" fact sheets), each **cross-checked**
     against the reach's ZS_I gauges (`<river>_zs.csv`) — the assert caught a Cochem-loop
     misprojection that forced the centerline approach. Levels: Mosel 65→140 m, Saar 130→187 m,
     Lahn 66→106 m.
2. `source_datum --offset-surface reference/reference.vrt --clamp-positive` reprojects the
   reference onto each tile (bilinear, cross-CRS), subtracts it, and drops above-datum cells.
3. `source_normalize` (no `--crs`, keep per-tile CRS) → COG.

Validated end-to-end on real tiles: outer Elbe km710–728 (BSH grid) and inner Elbe km620–639
(Hamburg, assembled fill) for SKN, a Rhein bed at Köln for GlW, and a Main bed in the Griesheim pool
for the Stauziel step — all through the VRT, yielding water-only depths below their datum with land
clamped off (the Main's flat pools reading a uniform depth below Stauziel).

**Clamp caveat / follow-up.** `--clamp-positive` drops everything above chart datum, which removes
the surrounding land *and* intertidal drying flats a chart would show. Follow-up: instead of a
blunt `>0` clamp, reconcile against the OSM land–water mask (`landmask.py`) so genuine drying areas
inside mapped water survive while dike/land terrain is dropped.

**Remaining inland reaches (deferred).** Active so far: free-flowing Rhein (**GlW**) and the lower
Main (**Stauziel** step). Sourcing note: PEGELONLINE (`pegelonline.wsv.de/webservices/rest-api/v2`)
exposes each gauge's `gaugeZero` (PNP in NHN) and its characteristic values — both **GlW** *and*
**ZS_I** (Stauziel) — so `harvest_gauges.py` reads them straight from the API; where the gauges are
too sparse for the pool structure (as on the Main) a checked-in barrage-level table fills the gaps.
Still commented out in `file_list.txt`: the **Rhein upper** (km 164–337, impounded — needs the
Basel–Iffezheim pool Stauziele) and **Mosel / Saar / Lahn** (impounded, only 2–3 gauges each, so
they'll lean on barrage tables like the Main); the **Elbe-upper / Oder / Weser / Ems** reaches
publish no GlW/Stauziel at all (MNW/MW only) and need another source or an MNW proxy. Each reuses
the same `build_reference.py` machinery (spine + per-km value → subtract) — tracked separately.

## Pipeline

Run from `pipelines/`: `just ../sources/dgm_w/`. Prepared path modeled on `noaa_estuarine` (the
other `mixed_crs` source), with the datum step (above) inserted after unzip so the synced COG is
already referenced to chart datum.
