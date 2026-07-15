# Bathymetry tiling pipeline. Run recipes from the repo root; they execute in
# pipelines/ (via `set working-directory`). Stages: source -> aggregation (mosaic +
# vector forks) -> mosaic-index -> terrain (per-zoom render) -> bundle; then worker/
# serves planet + grid-cell overlays. See CONTRIBUTING.md.

set working-directory := 'pipelines'

# Prepare one source: fetch -> datum -> normalize -> bounds -> polygon -> tarball, then
# assemble catalog.json. The catalog step is the shared tail, so every source (prepared or
# mirrored) gets a generated catalog item without editing each recipe. RECIPE_HASH (the
# source's recipe content hash, supplied by the caller; empty locally) rides into the item.
source id:
    just ../sources/{{id}}/
    uv run python source_catalog.py {{id}}

# Prepare the OSM land mask once (download -> unzip -> EPSG:3857 FlatGeobuf at
# store/landmask/land.fgb). Flagged coarse sources (GEBCO/EMODnet) clamp negative
# land pixels against it during aggregation. LANDMASK overrides the output path.
landmask:
    uv run python landmask.py prep

# Prepare the inland-water mask once (Overture water theme -> EPSG:3857 FlatGeobuf at
# store/landmask/water.fgb). The land clamp subtracts it so flagged coarse sources keep
# their depths inside mapped rivers/lakes. Optional — absent, the clamp stays land-only.
# WATERMASK overrides the output path.
watermask:
    uv run python landmask.py prep-water

# Prepare every source under sources/.
sources:
    #!/usr/bin/env bash
    set -euo pipefail
    for id in $(uv run python -c "import config; print('\n'.join(config.sources()))"); do
        echo "── source: $id ──"
        just ../sources/"$id"/
    done

# Planet build: cover -> aggregate -> mosaic-index -> terrain -> bundle -> vector layers (BBOX="W,S,E,N"
# for a region). Soundings + depare bundle BEFORE contours: the contours tile-join folds their
# pmtiles into vector.pmtiles in the same single pass. Drying rides inside depare (a DEPARE band
# with negative drval1) — aggregation_run generates its per-tile FGB, which depare consumes.
# Coverage right after cover (it needs only footprints + the covering) so missing footprints
# fail in the first minute, not after hours of aggregation. Coverage honors BBOX, so a preview
# builds only the region's footprints (the build box builds the whole planet).
# The build box runs these same recipes one at a time, pushing to R2 between stages so an
# interrupted build resumes — see docs/build.md.
planet:
    just cover
    just coverage
    just aggregate
    just mosaic-index
    just terrain
    just bundle
    just soundings
    just depare
    just contours

# Plan the covering: slice the planet into aggregation tiles (BBOX="W,S,E,N" for a region).
cover:
    uv run python aggregation_covering.py

# Aggregate every stale tile: reproject -> merge -> smooth -> encode terrain, forking
# contours/soundings/depare off each merged DEM. Staleness is per-fork content-hash keys
# (keys.py: inputs ‖ code ‖ config ‖ toolchain); a fresh fork is skipped within the tile.
# AGG_PROCESSES caps concurrent tiles (each holds a multi-GB merged DEM); unset = all cores.
# FORCE_REBUILD=1 ignores the keys and rebuilds every tile (escape hatch).
aggregate:
    uv run python aggregation_run.py

# Write store/source-manifest.txt: the source files the dirty tiles reference — lets the
# build box hydrate exactly the dirty set from R2, then aggregate from local disk.
sources-manifest:
    uv run python aggregation_run.py sources-manifest

# Assemble the stage-2 mosaic index from the tile COGs `aggregate` persisted: the GeoParquet
# tile index (= manifest), the planet z8 overview COG, and the mosaic.gti pointer (written last).
# Run after `aggregate`. A bbox build produces a window-scoped GTI (QGIS-inspectable) but writes
# no planet-scoped pointer flip — that's a workflow concern (see mosaic._write_gti / 5b).
mosaic-index:
    uv run python mosaic.py index

# Stage 3 terrain: per-zoom Terrarium render from the MOSAIC (replaces the old 2x2-average
# downsample pyramid). Plan the render stems (native aggregation tiles + coalesced overview
# parents), then render each stale stem by reading the mosaic at that zoom's resolution ->
# depth/zoom-gated smooth -> Terrarium encode. Needs the mosaic GTI, so run AFTER mosaic-index.
# TERRAIN_PROCESSES caps concurrent stems (each holds a multi-GB window); unset = all cores.
terrain:
    uv run python terrain.py cover
    uv run python terrain.py run

# Concat the single-zoom pmtiles into planet.pmtiles + one overlay per populated
# OVERLAY_SPLIT_Z grid cell + manifest.json (groups bundled in parallel).
bundle:
    uv run python bundle.py

# Walk the local store and write store/manifests/<covering-id>.json — the content name / empty
# marker of every artifact this build's covering produced — printing the id. The workflow publishes
# it and flips the store pointer LAST (the next build hydrates exactly these; the GC keeps them).
# A bbox build never runs it: regional runs write no planet-scoped pointer. R2-agnostic — the id is
# the covering ULID, the names are store-root-relative, and this step knows nothing about buckets.
store-manifest:
    uv run python store_manifest.py write

# Contours, whole set: one global tippecanoe over every contour FGB, then one tile-join that
# also folds in the soundings/depare pmtiles already bundled → vector.pmtiles.
contours:
    uv run python contour_run.py bundle

# Soundings: bundle the per-tile points into soundings.pmtiles. Run BEFORE contours —
# their single tile-join folds this layer into vector.pmtiles (a separate fold re-joined
# the whole planet archive per layer).
soundings:
    uv run python soundings_run.py bundle

# Depth areas (ENC DEPARE): bundle the per-tile partitions into depare.pmtiles (folded into
# vector.pmtiles by the contours tile-join, same as soundings). Carries three feature kinds in
# one layer — depth bands, drying (negative drval1), and nodata (no drval1) — built per tile by
# aggregation_run off the merged DEM + the OSM land + inland-water polygons.
depare:
    uv run python depare_run.py bundle

# Source-provenance footprints -> their own store/bundle/coverage.pmtiles (z0-8;
# the renderer overzooms it deeper). Needs store/polygon/*.gpkg + a covering;
# fails loudly without footprints — a build without them ships a dead layer.
# Honors BBOX: a preview builds only the region's footprints (whole planet if unset).
coverage:
    uv run python contour_run.py coverage

# Build a regional preview into the local Worker R2 — a faithful slice of the planet
# build for BBOX="W,S,E,N" (default: NY harbor; e.g. Chesapeake = "-76.5,37.0,-76.0,37.5").
# Refreshes each source's tiny bounds.csv from R2 and range-reads the COGs from R2 (mirrored
# and prepared sources alike) via SOURCE_VSI_BASE — the same read path as CI's aggregate, so
# no local source prep and no upstream traffic. `just preview-local` instead builds from
# already-prepared sources in store/source (no R2; SOURCE_VSI_BASE unset). Override
# SOURCE_VSI_BASE/BOUNDS_BASE for a mirror.
# The coarse-source land clamp needs the land mask: streamed from R2 when published, else built
# locally once (a 700 MB OSM download; override with LANDMASK to reuse an existing copy). The
# inland-water mask (clamp subtraction, #24 inverse clamp, depare nodata) is streamed from R2
# when published, else built bbox-scoped from Overture in seconds (override with WATERMASK).
# View with `just dev` (tile Worker on :8787 + Vite viewer on :5173).
preview bbox="-74.30,40.40,-73.75,40.80" local="":
    #!/usr/bin/env bash
    set -euo pipefail
    export BBOX="{{bbox}}"
    if [ -z "{{local}}" ]; then
        export SOURCE_VSI_BASE="${SOURCE_VSI_BASE:-/vsicurl/https://data.openwaters.io/bathymetry/source}"
        BOUNDS_BASE="${BOUNDS_BASE:-https://data.openwaters.io/bathymetry/source}"
        POLY_BASE="${POLY_BASE:-${BOUNDS_BASE%/source}/polygon}"
        mkdir -p store/polygon
        for id in $(uv run python -c "import config; print('\n'.join(config.sources()))"); do
            mkdir -p "store/source/$id"
            # bounds.csv, catalog.json, and polygon all fetch from R2 only when absent locally
            # — a local copy (from local prep, or edited to iterate) always wins, mirroring
            # source_path preferring local COGs. Fetch to a temp and move on success so a 404
            # never leaves a truncated file. A source with neither a bounds.csv nor local COGs
            # is pruned.
            [ -f "store/source/$id/bounds.csv" ] \
                || { curl -fsS "$BOUNDS_BASE/$id/bounds.csv" -o "store/source/$id/bounds.csv.tmp" \
                        && mv "store/source/$id/bounds.csv.tmp" "store/source/$id/bounds.csv" \
                        || rm -f "store/source/$id/bounds.csv.tmp"; }
            [ -f "store/source/$id/bounds.csv" ] || ls "store/source/$id"/*.tif >/dev/null 2>&1 \
                || { echo "skip $id (no bounds.csv locally or in R2)"; rm -rf "store/source/$id"; continue; }
            # Catalog item (priority/offset/flags + recipe hash — the tile keys read it).
            # Absent (a source registered before the catalog step existed) falls back to
            # metadata.json — so local metadata.json knob edits (priority/max_zoom) now take
            # effect without deleting a fetched catalog.json.
            [ -f "store/source/$id/catalog.json" ] \
                || { curl -fsS "$BOUNDS_BASE/$id/catalog.json" -o "store/source/$id/catalog.json.tmp" \
                        && mv "store/source/$id/catalog.json.tmp" "store/source/$id/catalog.json" \
                        || rm -f "store/source/$id/catalog.json.tmp"; }
            # Provenance footprint for the coverage tileset (mirrored sources have none).
            [ -f "store/polygon/$id.gpkg" ] \
                || { curl -fsS "$POLY_BASE/$id.gpkg" -o "store/polygon/$id.gpkg.tmp" \
                        && mv "store/polygon/$id.gpkg.tmp" "store/polygon/$id.gpkg" \
                        || rm -f "store/polygon/$id.gpkg.tmp"; }
        done
        # Land mask for the coarse-source clamp: prefer a local copy if it's already there,
        # else stream it from R2 like the sources; if neither exists, it's built locally (below).
        r2mask="https://data.openwaters.io/bathymetry/landmask/land.fgb"
        if [ ! -f store/landmask/land.fgb ] && curl -fsI "$r2mask" >/dev/null 2>&1; then
            export LANDMASK="${LANDMASK:-/vsicurl/$r2mask}"
        else
            export LANDMASK="${LANDMASK:-store/landmask/land.fgb}"
        fi
        # Inland-water mask (the clamp subtraction, the #24 inverse clamp, and depare's nodata
        # areas all read it): stream from R2 when published, else build it locally SCOPED TO THE
        # PREVIEW BBOX — prep-water honors BBOX as a -spat prefilter, so it pulls only this window
        # (seconds), and it's rebuilt each run since it's region-specific. A WATERMASK override is
        # honored untouched; whole-planet R2 always wins over a stale regional local copy.
        r2water="https://data.openwaters.io/bathymetry/landmask/water.fgb"
        if [ -n "${WATERMASK:-}" ]; then
            :
        elif curl -fsI "$r2water" >/dev/null 2>&1; then
            export WATERMASK="/vsicurl/$r2water"
        else
            export WATERMASK="store/landmask/water.fgb"
            rm -f "$WATERMASK"
        fi
    else
        unset SOURCE_VSI_BASE  # read prepared COGs from store/source on disk
        export LANDMASK="${LANDMASK:-store/landmask/land.fgb}"
        # Offline preview: use a local water mask only if it's already there (prep-water needs
        # the network); absent, the clamp/nodata degrade to land-only.
        export WATERMASK="${WATERMASK:-store/landmask/water.fgb}"
    fi
    # Build each mask locally if we're pointed at a local path with no file — the land mask is a
    # one-time OSM download; the water mask is the bbox-scoped Overture pull (skipped offline).
    case "$LANDMASK" in /vsicurl/*) : ;; *) [ -f "$LANDMASK" ] || just landmask ;; esac
    case "${WATERMASK:-}" in /vsicurl/*|"") : ;; *) [ -f "$WATERMASK" ] || { [ -z "{{local}}" ] && just watermask || echo "no water mask (offline preview) — clamp/nodata degrade to land-only"; } ;; esac
    rm -rf store/aggregation store/pmtiles store/bundle store/meta store/contour store/soundings store/depare
    just planet
    ../worker/seed.sh

# Preview from already-prepared sources in store/source (no R2/network for prepared sources).
preview-local bbox="-74.30,40.40,-73.75,40.80": (preview bbox "local")

# Run both dev servers in one terminal: tile Worker on :8787 + Vite viewer on :5173
# (the viewer defaults to localhost:8787, so no VITE_TILES_BASE needed). Ctrl-C stops both.
# Works in the container too (`./docker.sh dev`): there the servers bind 0.0.0.0 so the
# published ports reach them; on the host they stay on localhost.
dev:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    # -x check, not -d: in the container node_modules is a (possibly empty) named volume.
    [ -x node_modules/.bin/vite ] || npm ci
    npm run dev -w worker -- --ip 0.0.0.0 &
    worker=$!
    # Kill only the worker we spawned — `kill 0` would TERM the whole process group,
    # including the parent `just`. Ctrl-C is the intended stop, so exit 0 keeps just
    # from reporting a failed recipe; the EXIT trap then reaps the worker.
    trap 'kill "$worker" 2>/dev/null || true' EXIT
    trap 'exit 0' INT TERM
    npm run dev -- --host

# The whole test suite.
test: test-sources test-engine test-workflows

# Offline self-checks (synthetic data, no network).
test-sources:
    uv run python test_source_stage.py
    uv run python source_mirror.py --check
    uv run python source_catalog.py --check
    uv run python source_remote.py
test-engine:
    uv run python test_engine.py
    uv run python aggregation_reproject.py --check
    uv run python keys.py --check
    uv run python mosaic.py --check
    uv run python terrain.py --check
    uv run python scheduler.py --check
    uv run python store_manifest.py --check

# Lint the GitHub Actions workflows.
test-workflows:
    cd "{{justfile_directory()}}" && actionlint

# Test the GC's Collect step (scripts/gc-collect.sh — the exact script gc.yml runs, local
# backend) against a synthetic store tree: happy path + every refusal guard. Needs bash + jq;
# ci.yml runs it on every push.
test-gc:
    bash test_gc.sh
