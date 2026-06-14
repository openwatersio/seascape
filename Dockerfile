FROM ubuntu:24.04

LABEL org.opencontainers.image.source="https://github.com/openwatersio/gebco-tiles"
LABEL org.opencontainers.image.description="GEBCO bathymetry to vector tile pipeline"

ENV DEBIAN_FRONTEND=noninteractive

# GDAL (with Python bindings for terrain-rgb encoding), jq, and build deps.
RUN apt-get update && apt-get install -y \
    gdal-bin \
    python3-gdal \
    python3-numpy \
    python3-scipy \
    python3-rasterio \
    python3-pip \
    python3-venv \
    bc \
    jq \
    curl \
    unzip \
    sqlite3 \
    build-essential \
    libsqlite3-dev \
    zlib1g-dev \
    git \
  && rm -rf /var/lib/apt/lists/*

# Install rio-rgbify for Terrain-RGB encoding.
RUN python3 -m venv --system-site-packages /opt/rio-venv \
  && /opt/rio-venv/bin/pip install --no-cache-dir rio-rgbify
ENV PATH="/opt/rio-venv/bin:$PATH"

# Install tippecanoe (Felt fork).
RUN git clone --depth 1 https://github.com/felt/tippecanoe.git /tmp/tippecanoe \
  && cd /tmp/tippecanoe \
  && make -j$(nproc) \
  && make install \
  && rm -rf /tmp/tippecanoe

# Install pmtiles CLI.
RUN ARCH=$(dpkg --print-architecture) \
  && case "${ARCH}" in amd64) ARCH=x86_64;; arm64) ;; *) echo "Unsupported arch: ${ARCH}" && exit 1;; esac \
  && VERSION=$(curl -sL -o /dev/null -w '%{url_effective}' https://github.com/protomaps/go-pmtiles/releases/latest | grep -oE '[^/]+$') \
  && curl -L -o /tmp/go-pmtiles.tar.gz \
    "https://github.com/protomaps/go-pmtiles/releases/download/${VERSION}/go-pmtiles_${VERSION#v}_Linux_${ARCH}.tar.gz" \
  && tar -xzf /tmp/go-pmtiles.tar.gz -C /usr/local/bin pmtiles \
  && chmod +x /usr/local/bin/pmtiles \
  && rm /tmp/go-pmtiles.tar.gz

WORKDIR /app
COPY scripts/ /app/scripts/
RUN chmod +x /app/scripts/*
ENV PATH="/app/scripts:$PATH"

# No ENTRYPOINT: run a specific step, e.g. `docker run img build` (full pipeline),
# or `docker run img terrain` / `docker run img contour` for individual artifacts.
CMD ["build"]
