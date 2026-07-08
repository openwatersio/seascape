# Vector depth-band polygons (ENC DEPARE) alongside contour lines

## Goal

Add ENC-style depth-area polygons (S-57 DEPARE: banded water polygons carrying their depth range) as a vector layer next to `contours`, so band tints get crisp edges that sit exactly on the isobaths, stay sharp under overzoom (the raster color-relief blurs above native zoom), and can be recolored client-side by safety depth with ordinary fill expressions — the ECDIS model, where the safety contour snaps to the next-deeper available band edge. The raster color-relief stays; bands are an alternative shading the style can select.

## Approach

`gdal_contour -p` (polygon mode, `-amin`/`-amax`) on the same merged, smoothed DEM each aggregation tile already contours. Its polygon edges are the same linearly-interpolated isolines `gdal_contour` emits as lines, so band edges and contour lines coincide by construction (see "smoothing" below for the one exception). New sibling module `pipelines/depare_run.py`, shaped like `drying_run.py`.

### Band levels

The full contour ladders: `CONTOUR_LEVELS` + 0 for the metre set (0 closes the shoalest band at the shoreline), `CONTOUR_LEVELS_FT` + 0 tagged `sys=ft`. Every charted isobath is a band edge, so safety recoloring snaps to any level the chart draws (ECDIS behavior; a value between levels rounds deeper — bias shallow) and band edges coincide with every contour line, not just the shoal ones.

Partition polygons can't be per-zoom thinned with a filter (dropping a partition leaves a hole), so the low-zoom cost of the full ladder is controlled by a zoom floor instead: the layer bundles and renders from z6 (the contour lines' presentation floor), with the raster relief carrying z<6. If deep-ocean z6–8 tiles still bust tippecanoe's budget, trim DEPARE levels deeper than −200 m (config-only): those bands render as near-identical near-white tints and no safety value lives down there.

### Attributes

`drval1`/`drval2` (ENC names): positive-down metres, shallow/deep bound of the band, derived from `-amin`/`-amax` (negate, swap). Plus `sys` (`m`/`ft`). Drop partitions with `amin >= 0` (land — the encoder clamps water to ≤ −LSB, land ≥ 0). No `-j` per-zoom filter needed, so the Integer64-string `-j` trap doesn't apply; `-T drval1:float -T drval2:float` (fathom edges aren't integer metres).

### Smoothing and cracks — the load-bearing decisions

- **No Chaikin, no shapely simplify on the polygons.** Adjacent partitions share edges; any per-feature smoothing/simplification treats the shared chain differently in each polygon (different ring anchors) and opens see-through cracks between bands. Leave geometry raw in the FGBs.
- **tippecanoe `--detect-shared-borders`** does crack-free simplification of shared polygon borders per zoom — this flag exists for exactly this layer shape. Because maxzoom tiles simplify crack-free, overzoom stays crack-free too.
- Line/edge coincidence: nav-band contour lines (≤ `CONTOUR_NAV_SMOOTH_MAX`, 30 m) skip Chaikin, so every shoal band edge matches its line to within the lines' sub-pixel simplify. Deeper lines are Chaikin-smoothed and will sit slightly deep of the raw band edge — visible only as a sliver between near-white deep tints, where tint carries no safety meaning. Accepted; do not engineer ring-matched smoothing.

### Per-tile flow (`depare_run.generate`)

Same seam contract as contours/drying — deterministic on the buffered DEM, restricted to the unbuffered tile:

1. `gdal_contour -q -p -amin amin -amax amax -fl <levels> -f FlatGeobuf` on the merged 3857 DEM, once per level set (m, ft).
2. Load with geopandas; drop land partitions; add `drval1`/`drval2`/`sys`.
3. Clip to the unbuffered tile bbox **in shapely** (`make_valid` + `box` intersection + the `_polys` polygon-only reducer — reuse/lift from `drying_run`; ogr2ogr `-clipsrc` is the GDAL 3.8 GeometryCollection trap).
4. Reproject to 4326, write `store/depare/{z}-{x}-{y}-{child_z}.fgb` (tile-keyed so clean tiles persist across incremental runs).

Skip knob: `SKIP_DEPARE`, wired in `aggregation_run.py` next to the `drying_run.generate` fork.

### Bundle

`depare_run.bundle()` + `fold()`, copied from drying's shape:

- Orphan filter via `contour_run._live_fgbs` / `_current_stems`.
- **Shared tileset maxzoom** via `contour_run.bundle_maxz` (drying's lesson: a layer bundled shallower than the joined tileset vanishes from deeper tiles).
- tippecanoe: `-l depare -Z 6 -z <maxz> --detect-shared-borders --coalesce-smallest-as-needed` — coalesce, **not** `--drop-densest-as-needed`: dropping a partition punches a tint hole; coalescing mis-tints a sub-pixel blob instead.
- `fold()` tile-joins `depare.pmtiles` into `vector.pmtiles` (`-pk`), after the contour bundle like drying.

Scale check: vertex weight ≈ 2× the corresponding contour lines (each isoline is stored in both neighbouring partitions) with no zoom tiering, concentrated in deep-ocean mid zooms — the z6 floor plus `--simplification` headroom covers it, and the abyssal-level trim above is the escape hatch. If a shoal-fractal tile still busts the size limit, raise `--simplification`, not the drop policy.

## Drying seam (considered folding into this pipeline — rejected)

Drying stays in `drying_run`. It can't come from `gdal_contour -p`: its definition needs the OSM land mask (a bare [0, DRYING_CAP] DEM band repaints polders and low coastal plains as foreshore), and post-intersecting with mask polygons just rebuilds `drying_run` with extra steps. Nor is exact drying/band edge alignment achievable: they're separate MVT layers from separate tippecanoe runs (`--detect-shared-borders` only works within a layer), so coincident edges would diverge under independent simplification into cracks. The robust cross-layer seam is the existing overlap design — drying's seaward overhang paints over the band edge, making the drying edge the visible waterline.

Bands mode does tighten the overhang requirement: in relief mode the raster is continuous under drying, but the shallowest band's 0-edge is an independently-wobbling vector edge, and the current `DRYING_SEAWARD_PX=1` is inside drying's own 1.5 px DP tolerance — a bare-basemap sliver can open between blue and green. Raise `DRYING_SEAWARD_PX` to 3 (overhang > combined simplification wobble of both layers; shoal-conservative — water only reads narrower) in the same rebuild depare already forces.

## Style (`style/index.ts`)

- New fill layer `depth-areas` (`source-layer: depare`, minzoom 6 — the same presentation floor as `contour-lines`), between `depth-shading` and `drying-areas`. Filter by `sys` per unit like `contour-lines`. In bands mode `depth-shading` keeps z<6 (maxzoom 6); same palette, so the handoff is a sharpness change, not a colour change.
- `fill-color`: literal expression keyed on `drval1` — `step`-style match assigning `bandColors`, with every band whose `drval2 <= safety-snapped-edge` painted `flavor.hazard`. Same literal-expression pattern as `depthRelief`; a `depthAreasColor(flavor, {unit, safety})` helper next to it. Opacity 0.85 to match.
- Selection: a `shading: "relief" | "bands"` option on `layers()`/`style()` (default `"relief"`) emits one or the other visible — don't stack both at 0.85, the hues compound. `applyState` gains the `depth-areas` filter + fill-color updates.
- Drying still paints over the shoalest band; land wash unchanged (relief ramp or, in bands mode, no fill over land — OSM base + drying carry it; verify in preview).

Viewer: a shading toggle in `index.js` mirroring the existing unit/safety controls, so preview can A/B it.

## CI (`.github/workflows/build.yml`)

Mirror every `store/drying` line for `store/depare`:

- aggregate job: `mkdir store/depare`, `aws s3 sync store/depare …/bathymetry/depare`.
- Prune job: orphan-FGB prune stanza (covering re-tile).
- contour-bundle job: pull `bathymetry/depare`, run bundle + fold after drying's.
- Justfile: `depare` recipe (`bundle` + `fold`) called from the `planet` recipe next to `drying`; add `store/depare` to the clean recipe.

Worker: no change — the layer rides `vector.pmtiles` passthrough.

## Rebuild caveat (do not skip)

A new fork produces nothing for clean tiles: the incremental diff only re-aggregates dirty tiles, so the first CI build after merge would carry depare FGBs for dirty tiles only. Clear the R2 aggregation/covering state (or force-dirty everything) before the first full build — same trap as contour-level config changes.

## Checks

- `depare_run.py check` (repo convention): synthetic DEM → partitions exactly tile the water (union == water footprint, pairwise disjoint), `drval1 < drval2`, land dropped, byte-identical on re-run (seam contract reduces to determinism), shapely clip keeps the layer uniformly polygon (bowtie + GeometryCollection cases, lifted from drying's check).
- `test_engine.py`: `check_depare` adjacent-tile seam test mirroring `check_drying`.
- Style: extend `npm test` for `depthAreasColor` (band → color, safety snapping deeper, hazard painting).
- Visual: `just preview` in both shadings, m + ft, safety on/off; drag `vector.pmtiles` into the PMTiles viewer to confirm the `depare` layer and attributes. In bands mode, inspect the waterline at overzoom for blue/green slivers (the drying-overhang margin) and macrotile boundaries for band cracks.

## Sequence

1. `config.py` levels + `depare_run.py` (generate/bundle/fold/check) + `aggregation_run.py` fork + Justfile recipes.
2. `test_engine.py` seam check.
3. Style layer + `shading` option + `applyState` + viewer toggle + style tests.
4. `just preview` evaluation; tune tippecanoe flags if tiles bust limits.
5. build.yml wiring, then a full (state-cleared) build dispatch.

## Skipped (deliberately)

- Deriving contour lines from polygon boundaries, or drawing the polygons *as* the lines (true ENC DEPCNT/DEPARE topology) — evaluated and rejected: each partition ring fuses two isobath levels (no per-level filtering or labelling), macrotile clip edges baked into the rings would be stroked and labelled as isobaths, and the two `gdal_contour` modes share one generator on one DEM, so line/edge coincidence is already guaranteed without chaining vector ops. Clip edges are harmless to *fills* (fills don't stroke their boundary — the drying layer already proves macrotile-clipped polygons tile invisibly).
- Nested/cumulative bands ("deeper than L") — translucent fills composite once per covering feature, so deep water stacks to ~full opacity and off-palette hues; partitions cover each pixel exactly once, keeping the 0.85-over-basemap design exact.

## Follow-ups (explicitly not this plan)

- Crank the depth-gated deep-water DEM smoothing (`smooth.py`) to see whether the deep-line Chaikin can retire — would close the residual deep line/band-edge mismatch at the source. Each tuning pass is a planet re-aggregation, and the DEM feeds shading/soundings/drying too.
- Uniform polygon smoothing (cyclic Chaikin with clip-boundary vertices pinned) if raw deep band edges look noisy in preview despite the above.
