# CUDEM territory products (HI / PR / USVI / Guam / AmSam / CNMI) — planning doc

Ingest the six NOAA CUDEM territory topobathy products at both resolution tiers.
Closes [#32](https://github.com/openwatersio/seascape/issues/32).

## Problem: CONUS is built, the territories are not

`sources/cudem` (1/9″) and `sources/cudem_third` (1/3″) mirror NOAA's national
topobathy manifests, but the six territory products — Hawaii, Puerto Rico, US Virgin
Islands, Guam, American Samoa, CNMI — are separate NCEI datasets that were never
listed. The NCEI page carries all six at both resolutions.

## Approach: extend the two existing filelists

They live on the **same bucket** (`noaa-nos-coastal-lidar-pds`), as the **same COG
tiles**, reached by the **same** `source_mirror.py` mirrored path. `file_list.txt`
already accepts multiple manifest lines and `enumerate_keys` already enforces a
single bucket across them — so the whole change is twelve `urllist<ID>.txt` URLs
appended to the two existing filelists. No new source directories, no new code.

Grouping into the existing two sources (rather than sibling `cudem_territories*`
dirs, or twelve per-territory dirs) keeps this at the repo's existing granularity —
CONUS already lumps 18 regions under one source. Split later only if a territory
churns and trips the shrink guard (see Gotchas).

### Dataset IDs

1/9″ (→ z13): Hawaii `9428`, PuertoRico `9525`, USVI `9529`, Guam `9462`,
AmSam `9460`, CNMI `9560` — **106** tiles.

1/3″ (→ z12): Hawaii `9429`, PuertoRico `9524`, USVI `9528`, Guam `9463`,
AmSam `9461`, CNMI `9561` — **136** tiles.

Each `urllist` also names STAC `.json`, `.html`, `.xml`, and a tileindex `.zip`; the
existing `.tif`/`.tiff` filter in `enumerate_keys` drops them, exactly as it does for
CONUS.

## Concrete changes

- `sources/cudem/file_list.txt` — 6 territory 1/9″ urllist URLs + header note.
- `sources/cudem_third/file_list.txt` — 6 territory 1/3″ urllist URLs + header note.
- `sources/README.md`, `README.md` — coverage now "US coast + territories".

Nothing else: sources auto-register via `config.sources()` (directory glob), and the
shared `source_catalog.py` tail generates the catalog item.

## Gotchas

- **Vertical datum varies per territory.** Unlike CONUS (uniform NAVD88), each
  nearshore product ships its own local datum — verified in the tile headers:
  HI = MSL (EPSG:9705), PR = PRVD02, USVI = VIVD09, Guam = GUVD04, AmSam = ASVD02,
  CNMI = NAVD88. The 1/3″ Pacific offshore band (Guam/AmSam/CNMI) is labeled NAVD88.
  All are local-MSL-family, so ingesting **as-is** — no normalize, exactly like CONUS
  NAVD88 — is consistent with current behavior. When [#16](https://github.com/openwatersio/seascape/issues/16)'s
  low-water offset lands, each territory needs its **own** offset (distinct datums);
  the per-territory datum is recorded in the `file_list.txt` header for that reason.
- **NoData varies per tile** (`-9999` and `-999999`, vs CONUS `-99999`). Not a
  problem: `gdalwarp` reads each tile's nodata + CRS at reproject and remaps to the
  pipeline's `-dstnodata -9999`, so no per-tile handling is needed.
- **Shrink guard is per-source.** Lumping six territories into one source means one
  territory's dataset being re-IDed is a ~1/7 drop — past `SHRINK_TOLERANCE` (5%), so
  the whole source would refuse to publish until re-listed (or `MIRROR_ALLOW_SHRINK=1`).
  Acceptable for stable published editions; the escape hatch if it recurs is to split
  the territories into their own sibling source.

## Validation

1. `enumerate_keys('cudem')` / `('cudem_third')` — confirmed single bucket, no dupe
   keys, totals 1036 (930 CONUS + 106 territory) and 509 (373 + 136). ✔
2. Opened a sample tile from every territory at both tiers — all COGs (512 px tiles,
   overviews), CRS defined, so `source_mirror` header reads and aggregation range
   reads work unchanged. ✔
3. Territories are spatially disjoint from CONUS, so z13-vs-z13 priority never
   collides.

The full mirror run (header reads on the ~242 new keys + object copy to the data
bucket) happens in the scheduled sources workflow / build box, not locally.

## Open questions

- Whether the local territory datums warrant a per-territory low-water offset now, or
  stay deferred to #16 alongside the CONUS NAVD88 offset. Deferred here.
