#!/usr/bin/env bash
# Shared configuration for the GEBCO → vector tile pipeline.
# Source this from other scripts: source "$(dirname "$0")/config.sh"

set -euo pipefail

# ─── Paths ────────────────────────────────────────────────────────────────────
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${PROJECT_DIR}/data"
WORK_DIR="${PROJECT_DIR}/work"
OUTPUT_DIR="${PROJECT_DIR}/output"

mkdir -p "${DATA_DIR}" "${WORK_DIR}" "${OUTPUT_DIR}"

# ─── GEBCO source ─────────────────────────────────────────────────────────────
# GEBCO GeoTIFF — full global grid (~7.5 GB uncompressed, 4.24 GB zipped).
# https://www.gebco.net/data-products-gridded-bathymetry-data/gebco2026-grid
GEBCO_YEAR="${GEBCO_YEAR:-2026}"
GEBCO_URL="${GEBCO_URL:-https://dap.ceda.ac.uk/bodc/gebco/global/gebco_${GEBCO_YEAR}/ice_surface_elevation/geotiff/gebco_${GEBCO_YEAR}_geotiff.zip}"
GEBCO_ZIP="${DATA_DIR}/gebco_${GEBCO_YEAR}.zip"
GEBCO_TIF="${DATA_DIR}/gebco_${GEBCO_YEAR}.tif"
GEBCO_VRT="${DATA_DIR}/gebco_${GEBCO_YEAR}.vrt"

# ─── Bounding box (optional, for regional extracts) ──────────────────────────
# Format: "west,south,east,north"
# Leave empty for full global processing.
# Examples:
#   Bahamas:        BBOX="-85,20,-70,35"
#   US East Coast:  BBOX="-82,24,-65,45"
#   Mediterranean:  BBOX="-6,30,36,46"
BBOX="${BBOX:-}"

# ─── Contour levels ──────────────────────────────────────────────────────────
# Non-uniform: fine intervals for shallow water, coarse for deep.
# Must be in increasing order (most negative first) for gdal_contour -fl.
# Overridable so the sharded low-zoom band can pass only the deep levels that
# render at z≤7 (see SCALING.md / the tippecanoe filter in scripts/contour).
CONTOUR_LEVELS="${CONTOUR_LEVELS:--10000 -8000 -6000 -5000 -4000 -3000 -2000 -1500 -1000 -500 -200 -150 -100 -75 -50 -45 -40 -35 -30 -25 -20 -15 -14 -13 -12 -11 -10 -9 -8 -7 -6 -5 -4 -3 -2 -1}"

# ─── Terrain RGB ──────────────────────────────────────────────────────────────
# GEBCO is 15 arc-second (~450m at equator). Zoom 9 ≈ 305m/pixel — a good
# match for the native resolution. MapLibre color-relief and maplibre-contour
# handle overzooming smoothly on the GPU / in web workers.
TERRAIN_MAX_ZOOM="${TERRAIN_MAX_ZOOM:-9}"

# ─── Processing ──────────────────────────────────────────────────────────────
# Set FORCE=1 to rebuild all intermediate and output files from scratch.
FORCE="${FORCE:-}"

THREADS="${THREADS:-$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)}"

# Tippecanoe max zoom for the contour tileset.
# GEBCO is 15 arc-sec (~463 m/px at the equator), which is native around z8.4,
# so z9 is the real resolution ceiling. MapLibre overzooms past it for display.
MAX_ZOOM="${MAX_ZOOM:-9}"

# Minimum tile zoom. The global base band uses 0; regional high-res bands set
# this (e.g. 10) so their tiles don't collide with the base — see scripts/build.
MIN_ZOOM="${MIN_ZOOM:-0}"

# ─── Multi-source priority mosaic ────────────────────────────────────────────
# Regional high-res sources layered on top of the GEBCO base. See sources.conf.
SOURCES_CONF="${SOURCES_CONF:-${PROJECT_DIR}/scripts/sources.conf}"
MOSAIC_VRT="${WORK_DIR}/mosaic.vrt"

# OSM water polygons — contours are clipped to these so land/intertidal features
# don't produce spurious lines (more accurate than the depth<0 filter alone).
# Download: https://osmdata.openstreetmap.de/data/water-polygons.html
# If absent, the contour step falls back to depth<0 only.
WATER_POLYGONS="${WATER_POLYGONS:-${DATA_DIR}/water-polygons-split-4326/water_polygons.shp}"

# ─── Helpers ─────────────────────────────────────────────────────────────────
log() { echo "[$(date '+%H:%M:%S')] $*" >&2; }

# Returns 0 (true) if the file exists and FORCE is not set.
cached() { [[ -z "${FORCE}" && -e "$1" ]]; }

# Print active sources.conf rows (comments/blanks stripped, whitespace around the
# pipe field separators trimmed) so consumers can `IFS='|' read` them directly.
sources_rows() {
  [[ -f "${SOURCES_CONF}" ]] || return 0
  grep -vE '^[[:space:]]*(#|$)' "${SOURCES_CONF}" \
    | sed -E 's/[[:space:]]*\|[[:space:]]*/|/g; s/^[[:space:]]+//; s/[[:space:]]+$//'
}

check_deps() {
  local missing=()
  for cmd in "$@"; do
    if ! command -v "$cmd" &>/dev/null; then
      missing+=("$cmd")
    fi
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    echo "ERROR: Missing required tools: ${missing[*]}"
    echo "Install them or use the project Dockerfile."
    exit 1
  fi
}

# Resolve the input DEM from an argument or the default clipped file.
# Usage: resolve_input_dem "$1"
# Sets INPUT_TIF and SUFFIX as globals.
resolve_input_dem() {
  INPUT_TIF="${1:-}"
  if [[ -z "${INPUT_TIF}" ]]; then
    local clipped="${WORK_DIR}/gebco_clipped${BBOX:+_${BBOX}}.tif"
    if [[ -f "${clipped}" ]]; then
      INPUT_TIF="${clipped}"
    else
      log "ERROR: No input DEM found. Run ./scripts/download first."
      exit 1
    fi
  fi
  SUFFIX="${BBOX:+_${BBOX}}"
  log "Input DEM: ${INPUT_TIF}"
}
