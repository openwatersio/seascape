# Bathymetry tiling pipeline. Run recipes from the repo root; they execute in
# pipelines/ (via `set working-directory`). Stages: source -> aggregation ->
# downsampling -> bundle; then worker/ serves planet + per-source overlays.
# See CONTRIBUTING.md.

set working-directory := 'pipelines'

# Prepare one source: fetch -> datum -> normalize -> bounds -> polygon -> tarball.
source id:
    just ../sources/{{id}}/

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

# Plan the covering: slice the planet into aggregation tiles (BBOX="W,S,E,N" for a region).
cover:
    uv run python aggregation_covering.py

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

# Contours, whole set (local/regional). CI shards these across runners — see below.
contours:
    uv run python contour_run.py bundle

# Print the CI contour-shard matrix as JSON (<= max shards, sized to the FGB count).
contour-matrix max:
    @uv run python contour_run.py bundle-matrix {{max}}

# tippecanoe one contour shard i of n (strided FGB slice -> contours-shard-{i}.pmtiles).
contour-shard i n:
    uv run python contour_run.py bundle-shard {{i}} {{n}}

# tile-join the per-shard contour pmtiles into contours.pmtiles.
contour-merge:
    uv run python contour_run.py bundle-merge

# Build the NY-harbor demo (GEBCO base + CUDEM) and seed it into the local Worker
# R2. Requires the GEBCO grid extracted in data/ (clips it locally; no 4 GB fetch).
# To view, run the two dev servers in separate terminals:
#   cd worker && npm install && npm run dev               # tile Worker on :8787
#   VITE_TILES_BASE=http://localhost:8787 npm run dev     # Vite on :5173 (repo root)
preview:
    #!/usr/bin/env bash
    set -euo pipefail
    rm -rf store/aggregation store/pmtiles store/bundle store/meta store/contour
    mkdir -p store/source/gebco
    gdal_translate -q -projwin -74.30 40.80 -73.75 40.40 \
      ../data/gebco_2026_n90.0_s0.0_w-90.0_e0.0_geotiff.tif store/source/gebco/gebco_0.tif
    uv run python source_bounds.py gebco
    [ -f store/source/cudem_ne/bounds.csv ] || just ../sources/cudem_ne/
    BBOX="-74.30,40.40,-73.75,40.80" just planet
    ../worker/seed.sh

# Offline self-checks (synthetic data, no network).
test-sources:
    uv run python test_source_stage.py
test-engine:
    uv run python test_engine.py
