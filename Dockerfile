# Use Official OSGeo image (Ubuntu 24.04 + current GDAL with the HDF5/BAG drivers)
FROM ghcr.io/osgeo/gdal:ubuntu-full-3.13.1

LABEL org.opencontainers.image.source="https://github.com/openwatersio/seascape"
LABEL org.opencontainers.image.description="Bathymetry → tile pipeline)"

ENV DEBIAN_FRONTEND=noninteractive

# Build deps for tippecanoe (GDAL CLI comes with the base image).
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git unzip \
    build-essential libsqlite3-dev zlib1g-dev \
    shellcheck \
  && rm -rf /var/lib/apt/lists/*

# rclone — the publish rules shell it from inside this container (the R2 pushes that the
# legacy workflows ran on the host). Pinned + sha256-verified per arch, same 1.74.4 pin as
# the workflows' host installs (survived R2's version-id breakage; do not float).
ARG TARGETARCH=amd64
RUN case "$TARGETARCH" in \
      arm64) sha=97685285c9ad6a0cf17d5844115d2a67245af6444db672187074bd9c358de419 ;; \
      *)     sha=fe435e0c36228e7c2f116a8701f01127bb1f694005fc11d1f27186c8bca4115d ;; \
    esac \
  && curl -fsSL -o /tmp/rclone.zip "https://downloads.rclone.org/v1.74.4/rclone-v1.74.4-linux-${TARGETARCH:-amd64}.zip" \
  && echo "$sha  /tmp/rclone.zip" | sha256sum -c \
  && unzip -q -j /tmp/rclone.zip '*/rclone' -d /usr/local/bin \
  && rm /tmp/rclone.zip

# Every source is read over /vsicurl (R2/NOAA), so a transient HTTP/curl blip — an R2
# 500 InternalError, a "Recv failure: Connection reset by peer" — must not kill an
# hour-long aggregate shard mid-read. Retry such errors instead of failing. Applies to
# every gdal subprocess (and local dev). Mirrors the http_download backoff on the Python side.
ENV GDAL_HTTP_MAX_RETRY=5 \
  GDAL_HTTP_RETRY_DELAY=1 \
  GDAL_HTTP_USERAGENT="seascape/1.0 (+https://github.com/openwatersio/seascape)"

# tippecanoe + tile-join (Felt fork) — vector tiles. Pinned to the commit our patch targets and
# patched: the stock leaf-prevention guard consults only feature_minzoom, so
# --generate-variable-depth-tile-pyramid prunes features carrying an explicit tippecanoe.minzoom
# above the leaf — the patch fixes it (see patches/ + docs plan Part 2). patches/*.patch rides in
# the image hash (ghcr-login), so editing the patch rebuilds the toolchain image.
COPY patches/tippecanoe-variable-depth-minzoom.patch /tmp/tc.patch
RUN git clone https://github.com/felt/tippecanoe.git /tmp/tippecanoe \
  && cd /tmp/tippecanoe && git checkout 0c650b8 \
  && git apply /tmp/tc.patch \
  && make -j"$(nproc)" && make install && rm -rf /tmp/tippecanoe /tmp/tc.patch

# just (task runner) + uv (Python env manager) — the pipeline's two entrypoints.
RUN curl --proto '=https' --tlsv1.2 -sSf https://just.systems/install.sh \
      | bash -s -- --to /usr/local/bin
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

# actionlint — lints .github/workflows (`just test-workflows`). Installer script pinned to
# the same tag as the version it fetches, so neither can drift under an unchanged image hash.
RUN curl -fsSL https://raw.githubusercontent.com/rhysd/actionlint/v1.7.12/scripts/download-actionlint.bash \
      | bash -s -- 1.7.12 /usr/local/bin

# Node 22 — lets the dev servers (`just dev`) and the preview seed step run in-container,
# so Docker is the only local dependency needed to see the map (`./docker.sh dev`).
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
  && apt-get install -y --no-install-recommends nodejs \
  && rm -rf /var/lib/apt/lists/*

# Python deps only (rasterio/scipy/pmtiles/geopandas/… from wheels; GDAL stays CLI).
# The code (pipelines/, sources/, Justfile) is NOT baked in — mount the repo at /app
# at runtime (`./docker.sh <recipe>` locally; CI mounts its checkout), so code changes
# never rebuild the image. The venv lives outside /app so the mount can't shadow it;
# `uv run` finds it via UV_PROJECT_ENVIRONMENT (and self-syncs if the mounted lock
# ever drifts from the baked env).
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app
COPY pyproject.toml uv.lock /app/
RUN uv sync --frozen --no-install-project
ENV PATH="/opt/venv/bin:${PATH}"

# The build is one Snakemake DAG (docker.sh fronts it). e.g.
# `docker run -v "$PWD:/app" img snakemake planet` (BBOX=… scopes a region);
# `just` still hosts the tests + dev servers.
CMD ["just", "--list"]
