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

## Run 38586204681

- Result: success for commit `dc2deec1419057738a00bed4c5d6894425965a5a` (main after #196, #195 and #208). Resumed the store volume left by run 38487841311, which had completed every mosaic tile before stopping on the GEOS overlay failure #208 fixes.
- Build job: 15:05:06–03:27:53 UTC (12h22m47s) on a ccx63; 36,198 jobs, zero failures, zero retries; runner teardown succeeded. Full log at `docs/perf/logs/build-38586204681-2026-10-14T15-05-06Z.log`.
- Publication order: vector merge and staging (manifest last, 02:10 UTC) preceded the mosaic publish (03:24 UTC, 4,465 tiles staged, 1,380 uploaded new, candidate GTI `mosaic-candidate-ce7c232a0da5.gti`). The serving pointer flips only at release, so the order between staging and the candidate publish does not matter for the archival build.
- R2 inventory: 245 objects, 49,508,316,914 bytes: 240 overlays (one per manifest cell, none unreferenced, none empty), planet 1,540,902,021, vector 9,495,842,425, coverage 5,611,087, manifest 5,940 (equal to its R2 size), plus the 5.15 GB land archive. The land archive is a fifth top-level object the "overlay count + 4" rule above predates.
- Manifest: planet z0–8 with global bounds, vector max zoom 15, 34 source IDs, DEPARE present (per-zoom tiers from #195).
- `pmtiles verify` passed for all three primary archives: planet z0–8 webp, 87,381 addressed tiles; vector z4–15 mvt, 19,971,519 addressed tiles; coverage z0–8.
- Preview smoke test against the exact SHA: globe at z2 complete with no seams; IJmuiden z12 (soundings, isobaths, foreshore, bands); SF Bay z10.6, the baseline failure location, shows the full CUDEM/NOS estuary detail; Hamilton Harbour z11 (no false depths over the city); mid-Pacific z10 falls back to the planet archive with 4,000 m isobaths and soundings. Every tile request returned 200; the only console errors are the site favicon and Mapterhorn land-relief tiles that do not exist over open ocean. Aborted requests during zoom animation are client cancellations.
- Known limitation, not a release blocker: isobaths and depth areas at z6–9 are drawn with long straight segments. The tier files carry vertices every pixel; the shallow bundle run's `--simplification 8` at detail 10 removes them (a 4.9 km tolerance at z6). #210 changes those settings; its re-bundle is the check.

## Run 38765614255

- Result: success for commit `a1bbdb8b38a22e782273840e18c4b6181d068f22` (main after #210). A re-bundle of run 38586204681's store: 4,471 jobs (the shallow run, 4,465 cells, join, self-check, bundles, staging), zero failures, 1h23m on a ccx63. The shallow run took 26 minutes at 6 GB; the overflow gate armed on no run.
- R2 inventory: 245 objects, 50,274,293,846 bytes. Only `vector.pmtiles` (10,261,819,357 bytes, up 8% from 9,495,842,425) and `manifest.json` changed; planet, coverage, land and the 240 overlays carry over from run 38586204681.
- `pmtiles verify` passed on the vector archive: z4–15, 19,971,521 addressed tiles. The in-build self-check passed: sampled invariants ok.
- Served tiles, median segment length at the two example views: Kuril z6 12.1 → 3.9 px, z7 14.5 → 4.0 px; Southern California z6 9.0 → 2.6 px, z7 12.3 → 3.4 px; z8 unchanged in both (the tier cut's own floor). Tile bytes about doubled at z6–7, far under the ceilings.
- Known limitations, not release blockers: at a compiled tier's last zoom the cut's own vertex floor leaves 6–7 px segments (z8 here); and in Bands mode the z5/z6 handoff changes the shelf tint, since the vector tier tints a 0–200 m band by `drval1` while the raster ramp below z6 tints by pixel depth.

## Runs 38836629029 and 38894435408

- Result: success for commit `d189a09ab7c2b862fd1f7d950e77c2ae2f6c59d3` (main after #213, #214 and #215). Run 38836629029 (`4cb82ff`) re-forked every stem for the low-zoom depth-area tiers: 22,473 planned jobs on a ccx63, and it failed only in `vector_shallow`, where the z4 tile 4/13/7 (Philippines and northern Borneo) reached 525 KB compressed and tippecanoe coalesced features. Run 38894435408 re-bundled the banked forks with the shallow run's low zooms at detail 11: 6 jobs, zero failures. Its shallow bundle thinned only tiny polygons at z4–7, and the overflow gate did not arm.
- Fork benchmarks from run 38836629029: depare 4,399 jobs and 125 CPU-hours, the longest 205 minutes (`8-63-105-15`, a Gulf marsh stem at cz15); windows 87 CPU-hours; soundings 26; contours 3; peak RSS 7.7 GB. The Arctic stem `4-3-3-8`, 316 minutes in run 38586204681, took 101: 64 minutes for its native tier and 18, 5 and 4 for the z7–8, z6 and z4 tiers, which reuse the native tidal-water union.
- R2 inventory: 245 objects, 50,743,075,443 bytes. `vector.pmtiles` is 10,730,600,954 bytes, up 5% from 10,261,819,357.
- The in-build vector self-check passed over 19,965,847 tiles, z4–15, with sampled invariants ok, and the join censused 104,713,957 feature ids per cell with none missing. `pmtiles verify` passed for all three primary archives: vector z4–15 mvt, 19,965,847 addressed tiles; planet z0–8, 87,381; coverage z0–8, 15,684.
- Served z4 tiles, which the Worker re-encodes at detail 12: 13/7 (Philippines) 701 KB, 12/7 (Indonesia) 436 KB, 3/3 (Canadian Arctic) 358 KB, 8/4 (Norway and the Kattegat) 322 KB, 14/5 (Kuril arc) 277 KB.
