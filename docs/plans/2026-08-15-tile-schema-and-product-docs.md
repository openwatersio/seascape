# Tile schema version and product documentation

Implements [#129](https://github.com/openwatersio/seascape/issues/129) (a declared `schema` version for the tile↔client contract) together with documentation for each published product and for the cartographic decisions behind them. The two belong together: the schema number is only meaningful if the contract it versions is written down, and the contract doc is the natural home for most of the product documentation.

## Part 1: schema version

A single integer, `schema`, starting at 1. The build is the only writer; everything else relays or asserts it.

### 1. Declare it in the build

`SCHEMA = 1` in `pipelines/config.py`, written into `manifest.json` by `stage_build()` in `pipelines/bundle.py` (`"schema": config.SCHEMA`). A staged build carries its own schema, so a promotion cannot mix schemas — the manifest is copied last and atomically already.

### 2. Serve it from the worker

The worker builds TileJSON from the manifest it fetched, so it relays the manifest's value rather than compiling in its own:

- `/raster.json` and `/vector.json` in `worker/src/index.ts`: add `schema: mf.schema ?? 1` (the fallback covers manifests staged before this ships; drop it after the next release).
- `/coverage.json`: pass the schema through to `coverageTileJSON()` in `worker/src/coverage.ts` the same way attribution is passed today.

Coverage shares the main schema number rather than getting its own. It promotes as part of the same release unit, and a second version scheme is coupling nobody has asked for. Revisit only if coverage ever ships on its own cadence.

### 3. Export it from the style package

`export const SCHEMA = 1` in `style/index.ts`, with a doc comment pointing at [the contract](../schema.md). This is a deliberate second declaration, not duplication: the package declares which schema it *targets*, and the point of the whole exercise is that the two declarations can disagree.

### 4. Assert it in the viewer

`index.js` fetches `/vector.json` after creating the map (the map itself still needs nothing up front), compares `tilejson.schema` against the package's `SCHEMA`, and on mismatch replaces the map with a full-screen error naming both numbers.

The mismatch is **fatal**, not a warning. The failure this exists to prevent is a plausible-looking wrong depth on a navigation-adjacent product; a dismissible banner over a subtly wrong chart defeats the purpose. A TileJSON without a `schema` field passes (older worker, pre-schema tileset — nothing to compare against).

The style package exports only the constant. Consumers compare it themselves; a one-line comparison doesn't earn a helper function.

### 5. Document the bump rule

New `### Schema` subsection under "Conventions" in `CONTRIBUTING.md`, linking to `docs/schema.md` for the contract itself:

- **Bumps when** a previously valid reader becomes wrong: renaming or removing a layer or field, changing the meaning, unit, or datum of an existing field, changing the raster encoding or its quantization, or narrowing a zoom range.
- **Does not bump for** data rebuilds, added layers or fields, or widened zoom ranges. Additive changes are free; the number moves only when something a client already does becomes wrong.
- **Release rule**: a schema bump makes the style release that targets it a package **major** — it stops working against the old tiles, which is semver's definition of breaking. The converse does not hold: package majors can happen for API reasons alone, so the package version never encodes the schema number. Consumers pin the package as usual, and a schema migration cannot arrive silently through `npm update`.

### Resolved open questions from the issue

- **Coverage versioning**: shared number (step 2 above).
- **Fatal vs warning**: fatal (step 4 above).
- **Serving older tilesets during rollout**: no. Promotion replaces the serving build as a unit; there is no window where two tilesets coexist, so nothing to build.

## Part 2: product and cartography documentation

Every published product gets a doc, and the "why" behind the cartographic choices gets its own. All linked from the README's Usage section.

### `docs/schema.md` — the tile contract (normative)

The definition behind schema v1, and the doc a schema bump must update. Covers both tilesets a consumer decodes:

- **Vector** (`vector.pmtiles`, served as `{z}/{x}/{y}.pbf`): each layer (`depare`, `contours`, `soundings`, `coverage`) with its fields, types, units, and semantics — `drval1`/`drval2` as metres below chart datum with negative = drying, `depth_m`/`depth_ft`/`depth_fm`, `rank`, `sys`, provenance fields. Zoom range and the variable-depth/overzoom serving behavior a client observes.
- **Raster** (`{z}/{x}/{y}.webp`): terrarium encoding, elevation in metres, per-zoom quantization, and the category codes — 0 = water of unknown depth, 1 = drying, 2 = land, negative = depth below datum. The drying cap and the land sentinel as a client sees them.
- **Semantics a client cannot see but depends on**: 0 means chart datum, drying heights are positive within the cap, a coarser zoom never reads deeper than the data beneath it (shoal bias).
- **Coverage** (`coverage.pmtiles`): the provenance layer and its fields.

Source material already exists as code comments: `worker/src/mask.ts`, `pipelines/encode.py`, `pipelines/terrain.py`, the `vector_layers` blocks in `worker/src/index.ts`. The doc becomes the one normative statement; the comments shrink to pointers where they'd otherwise duplicate it.

### `docs/cartography.md` — the decisions (rationale)

Why the contract looks the way it does, for contributors and curious users:

- Why the raster treats 0 as unknown-depth water, 1 as drying, 2 as land: the non-negative domain is categorical because depth shading only needs "how deep", while land/drying/unknown need to survive resampling and quantization as distinct renderable classes.
- Shoal bias: why aggregation never reads deeper than the data beneath it, and where that constraint binds (pyramids, coarsening, smoothing).
- The drying cap (16 m) and why the rare genuine 16–17 m drying sites classify as land.
- Depth bands: chart-convention shoal-dark → deep-white tints, perceptual spacing, band edges on charted isobaths, and the ft/fm ladder as a hard requirement.
- Safety contour emphasis and drying portrayal (ENC-style, `drval1 < 0`).
- Contour generalization by zoom and why coarse zooms show fewer levels.

Written from this repo's code and public standards (INT1, NOAA chart conventions) only.

### `style/README.md` — the npm package

Ships in the published package and renders on npmjs.com. Usage (`style()`, `sources()` + `layers()` piecemeal), runtime parameters (`unit`, `safety`, `shading`), `applyState()`, `readDepth()`, flavors and overrides, `SCHEMA` and how to assert it. Much of this exists as the module doc comment in `style/index.ts`; the README is the consumer-facing version with examples. Add `README.md` to the package `files` list (npm includes it regardless, but be explicit).

### README and CONTRIBUTING links

- README Usage section: link the three docs ("Tile schema and formats", "Cartographic decisions", style package README).
- CONTRIBUTING: the bump rule (Part 1 step 5) plus a pointer to `docs/schema.md` from the Serving section.

## Order of work

1. `docs/schema.md` first — the contract doc gives version 1 a definition before the number exists anywhere.
2. Part 1 (config → manifest → worker → style → viewer → CONTRIBUTING), one PR. Tests: worker TileJSON tests assert the field; a style test asserts `SCHEMA` is exported and an integer.
3. `docs/cartography.md` and `style/README.md`, second PR — pure documentation, no code risk.

A schema field appears in served TileJSON only after the next release promotes a manifest that carries it; the worker fallback covers the gap.
