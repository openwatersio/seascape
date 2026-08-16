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
# `./docker.sh run <argv…>` executes argv verbatim in the container (no just routing).
if [ "$1" = "run" ]; then shift; cmd=("$@"); fi
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
ports=""; if [ "${1:-}" = "dev" ]; then ports="-p 5173:5173"; fi
# CI mounts the persistent store volume at /app/state.
state=""; if [ -n "${STATE:-}" ]; then state="-v $STATE:/app/state"; fi
# CI points TMP (per-run logs/benchmarks) at local disk, off the network volume — forwarded
# only as the mount SOURCE, never into the container env (keeps container tempfile at /tmp).
tmp=""; if [ -n "${TMP:-}" ]; then tmp="-v $TMP:/app/tmp"; fi
# Forward only the knobs that are SET: `docker run -e VAR` with VAR unset on the host
# does not fall through to the image — it DELETES the Dockerfile's ENV value for VAR
# (this silently stripped GDAL_CACHEMAX/GDAL_NUM_THREADS from every CI build).
envs=()
for v in BBOX SOURCE_VSI_BASE BOUNDS_BASE LANDMASK WATERMASK \
  GDAL_NUM_THREADS MACROTILE_Z OVERLAY_SPLIT_Z NUM_OVERVIEWS AGG_PROCESSES BUNDLE_PROCESSES GDAL_CACHEMAX \
  CPL_VSIL_CURL_CHUNK_SIZE CPL_VSIL_CURL_CACHE_SIZE GDAL_HTTP_MULTIPLEX GDAL_HTTP_VERSION \
  VSI_CACHE VSI_CACHE_SIZE MALLOC_ARENA_MAX GDAL_INGESTED_BYTES_AT_OPEN \
  MERGE_SCRATCH VECTOR_SCRATCH \
  SMOOTH_DEM_SIGMA SMOOTH_SLOPE_LOW SMOOTH_SLOPE_HIGH SKIP_SMOOTH \
  SKIP_CONTOURS SKIP_SOUNDINGS SKIP_DEPARE DEPARE_TIMEOUT DEPARE_CONTOUR_BIN DEPARE_TIMING CONTOUR_NAV_SMOOTH_MAX \
  SOUND_CELL_PX SOUND_MIN_DEPTH_M DRYING_CAP \
  RCLONE_CONFIG_R2_TYPE RCLONE_CONFIG_R2_PROVIDER RCLONE_CONFIG_R2_ENDPOINT \
  RCLONE_CONFIG_R2_ACCESS_KEY_ID RCLONE_CONFIG_R2_SECRET_ACCESS_KEY \
  RCLONE_CONFIG_R2_NO_CHECK_BUCKET DATA_BUCKET PUBLIC_BASE MIRROR_ALLOW_SHRINK SHA; do
  if [ -n "${!v+x}" ]; then envs+=(-e "$v"); fi
done
# node_modules is shadowed by a named volume: the host's install is
# platform-specific (darwin vs linux binaries), so the container keeps its own.
# nofile: ~96 concurrent snakemake jobs' pipes + per-job benchmark /proc reads exhaust
# the default soft limit in the parent.
exec docker run --rm $tty $ports $state $tmp --ulimit nofile=65536:65536 \
  ${envs[@]+"${envs[@]}"} \
  -v "$PWD:/app" \
  -v seascape-node-modules:/app/node_modules \
  "$image" "${cmd[@]}"
