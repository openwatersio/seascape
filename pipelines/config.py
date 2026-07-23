"""Pipeline configuration + source access.

Scripts run from the ``pipelines/`` directory; the
``store/`` working dirs are cwd-relative. Sources live in ``../sources/<id>/``,
each with ``metadata.json`` (attribution + bathymetry knobs) and ``file_list.txt``.
Ported from scripts/config.sh.
"""

import hashlib
import json
import os
from glob import glob

SOURCES_DIR = os.environ.get("SOURCES_DIR", "../sources")

# Standard INT isobaths (IHO S-4 B-411), metres, most-negative first. The shallow
# ladder (2/5/10/20/30) is also the S-52 safety-contour value set; fine depth detail
# between curves is the soundings layer's job, not extra isobaths. Env-tunable like the
# other contour knobs; the resolved list enters the contour/depare tile keys. The style
# hand-mirrors the DEFAULT (style/index.ts DEPARE_LADDER_M/FT), so contour_run/depare_run
# warn when an override diverges from CONTOUR_LEVELS_DEFAULT.
_CONTOUR_LEVELS_DEFAULT = (
    "-10000 -8000 -6000 -5000 -4000 -3000 -2000 -1000 -500 -300 -200 "
    "-100 -50 -30 -20 -10 -5 -2"
)
CONTOUR_LEVELS_DEFAULT = [int(x) for x in _CONTOUR_LEVELS_DEFAULT.split()]
CONTOUR_LEVELS = [int(x) for x in os.environ.get("CONTOUR_LEVELS", _CONTOUR_LEVELS_DEFAULT).split()]

# Drying areas (green foreshore): seabed above chart datum that covers/uncovers with the tide —
# elevation in [0, DRYING_CAP] seaward of the OSM land line. The cap anchors to the global maximum
# of HAT-LAT, the highest ground that still floods and dries (~16.3-17 m Bay of Fundy/Burntcoat
# Head; Ungava ~16.8; Bristol Channel ~15). 16 is deliberately the round value just below that
# extreme tail: genuine 16-17 m drying at the two or three most extreme sites classifies as land.
# Two inherent biases: over-inclusion on low-MHW coasts (bluff
# toes up to the cap tint as foreshore) and under-inclusion of the MHW-HAT band in mega-tidal
# estuaries (it sits on OSM's land side). A spatially-varying HAT-LAT surface is the upgrade path.
# Purely a classifier: it bounds the depare drying bucket and the terrain render's drying code —
# the published raster carries category codes (0/1/2), never the cap itself.
DRYING_CAP = float(os.environ.get("DRYING_CAP", "16"))

# Feet/fathom isobaths: a second contour set at the classic fathom curves. Friendly feet depths
# (6, 12, 18, 30, 60, 120, 180, 300, 600 ft …) are exactly whole fathoms in feet, so one geometry
# labels as either — the viewer picks feet or fathoms. In metres (negative, positive-down) for
# gdal_contour. Full depth range so feet/fathom mode has isobaths everywhere, not just the shelf.
FATHOM_CURVES = [1, 2, 3, 5, 10, 20, 30, 50, 100, 200, 300, 500, 1000, 2000, 3000, 5000]
# Ascending (deepest first, like CONTOUR_LEVELS) — gdal_contour -fl needs strictly increasing.
CONTOUR_LEVELS_FT = sorted(round(-fm * 1.8288, 4) for fm in FATHOM_CURVES)

# Depth-area (ENC DEPARE) partition levels: every charted isobath is a band edge, plus 0 to
# close the shoalest band at the shoreline (the encoder holds land at >= 0, water below).
# gdal_contour -p buckets the DEM between successive levels; the style tints buckets off
# drval1 and snaps the safety contour to the next-deeper level. Mirrored in style/index.ts
# (DEPARE_LADDER_M / DEPARE_LADDER_FT) — keep them in sync.
DEPARE_LEVELS = CONTOUR_LEVELS + [0]
DEPARE_LEVELS_FT = CONTOUR_LEVELS_FT + [0]


def sources():
    """All source ids (directory names under SOURCES_DIR)."""
    return sorted(os.path.basename(p) for p in glob(f"{SOURCES_DIR}/*") if os.path.isdir(p))


def load_metadata(source):
    with open(f"{SOURCES_DIR}/{source}/metadata.json") as f:
        return json.load(f)


# Cache the generated catalog item per (cwd, source): a build hydrates it once and never
# rewrites it, and the tile keys read it per source per tile. cwd is in the key so a test that
# chdirs between tmp stores never sees another store's item.
_catalog_cache = {}


def load_catalog(source):
    """The generated ``store/source/<id>/catalog.json`` item if present, else None. The item is
    the single machine-facing view of a source (bounds + recorded datum offset + flags + recipe
    hash); phase 3 re-points the build's per-source reads at it, with metadata.json as fallback."""
    ck = (os.getcwd(), source)
    if ck not in _catalog_cache:
        path = f"store/source/{source}/catalog.json"
        item = None
        if os.path.isfile(path):
            with open(path) as f:
                item = json.load(f)
        _catalog_cache[ck] = item
    return _catalog_cache[ck]


# Build property -> the catalog item's seascape:* field. The catalog item is generated from
# metadata.json (+ the datum sidecar) at source-prep time, so the two agree; metadata.json stays
# the fallback until every source has re-registered with an item.
_CATALOG_KEY = {
    "priority": "seascape:priority",
    "max_zoom": "seascape:max_zoom",
    "land_clamp": "seascape:land_clamp",
    "offset": "seascape:datum_offset_m",
    "negate": "seascape:negate",
    "raw": "seascape:raw",
    "band": "seascape:band",
    "mixed_crs": "seascape:mixed_crs",
}


def source_property(source, name, default=None):
    """A source's build property (priority/max_zoom/land_clamp/offset/negate/raw/band/
    mixed_crs), read from the catalog item's seascape:<field> when present, else metadata.json.
    ``offset`` lives only in the catalog (recorded by source_datum at prep time), so it defaults
    to 0 without an item."""
    cat = load_catalog(source)
    if cat is not None:
        props = cat.get("properties", {})
        val = props.get(_CATALOG_KEY[name])
        # Cutover fallback (docs/plans/2026-07-23-declarative-sources.md): a catalog published
        # before the rename still carries seascape:volatile. Delete once every source has re-registered.
        if val is None and name == "raw":
            val = props.get("seascape:volatile")
        if val is not None:  # a genuinely-null field (an uncapped max_zoom) falls through
            return val
    if name == "offset":
        return default if default is not None else 0.0
    meta = load_metadata(source)
    if name == "raw" and "raw" not in meta:
        return meta.get("volatile", default)  # legacy metadata key, same cutover window
    return meta.get(name, default)


def source_recipe_hash(source):
    """The content hash the source stage stamped into the catalog item (seascape:recipe_hash) —
    the per-source input the tile keys read. None locally (RECIPE_HASH is empty on a laptop) and
    with no item: the covering row + resolved props still key the tile, so a local build stays
    incremental on source-file/metadata changes."""
    cat = load_catalog(source)
    if cat is None:
        return None
    return cat.get("properties", {}).get("seascape:recipe_hash")


def source_path(source, filename):
    """A bounds.csv ``filename`` resolved to a GDAL-openable path. Three forms:

    1. The filename is already a full ``/vsi`` path (a streaming source like CUDEM,
       registered straight off a public bucket) — use it verbatim.
    2. ``SOURCE_VSI_BASE`` is set (the CI aggregate job reading prepared COGs from
       the public data bucket) — resolve ``<base>/<source>/<filename>``, e.g.
       ``/vsicurl/https://data.openwaters.io/bathymetry/source/gebco/gebco_0.tif``,
       but a locally-prepared copy on disk wins if it exists (a preview reads the
       source you're iterating on from disk and streams the rest from R2).
    3. Otherwise (local dev) — ``store/source/<source>/<filename>`` on disk.

    So locally-prepared sources stream from R2 in CI yet read from disk locally,
    with no change to how they're prepared. Everything is public ``/vsicurl`` — no
    credentials in the read path.
    """
    if filename.startswith("/vsi"):
        return filename
    local = f"store/source/{source}/{filename}"
    base = os.environ.get("SOURCE_VSI_BASE")
    if base:
        return local if os.path.isfile(local) else f"{base}/{source}/{filename}"
    return local


def file_list(source):
    path = f"{SOURCES_DIR}/{source}/file_list.txt"
    if not os.path.isfile(path):
        return []
    with open(path) as f:
        return [l.strip() for l in f if l.strip() and not l.lstrip().startswith("#")]


def item_hash(url):
    """The store's raw/ filename for a fetch item: first 16 hex of sha256(url). Hashing
    the URL (not a list index) means inserting an item can't re-key later items, so the
    box refetches only genuinely new URLs."""
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def items(source):
    """The enumerated fetchable items (store/source/<id>/items.txt, one URL per line),
    written by the enumerate checkpoint. Empty if it hasn't run yet."""
    path = f"store/source/{source}/items.txt"
    if not os.path.isfile(path):
        return []
    with open(path) as f:
        return [l.strip() for l in f if l.strip()]
