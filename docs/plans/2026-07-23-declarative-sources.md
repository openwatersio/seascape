# Declarative source ingestion

Every source declares how to enumerate, unpack, and materialize its files as data in
`metadata.json`; one engine executes the declarations through one Snakemake chain. Adding a
source that uses existing mechanisms touches only `sources/<id>/` — never pipeline code. The
prepped/"mirrored" lane split and `source_prep.py`'s content-sniff routing both dissolve.

## Why

- `stage()` (source_prep.py) re-discovers per run what the source author knew at add time:
  eight magic-byte routes, two nested re-sniffs (gzip→{e00,tar,tif}), and one emergent
  fallthrough (a zip with no tif members silently becomes an asc-mosaic candidate). Shape
  knowledge belongs to the source, not the engine.
- The two lanes weld together three independent choices — how files are *enumerated*
  (static list vs bucket listing), whether bytes are *transformed* (normalize vs 1:1), and
  *when* (on-change vs weekly cron). S-102/CUDEM sit at one corner, everything else at the
  other, and nz_coastal needs a corner that doesn't exist (listed + processed + weekly):
  LINZ adds surveys ~monthly, so a committed harvest snapshot is perpetually stale, but the
  raw lane would leave LERC tiffs that a laptop's CLI GDAL can't decode. Normalize-on-ingest
  plus listed enumeration solves both — the model just can't express it today.
- "Mirrored"/"volatile" is a misnomer: every source is mirrored to store/R2. What the flag
  actually encodes is a materialization policy.

## The three axes

| Axis | Key | Values | Default |
|---|---|---|---|
| Enumerate | `filter` + file_list.txt line forms | static URLs · bucket prefix (trailing `/`) · urllist manifest; `filter` = fnmatch over listed keys | static |
| Unpack | `unpack` | `"zip:<glob>"` · `"tar.gz:<glob>!1"` · `"7z:<glob>"` · `"gz"` · `"e00"` · `"netcdf"` · `"asc-mosaic"` | none (bare raster) |
| Materialize | `raw` | `true`: bytes 1:1, register from header reads, rclone at publish · absent: fetch → unpack → datum → normalize | processed |

- `!N` on an unpack glob asserts exactly N matches per archive (the Great Lakes
  one-`*_lld.tif`-per-tarball tripwire). `unpack` absorbs `archive_members`.
- Refresh cadence is derived, not declared: list-enumerated sources re-enumerate on the
  weekly sources cron (that is what a listing means); static sources change only when their
  committed file_list.txt changes. The word `volatile` dies.
- `raw` stays explicit — it is a policy choice, not a byte property, and both inference
  routes fail on real sources: nz_coastal needs zero normalization params yet must be
  processed (normalize's value is the canonical ZSTD re-encode, not metadata assignment),
  while noaa_s102 declares `negate: true` yet must stay raw (its negate applies at
  aggregation read — see source_catalog's sidecar logic — and its uncertainty band +
  per-tile UTM must not be flattened). GDAL-readability discriminates nothing: nearly every
  source is readable; that is why sniffing worked at all.
- Transform knobs (`crs`, `nodata`, `negate`, `datum_offset_m`, `clamp_positive`) are
  already declarative and unchanged. The read-time-vs-baked `negate` duality stays resolved
  by the existing datum-sidecar rule.

## Example declarations

```jsonc
// nz_coastal — s3://nz-coastal => */dem_1m/*/*.tiff => normalize
// file_list.txt: one line, https://nz-coastal.s3.ap-southeast-2.amazonaws.com/
{ "filter": "*/dem_1m/*/*.tiff", "max_zoom": 14 }

// great_lakes — tar.gz => exactly one *_lld.tif => normalize
{ "unpack": "tar.gz:*_lld.tif!1", "crs": "EPSG:4326", "clamp_positive": true }

// noaa_s102 — bucket listing, bytes 1:1, negate at read
{ "raw": true, "filter": "*.h5", "dedupe": "s102-issue", "negate": true,
  "band": 1, "mixed_crs": true, "priority": 1, "max_zoom": 14 }
```

The S-102 issue-field dedupe becomes a *named strategy* (`dedupe:`) — upstream-specific
logic the engine offers and sources pick. A genuinely new mechanism (the next e00) is one
new entry in the unpack/dedupe dispatch table; that is the irreducible code change.
`harvest.py` remains the escape hatch for API-gated enumerations (batnas' login token,
uk_surfzone's key sweep) and keeps writing file_list.txt.

## Migration map (all 24 sources)

| Sources | Declaration |
|---|---|
| gebco, emodnet, ausbathytopo, gbr30, infomar_10m/25m | `unpack: "zip:*.tif"` |
| great_lakes | `unpack: "tar.gz:*_lld.tif!1"` |
| african_great_lakes | `unpack: "7z:*_ras.tif"` |
| bodensee, lac_leman, lac_neuchatel | `unpack: "asc-mosaic"` |
| noaa_estuarine | `unpack: "netcdf"` |
| gsc_atlantic, lake_tahoe | `unpack: "e00"` |
| ddm, vaklodingen, swiobc, batnas, gsc_pacific, uk_surfzone | (bare rasters — no keys) |
| cudem, cudem_third | `raw: true` (urllist lines as today) |
| noaa_s102 | `raw: true, filter: "*.h5", dedupe: "s102-issue"` |
| nz_coastal | `filter: "*/dem_1m/*/*.tiff"` — listed + processed |

## Engine + Snakefile shape

- `stage()`'s sniff-router becomes a flat `UNPACKERS` dispatch table over the existing
  extractor functions. Sniffing survives only as validation: bytes that contradict the
  declaration are a corrupt raw (deleted, refetch message) — the self-heal, collision
  guard, and clear-stale behavior all carry over.
- One chain for every source, checkpointed at enumeration (single code path — no
  parse-time file_list special case):

  ```
  checkpoint enumerate  sources/<id>/{file_list.txt,metadata.json} -> store/source/<id>/items.txt
                        static: echo the committed list; listed: ListObjectsV2/urllist +
                        filter + dedupe + shrink guard (the source_mirror guards move here)
  rule fetch_item       processed sources only, one job per item; raw/<urlhash> not
                        raw/<index> so a list insertion stops mass-refetching later indices
  rule prep_source      declared unpack + datum + normalize (processed) — no sniff routing
  rule register         bounds.csv: local files (processed) | header reads with
                        carry-forward + broken-product triage (raw)
  catalog_item / polygon / publish   unchanged (catalog.json last; raw publishes objects)
  ```
- `PREPPED`/`MIRRORED`/`STREAMED` wildcard-constraint lists derive from declarations.
- Weekly cron = `-R enumerate` over list-enumerated sources; the cross-invocation
  checksum-curing behavior (refresh then catalogs/publish) is unchanged.

## Phases (each lands green independently)

1. **Declare unpack.** Add `unpack` to the ~14 archive/format sources; dispatch+validate in
   stage(); delete sniff-routing and `archive_members`. No enumeration change.
2. **Checkpoint enumeration.** `enumerate` checkpoint for all sources, `filter` key,
   URL-hash raw names, mirror guards relocated. Convert nz_coastal to listed+processed
   (deletes its harvest.py; 373-line file_list → 1 line).
3. **Rename the concept.** `volatile` → `raw` in metadata/config/catalog/docs; derive the
   Snakefile source classes; rewrite sources/README "Access patterns".

## Costs / seams

- Store migration: existing `raw/<index>` files re-key to `raw/<urlhash>` (one-time rename
  pass, or accept a refetch per source on first touch — decide at phase 2).
- Per-item incrementality inside prep (re-normalizing only new items of a listed processed
  source) is deliberately out of scope; the seam is items.txt diffing, noted for later.
- catalog.json's `seascape:volatile` key renames with a fallback read during cutover.
