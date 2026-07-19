#!/usr/bin/env bash
# Run any pipeline recipe in the toolchain container — the only local dependency is
# Docker: `./docker.sh planet`, `./docker.sh source <id>`, `BBOX="W,S,E,N" ./docker.sh
# preview`. No args → list the recipes. Forwards to `just` inside the container.
#
# The image holds only the toolchain + Python deps (the build is layer-cached and only
# changes when Dockerfile or pyproject.toml/uv.lock do); the repo is mounted
# at /app, so the current code runs as-is and outputs land on the host under
# pipelines/store/. Pipeline env knobs are forwarded (unset ones stay unset inside).
#
# CI reuses this wrapper: IMAGE=<ref> (plus optional IMAGE_TAG) runs a prebuilt image
# (pulled, never built) and STATE=<dir> bind-mounts the persistent store at /app/state
# (the Snakemake workdir).
set -euo pipefail
cd "$(dirname "$0")"
if [ $# -eq 0 ]; then set -- --list; fi
# `./docker.sh snakemake …` runs snakemake (repo-root Snakefile) instead of a just
# recipe — the same container, deps, and mounts either way.
cmd=(just "$@")
if [ "$1" = "snakemake" ]; then shift; cmd=(uv run snakemake "$@"); fi
image="${IMAGE:-seascape-build}"
# CI's env carries the repo image and its deps tag separately — compose the ref here.
if [ -n "${IMAGE_TAG:-}" ]; then image="$image:$IMAGE_TAG"; fi
if [ "$image" = seascape-build ]; then
  # Tag on the deps files (CI's IMAGE_TAG keying) so an unchanged image skips the
  # docker build round-trip entirely; code is mounted, so nothing else can stale it.
  deps=$(cat Dockerfile pyproject.toml uv.lock | { sha256sum 2>/dev/null || shasum -a 256; } | cut -c1-12)
  image="seascape-build:deps-$deps"
  docker image inspect "$image" >/dev/null 2>&1 || docker build -t "$image" .
else
  docker image inspect "$image" >/dev/null 2>&1 || docker pull "$image"
fi
tty=""; if [ -t 0 ]; then tty="-it"; fi
# `dev` serves the viewer/Worker — publish their ports to the host.
ports=""; if [ "${1:-}" = "dev" ]; then ports="-p 5173:5173 -p 8787:8787"; fi
# CI mounts the persistent store volume at /app/state.
state=""; if [ -n "${STATE:-}" ]; then state="-v $STATE:/app/state"; fi
# Toolchain identity (utils.toolchain): the image ID pins the exact GDAL/tippecanoe
# build, like the GHCR image tag does on the build box.
export TOOLCHAIN="${TOOLCHAIN:-$(docker image inspect -f '{{.Id}}' "$image")}"
# node_modules is shadowed by a named volume: the host's install is
# platform-specific (darwin vs linux binaries), so the container keeps its own.
# nofile: ~96 concurrent snakemake jobs' pipes + per-job benchmark /proc reads exhaust
# the default soft limit in the parent.
exec docker run --rm $tty $ports $state --ulimit nofile=65536:65536 \
  -e TOOLCHAIN \
  -e BBOX -e SOURCE_VSI_BASE -e BOUNDS_BASE -e LANDMASK -e WATERMASK \
  -e MACROTILE_Z -e OVERLAY_SPLIT_Z -e NUM_OVERVIEWS -e AGG_PROCESSES -e BUNDLE_PROCESSES -e GDAL_CACHEMAX \
  -e CPL_VSIL_CURL_CHUNK_SIZE -e CPL_VSIL_CURL_CACHE_SIZE -e GDAL_HTTP_MULTIPLEX -e GDAL_HTTP_VERSION \
  -e VSI_CACHE -e VSI_CACHE_SIZE -e GDAL_INGESTED_BYTES_AT_OPEN \
  -e SMOOTH_DEM_SIGMA -e SMOOTH_SLOPE_LOW -e SMOOTH_SLOPE_HIGH -e SKIP_SMOOTH \
  -e SKIP_CONTOURS -e SKIP_SOUNDINGS -e SKIP_DRYING -e CONTOUR_NAV_SMOOTH_MAX \
  -e SOUND_CELL_PX -e SOUND_MIN_DEPTH_M -e DRYING_CAP \
  -e RCLONE_CONFIG_R2_TYPE -e RCLONE_CONFIG_R2_PROVIDER -e RCLONE_CONFIG_R2_ENDPOINT \
  -e RCLONE_CONFIG_R2_ACCESS_KEY_ID -e RCLONE_CONFIG_R2_SECRET_ACCESS_KEY \
  -e RCLONE_CONFIG_R2_NO_CHECK_BUCKET -e DATA_BUCKET -e PUBLIC_BASE -e MIRROR_ALLOW_SHRINK -e SHA \
  -v "$PWD:/app" \
  -v seascape-node-modules:/app/node_modules \
  "$image" "${cmd[@]}"
