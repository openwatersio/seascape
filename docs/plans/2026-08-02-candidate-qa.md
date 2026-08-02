# Candidate build QA — cartographic issues to investigate

Issues spotted eyeballing the staged candidate (`build/c52dbf4…`). One entry per issue: viewer link, what's wrong, suspects, and disposition once diagnosed.

## 1. Drying triangle in Lake Winnebago, Wisconsin

- Where: http://localhost:5173/#11.64/44.0752/-88.6702
- Symptom: a drying-zone triangle rendered in a freshwater lake — drying geometry (chart-datum foreshore) has no business in an inland lake.
- Suspects, most likely first:
  - Lake-surface datum: GEBCO carries the lake bed relative to sea level; a lake whose surface sits above 0 m makes shallow bed pixels read as "above datum" → classified drying. The per-lake surface-elevation correction (HydroLAKES) is planned but not built.
  - The drying bucket's [0, DRYING_CAP] band + effective-land subtraction misclassifying lake shallows (the boxy-drying rebuild plan covers geometry quality, but the datum question decides whether ANY drying belongs here).
  - Triangle shape suggests a single coarse cell/pond-fill artifact — check which source and child_z covers this tile before blaming the datum.
- Next step: decode the tile, identify source + drval band of the offending polygon.

## 2. Drying zones in Lake Ladoga, Russia

- Where: http://localhost:5173/#8.38/60.136/31.368
- Symptom: drying rendered in another inland lake.
- Pattern with issue 1: two freshwater lakes, both showing drying — strengthens the lake-surface-datum suspect. Ladoga's surface sits ~5 m above sea level: bed pixels between 0 and +5 m read as "above datum" → drying band, while the deep basin stays normal depths. Winnebago (surface ~226 m) doesn't fit that simple story — its bed should read far above datum, yet only a triangle shows — so decode both and compare sources before settling on one mechanism.
- Likely resolution class: per-lake surface correction (HydroLAKES plan) or masking drying to tidal waters only (drying is meaningless off chart datum; inland waters should never carry the band).
