# Soundings chart conformance — planning doc

_Written 2026-08-17. Point-in-time; the code is the source of truth._

Status: in progress — Phases 1-4 implemented and committed on `soundings-chart-conformance`; Phase 5 (drying) blocked on the datum workstream.

## Problem

An audit of the sounding pipeline and style against S-4, S-52 and the S-57 encoding guide found eleven divergences. Datum correction and data-quality attribution are tracked elsewhere. The remaining nine are here.

The findings, numbered as the phases below cite them:

| # | Finding | Status |
|---|---|---|
| 1 | Decimetre labels printed from sources not on chart datum (MSL-referenced UK/NZ/NL/AU sources) | Out of scope — datum workstream |
| 2 | No data-quality attribution: GEBCO-derived and 0.25 m multibeam soundings render identically (`M_QUAL` is mandatory, S-57 UOC §2.2.3) | Out of scope — quality workstream |
| 3 | 1.0 m floor deleted the least depths S-4 B-410b ranks highest | Fixed — 0.2 m, from the measured waterline-noise knee |
| 4 | Only the shoalest pixel per cell is emitted, so a channel's deep line can never appear (B-410b also wants maximum depths; B-403.1a "the full range of depth") | Fixed — error-driven deep-side repair; the Ambrose corridor gains its 26–31 m line and axis infill with no channel-specific code |
| 5 | Metre precision bands cut at 6 m; S-4 B-412 says decimetres to 21 m, half-metres to 31 m | Fixed |
| 6 | Whole fathoms everywhere; charts print fathoms-and-feet below 11 fathoms | Fixed — derived from `depth_ft` in the style |
| 7 | No drying heights (S-4 B-413: shown as soundings, rounded up, underlined) | Blocked on finding 1 |
| 8 | Sounding density is uniform per cell — responds to depth (thin tiers) but not to seabed roughness (S-4 B-410d wants both) | Largely fixed — repair insertions land where interpolation fails, which is where structure is; the lattice itself stays uniform |
| 9 | Nothing verified S-4 B-410a's "final test of depth selection" (interpolation from the charted field) | Fixed — standing gate in `perf/soundings.py` |
| 10 | Zoom thinning graded by a sounding's own depth, not its significance (S-57 UOC table 2.5 grades SCAMIN by significance, 1→4 steps) | Largely answered — prime retention + ring-tied display; full grading only if the B-410a residual demands it |
| 11 | All soundings set upright — the posture S-4 B-412.4 reserves for *unreliable* soundings; B-412.1 wants sloping numerals | Fixed — Noto Sans Italic, self-hosted |

Two are already fixed on this branch and are waiting on a rebuild to take effect:

- Soundings were placed on a jittered lattice node rather than on the pixel their depth came from, so a quarter of them in a sampled New York view sat on land, displaced by up to a cell (~400 m measured). `_shoalest_grid` now carries the winning pixel's column/row, `_reduce_shoalest` carries it up each pyramid level, and `_pyramid` places the point there.
- Label typography: flat 18 px (S-52 §5.2.1(2) sizes a sounding digit at ~3.5 mm; §3.1.5 forbids shrinking text when zooming out) and a 0.6 decimetre subscript.

## Goals / Non-goals

Goal: bring the emitted sounding field and its labels in line with the cited standards, at a rebuild cost proportional to the correctness gained.

Non-goals: vertical datum correction and CATZOC/`M_QUAL` attribution (tracked separately); ENC conformance as a claim — this is a derived product built from a DEM, and the standards library is explicit that a synthetic field is _background_ sounding selection, not chart-authentic prime selection.

## What drives the phasing

A soundings rebuild is the expensive unit, so phases are cut to minimize how many are needed, and every change that requires one is batched into the same rebuild. The true-position fix already forces one, so the cheap correctness fixes ride along with it rather than costing a second.

Measurement comes before redesign. S-4 §B-410a supplies a testable acceptance criterion, so the harness that checks it is built first and the results scope the redesign, rather than rebuilding an algorithm on a guess.

`pipelines/perf/` already has the shape this needs: `bench.py` for staged runs against fixture stems, `gates.py` for hard pass/fail assertions on a generalization change, `metrics.py` for the measurements. New checks land there.

## Phase 1 — measurement harness

No rebuild. Runs locally on perf fixtures.

S-4 §B-410a gives the acceptance test directly:

> "The final test of depth selection is that no source material should contain depths shoaler than the mariner would expect by interpolating the depth in any position from the charted soundings and depth contours."

Implementing that test is far cheaper than implementing the triangular selection method it validates, and it tells us which of the Phase 3 items actually need work.

1. **Interpolation gate** in `perf/gates.py`: given a stem's DEM window, its emitted soundings and its contours, interpolate expected depth across the cell and report every source pixel shoaler than that expectation, by magnitude. This is the finding-9 test.
2. **Waterline-noise measurement** for finding 3: re-run a coastal fixture at candidate `SOUND_MIN_DEPTH_M` values (1.0, 0.5, 0.3, 0.2, 0.0) and record how many new soundings are real shoals versus land-clamp edge artifacts. Picks the floor with evidence instead of keeping 1.0 by default.
3. **Density and clutter stats** for finding 8: nearest-neighbour spacing distribution per zoom against the widely-quoted NOAA practice (critical ≥6 mm, supporting ≥10 mm, fill 15–30 mm at chart scale — second-hand via Skopeliti 2020, not citable as a standard, so a target not a gate), plus label-collision rate from the style.

Exit criterion: a measured baseline for the current algorithm on at least one shoal-rich and one deep fixture.

### Results so far

Harness is `pipelines/perf/soundings.py`, wired into `just test-perf`. Its self-check proves B-410a fires on a hidden pinnacle (~18 m shortfall), clears when that pinnacle is charted or ringed by an isobath, and that a sounding which does not display at a zoom does not count as charted there.

**Spacing never reaches critical density, and varies only with depth.** Three published stems, nearest-neighbour median in mm at chart scale:

| stem         | z8   | z10  | z12  | z14   |
| ------------ | ---- | ---- | ---- | ----- |
| `8-73-99-14` | 15.6 | 14.5 | 14.2 | 14.0  |
| `8-75-96-14` | 14.4 | 14.1 | 14.0 | 13.9  |
| `6-35-18-10` | 14.1 | 14.0 | 55.9 | 223.6 |

Flat ~14 mm on the supporting/fill boundary, never at critical (6 mm). The deep control below reads 30–51 mm, so `SOUND_THIN_TIERS` does coarsen the field with depth as designed — the gap in finding 8 is narrower than first stated: spacing responds to _depth_, but not to _roughness_, and S-4 §B-410d asks for both.

**New finding: coarse-source regions starve when you zoom in.** `6-35-18-10` is a `child_z = 10` stem. Its sounding count freezes at 9,438 past z10 (the finest pyramid level rides uncapped, `_tc`), so spacing blows out to 224 mm at z14 — soundings roughly 22 cm apart on screen. Correct per the current design, but it means the areas with the weakest source data present as the emptiest chart exactly when a mariner zooms in for detail. Worth its own decision: either densify from the same data, or say something explicit about coverage.

**B-410a on real bathymetry** — the `terrebonne` z12 marsh fixture, 216 points emitted:

| zoom | charted | soundings only              | with contours               |
| ---- | ------- | --------------------------- | --------------------------- |
| z12  | 16      | 75.1%, max 1.64 m, p99 1.11 | 86.5%, max 2.11 m, p99 1.90 |
| z13  | 30      | 74.9%, max 2.02 m, p99 1.26 | 85.3%, max 2.11 m, p99 1.90 |
| z14  | 56      | 76.1%, max 2.03 m, p99 1.26 | 83.3%, max 2.11 m, p99 1.90 |
| z15  | 105     | 76.6%, max 2.03 m, p99 1.32 | 82.9%, max 2.11 m, p99 1.90 |

Two things stand out, and neither is the one expected.

**The violation rate is flat across zoom.** Quadrupling the sounding count from z12 to z15 barely moves it. The misses are therefore not a thinning problem — significance grading (finding 10) would not have fixed them.

**Adding contours makes it worse**, from ~75% to ~85%. That is backwards on its face: contours are more charted information. The mechanism turned out to be specific and is now its own check.

### The ring finding

A closed contour with no sounding on it is a charted shoal with no charted least depth. A mariner interpolating across the ring reads the ring's own level, so the shoal is understated by however much shoaler the ground really is — which is why adding contours raised the violation rate rather than lowering it. S-4 §B-410b puts "least depths over shoals, banks and sills" at the very top of what must survive selection, so this is the clearest conformance failure in the audit, and the most actionable: the fix is a rule, not a tuning parameter.

Two corrections to the first pass at measuring it, both found while implementing the fix:

- **Not every closed isobath is a shoal ring.** In marsh where most ground is shallower than 2 m, a closed 2 m contour usually encloses a _deep pocket_. Only a ring whose interior rises above its own level owes a least depth. Of the fixture's 15 closed rings, **4** are shoal rings.
- **The first gate sampled each ring's bounding box, not the ring.** For a small ring in shoal ground the box is mostly water outside it, which read as an understated shoal that was not there. That inflated the original "14 of 15 understating by up to 2.38 m". The gate now rasterizes the polygon, and carries a regression test built on a deep pocket whose bounding box would trip the old code.

Corrected baseline: **4 of 4 shoal rings bare at z12**, worst understatement 1.30 m.

This lands as `soundings.py rings`, with the self-check proving it fires on a bare shoal ring, stays quiet once a sounding is inside, ignores a ring sitting exactly on its own level, and ignores a deep pocket.

### What this does to Phase 3

The ordering changes. Emitting the least depth inside every closed isobath is now the first piece of work, ahead of density tuning — it is a bounded, rule-shaped change with a direct citation, and it addresses the largest measured gap. Finding 10 (significance grading) drops down the list, since the flat-across-zoom result says thinning is not what is hurting.

### The deep control

`iberian-abyssal` is a new fixture site (`8-110-91-10`, −24.61/45.58, GEBCO-only, cz10 built at z8) added because every other site is Gulf marsh or Delmarva lagoon. Ground: 1358–3937 m, median 3098 m, standard deviation 215 m — flat, deep, no land at all.

| zoom | charted | violating | max shortfall | p99    | spacing |
| ---- | ------- | --------- | ------------- | ------ | ------- |
| z8   | 9       | 0.00%     | −0.62 m       | −365 m | 51.0 mm |
| z9   | 25      | 0.02%     | 54 m          | −98 m  | 29.5 mm |
| z10  | 81      | 1.11%     | 271 m         | 3.9 m  | 31.4 mm |

**The algorithm passes where it should.** Negative shortfall means the charted field reads shoaler than the ground — conservative, the safe direction — and at z8 the p99 is −365 m, i.e. strongly shoal-biased throughout. This is the control that makes the marsh numbers interpretable: ~75–85% violation is a property of that terrain, not a systemic defect in the selection.

Two secondary observations. Violations _rise_ with zoom here (0% → 1.11%), the opposite of intuition: coarse levels are thinned to the shoalest by `SOUND_THIN_TIERS`, so they are heavily shoal-biased, while the finest level charts deeper points too and can overshoot locally. And the 271 m worst case at z10 is ~9% of a 3000 m seabed — proportionally small, and in water where it carries no navigational consequence.

With both ends measured, Phase 3's scope is settled: the work is in shoal, structured terrain, and the ring rule is the first piece.

## Phase 2 — the batched rebuild

Status: **implemented, awaiting a rebuild to take effect.** One rebuild carries all of it.

- **True position.**
- **Finding 5 — metre bands.** S-4 §B-412 has three: decimetres to 21 m, half-metres 21–31 m, whole metres above; `_depths` cut at 6 m. The half-metre band renders for free — a `.5` residual prints as `23₅`.
- **Finding 6 — fathoms and feet.** Charts print fathoms _and feet_ up to 11 fathoms, fathoms only beyond (Canada CHS Chart 1 2022; US Chart No. 1 §I), so at 3 fathoms we printed `3` where a chart prints `3₄`. A fathom is exactly six feet, so the viewer derives both digits from `depth_ft` (`floor(ft / 6)` and `ft % 6`). **No schema change and no new field.** The first attempt put feet in `depth_fm`'s tenths slot, which changed the meaning of a published field — forcing a schema bump and a package major — and left a mixed-radix number where `3.4` did not mean 3.4 fathoms. Deriving it in the style is free and unambiguous.
- **Finding 3 — shoal floor at 0.2 m.** Measured across the four Gulf marsh fixtures: admitted cells grow smoothly from 1.0 m down to 0.1 m, then jump 2–5× at the 0.1 → 0.0 step, which is the waterline-noise regime. 0.2 m clears it with headroom. The old floor's justification ("cells just read 0") no longer holds now that decimetres print — those read `0₂`.

No separator goes between the number and its sub-unit digit. S-4 §B-412.1 offers two forms, not a combination: decimetres set subscript, _or_ on the same baseline where "they must be separated by a comma, full stop or decimal point". A point under the subscript form is neither, and on the fathom ladder it would misread 3 fathoms 4 feet as 3.4 fathoms.

A latent bug surfaced here: `floor(0.3 * 10)` evaluates to 2, so 0.3 m charted as 0.2 m — shoal-safe, but wrong by a decimetre, and the same class of error made 3 fathoms 4 feet compute as 3 feet. Every unit conversion now floors through one epsilon helper.

## Phase 3 — selection redesign

Scoped by Phase 1's results: only the parts measured to fail get built.

### The ring rule — implemented

`_enclosed_shoals` emits the least depth inside every closed isobath as a **prime** sounding.

A closed isobath at level L is exactly the boundary of a connected component of `{depth < L}` that does not reach the window edge, so this reads off the DEM at the same `config.CONTOUR_LEVELS` the contours are cut at. No contour file is involved, which keeps soundings a sibling of the contour stage rather than making it a child and serialising the DAG. Levels are bounded to the navigational band (200 m): past it an enclosed rise is a seabed feature, not a least depth a vessel can touch, and each level costs a labelling pass.

Prime soundings are never zoom-capped. B-410b's "must always be shown" does not stop applying when the mariner zooms out, and the graduated SCAMIN policy puts the most significant soundings at the top of the ladder (S-57 App B.1 Annex A UOC, Ed 4.4.0, table 2.5) — so finding 10's significance grading is now answered for the class where it matters most, without a general redesign.

Measured on the `terrebonne` marsh fixture, with the corrected gate:

**Prime gets no ink of its own.** Emphasis-by-prime was tried and reverted: `prime` means "this feature's isobath closes inside the build window", which is right for retention and collision priority but is not a hazard ranking — a coastal shelf whose contour closes beyond the window edge is never prime, while a 0.5 m wrinkle at 29 m is, so black type on prime read exactly backwards (a black 19.8 m bank beside a grey 9 m shelf). Soundings render in one colour, paper-chart style (S-4 sets one style for all soundings; type distinctions mean reliability, B-412.4); hazard stays in the tint and the isobaths. A hazard-shaped emphasis needs scale-appropriate selection (finding 8), not a new paint rule.

**Prime displays exactly where its enclosing isobath displays.** A prime's minzoom is its deepest enclosing ring's display zoom, read off `CONTOUR_TIERS` (the table that already thins isobath levels by zoom). B-410b's "must always be shown" is a claim about the charted feature: once generalization drops the ring at a scale, the chart makes no claim there for the sounding to complete — and promoting primes past their rings is what put decimetre least depths over the generalized coastline of coarse views. This replaces both the unbounded promotion and a pixel-buffer land-clearance filter that was prototyped and measured (flooring the whole lattice cost half the marsh fixture's field, B-410a 35% → 78%): the standards' mechanism for labels-too-close-to-land is scale selection, not a mask, and the ring tie is scale selection.

## Follow-ups (out of scope)

- **Feature-semantic generalization at low zoom.** The pipeline generalizes surfaces (per-zoom quantization, depth-gated smoothing, zoom-tiered contour levels, simplification) but never deletes, merges or exaggerates whole features by significance at scale — the islet/pit/peak tests, sub-legible ring deletion under the shoal-bias constraint (a deep pocket may be dropped; a shoal must merge into surrounding shallow water or be exaggerated, never vanish). The deferred ring-drop item from the contour-noise work belongs here, and `perf/gates.py`'s shoal-bias and displacement gates already exist to police it. Contour/depare scope, not soundings.

|        | points | shoal rings bare | worst understatement | B-410a z12         | B-410a z15         |
| ------ | ------ | ---------------- | -------------------- | ------------------ | ------------------ |
| before | 207    | 4 / 4            | 1.30 m               | 86.5% (p99 1.90 m) | 82.9% (p99 1.90 m) |
| after  | 1245   | 1 / 4            | 0.07 m               | 34.8% (p99 1.75 m) | 24.3% (p99 1.33 m) |

The remaining bare ring understates by 0.07 m — the contour polygon is generalized, so its interior and the DEM component disagree slightly at the margin. Not worth chasing.

The deep control is byte-identical (115 points, same violation rates): nothing in the abyss is enclosed shoaler than 200 m, so no prime soundings are minted there. Cost is ~2.6 s added on a 4226 px window, ~0.3 s on 2178 px.

### Still open

- **Finding 4 — deep soundings.** Every cell contributes its shoalest pixel and nothing else, so a channel's deep line can never appear. S-4 §B-410b requires maximum depths too, and §B-403.1a wants "sufficient numbers of deeper soundings retained to show the full range of depth" for echo-sounder fixing and anchorage choice.
- **Finding 8 — density by seabed character.** S-4 §B-410d: flat ground wants a minimum of evenly spaced soundings, "irregular bottom topography should be represented by a denser, and probably irregular, pattern". One global `SOUND_CELL_PX` cannot do that; modulate acceptance by local depth variance. The true-position fix already gets the "irregular" half.
- **Finding 9** is a standing gate rather than a work item: the B-410a test runs on every generalization change, like the existing shoal-bias gates.
- **Finding 10** is largely answered by prime soundings; a full prime/supporting/fill classification is only worth building if the remaining B-410a residual demands it.

The residual after the ring rule is ~25–35% of sampled marsh pixels, still shoaler than interpolation implies by up to ~2 m. Findings 4 and 8 are what remain to attack it, and both rewrite the same selection pass, so they are one design change rather than two.

## Phase 4 — typography and a hosted font stack

No rebuild; independent of everything above and can run in parallel.

**Finding 11 — sloping numerals.** S-4 §B-412.1 wants sloping sans-serif; upright is reserved for unreliable soundings (§B-412.4), so we currently render every sounding in the "do not trust this" posture. Getting an italic face means self-hosting glyphs.

The style currently defaults to `demotiles.maplibre.org`, which is a demo subset — it ships only ₂ ₃ ₄ of the Unicode subscript block, which is why the decimetre digit is a scaled `format` section rather than a subscript character. It is also a third-party host in the serving path of a navigation product.

### Where it lives

A separate repo, not `seascape/`:

- The output is shared. Every Open Waters map on `tiles.openwaters.io` wants the same stack; baking it into the bathymetry repo makes one product the owner of a common asset.
- Release cadence is opposite. The seascape Worker keys everything to `RELEASE_PREFIX` (a per-build R2 prefix). Fonts must be release-independent and effectively immutable — coupling them to bathymetry builds is exactly wrong.
- No toolchain overlap. Glyph prep is a Rust/Node job with nothing in common with the Python/GDAL/Snakemake pipeline, and seascape's CI is already heavy.
- Licensing is self-contained: the OFL text ships beside the artifacts.
- Should include GitHub Actions to auto deploy

`openwaters/tile-fonts`, scoped to fonts and glyphs. Sprites are a separate problem with a separate toolchain; when Phase 5's drying underline or the seamark symbol set needs one, that gets its own home rather than being pre-empted here.

### Shape — built

`openwaters/tile-fonts`, following the [protomaps/basemaps-assets](https://github.com/protomaps/basemaps-assets) precedent. Built and verified locally; not yet pushed to a remote.

- Glyphs are cut by Stadia's [`build_pbf_glyphs`](https://github.com/stadiamaps/sdf_font_tools) rather than MapLibre's `font-maker`: both wrap the same `sdf-glyph-foundry`, but `build_pbf_glyphs` ships prebuilt binaries and a `cargo install`, where `font-maker` needs a cmake build from source. Its macOS release asset is named `x86_64` but is a native arm64 binary.
- A fontstack is composed from many per-script Noto TTFs, so worldwide coverage is a font list rather than a code problem. Sourced from the `notofonts.github.io` monthly release tags (`noto-monthly-release-YYYY.MM.01`), pinned in `fonts.json`.
- Faces: **Noto Sans Regular** (keeps the name the style already uses, and becomes the S-4 §B-412.4 upright posture for low-reliability soundings), **Noto Sans Italic** (the §B-412.1 sloping default for soundings), **Noto Sans Medium** (emphasis). All SIL OFL, redistributed with the licence.
- Output is `fonts/<Fontstack Name>/<start>-<end>.pbf`, published to the existing `tiles` R2 bucket under a `fonts/` prefix.

Built from the 2026.08.01 release: Regular 12,225 glyphs / 50 faces, Medium 6,530 / 20, Italic 3,278 / 4, 256 ranges each. **Subscript digits U+2080–2089 are 10 of 10 present**, against 3 of 10 on the demotiles stack the style currently uses — which is the gap that forced the decimetre digit to be a scaled `format` section instead of a subscript character.

Two things only running it revealed, both now fixed: Tibetan is not in the monolithic Noto repo in any variant (a stack with a silent hole renders as tofu, so a missing source is fatal and every path is resolved before any glyph is cut), and the coverage checker required U+2072/U+2073, which are unassigned in Unicode and produced a false failure on a good stack.

Serving needs no Worker. Glyph PBFs are immutable static files, and an R2 bucket on a custom domain covers all three requirements natively:

- **Caching** — Cloudflare caches from an R2 custom domain off each object's own `Cache-Control`, so `public, max-age=31536000, immutable` set at upload is the whole story.
- **CORS** — configured on the bucket, or as a response-header rule on the hostname.
- **TLS and CDN** — come with the custom domain.

So the deliverable is a build script plus an upload, not a service. The one thing to verify against the account's actual DNS is whether `tiles.openwaters.io` can carry an R2 custom domain alongside the existing `tiles.openwaters.io/seascape/*` Worker route — more specific Worker routes take precedence over a custom domain, so the two should coexist, but if that turns out awkward a dedicated hostname is the boring alternative and costs nothing.

Either way, font serving never rides a bathymetry deploy, which is the property that matters.

Then in `style/`: `DEFAULT_GLYPHS` points at the new host, and `Flavor.font` gains the italic face for soundings while `source-labels` stays upright.

Verification: assert the subscript block (U+2080–2089) is present in the built stack, since that absence is what forced the current workaround.

## Phase 5 — drying heights

Gated on the datum workstream; needs Phase 4 first.

**Finding 7.** `_shoalest_grid` selects `block < 0`, so nothing at or above datum becomes a sounding. S-4 §B-413 requires drying soundings shown as drying heights, rounded **up** (the opposite of the truncation everywhere else), with the metre numerals underlined. A drying height on an uncorrected MSL grid is meaningless, hence the datum gate; the underline needs a sprite, since `format` cannot draw one.

## Risks

The Phase 3 redesign changes what a mariner sees at every zoom. The existing `perf/gates.py` shoal-bias assertions plus the new interpolation gate are the guard — a failure there is a defect, not a threshold to widen.

Emitting deep soundings (finding 4) is the one change that can _reduce_ apparent shoal bias in a rendered view by competing for label space with shoal soundings. `symbol-sort-key` already sorts shoalest-first so collisions resolve toward safety, but the collision rate from Phase 1 should be re-measured after.
