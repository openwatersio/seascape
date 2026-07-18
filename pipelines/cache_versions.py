"""Explicit cache contracts. Bump only when that artifact's output semantics change."""

MERGE = "merge-v1"
LANDMASK = "landmask-v1"
SMOOTH = "smooth-v1"
ENCODE = "encode-v1"
MOSAIC_TILE = "mosaic-tile-v1"
MOSAIC_INDEX = "mosaic-index-v1"
PLANET_OVERVIEW = "planet-overview-v1"
MOSAIC_GTI = "mosaic-gti-v2"  # v2 declares ResX/ResY instead of sampling an arbitrary tile
TERRAIN = "terrain-v1"
CONTOUR = "contour-v1"
SOUNDINGS = "soundings-v1"
DEPARE = "depare-v1"
TERRAIN_BUNDLE = "terrain-bundle-v1"
SOUNDINGS_BUNDLE = "soundings-bundle-v1"
DEPARE_BUNDLE = "depare-bundle-v1"
VECTOR_BUNDLE = "vector-bundle-v1"


def all_versions():
    return {name.lower(): value for name, value in globals().items()
            if name.isupper() and isinstance(value, str)}
