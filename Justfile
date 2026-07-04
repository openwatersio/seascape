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

# Prepare every source under sources/.
sources:
    #!/usr/bin/env bash
    set -euo pipefail
    for id in $(uv run python -c "import config; print('\n'.join(config.sources()))"); do
        echo "── source: $id ──"
        just ../sources/"$id"/
    done

# Planet build: cover -> aggregate -> downsample -> bundle -> contours (BBOX="W,S,E,N" for a region).
planet:
    just cover
    uv run python aggregation_run.py
    just combine
    just contours
    just soundings
    just drying

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

# Contours, whole set (local/regional). CI shards these across runners — see below.
contours:
    uv run python contour_run.py bundle

# Soundings: bundle the per-tile points, then fold them into vector.pmtiles (one vector source).
soundings:
    uv run python soundings_run.py bundle
    uv run python soundings_run.py fold

# Drying areas (green foreshore): bundle the per-tile polygons, then fold into vector.pmtiles.
drying:
    uv run python drying_run.py bundle
    uv run python drying_run.py fold

# tippecanoe this shard's local FGBs -> contours-shard-{i}.pmtiles (CI pulls only the
# shard's slice + writes store/contour-maxz.txt; merged by contour-merge).
contour-shard i:
    uv run python contour_run.py bundle-shard {{i}}

# tile-join the per-shard contour pmtiles into vector.pmtiles.
contour-merge:
    uv run python contour_run.py bundle-merge

# Build a regional preview into the local Worker R2 — a faithful slice of the planet
# build for BBOX="W,S,E,N" (default: NY harbor; e.g. Chesapeake = "-76.5,37.0,-76.0,37.5").
# Refreshes each source's tiny bounds.csv from R2 and range-reads the COGs from R2 (prepared
# sources) / NOAA (streaming) via SOURCE_VSI_BASE — the same read path as CI's aggregate, so
# no local source prep. `just preview-local` instead builds from already-prepared sources in
# store/source (no R2; SOURCE_VSI_BASE unset). Override SOURCE_VSI_BASE/BOUNDS_BASE for a mirror.
# The coarse-source land clamp needs the land mask: streamed from R2 when published, else built
# locally once (a 700 MB OSM download; override with LANDMASK to reuse an existing copy).
# View with the two dev servers in separate terminals:
#   cd worker && npm install && npm run dev               # tile Worker on :8787
#   VITE_TILES_BASE=http://localhost:8787 npm run dev     # Vite on :5173 (repo root)
preview bbox="-74.30,40.40,-73.75,40.80" local="":
    #!/usr/bin/env bash
    set -euo pipefail
    export BBOX="{{bbox}}"
    if [ -z "{{local}}" ]; then
        export SOURCE_VSI_BASE="${SOURCE_VSI_BASE:-/vsicurl/https://data.openwaters.io/bathymetry/source}"
        BOUNDS_BASE="${BOUNDS_BASE:-https://data.openwaters.io/bathymetry/source}"
        for id in $(uv run python -c "import config; print('\n'.join(config.sources()))"); do
            mkdir -p "store/source/$id"
            curl -fsS "$BOUNDS_BASE/$id/bounds.csv" -o "store/source/$id/bounds.csv" \
                || { echo "skip $id (no bounds.csv in R2)"; rm -rf "store/source/$id"; }
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
    else
        unset SOURCE_VSI_BASE  # read prepared COGs from store/source on disk
        export LANDMASK="${LANDMASK:-store/landmask/land.fgb}"
    fi
    # Build the mask locally (one-time OSM download) if we're pointed at a local path with no file.
    case "$LANDMASK" in /vsicurl/*) : ;; *) [ -f "$LANDMASK" ] || just landmask ;; esac
    rm -rf store/aggregation store/pmtiles store/bundle store/meta store/contour store/soundings store/drying
    just planet
    ../worker/seed.sh

# Preview from already-prepared sources in store/source (no R2/network for prepared sources).
preview-local bbox="-74.30,40.40,-73.75,40.80": (preview bbox "local")

# Offline self-checks (synthetic data, no network).
test-sources:
    uv run python test_source_stage.py
    uv run python source_register_remote_geopkg.py --check
test-engine:
    uv run python test_engine.py
    uv run python aggregation_reproject.py --check
