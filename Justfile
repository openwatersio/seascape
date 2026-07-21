# Bathymetry tiling pipeline. The build itself is one Snakemake DAG in one entry file (the
# repo-root Snakefile; `pipelines/build.smk` is included from it, gated on the `cover`
# checkpoint) — run via `snakemake` / `./docker.sh snakemake`, not this file. What remains here
# is the test suite, the dev servers, and the one-time mask prep. See CONTRIBUTING.md.

set working-directory := 'pipelines'

# Prepare the OSM land mask once (download -> unzip -> EPSG:3857 FlatGeobuf at
# store/landmask/land.fgb). Flagged coarse sources (GEBCO/EMODnet) clamp negative
# land pixels against it during the merge. LANDMASK overrides the output path.
landmask:
    uv run python landmask.py prep

# Prepare the inland-water mask once (Overture water theme -> EPSG:3857 FlatGeobuf at
# store/landmask/water.fgb). The land clamp subtracts it so flagged coarse sources keep
# their depths inside mapped rivers/lakes. Optional — absent, the clamp stays land-only.
# WATERMASK overrides the output path.
watermask:
    uv run python landmask.py prep-water

# Regional preview: sources streamed from R2 (a locally-prepped source wins), the whole
# cartographic chain into store/bundle, then seed the local Worker. Depth areas are skipped
# by default (their dense-tile tail is unbounded); SKIP_DEPARE= re-enables them.
preview bbox="-74.30,40.40,-73.75,40.80":
    #!/usr/bin/env bash
    set -euo pipefail
    export BBOX="{{bbox}}"
    export SOURCE_VSI_BASE="${SOURCE_VSI_BASE:-/vsicurl/https://data.openwaters.io/bathymetry/source}"
    export LANDMASK="${LANDMASK:-/vsicurl/https://data.openwaters.io/bathymetry/landmask/land.fgb}"
    export WATERMASK="${WATERMASK:-/vsicurl/https://data.openwaters.io/bathymetry/landmask/water.fgb}"
    export SKIP_DEPARE="${SKIP_DEPARE-1}"
    # One invocation: the `cover` checkpoint runs inside the bundles build (streamed sources),
    # then the DAG re-evaluates into the per-stem mosaic/fork/terrain jobs.
    uv run snakemake -s ../Snakefile bundles --config stream=1 --cores 8
    # seed.sh needs manifest.json; stage_build writes it locally (publish is a separate, box-only step)
    uv run python -c "import bundle; bundle.stage_build()"
    ../worker/seed.sh

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
    uv run python source_mirror.py --check
    uv run python source_catalog.py --check
    uv run python source_remote.py
    uv run python source_fetch.py --check
    uv run python source_prep.py --check
    uv run python source_check.py --check
    uv run snakemake -s ../Snakefile -n sources > /dev/null

# Build self-checks: the e2e (real stage-1 CLIs + the unified DAG), the `cover` checkpoint
# seam (test_build), and each module's --check.
test-engine:
    uv run python test_engine.py
    uv run python aggregation_reproject.py --check
    uv run python aggregation_covering.py --check
    uv run python mosaic.py --check
    uv run python bundle.py --check
    uv run python test_build.py
    uv run python terrain.py --check

# Lint the GitHub Actions workflows.
test-workflows:
    cd "{{justfile_directory()}}" && actionlint

# Test the GC's Collect step (scripts/gc-collect.sh — the exact script gc.yml runs, local
# backend) against a synthetic store tree: happy path + every refusal guard. Needs bash + jq;
# ci.yml runs it on every push.
test-gc:
    bash test_gc.sh
