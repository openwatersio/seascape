# NOAA VDatum grid bundle — inventory and empirical sign verification

De-risk for Phase 1 of `docs/plans/2026-08-03-datum-vdatum.md`. Everything below is measured against the extracted bundle (`vdatum_all_20250917.zip`, 3.09 GB compressed / 21.0 GB uncompressed, SHA of the dated URL is the version pin), the NOAA CO-OPS metadata API, and NOAA's own VDatum web API as arbiter.

## 1. Bundle shape

Single top-level `vdatum/` directory. 52 tidal regions + support directories.

| path | role |
|---|---|
| `vdatum/<REGION>/<REGION>_{tss,mllw,mlw,mtl,dtl,mhw,mhhw}.gtx` | the tidal surfaces (all 52 regions) |
| `vdatum/<REGION>/<REGION>_*_unc.gtx`, `<REGION>_svu_*.gtx`, `<REGION>_*_svu.gtx`, `<REGION>_unc.gtx` | per-surface uncertainty twins (naming is inconsistent between region vintages) |
| `vdatum/<REGION>/<REGION>_lwd.gtx` | river Low Water Datum (7 regions) |
| `vdatum/<REGION>/<REGION>_marine.gtx` | Gulf "marine" variant (6 TX/LA regions) |
| `vdatum/<REGION>/<REGION>_itide.gtx` | PRVI only |
| `vdatum/<REGION>/<REGION>.met` | **authoritative per-region metadata** — bbox in 0–360 lon, `horz` frame, tidal epoch |
| `vdatum/<REGION>/<REGION>.bnd`, `bounding_polygon_N.dat`, `.kml` | the region's true (non-rectangular) validity polygon |
| `vdatum/core/geoid18/`, `geoid12b/`, `geoid12a/`, `geoid09/…`, `xgeoid16b/…/xgeoid20b/` | geoid models, needed for the West Coast / Alaska chain (see §3) |
| `vdatum/core/{ncla,nclo,hpgnla,hpgnlo,vcn}/`, `vdatum/NADCON5/` | horizontal datum shifts — not needed for a vertical separation |
| `vdatum/CRD/crd.gtx` | Columbia River Datum, 0.0002° over lon −124.31..−121.92, lat 45.34..46.37 |
| `vdatum/IGLD85/hydroc*.gtx` | IGLD85 dynamic-height correctors for the Great Lakes (0.05°, tiny) |
| `vdatum/lib/` | bundled JRE — skip on harvest (~300 MB) |
| `vdatum/tidal_area.inf` | machine-readable roll-up of every region's `.met` (incl. the `grids=` list) |

Harvester glob: `vdatum/*/*_tss.gtx` and `vdatum/*/*_mllw.gtx`, region id = the parent directory name. **Gotcha:** `TXintra00_8301/txintra00_8301_svu_mtl.gtx` is lowercase-prefixed and does not match its own directory's prefix — a `<REGION>_*` glob misses it. It is an uncertainty file, so not load-bearing here, but the same defect could appear elsewhere in a future release.

### GTX file properties (identical across all regions)

- Driver `GTX`, Float32, **nodata `-88.8888`**, single band.
- CRS advertised as EPSG:4326 by GDAL. This is the driver hardcoding WGS 84; the true horizontal frame is the `.met` `horz` field (NAD83 / IGS08 / IGS14). At this scale the horizontal difference (~1 m) is irrelevant for sampling a metre-scale separation field.
- **Longitude axis is 0–360.** Origin for the Florida region is `278.1375`. Any probe or warp must add 360 to western longitudes, or the file must be shifted at harvest time.
- Registration: the `.met` `minlon` (e.g. `278.138`) is the first *node* centre and GDAL reports `Origin = 278.1375` — the GTX driver already converts NOAA's node registration to GDAL's pixel-is-area corner convention. **No half-pixel correction is needed**; node values land on pixel centres, so bilinear warping is correct as-is.
- Resolution ranges 0.0005° (≈55 m) in the dense inner regions to 0.006° (≈600 m) in `TXLAgulf00` and 0.005° in `WCoffsh00`/`PRVIof00`.

### Per-region index

| region | frame (`horz`) | lon | lat | size px | res (deg) | extras |
|---|---|---|---|---|---|---|
| `AKglacier00_8301` | IGS08 | -141.000 .. -131.902 | 56.560 .. 58.340 | 4551x1781 | 0.002 | — |
| `AKwhale00_8301` | IGS08 | -138.100 .. -129.902 | 53.611 .. 56.570 | 4101x2961 | 0.002 | — |
| `AKyakutat00_8301` | IGS08 | -142.500 .. -133.702 | 58.331 .. 60.110 | 4401x1781 | 0.002 | — |
| `ALFLgom02_8301` | NAD83 | -88.057 .. -85.323 | 29.648 .. 30.409 | 2735x762 | 0.001 | — |
| `ALmobile02_8301` | NAD83 | -88.166 .. -87.677 | 30.218 .. 30.742 | 490x527 | 0.001 | — |
| `CAmontby13_8301` | IGS14 | -124.757 .. -120.398 | 34.902 .. 37.401 | 4360x2500 | 0.001 | `_unc` |
| `CAoregon00_8301` | IGS14 | -126.453 .. -123.945 | 39.999 .. 42.705 | 2509x2707 | 0.001 | `_unc` |
| `CAsfbay13_8301` | IGS14 | -126.139 .. -122.390 | 37.399 .. 40.002 | 3750x2604 | 0.001 | `_unc` |
| `CAsfdel00_8301` | IGS14 | -123.066 .. -121.304 | 37.341 .. 38.613 | 2938x2121 | 0.0006 | `_unc` |
| `CAsouin00_8301` | IGS14 | -119.268 .. -117.025 | 32.562 .. 34.269 | 3740x2845 | 0.0006 | `_unc` |
| `CAsouthn00_8301` | IGS14 | -122.501 .. -117.101 | 32.290 .. 34.924 | 5401x2635 | 0.001 | `_unc` |
| `DEVAemb23_8301` | IGS14 | -75.802 .. -74.998 | 37.367 .. 38.753 | 671x1387 | 0.0012 | `_unc` |
| `DEdelbay33_8301` | IGS14 | -75.632 .. -74.696 | 38.748 .. 40.254 | 469x754 | 0.002 | `_unc` |
| `FLGAeastbays31_8301` | NAD83 | -81.862 .. -80.022 | 26.169 .. 31.450 | 1841x5282 | 0.001 | `lwd` |
| `FLGAeastshelf41_8301` | NAD83 | -81.468 .. -78.979 | 26.169 .. 31.446 | 2490x5278 | 0.001 | — |
| `FLandrew02_8301` | NAD83 | -85.870 .. -85.381 | 29.979 .. 30.325 | 490x347 | 0.001 | — |
| `FLapalach01_8301` | NAD83 | -86.515 .. -82.490 | 28.132 .. 30.202 | 2684x1381 | 0.0015 | — |
| `FLjoseph03_8301` | NAD83 | -85.419 .. -85.300 | 29.672 .. 29.916 | 124x245 | 0.001 | — |
| `FLpensac02_8301` | NAD83 | -87.701 .. -85.851 | 30.191 .. 30.665 | 1851x475 | 0.001 | — |
| `FLsoicw01_8301` | NAD83 | -80.149 .. -80.099 | 25.910 .. 26.173 | 101x528 | 0.0005 | — |
| `FLsouth12_8301` | NAD83 | -83.491 .. -79.581 | 23.814 .. 26.175 | 2608x1575 | 0.0015 | `lwd` |
| `FLwest01_8301` | NAD83 | -83.513 .. -81.607 | 26.152 .. 28.162 | 2542x2681 | 0.00075 | — |
| `GASCNCsab31_8301` | NAD83 | -81.327 .. -77.258 | 31.442 .. 33.949 | 4070x2508 | 0.001 | — |
| `LATXintra00_8301` | NAD83 | -94.440 .. -92.360 | 29.538 .. 30.273 | 4161x1839 | 0.0005 | `lwd`, `marine`, `svu_*` |
| `LATXoffshr00_8301` | NAD83 | -95.582 .. -92.322 | 28.396 .. 29.794 | 1631x700 | 0.002 | `marine`, `svu_*` |
| `LAatchaf00_8301` | NAD83 | -92.365 .. -91.148 | 29.130 .. 29.954 | 2435x2061 | 0.0005 | `lwd`, `marine`, `svu_*` |
| `LAmobile02_8301` | NAD83 | -93.000 .. -88.000 | 28.000 .. 30.500 | 5001x2501 | 0.001 | — |
| `MDVAechb11_8301` | IGS14 | -76.442 .. -75.597 | 36.726 .. 39.616 | 705x2891 | 0.0012 | `_unc` |
| `MDnwchb11_8301` | IGS14 | -77.381 .. -76.129 | 37.898 .. 39.482 | 1253x1981 | 0.001 | `_unc` |
| `MENHMAgome23_8301` | NAD83 | -71.200 .. -65.580 | 39.890 .. 45.568 | 3307x3341 | 0.0017 | — |
| `NCcoast11_8301` | NAD83 | -78.350 .. -74.150 | 32.650 .. 37.050 | 8401x8801 | 0.0005 | — |
| `NCinner11_8301` | NAD83 | -78.310 .. -75.459 | 33.830 .. 36.780 | 5702x5901 | 0.0005 | `lwd` |
| `NJVAmab33_8301` | IGS14 | -75.720 .. -73.590 | 36.744 .. 39.364 | 853x1311 | 0.0025 | `_unc` |
| `NJncstemb12_8301` | NAD83 | -74.572 .. -73.972 | 39.280 .. 40.950 | 601x1540 | 0.001 | `_unc` |
| `NJscstemb32_8301` | IGS14 | -74.922 .. -74.358 | 38.898 .. 39.467 | 806x1139 | 0.0007 | `_unc` |
| `NYNJhbr34_8301` | NAD83 | -74.572 .. -73.502 | 39.280 .. 42.766 | 928x3487 | 0.001 | `_unc` |
| `NYgr8bay34_8301` | NAD83 | -74.572 .. -72.406 | 39.280 .. 40.901 | 1356x381 | 0.001 | `_unc` |
| `ORcoain00_8301` | IGS14 | -124.429 .. -123.934 | 40.568 .. 43.502 | 826x4891 | 0.0006 | `_unc` |
| `ORcoast00_8301` | IGS14 | -126.453 .. -123.796 | 42.699 .. 45.368 | 2658x2670 | 0.001 | `_unc` |
| `PRVIis00_8301` | NAD83 | -68.309 .. -63.888 | 17.230 .. 18.951 | 8843x3443 | 0.0005 | `itide`, `_svu` |
| `PRVIof00_8301` | NAD83 | -69.640 .. -60.825 | 14.422 .. 22.277 | 1764x1572 | 0.005 | `itide`, `_svu` |
| `RICTbis44_8301` | NAD83 | -74.572 .. -71.102 | 39.280 .. 41.931 | 2402x2651 | 0.001 | `_unc`, `unc` |
| `TXLAgulf00_8301` | NAD83 | -96.980 .. -91.118 | 25.700 .. 29.500 | 978x761 | 0.006 | `svu_*` |
| `TXcentr00_8301` | NAD83 | -96.867 .. -94.440 | 28.180 .. 29.860 | 4855x4201 | 0.0005 | `lwd`, `marine`, `svu_*` |
| `TXintra00_8301` | NAD83 | -97.807 .. -96.775 | 25.940 .. 28.277 | 1033x2338 | 0.001 | `lwd`, `marine`, `svu_*` |
| `TXoffshr00_8301` | NAD83 | -97.394 .. -95.352 | 25.940 .. 28.806 | 1022x1434 | 0.002 | `lwd`, `marine`, `svu_*` |
| `VAswchb11_8301` | IGS14 | -77.442 .. -76.127 | 36.728 .. 38.233 | 823x1255 | 0.0016 | `_unc` |
| `WAcoast00_8301` | IGS14 | -126.378 .. -121.538 | 45.232 .. 48.176 | 4841x2945 | 0.001 | `_unc` |
| `WAjdfin00_8301` | IGS14 | -122.793 .. -122.142 | 48.019 .. 48.997 | 1086x1631 | 0.0006 | `_unc` |
| `WAjdfuca14_8301` | IGS14 | -126.344 .. -122.177 | 47.946 .. 49.147 | 4168x1202 | 0.001 | `_unc` |
| `WApugets13_8301` | IGS14 | -123.184 .. -122.177 | 47.016 .. 48.020 | 1008x1005 | 0.001 | `_unc` |
| `WCoffsh00_8301` | IGS14 | -132.385 .. -119.730 | 30.755 .. 50.915 | 2532x4033 | 0.005 | `_unc` |

Region bboxes overlap heavily (e.g. `FLGAeastbays31` × `FLGAeastshelf41`, `WAcoast00` × `WCoffsh00`). Mosaic priority used here and validated by the station check: **smallest-bbox region first, fall through on nodata**. VDatum itself selects with the `.bnd` polygon; that is more correct at region edges and should be used if seam artefacts appear.

## 2. Surface semantics (established empirically, not from folklore)

Every tidal `.gtx` in a region is **the height of that datum's surface relative to Local Mean Sea Level, in metres, positive up**. Verified at Mayport 8720211 against CO-OPS published values (station-datum feet → metres):

| grid | value at Mayport | CO-OPS (datum − MSL) | match |
|---|---|---|---|
| `mllw` | −0.7734 | −0.774 | ✓ |
| `mlw` | −0.7258 | −0.726 | ✓ |
| `mtl` | −0.0160 | −0.015 | ✓ |
| `dtl` | +0.0050 | +0.006 | ✓ |
| `mhw` | +0.6939 | +0.694 | ✓ |
| `mhhw` | +0.7834 | +0.783 | ✓ |
| `tss` | +0.1730 | NAVD88 − MSL = +0.174 | ✓ (NAD83-frame regions only) |

So `mllw` is always negative (MLLW below LMSL), and `tss` is the height of the **model reference surface** above LMSL — but *which* reference surface depends on the region's `horz` frame. That is the trap.

## 3. The composition formula

Let `S` = the **NAVD88 − MLLW separation**, i.e. the height of the NAVD88 zero surface above the MLLW surface. Applying it to a NAVD88 bed elevation gives the chart-datum elevation:

```
z_MLLW = z_NAVD88 + S
```

`S > 0` almost everywhere → the bed rises → depths get shallower → bias-shallow-safe.

The plan's reference-in-source-frame convention ("MLLW height expressed in NAVD88", Mayport ≈ −0.947) is simply `−S`.

### 3a. `horz = NAD83` regions — Atlantic, Gulf, PRVI, and some NY/NJ/CT

```
S = tss − mllw
```

`tss` here is referenced to the **hybrid geoid (GEOID18/GEOID12B), which by construction *is* the NAVD88 surface**, so no geoid term is needed. Verified to ≤8 mm at 7 stations (Mayport −2 mm, Charleston SC +1 mm, Boston +2 mm, Beaufort NC +2 mm, New London +7 mm, Port Isabel −2 mm, Panama City −8 mm).

### 3b. `horz = IGS14` / `IGS08` regions — entire West Coast, Puget Sound, SE Alaska, most of Chesapeake/Delaware

`tss` in these regions is referenced to the **gravimetric xGEOID20B surface in the IGS14/ITRF frame**, not to NAVD88. `tss − mllw` alone is wrong by **+0.55 to +1.20 m** (always too positive → would over-shallow by up to a metre). The full chain is:

```
S = (N_hybrid + δ − N_xgeoid20b) + tss − mllw

  N_hybrid      geoid height from vdatum/core/geoid18/  (CONUS, PRVI)
                                  vdatum/core/geoid12b/ (Alaska — GEOID18 has no AK coverage)
  δ             NAD83(2011) → ITRF2014 ellipsoid-height change at h = 0,
                i.e. PROJ EPSG:6319 → EPSG:7912 (≈ −0.26 m Neah Bay … −1.49 m Mayport)
  N_xgeoid20b   vdatum/core/xgeoid20b/conuspac.gtx
```

Derivation, all heights positive up: `h_NAD83 = z_NAVD88 + N_hybrid`; `h_ITRF = h_NAD83 + δ`; height above the xGEOID surface `H_x = h_ITRF − N_xgeoid20b`; `z_LMSL = H_x + tss`; `z_MLLW = z_LMSL − mllw`.

NOAA's own web API corroborates the frame crossing: `region=westcoast` with `t_h_frame=NAD83_2011` is rejected with *"For West Coast Region, Target Horizontal Frame should be IGS14 for Tidal"* (same for `chesapeak_delaware`).

Accuracy across 22 IGS14/IGS08 stations: median |error| ≈ 1.4 cm, 19/22 within 4 cm. Worst cases are La Push WA −12 cm (river-mouth gauge inside the coarse `WAcoast00` grid) and North Spit CA +6 cm. VDatum's own stated uncertainty for this transform is 9 cm, so the residuals are inside the product's noise floor. xGEOID20B beat 19B/18B/16B overall; the spread between xGEOID versions is 1–3 cm.

**The rule must be read from each region's `.met` `horz` field, never guessed from the region name.** The two conventions are interleaved: `RICTbis44`, `NYNJhbr34`, `NYgr8bay34`, `NJncstemb12` are NAD83 while their neighbours `NJscstemb32`, `NJVAmab33`, `DEdelbay33`, `DEVAemb23`, `MDnwchb11`, `MDVAechb11`, `VAswchb11` are IGS14.

### 3c. PRVI regions — PRVD02 / VIVD09

```
S = −mllw
```

`tss` is **not** part of the chain. PRVD02 and VIVD09 are LMSL realizations, so the local-datum-to-LMSL step is ≈ 0 across the whole archipelago. Verified: San Juan +0.229 vs published +0.232; Magueyes Island +0.098 vs +0.085; Charlotte Amalie +0.120 vs +0.116. NOAA's API agrees (`PRVD02 → MLLW` returns 0.230 at San Juan, 0.090 at Magueyes) and **rejects NAVD88 outright for `region=prvi`** ("Unsupported vertical datum [NAVD88]"). Using `tss − mllw` here would be wrong by −0.38 to −0.45 m in the *deeper* direction — the unsafe one.

## 4. Benchmark stations

Published values are `(ortho_datum − MLLW)` from the CO-OPS metadata API, epoch 1983–2001, converted from station-datum feet at 0.3048 m/ft.

API: `https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations/{id}/datums.json`
Coordinates: `https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations/{id}.json`

| station | id | lon | lat | region | frame | source datum | published S (m) | grid S (m) | diff |
|---|---|---|---|---|---|---|---|---|---|
| Mayport Naval Sta., St Johns River FL | 8720211 | −81.4133 | 30.4000 | `FLGAeastbays31_8301` | NAD83 | NAVD88 | +0.948 | +0.946 | −0.002 |
| Panama City FL | 8729108 | −85.6644 | 30.1497 | `FLandrew02_8301` | NAD83 | NAVD88 | +0.171 | +0.163 | −0.008 |
| Port Isabel TX | 8779770 | −97.2155 | 26.0612 | `TXintra00_8301` | NAD83 | NAVD88 | +0.259 | +0.257 | −0.002 |
| Boston MA | 8443970 | −71.0503 | 42.3539 | `MENHMAgome23_8301` | NAD83 | NAVD88 | +1.676 | +1.679 | +0.002 |
| Charleston SC | 8665530 | −79.9236 | 32.7808 | `GASCNCsab31_8301` | NAD83 | NAVD88 | +0.957 | +0.958 | +0.001 |
| San Diego CA | 9410170 | −117.1767 | 32.7156 | `CAsouin00_8301` | IGS14 | NAVD88 | +0.131 | +0.154 | +0.023 |
| San Francisco CA | 9414290 | −122.4659 | 37.8063 | `CAsfdel00_8301` | IGS14 | NAVD88 | **−0.018** | +0.003 | +0.021 |
| Astoria OR | 9439040 | −123.7683 | 46.2073 | `WAcoast00_8301` | IGS14 | NAVD88 | **−0.064** | −0.096 | −0.032 |
| Skamokawa WA | 9440569 | −123.4565 | 46.2703 | `WAcoast00_8301` | IGS14 | NAVD88 | **−0.530** | −0.535 | −0.004 |
| Seattle WA | 9447130 | −122.3393 | 47.6026 | `WApugets13_8301` | IGS14 | NAVD88 | +0.713 | +0.722 | +0.009 |
| Yakutat AK | 9453220 | −139.7334 | 59.5485 | `AKyakutat00_8301` | IGS08 | NAVD88 | +0.180 | +0.214 | +0.034 |
| Port Alexander AK | 9451054 | −134.6470 | 56.2467 | `AKwhale00_8301` | IGS08 | NAVD88 | +0.360 | +0.373 | +0.013 |
| San Juan PR | 9755371 | −66.1164 | 18.4589 | `PRVIis00_8301` | NAD83 | PRVD02 | +0.232 | +0.229 | −0.003 |
| Charlotte Amalie VI | 9751639 | −64.9258 | 18.3306 | `PRVIis00_8301` | NAD83 | VIVD09 | +0.116 | +0.120 | +0.004 |
| Honolulu HI | 1612340 | −157.8645 | 21.3033 | — | — | local tidal | n/a (MSL−MLLW +0.250) | **no coverage** | — |
| Apra Harbor, Guam | 1630000 | 144.6564 | 13.4434 | — | — | GUVD04 | +0.421 | **no coverage** | — |

Full 44-station sweep in `formula_check.txt` / `station_check.txt`.

### Is the "PNW sign flip" real?

**Partly. The claim as written in the plan doc is misleading and should be corrected.**

`S < 0` (NAVD88 below MLLW) occurs at exactly four stations out of the 41 with published NAVD88, and none of them is the open Pacific Northwest coast:

| station | S |
|---|---|
| Skamokawa WA (Columbia River, 60 km inland) | −0.530 |
| Astoria OR (Columbia River mouth) | −0.064 |
| Monterey CA | −0.043 |
| San Francisco CA | −0.018 |

Everywhere else on the West Coast `S` is positive: Neah Bay +0.256, Westport +0.341, Toke Point +0.250, Port Angeles +0.131, La Push +0.442, Seattle +0.713, Tacoma +0.728, all Oregon coast stations +0.03..+0.24, all southern California +0.04..+0.13. So:

- The flip is **real but confined to the Columbia River estuary** (where it is material, −0.53 m at Skamokawa) plus two central-California stations where it is ±2–4 cm — inside VDatum's own 9 cm uncertainty and not meaningfully distinguishable from zero.
- Puget Sound, the Strait of Juan de Fuca and the outer WA/OR coast — the places one would call "the Pacific Northwest" on a chart — are all firmly positive, up to +0.73 m at Seattle.
- The plan's Alternatives section says a scalar CONUS-east offset is "wrong sign in the Pacific NW". The stronger and correct objection is **magnitude, not sign**: a single scalar would be off by 1.5 m between Boston (+1.68) and San Diego (+0.13), and by 3.3 m if Alaska were in scope.

Practical consequence for bias-shallow: MLLW is simply not the datum NOAA charts the Columbia against. The reach uses CRD (§4a), and correcting it to MLLW charts it deeper than its own datum — by 0.54 m at St Helens, growing upstream. Skamokawa's −0.53 m is the estuary end of that, not its worst case.

## 4a. Columbia River Datum (`CRD/crd.gtx`)

`vdatum/CRD/` sits outside the 52 tidal regions: no `.met`, an `.inf` sidecar instead (`horz=NAD83`, 11735×5023 at 0.0002035314°, released 2022-06-06), plus `CRD.bnd` and `CRD.kml`.

**Frame, established empirically:** a `crd.gtx` value is the **height of the CRD surface above NAVD88** — i.e. it is already `−S` on the reference convention, so it is subtracted verbatim with no composition. It is *not* a height re LMSL like the tidal `.gtx` surfaces: at Skamokawa CRD−LMSL is −1.27 m while the grid reads +0.41.

```
S_crd = NAVD88 − CRD = −crd.gtx        reference = −S_crd = crd.gtx
```

**Ground truth.** CO-OPS publishes no CRD row; it publishes `CRD_OFFSET`, and the datum sits at that value **on the station's own datum**, so `S_crd = (NAVD88 − CRD_OFFSET) ft × 0.3048`. Three Columbia stations carry both rows:

| station | id | lon | lat | NAVD88 (ft) | CRD_OFFSET (ft) | published S (m) | grid S (m) | diff |
|---|---|---|---|---|---|---|---|---|
| Skamokawa WA | 9440569 | −123.4565 | 46.2703 | −1.31 | −0.06 | −0.381 | −0.412 | −0.031 |
| TEMCO Kalama Terminal WA | 9440357 | −122.8367 | 45.9867 | +2.38 | +5.95 | −1.088 | −1.063 | +0.025 |
| St Helens OR | 9439201 | −122.7970 | 45.8650 | −4.26 | +0.02 | −1.305 | −1.287 | +0.018 |

Kalama is the station that pins the reading: its 5.95 ft offset is large enough to separate interpretations that Skamokawa and St Helens (0.06 and 0.02 ft) cannot. Reading `CRD_OFFSET` as a correction *added to* station datum misses Kalama by 3.6 m; reading it as `CRD − MLLW` misses St Helens by 0.57 m. Only "CRD zero is at `CRD_OFFSET` on the station datum" fits all three, to ≤3.2 cm.

A fourth, independent anchor comes free at the mouth: CRD is defined to equal MLLW at the river entrance, and at Astoria 9439040 the grid reads +0.0648 against a published MLLW−NAVD88 of +0.0640 — **0.8 mm**.

NOAA's VDatum web API is *not* an arbiter here: `t_v_frame=CRD` returns error 412 for every region.

**Extent vs. validity.** `crd.gtx`'s non-nodata footprint is much larger than `CRD.bnd`: it carries values west to lon −124.30, ~50 km out over the open Pacific, where MLLW is the charted datum and the CRD extrapolation disagrees by up to 9 cm. `CRD.bnd` (15 vertices, lon −123.727..−121.872, lat 45.302..46.410, covering the river to Bonneville plus the Willamette to Oregon City) is the datum boundary. Conversely the envelope is coarse — the grid fills only 29% of it, the rest being uplands inside the corridor — so inside the envelope the grid's holes must be filled from CRD itself rather than left on MLLW.

**Seam at the boundary.** Sampling 1200 points around the ring, only 34 have both grids valid (the rest of the ring is on land); all lie on the seaward edge near lon −123.726. Median step 2.5 cm, max 11.0 cm, CRD the deeper — at or inside VDatum's own 9 cm uncertainty, because the boundary sits where the two datums have converged. Upstream the divergence grows monotonically: −0.12 m at Skamokawa, −0.67 m at Longview/Kalama, −0.56 m at Vancouver, with MLLW always the deeper of the two.

## 5. Territory chains

| territory | CUDEM/product vertical datum | VDatum tidal grid | chain to MLLW |
|---|---|---|---|
| Puerto Rico | PRVD02 | `PRVIis00_8301` (0.0005°), `PRVIof00_8301` (0.005°) | `S = −mllw`; PRVD02 ≡ LMSL. NAVD88 not defined here |
| US Virgin Islands | VIVD09 | same two regions | `S = −mllw`; VIVD09 ≡ LMSL |
| SE Alaska | NAVD88 | `AKglacier00`, `AKwhale00`, `AKyakutat00` (0.002°, IGS08) | §3b with **GEOID12B** (GEOID18 has no Alaska coverage) |
| Rest of Alaska (Cook Inlet, Aleutians, Bering, Arctic) | NAVD88 | **none** | no grid. Anchorage's published `S = +3.283 m` is the largest separation in the US and completely uncorrected |
| Hawaii | local tidal datum per island | **none** | no grid |
| Guam | GUVD04 | **none** | no grid |
| CNMI / Saipan | NMVD03 | **none** | no grid |
| American Samoa | ASVD02 | **none** | no grid |
| Great Lakes | IGLD85 / LWD | `IGLD85/hydroc*.gtx` are dynamic-height correctors, not a chart-datum separation | out of scope (already handled by the `great_lakes` source) |

Confirmed two ways: the bundle contains no Pacific-island directory, and the VDatum download page lists no Pacific-island region. The web API confirms the supported set is CONTIGUOUS, CHESAPEAK_DELAWARE, WESTCOAST, PRVI, AK.

For the uncovered Pacific territories, the only available reference is per-station CO-OPS separations. On small islands MLLW−LMSL is nearly spatially constant, so a per-island scalar is defensible — but Guam already disagrees with itself:

| station | ortho datum | ortho − MLLW | MSL − MLLW |
|---|---|---|---|
| Nawiliwili HI 1611400 | local | n/a | +0.250 |
| Honolulu HI 1612340 | local | n/a | +0.250 |
| Kahului HI 1615680 | local | n/a | +0.341 |
| Hilo HI 1617760 | local | n/a | +0.351 |
| Midway 1619910 | local | n/a | +0.198 |
| Apra Harbor, Guam 1630000 | GUVD04 | +0.421 | +0.418 |
| Pago Bay, Guam 1631428 | GUVD04 | +0.238 | +0.308 |
| Saipan 1633227 | NMVD03 | +0.396 | +0.393 |
| Pago Pago, Am. Samoa 1770000 | ASVD02 | n/a | +0.415 |

Guam varies by 0.18 m across a 15 km-wide island (windward vs leeward), so "one scalar per territory" carries ~0.2 m error there. All of these are small and positive, and the scalar is *added* to raise the bed, so the conservative choice across an island's stations is the **maximum**: undershooting leaves part of the original deep bias in place, overshooting charts shallower than truth, and only the latter is the bias-shallow-safe direction.

## 6. Offshore coverage extent

Seaward transects, probing `mllw` and falling through overlapping regions on nodata (`offshore_extent.txt`):

| transect | last coverage | notes |
|---|---|---|
| Mayport FL, due E | **~100–150 km** (`FLGAeastshelf41`, edge at lon −79.0) | value drifts −0.835 → −0.642 over that span |
| Panama City FL, due S | **~100–150 km** (`FLapalach01`) | hole at 5 km offshore between `FLandrew02` and `ALFLgom02` |
| Westport WA, due W | **~600 km** (`WAcoast00` → `WCoffsh00` at ~200 km) | continuous, −1.43 → −1.17 |
| Neah Bay WA, due W | **~500 km** | hole at 5 km |
| San Diego CA, due W | **>700 km** (`WCoffsh00`) | still valid at the far edge of the probe |
| San Juan PR, due N | **~400–500 km** (`PRVIis00` → `PRVIof00` at ~60 km) | −0.229 → −0.261 |
| Magueyes PR, due S | **~300–400 km** | flat ≈ −0.098 |
| Sitka AK, due W | **~150 km** (`AKglacier00`) | holes at 20–30 km and beyond 150 km |

Implications for the plan's nearest-fill distance:

- The West Coast and PRVI are covered far past any CUDEM footprint. **No fill is needed there.**
- The Atlantic and Gulf shelves cut off at 100–150 km offshore. CUDEM's 1/9″ and 1/3″ tiles stay well inside that, so the outer edge is unlikely to bite; the real exposure is **interior holes**, not the outer boundary.
- Interior nodata holes exist even a few kilometres offshore (Panama City at 5 km, Neah Bay at 5 km, Sitka at 20–30 km) where two adjacent regions' `.bnd` polygons do not quite meet. **A hole-filling pass matters more than an outward extension.** A fill radius of ~10–20 km closes all the holes observed here; the plan's "nearest-fill outward a bounded distance" should be sized for that, not for the 100 km offshore edge.
- The separation field is smooth (metres per hundred km), so nearest-neighbour fill is adequate; a distance-weighted fill would be marginally better but is not required.

## 7. Surprises that change the plan

1. **`tss − mllw` is not the universal answer.** The plan's expected shape holds for the Atlantic/Gulf but is wrong by up to 1.2 m on the West Coast and by −0.45 m in Puerto Rico. Composition must branch on the region's `.met` `horz` field, and the West Coast branch needs two core geoid grids plus a PROJ frame transform.
2. **No VDatum coverage for Hawaii, Guam, CNMI or American Samoa.** Those CUDEM territory products cannot be corrected from this bundle at all. Open question in the plan ("whether `cudem_third`'s registration carries territory files on non-NAVD88 datums that need separate grid bands or exclusion") resolves to: PR/VI yes (via `−mllw`), Pacific islands no — scalar-from-station or leave uncorrected.
3. **No VDatum coverage for Alaska outside the southeast panhandle.** Anchorage's +3.28 m is the largest separation in the country and there is no grid within 1000 km of it. Any CUDEM Alaska tile outside the three SE-AK boxes stays uncorrected.
4. **The Columbia River is where the correction goes the unsafe way** (−0.53 m at Skamokawa). `CRD/crd.gtx` is the datum charts actually use there and is a candidate override.
5. **Interior holes, not the offshore edge, drive the fill design** (§6).
6. **Longitudes are 0–360** and must be shifted at harvest; registration needs no fix because the GTX driver already handles it.
7. **Region selection needs the `.bnd` polygons**, not bboxes, if seams appear — the bboxes overlap by hundreds of kilometres.
8. **Accuracy ceiling is ~9 cm** (VDatum's own stated uncertainty for NAVD88→MLLW), with observed station residuals up to 15 cm in the coarse `LAmobile02` Mississippi Sound grid and 12 cm at La Push. That is the floor on any downstream accuracy claim, and it is an order of magnitude better than the ~0.95 m error being corrected.

## 8. Runnable check

`gdallocationinfo` reproduces Mayport directly (note the +360 longitude):

```sh
R=vdatum/FLGAeastbays31_8301/FLGAeastbays31_8301
python3 -c "print($(gdallocationinfo -valonly -geoloc ${R}_tss.gtx  278.5867 30.4) - \
                   $(gdallocationinfo -valonly -geoloc ${R}_mllw.gtx 278.5867 30.4))"
# 0.9464  -- published NAVD88 - MLLW at 8720211 is +0.948
```

Ground truth for any station, no bundle needed:

```sh
curl -s https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations/8720211/datums.json \
  | python3 -c "import sys,json; d={x['name']:x['value'] for x in json.load(sys.stdin)['datums']}; \
                print(round((d['NAVD88']-d['MLLW'])*0.3048,3))"
# 0.948
```

Independent arbiter (NOAA's own transform), for a NAD83-frame region:

```sh
curl -s 'https://vdatum.noaa.gov/vdatumweb/api/convert?s_x=-81.4133&s_y=30.4&s_z=0.0&region=contiguous&s_h_frame=NAD83_2011&s_v_frame=NAVD88&s_v_unit=m&t_h_frame=NAD83_2011&t_v_frame=MLLW&t_v_unit=m'
# "t_z":"0.946", "uncertainty":"0.095"
```

West Coast requires `region=westcoast` and `t_h_frame=IGS14`; PRVI requires `region=prvi` and `s_v_frame=PRVD02`.

## 9. Artefacts left in the scratchpad

- `vdatum_all_20250917.zip`, extracted to `vdatum/` (JRE excluded)
- `region_index.json` / `region_index.txt` / `region_table.txt` — parsed `.met` index
- `stations/` — 49 CO-OPS datums + metadata JSONs
- `station_check.txt` — naive `tss − mllw` at every station (shows the West Coast failure)
- `formula_check.txt` — per-frame formula across all xGEOID candidates
- `final_check.txt` — the benchmark table
- `offshore_extent.txt` — coverage transects
