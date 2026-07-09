# Bathymetry tiling pipeline. Run recipes from the repo root; they execute in
# pipelines/ (via `set working-directory`). Stages: source -> aggregation ->
# downsampling -> bundle; then worker/ serves planet + grid-cell overlays.
# See CONTRIBUTING.md.

set working-directory := 'pipelines'

# Prepare one source: fetch -> datum -> normalize -> bounds -> polygon -> tarball.
source id:
    just ../sources/{{id}}/

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

# Planet build: cover -> aggregate -> downsample -> bundle -> vector layers (BBOX="W,S,E,N"
# for a region). Soundings + depare bundle BEFORE contours: the contours tile-join folds their
# pmtiles into vector.pmtiles in the same single pass. Drying rides inside depare (a DEPARE band
# with negative drval1) — aggregation_run generates its per-tile FGB, which depare consumes.
# Coverage right after cover (it needs only footprints + the covering) so missing footprints
# fail in the first minute, not after hours of aggregation.
planet:
    just cover
    just coverage
    uv run python aggregation_run.py
    just combine
    just soundings
    just depare
    just contours

# Plan the covering: slice the planet into aggregation tiles (BBOX="W,S,E,N" for a region).
cover:
    uv run python aggregation_covering.py

# Freeze the aggregate + downsample dirty work lists into the covering dir (plan job, after
# cover, before the covering tarball is pushed). Every shard then reads the SAME list from the
# covering instead of re-listing R2 itself — one partition, computed once, so no self-heal tile
# slips between shards whose listings drifted across matrix waves.
freeze-work:
    uv run python aggregation_run.py freeze
    uv run python downsampling.py freeze

# Run one aggregate shard i of n — the CI fan-out unit (`aggregation_run.py` alone = all dirty).
aggregate i n:
    uv run python aggregation_run.py shard {{i}} {{n}}

# Print the CI aggregate shard matrix as JSON (<= max shards, sized to the dirt).
shard-matrix max:
    @uv run python aggregation_run.py matrix {{max}}

# Single-runner terrain finish: overview pyramid -> planet/overlay bundles + manifest.
combine:
    uv run python downsampling.py cover
    uv run python downsampling.py run
    uv run python bundle.py

# CI downsampling fan-out (deep levels shard by ancestor, coarse tail on the bundler):
#   plan job   -> just downsample-cover   (writes -downsampling.csv beside the covering)
#   plan job   -> just downsample-matrix N (the shard matrix, sized to the dirt)
#   matrix job -> just downsample-shard-keys i n  (pick this shard's pmtiles to pull)
#   matrix job -> just downsample-shard i n
#   plan  job  -> just downsample-tail + bundle-matrix   (coarse tail, then the matrix)
downsample-cover:
    uv run python downsampling.py cover
downsample-matrix max:
    @uv run python downsampling.py matrix {{max}}
# Filter store/pmtiles-keys.txt -> store/shard-keys.txt (only the tiles shard i reads),
# so CI pulls a shard's slice of store/pmtiles instead of the whole tens-of-GB store.
downsample-shard-keys i n:
    uv run python downsampling.py shard-keys {{i}} {{n}}
downsample-shard i n:
    uv run python downsampling.py run shard {{i}} {{n}}
downsample-tail:
    uv run python downsampling.py run tail

# CI terrain bundle fan-out (planet + one overlay per OVERLAY_SPLIT_Z grid cell; each
# matrix job loops its chunk of groups pull->bundle->push->clean one group at a time, so
# a runner's disk is bounded by ONE group's tiles + output no matter how many sources land):
#   plan job   -> just downsample-tail + bundle-matrix N (tail, verify, emit chunk matrix)
#   matrix job -> just bundle-group-keys <name>          (pick this group's pmtiles to pull)
#   matrix job -> just bundle-group <name>               (bundle one group + its fragment)
#   merge job  -> just bundle-merge                      (fragments -> manifest.json)
bundle-matrix max:
    @uv run python bundle.py matrix {{max}}
bundle-group-keys name:
    uv run python bundle.py group-keys {{name}}
bundle-group name:
    uv run python bundle.py group {{name}}
bundle-merge:
    uv run python bundle.py merge

# Contours, whole set (local/regional); the final tile-join also folds in any
# soundings/depare pmtiles already bundled. CI shards these across runners — see below.
contours:
    uv run python contour_run.py bundle

# Soundings: bundle the per-tile points into soundings.pmtiles. Run BEFORE the contours
# bundle/merge — its single tile-join folds the layer into vector.pmtiles (a separate
# fold re-joined the whole planet archive per layer).
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
coverage:
    uv run python contour_run.py coverage

# tippecanoe this shard's local slice of every vector layer -> {contours,soundings,
# depare}-shard-{i}.pmtiles (CI pulls only the shard's slices + writes
# store/contour-maxz.txt so all layers tile to one depth; merged by contour-merge).
# Three invocations, not one -L run: the layers need different tippecanoe flags
# (soundings -r1, depare --detect-shared-borders, contours' per-zoom filter).
vector-shard i:
    uv run python contour_run.py bundle-shard {{i}}
    uv run python soundings_run.py bundle-shard {{i}}
    uv run python depare_run.py bundle-shard {{i}}

# tile-join the per-shard pmtiles (all layers) into vector.pmtiles.
contour-merge:
    uv run python contour_run.py bundle-merge

# Build a regional preview into the local Worker R2 — a faithful slice of the planet
# build for BBOX="W,S,E,N" (default: NY harbor; e.g. Chesapeake = "-76.5,37.0,-76.0,37.5").
# Refreshes each source's tiny bounds.csv from R2 and range-reads the COGs from R2 (prepared
# sources) / NOAA (streaming) via SOURCE_VSI_BASE — the same read path as CI's aggregate, so
# no local source prep. `just preview-local` instead builds from already-prepared sources in
# store/source (no R2; SOURCE_VSI_BASE unset). Override SOURCE_VSI_BASE/BOUNDS_BASE for a mirror.
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
            curl -fsS "$BOUNDS_BASE/$id/bounds.csv" -o "store/source/$id/bounds.csv" \
                || { echo "skip $id (no bounds.csv in R2)"; rm -rf "store/source/$id"; }
            # Provenance footprint for the coverage tileset (streaming sources have none).
            # Replace only on success — a 404 must not clobber a locally-prepared polygon.
            curl -fsS "$POLY_BASE/$id.gpkg" -o "store/polygon/$id.gpkg.tmp" \
                && mv "store/polygon/$id.gpkg.tmp" "store/polygon/$id.gpkg" \
                || rm -f "store/polygon/$id.gpkg.tmp"
        done
        # S-102 re-registers from a live catalog (and may not be in R2 yet), so re-sync it
        just source noaa_s102
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

# Offline self-checks (synthetic data, no network).
test-sources:
    uv run python test_source_stage.py
    uv run python source_register_remote_geopkg.py --check
test-engine:
    uv run python test_engine.py
    uv run python aggregation_reproject.py --check
