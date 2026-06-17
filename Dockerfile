FROM ubuntu:24.04

LABEL org.opencontainers.image.source="https://github.com/openwatersio/bathymetry-tiles"
LABEL org.opencontainers.image.description="Bathymetry → tile pipeline)"

ENV DEBIAN_FRONTEND=noninteractive

# GDAL CLI (invoked as a subprocess by the pipeline) + build deps for tippecanoe.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gdal-bin \
    ca-certificates curl git \
    build-essential libsqlite3-dev zlib1g-dev \
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

# The Python engine (rasterio/scipy/pmtiles/geopandas/… from wheels; GDAL stays CLI).
WORKDIR /app
COPY pipelines/ /app/pipelines/
COPY sources/ /app/sources/
COPY Justfile /app/Justfile
RUN cd pipelines && uv sync --frozen
ENV PATH="/app/pipelines/.venv/bin:${PATH}"

# Recipes run from /app; the Justfile redirects into pipelines/ itself. e.g.
# `docker run img just planet` (set BBOX=… for a region) or `… just source <id>`.
CMD ["just", "--list"]
