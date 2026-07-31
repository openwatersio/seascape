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
# cartographic chain (depth areas included) into store/bundle, then seed the local Worker.
# SKIP_DEPARE=1 opts out of the (now-bounded) depare fork if you only need the raster preview.
preview bbox="-74.30,40.40,-73.75,40.80":
    #!/usr/bin/env bash
    set -euo pipefail
    export BBOX="{{bbox}}"
    export SOURCE_VSI_BASE="${SOURCE_VSI_BASE:-/vsicurl/https://data.openwaters.io/bathymetry/source}"
    export LANDMASK="${LANDMASK:-/vsicurl/https://data.openwaters.io/bathymetry/landmask/land.fgb}"
    export WATERMASK="${WATERMASK:-/vsicurl/https://data.openwaters.io/bathymetry/landmask/water.fgb}"
    # One invocation: the `cover` checkpoint runs inside the bundles build (streamed sources),
    # then the DAG re-evaluates into the per-stem mosaic/fork/terrain jobs.
    # mem_gb budget from the actual environment (the Docker VM's memory, not the host's), minus
    # a 2 GB reserve — without it snakemake admits concurrent heavy forks the VM OOM-kills (137).
    # A dense-harbor z14 contour reserves ~10 GB: give the Docker Desktop VM >=16 GB for those.
    mem_gb=$(free -g 2>/dev/null | awk 'NR==2{print $2}' || sysctl -n hw.memsize | awk '{print int($1/1073741824)}')
    uv run snakemake -s ../Snakefile bundles --config stream=1 --cores 8 --resources mem_gb=$((mem_gb > 4 ? mem_gb - 2 : mem_gb))
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
    uv run python config.py --check
    uv run python source_enumerate.py --check
    uv run python source_mirror.py --check
    uv run python source_catalog.py --check
    uv run python source_remote.py
    uv run python source_fetch.py --check
    uv run python source_prep.py --check
    uv run python source_polygonize.py --check
    uv run python source_check.py --check
    uv run snakemake -s ../Snakefile -n sources > /dev/null

# Build self-checks: the e2e (real stage-1 CLIs + the unified DAG), the `cover` checkpoint
# seam (test_build), and each module's --check.
test-engine:
    uv run python test_engine.py
    uv run python aggregation_reproject.py --check
    uv run python aggregation_covering.py --check
    uv run python landmask.py --check
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

# Build contour-p, the DEPARE partition tool production uses (DEPARE_CONTOUR_BIN). Measuring
# marsh stems with stock gdal_contour measures a path that doesn't ship — the patch is 7x on a
# 4096px crop and 40x on a full z15 wetland window. Needs the local gdal-config to match the
# Dockerfile's pinned GDAL_MS_COMMIT.
contour-p:
    #!/usr/bin/env bash
    set -euo pipefail
    commit=b2e6057d1d0f2cb4c11bfdf79ab1a61def0ce9ca
    src="{{justfile_directory()}}"; build=$(mktemp -d); out=store/profile/bin
    mkdir -p "$out" "$build/ms"
    cp "$src/tools/contour-p/contour-p.cpp" "$build/"
    for f in point.h square.h utility.h level_generator.h segment_merger.h \
             contour_generator.h polygon_ring_appender.h; do
      curl -fsSL "https://raw.githubusercontent.com/OSGeo/gdal/$commit/alg/marching_squares/$f" \
        -o "$build/ms/$f"
    done
    cd "$build" && git apply --directory=ms -p3 \
      "$src/patches/gdal-polygon-ring-appender-quadratic.patch"
    g++ -O2 -std=c++17 -I. $(gdal-config --cflags) contour-p.cpp \
      -o "$src/pipelines/$out/contour-p" $(gdal-config --libs)
    "$src/pipelines/$out/contour-p" 2>&1 | grep -q usage
    rm -rf "$build"
    echo "contour-p ready: pipelines/$out/contour-p"

# Cut the local marsh profiling fixtures (small real windows over the Gulf, range-read from the
# published mosaic COGs) into store/profile/root. Needs R2 read creds — the public host refuses
# range requests.
perf-fixtures *sites:
    uv run python perf/fixtures.py build {{sites}}
    uv run python perf/fixtures.py masks

# Profile one real stage against a fixture and record wall / peak RSS / parts / vertices.
#   just perf depare 12-1015-1699-15 base
perf stage stem label="adhoc":
    uv run python perf/bench.py run {{stage}} {{stem}} --label {{label}}

# Compare two labelled runs; nonzero exit on a regression past threshold.
perf-compare a b:
    uv run python perf/bench.py compare {{a}} {{b}}

# Safety gates for a generalization change: shoal-bias, water-network connectivity, drying
# growth on connected water (raster); partition contract, bounded displacement, named-route
# continuity (vector). Hard assertions — a failure is a defect, not a threshold to widen.
perf-gate kind before after *args:
    uv run python perf/gates.py {{kind}} {{before}} {{after}} {{args}}

# Every self-check in the profiling harness.
test-perf:
    uv run python perf/fixtures.py --check
    uv run python perf/metrics.py --check
    uv run python perf/bench.py --check
    uv run python perf/gates.py --check
