# Build validation

Use this checklist after a planet build and before running `release.yml`. A green Actions job is
necessary, but a build is releasable only when its publication order, artifact inventory, archive
structure, and rendered output are all verified.

This is the final acceptance gate, not the current optimization loop. While build performance and
cache control are being repaired, validate changes with synthetic or bounded regional builds and do
not spend a planet run on each individual correctness fix. Return to this checklist only for a
release-candidate milestone build that contains the accumulated fixes.

## 1. Confirm completion

Record the run ID and commit SHA, then require every non-release job to succeed:

```sh
RUN=29518661202
gh run view "$RUN" --json status,conclusion,headSha,jobs,url
```

In the log, confirm that:

- aggregation completeness checks passed;
- terrain, coverage, soundings, and contour bundles completed;
- the mosaic index and planet TIFF uploaded before `mosaic.gti` was flipped;
- `vector.pmtiles` uploaded successfully;
- `manifest.json` uploaded last and `build complete` followed it;
- runner deletion and leak checks succeeded;
- no `No space left`, OOM, transfer failure, or ignored subprocess failure appears.

Save the complete log under `docs/perf/logs/build-<run>-<created-at>.log` so later performance work
uses the same evidence as release validation.

## 2. Confirm the archival build in R2

Set `SHA` to the run's exact `headSha`; never validate a branch name or `HEAD` in its place.

```sh
SHA=e42639a484f8ec271a09256fe90e2ed6a27c8289
PREFIX="s3://data/bathymetry/build/$SHA"
aws s3 ls "$PREFIX/" --recursive --summarize
aws s3 cp "$PREFIX/manifest.json" /tmp/seascape-manifest.json
jq '{planet, overlay_cells: (.overlay.cells | length), source_ids}' /tmp/seascape-manifest.json
```

Require:

- one non-empty `planet.pmtiles`, `vector.pmtiles`, `coverage.pmtiles`, and `manifest.json`;
- one non-empty `overlay-<z>-<x>-<y>.pmtiles` for every key in `manifest.overlay.cells` and no
  unreferenced overlay in the build prefix;
- object count = overlay-cell count + 4;
- `manifest.planet.file == "planet.pmtiles"`, minzoom 0, maxzoom 8, global Web Mercator bounds, and
  manifest size equal to the R2 object size;
- expected source IDs are present and no source disappeared accidentally;
- DEPARE presence matches the build configuration. While `SKIP_DEPARE=1`, the vector archive must
  contain contours and soundings but no DEPARE layer.

The `data.openwaters.io` archival endpoint may intentionally reject direct PMTiles reads even when
the objects exist in R2. Validate archive contents through an authenticated R2 download or the
preview/release serving path; do not interpret a public archival 404 alone as a missing R2 object.

## 3. Validate PMTiles before release

Download the three primary archives, then inspect and structurally verify them:

```sh
mkdir -p /tmp/seascape-validation
aws s3 cp "$PREFIX/planet.pmtiles" /tmp/seascape-validation/planet.pmtiles
aws s3 cp "$PREFIX/vector.pmtiles" /tmp/seascape-validation/vector.pmtiles
aws s3 cp "$PREFIX/coverage.pmtiles" /tmp/seascape-validation/coverage.pmtiles

pmtiles show /tmp/seascape-validation/planet.pmtiles
pmtiles show /tmp/seascape-validation/vector.pmtiles
pmtiles show /tmp/seascape-validation/coverage.pmtiles
pmtiles verify /tmp/seascape-validation/planet.pmtiles
pmtiles verify /tmp/seascape-validation/vector.pmtiles
pmtiles verify /tmp/seascape-validation/coverage.pmtiles
```

Require the expected zoom ranges, bounds, tile type, attribution, and vector layer metadata. Extract
representative tiles and confirm they are non-empty at the expected zooms: global GEBCO, a populated
high-resolution overlay, contours, soundings, and coverage. Include a location outside all overlays
to verify high-zoom fallback to the planet archive.

## 4. Preview smoke test

Run the preview against the exact build SHA, not a mixture of local artifacts and a prior release.
Check at least:

- the whole globe at z0-z2 for complete raster coverage and no seams or blank bands;
- a coastline with contours and soundings at navigational zooms;
- one regional high-resolution source above z8 and its transition back to the planet base;
- one inland-water or Great Lakes location;
- raster units/settings and vector visibility after zooming and panning;
- browser console and tile requests for errors, 404s, repeated retries, or invalid ranges.

Release only after the visual checks pass and the preview reports no unexpected request failures.
Keep screenshots or concise notes for any anomaly; do not promote a build merely because the map
renders at the initial viewport.

## Run 29518661202 baseline

- Result: success for commit `e42639a484f8ec271a09256fe90e2ed6a27c8289`.
- Build job: 17:12:02–04:30:56 UTC (11h18m54s); resource teardown succeeded.
- R2 inventory: 238 objects, 25,365,033,787 bytes; 234 overlays and the four required top-level
  artifacts.
- Planet: 2,347,182,599 bytes, z0-8, global bounds; vector: 9,108,836,291 bytes; coverage:
  3,181,216 bytes.
- Manifest: 23 source IDs; uploaded last at 04:30 UTC.
- DEPARE was intentionally disabled.
- **Release validation failed.** At `#10.56/37.5044/-122.1502`, the build preview is visibly coarse
  and omits the detailed Bay/estuary coverage present in development. There are no browser console
  errors: the wrong pixels are present in successfully served tiles.
- The comparable release artifacts are 25.37 GB versus 59.20 GB in published build
  `6d7df3754aaca7ab99741d22aaa1098a5b960798`; planet is 2.35 GB versus 5.12 GB and Bay overlay
  `5-5-12` is 393 MB versus 885 MB. Do not release this build.
- Root cause: `mosaic.gti` omits `<ResX>`/`<ResY>`. GDAL 3.13 consequently infers a 305.748 m
  virtual-mosaic resolution (about z9) from one indexed tile and overzooms it for every finer terrain
  render, despite the Bay index row naming CUDEM + NOS Estuarine at 9.55 m. Fix the GTI resolution,
  re-render terrain, and repeat this checklist.
