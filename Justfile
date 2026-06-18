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

# CI downsampling fan-out (deep levels shard by ancestor, coarse tail on the bundler):
#   plan job   -> just downsample-cover   (writes -downsampling.csv beside the covering)
#   plan job   -> just downsample-matrix N (the shard matrix, sized to the dirt)
#   matrix job -> just downsample-shard-keys i n  (pick this shard's pmtiles to pull)
#   matrix job -> just downsample-shard i n
#   bundle job -> just downsample-tail-bundle
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
downsample-tail-bundle:
    uv run python downsampling.py run tail
    uv run python bundle.py

# Contours, whole set (local/regional). CI shards these across runners — see below.
contours:
    uv run python contour_run.py bundle

# tippecanoe this shard's local FGBs -> contours-shard-{i}.pmtiles (CI pulls only the
# shard's slice + writes store/contour-maxz.txt; merged by contour-merge).
contour-shard i:
    uv run python contour_run.py bundle-shard {{i}}

# tile-join the per-shard contour pmtiles into contours.pmtiles.
contour-merge:
    uv run python contour_run.py bundle-merge

# Build a regional preview into the local Worker R2: GEBCO base + the US streaming
# sources (CUDEM, BlueTopo) clipped to BBOX, best-wins. BBOX="W,S,E,N" (default: NY
# harbor; e.g. Chesapeake = "-76.5,37.0,-76.0,37.5"). Requires the GEBCO quadrant grids
# in data/ (clips locally; no 4 GB fetch). View with the two dev servers in separate terminals:
#   cd worker && npm install && npm run dev               # tile Worker on :8787
#   VITE_TILES_BASE=http://localhost:8787 npm run dev     # Vite on :5173 (repo root)
preview bbox="-74.30,40.40,-73.75,40.80":
    #!/usr/bin/env bash
    set -euo pipefail
    IFS=, read -r W S E N <<< "{{bbox}}"
    export BBOX="{{bbox}}"
    rm -rf store/aggregation store/pmtiles store/bundle store/meta store/contour
    mkdir -p store/source/gebco
    # GEBCO base: clip from a VRT over the quadrant grids so any BBOX works (assumes a
    # single GEBCO vintage in data/; the glob mosaics whatever quadrants are present).
    gdalbuildvrt -q -overwrite store/source/gebco.vrt ../data/gebco_*.tif
    gdal_translate -q -projwin "$W" "$N" "$E" "$S" store/source/gebco.vrt store/source/gebco/gebco_0.tif
    uv run python source_bounds.py gebco
    # US streaming sources: register manifest/gpkg tiles as /vsicurl refs (header reads
    # only, BBOX-prefiltered); aggregation range-reads the COGs straight from NOAA S3.
    # Re-registered every run so a changed BBOX re-scopes (0 tiles outside their coverage).
    uv run python source_register_remote_urllist.py cudem
    uv run python source_register_remote_geopkg.py bluetopo
    just planet
    ../worker/seed.sh

# Offline self-checks (synthetic data, no network).
test-sources:
    uv run python test_source_stage.py
test-engine:
    uv run python test_engine.py
