# Use Official OSGeo image (Ubuntu 24.04 + current GDAL with the HDF5/BAG drivers).
# Bumping it (GDAL/GEOS/PROJ) must bump `version` on mosaic_tile, the fork rules, and
# terrain_render (build.smk) — tools are not rule inputs.
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

# tippecanoe (Felt fork) — vector tiles. Pinned at felt/tippecanoe#399 (variable-depth
# pyramids must honor per-feature tippecanoe.minzoom and never prune children a pending minzoom
# still needs; the vector bundle depends on both). patches/ carries fixes not yet upstream:
# wagyu-drop-unplaceable-hole drops an orphan hole ring instead of aborting the run
# (mapbox/tippecanoe#761; only changes tiles that previously crashed). A pin or patches/
# change must bump `version` on the vector_* rules and landmask (build.smk) unless the
# change provably leaves previously-built tiles byte-identical.
COPY patches /tmp/patches
RUN git init -q /tmp/tippecanoe \
  && cd /tmp/tippecanoe \
  && git fetch -q --depth 1 https://github.com/felt/tippecanoe.git 0badb242bea6f77c8e388898868801f3f3a9088b \
  && git checkout -q FETCH_HEAD \
  && git apply /tmp/patches/wagyu-drop-unplaceable-hole.patch \
  && make -j"$(nproc)" && make install && rm -rf /tmp/tippecanoe

# contour-p — the DEPARE partition pass's polygon-contour tool: GDAL's marching_squares
# headers at the base image's commit, plus the patch in patches/. GDAL's
# PolygonRingAppender attaches rings in O(rings^2 x vertices), so a marsh coastline's
# ~100k disjoint 0 m rings never finished a gdal_contour -p ladder (measured on a z15
# wetland window: 4839 s stock, 121 s patched, byte-identical bands). Upstream as
# OSGeo/gdal#14983 (after #1750/#2241, unfixed post-#2908) — when it lands in the base
# image, delete this stanza and DEPARE_CONTOUR_BIN with it.
# Headers are fetched at the BASE IMAGE's GDAL commit: a base-image bump must re-pin
# GDAL_MS_COMMIT and re-verify the patch applies.
ARG GDAL_MS_COMMIT=b2e6057d1d0f2cb4c11bfdf79ab1a61def0ce9ca
COPY tools/contour-p /tmp/contour-p
RUN cd /tmp/contour-p && mkdir ms \
  && for f in point.h square.h utility.h level_generator.h segment_merger.h \
              contour_generator.h polygon_ring_appender.h; do \
       curl -fsSL "https://raw.githubusercontent.com/OSGeo/gdal/${GDAL_MS_COMMIT}/alg/marching_squares/$f" -o "ms/$f" || exit 1; \
     done \
  && git apply --directory=ms -p3 /tmp/patches/gdal-polygon-ring-appender-quadratic.patch \
  && g++ -O2 -std=c++17 -I. $(gdal-config --cflags) contour-p.cpp -o /usr/local/bin/contour-p $(gdal-config --libs) \
  && contour-p 2>&1 | grep -q usage \
  && rm -rf /tmp/contour-p /tmp/patches

# go-pmtiles — the vector bundle's `pmtiles merge` joins the fringe-filtered cell shards into one
# sparse vector.pmtiles (a pure concat of structurally-disjoint tiles, no tile-join boundary MERGE).
# Pinned + sha256-verified per arch, same style as rclone above (do not float).
RUN case "$TARGETARCH" in \
      arm64) asset=Linux_arm64;  sha=f8bd47e7ea866863489cad588fbaf2f31f42e5821f7a03f009b3769f05801cb1 ;; \
      *)     asset=Linux_x86_64; sha=3ed7dbf4ec2e6dfe5e25b6f70d1ffc932729f93c86db353bf514dd71010a312f ;; \
    esac \
  && curl -fsSL -o /tmp/pmtiles.tar.gz "https://github.com/protomaps/go-pmtiles/releases/download/v1.31.2/go-pmtiles_1.31.2_${asset}.tar.gz" \
  && echo "$sha  /tmp/pmtiles.tar.gz" | sha256sum -c \
  && tar -xzf /tmp/pmtiles.tar.gz -C /usr/local/bin pmtiles \
  && rm /tmp/pmtiles.tar.gz

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

# py-spy for stack-dumping any wedged job via docker exec. uv installs the PyPI wheel
# (hash-verified by the index) into the venv; uv venvs ship no pip.
RUN uv pip install --python /opt/venv/bin/python py-spy==0.4.2 \
  && /opt/venv/bin/py-spy --version
ENV PATH="/opt/venv/bin:${PATH}"

# The build is one Snakemake DAG (docker.sh fronts it). e.g.
# `docker run -v "$PWD:/app" img snakemake planet` (BBOX=… scopes a region);
# `just` still hosts the tests + dev servers.
CMD ["just", "--list"]
