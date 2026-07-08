"""Pipeline configuration + source access.

Scripts run from the ``pipelines/`` directory; the
``store/`` working dirs are cwd-relative. Sources live in ``../sources/<id>/``,
each with ``metadata.json`` (attribution + bathymetry knobs) and ``file_list.txt``.
Ported from scripts/config.sh.
"""

import json
import os
from glob import glob

SOURCES_DIR = os.environ.get("SOURCES_DIR", "../sources")

# Standard INT isobaths (IHO S-4 B-411), metres, most-negative first. The shallow
# ladder (2/5/10/20/30) is also the S-52 safety-contour value set; fine depth detail
# between curves is the soundings layer's job, not extra isobaths.
CONTOUR_LEVELS = [int(x) for x in (
    "-10000 -8000 -6000 -5000 -4000 -3000 -2000 -1000 -500 -300 -200 "
    "-100 -50 -30 -20 -10 -5 -2"
).split()]

# Drying areas (green foreshore): seabed above chart datum that covers/uncovers with the tide —
# elevation in [0, DRYING_CAP] seaward of the OSM land line. The cap (metres, ~the Bay of Fundy
# tidal range) keeps S-102's above-MHW shoreline topo (piers, bluff toes reach +20..+30 m) out of
# the foreshore tint. Ceiling: a fixed global cap over-includes steep coastal topo just seaward of
# the land line; upgrade path is a spatially-varying MHW surface (VDatum et al.).
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


def source_path(source, filename):
    """A bounds.csv ``filename`` resolved to a GDAL-openable path. Three forms:

    1. The filename is already a full ``/vsi`` path (a streaming source like CUDEM,
       registered straight off a public bucket) — use it verbatim.
    2. ``SOURCE_VSI_BASE`` is set (the CI aggregate job reading prepared COGs from
       the public data bucket) — resolve ``<base>/<source>/<filename>``, e.g.
       ``/vsicurl/https://data.openwaters.io/bathymetry/source/gebco/gebco_0.tif``.
    3. Otherwise (local dev) — ``store/source/<source>/<filename>`` on disk.

    So locally-prepared sources stream from R2 in CI yet read from disk locally,
    with no change to how they're prepared. Everything is public ``/vsicurl`` — no
    credentials in the read path.
    """
    if filename.startswith("/vsi"):
        return filename
    base = os.environ.get("SOURCE_VSI_BASE")
    if base:
        return f"{base}/{source}/{filename}"
    return f"store/source/{source}/{filename}"


def file_list(source):
    path = f"{SOURCES_DIR}/{source}/file_list.txt"
    if not os.path.isfile(path):
        return []
    with open(path) as f:
        return [l.strip() for l in f if l.strip() and not l.lstrip().startswith("#")]
