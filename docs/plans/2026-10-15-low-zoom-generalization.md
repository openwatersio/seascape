# Low-zoom isobath and depth-area generalization

At z6 to z9 the isobaths and depth-area edges draw as long straight segments with sharp corners, far coarser than the surface under them. Examples on build `dc2deec`: the Kuril arc at `#6.67/46.286/150.476` and the Southern California borderland at `#6.94/33.317/-118.74`.

## Where the vertices go

The tier files leave the pipeline with a vertex about every display pixel; the served tiles have one every ten. Measured on the staged archive and the local fixture root:

| what | z6 median segment | z6 p90 | z8 median |
|---|---|---|---|
| tier file `8-131-84-12-t6.fgb` (after `simplify_coverage`) | 1.22 km (1.0 px) | 1.73 km | |
| served tile, Kuril | 14.8 km (12 px) | 45.9 km | 2.25 km (7 px) |
| served tile, Southern California | 11.0 km (9 px) | 38.9 km | 1.93 km (6 px) |
| served tile, Georgia | 2.2 km | 17.8 km | 0.31 km |

The loss is in the bundle: [pipelines/contour_run.py](../../pipelines/contour_run.py) runs every tippecanoe pass with `--simplification 8`, and the shallow run (z4 to z9) at detail 10. Eight units of a 1024-unit tile is a Douglas-Peucker tolerance of 4.9 km at z6, 2.4 km at z7 and 1.2 km at z8, four display pixels at each zoom, and the straight runs it leaves are what the eye sees. Those settings were sized when the shallow tiles were native partitions of 0.5 to 2 MB; with the per-zoom tiers the largest z6 tile in the planet archive is tens of kilobytes.

Rebuilding the fixture shallow bundle under three settings, Haarlem z6 tile `6/32/21`:

| setting | vertices | median segment | max z6 tile bytes |
|---|---|---|---|
| `-S 8 -D 10` (current) | 12 | 32.6 km | 162 |
| `-S 2 -D 10` | 24 | 7.3 km | 187 |
| `-S 1 -D 12` | 105 | 1.7 km (1.4 px) | 287 |

The per-cell runs carry the same `-S 8` below their leaf zoom. On the Georgia cell `8-70-104`, `-S 1` raises the largest tile from 26 KB to 52 KB at z13 and the archive by a quarter, against the 800 KB ceiling.

A second, smaller contributor is the cut itself. A compiled tier is cut from the surface at its first zoom and simplified to 0.3 mm at that zoom, so at the tier's last zoom the vertices sit 2 px apart and the raster staircase under them is 2 px. The z7 tier's median segment at z8 is 1.9 km (6 px) even with tippecanoe set to 1.

## Plan

1. **Bundle settings.** Shallow run: `--simplification 1`, detail 12. Cell runs: `--simplification 1` below the leaf (the leaf already pins to 1). Bump the `vector_shallow` and `vector_cell` rule versions. This is a re-bundle only, no re-fork: roughly two hours of box time (shallow run, 4,465 cells, join, self-check, stage). Verify with the fixture bundles (segment lengths at z6 to z9, max tile bytes per zoom) and the in-build self-check; the census and seam guarantees are unchanged because `--no-simplification-of-shared-nodes` stays.
2. **Cut resolution and vertex floor per tier.** Cut each compiled tier from the surface one level finer than its first zoom and take `band_tol` from the tier's last zoom, so the tier's finest served zoom sees a vertex per pixel and the staircase under it is half a pixel. The z6 tier cuts at res(7), z7 to 8 at res(8), z9 to 10 at res(10), z11 to 12 at res(12); the native tier is unchanged. Four times the pixels per compiled cut, all of them small. Bump `depare_tile` and `contour_tile`; this re-forks depare and contours planet-wide (about four hours of box time) and must stay shoal-safe: `gates.py tiers` never-deeper and the cascade clamp still hold because the finer cut is the input the coarser tiers are clamped against. Decide against S-58 check 571 explicitly: the floor is 0.3 mm at the tier's finest zoom, 0.15 mm at its coarsest, within the 0.4 mm NOAA works at.
3. **Line joins.** The contour-lines layer has no layout block, so MapLibre draws miter joins; `line-join: round` and `line-cap: round` soften what remains at any zoom. Style only, no build.
4. **Edge smoothing, only if 1 and 2 leave it rough.** Smooth each coverage edge once, the way `simplify_coverage` simplifies each edge once, with the shoal-biased offset S-4 B-411.5 asks for. Per-line Chaikin is ruled out by the crack problem in the contour_run docstring.

Order: 1 and 3 first, re-bundle, look again; then 2 if the staircase at a tier's last zoom still shows; 4 last.

## Checklist

- [ ] Bundle settings and rule versions
- [ ] Style line joins
- [ ] Fixture measurements before and after
- [ ] Re-bundle on the box, smoke the two example views
- [ ] Tier cut resolution and floor
- [ ] Edge smoothing (if needed)
