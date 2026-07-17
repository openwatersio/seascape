# Use Official OSGeo image (Ubuntu 24.04 + current GDAL with the HDF5/BAG drivers)
FROM ghcr.io/osgeo/gdal:ubuntu-full-3.13.1

LABEL org.opencontainers.image.source="https://github.com/openwatersio/seascape"
LABEL org.opencontainers.image.description="Bathymetry → tile pipeline)"

ENV DEBIAN_FRONTEND=noninteractive

# Build deps for tippecanoe (GDAL CLI comes with the base image).
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git \
    build-essential libsqlite3-dev zlib1g-dev \
    shellcheck \
  && rm -rf /var/lib/apt/lists/*

# Every source is read over /vsicurl (R2/NOAA), so a transient HTTP/curl blip — an R2
# 500 InternalError, a "Recv failure: Connection reset by peer" — must not kill an
# hour-long aggregate shard mid-read. Retry such errors instead of failing. Applies to
# every gdal subprocess (and local dev). Mirrors the http_download backoff on the Python side.
ENV GDAL_HTTP_MAX_RETRY=5 \
    GDAL_HTTP_RETRY_DELAY=1

# tippecanoe + tile-join (Felt fork) — contour vector tiles.
RUN git clone --depth 1 https://github.com/felt/tippecanoe.git /tmp/tippecanoe \
  && cd /tmp/tippecanoe && make -j"$(nproc)" && make install && rm -rf /tmp/tippecanoe

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

# Recipes run from /app; the Justfile redirects into pipelines/ itself. e.g.
# `docker run -v "$PWD:/app" img just planet` (set BBOX=… for a region).
CMD ["just", "--list"]
