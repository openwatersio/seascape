#!/usr/bin/env bash
# Run any pipeline recipe in the toolchain container — the only local dependency is
# Docker: `./docker.sh planet`, `./docker.sh source <id>`, `BBOX="W,S,E,N" ./docker.sh
# preview`. No args → list the recipes. Forwards to `just` inside the container.
#
# The image holds only the toolchain + Python deps (the build is layer-cached and only
# changes when Dockerfile or pipelines/pyproject.toml/uv.lock do); the repo is mounted
# at /app, so the current code runs as-is and outputs land on the host under
# pipelines/store/. Pipeline env knobs are forwarded (unset ones stay unset inside).
set -euo pipefail
cd "$(dirname "$0")"
if [ $# -eq 0 ]; then set -- --list; fi
docker build -t bathymetry .
tty=""; if [ -t 0 ]; then tty="-it"; fi
# `dev` serves the viewer/Worker — publish their ports to the host.
ports=""; if [ "${1:-}" = "dev" ]; then ports="-p 5173:5173 -p 8787:8787"; fi
# Toolchain identity for the cache keys (keys.py): the local image ID pins the exact
# GDAL/tippecanoe build, like the GHCR image tag does on the build box.
export TOOLCHAIN="${TOOLCHAIN:-$(docker image inspect -f '{{.Id}}' bathymetry)}"
# node_modules is shadowed by a named volume: the host's install is
# platform-specific (darwin vs linux binaries), so the container keeps its own.
exec docker run --rm $tty $ports \
  -e TOOLCHAIN \
  -e BBOX -e SOURCE_VSI_BASE -e BOUNDS_BASE -e LANDMASK -e FORCE_REBUILD \
  -e MACROTILE_Z -e OVERLAY_SPLIT_Z -e NUM_OVERVIEWS -e AGG_PROCESSES -e BUNDLE_PROCESSES -e GDAL_CACHEMAX \
  -e CPL_VSIL_CURL_CHUNK_SIZE -e CPL_VSIL_CURL_CACHE_SIZE -e GDAL_HTTP_MULTIPLEX -e GDAL_HTTP_VERSION \
  -e VSI_CACHE -e VSI_CACHE_SIZE -e GDAL_INGESTED_BYTES_AT_OPEN \
  -e SMOOTH_DEM_SIGMA -e SMOOTH_SLOPE_LOW -e SMOOTH_SLOPE_HIGH -e SKIP_SMOOTH \
  -e SKIP_CONTOURS -e SKIP_SOUNDINGS -e SKIP_DRYING -e CONTOUR_NAV_SMOOTH_MAX \
  -e SOUND_CELL_PX -e SOUND_MIN_DEPTH_M -e DRYING_CAP \
  -v "$PWD:/app" \
  -v seascape-node-modules:/app/node_modules \
  bathymetry just "$@"
