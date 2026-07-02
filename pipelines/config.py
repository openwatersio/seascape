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

# Non-uniform bathymetric contour levels (metres, most-negative first): fine in
# the shallows, coarse in the deep. Ported from scripts/config.sh.
CONTOUR_LEVELS = [int(x) for x in (
    "-10000 -8000 -6000 -5000 -4000 -3000 -2000 -1500 -1000 -500 -200 -150 "
    "-100 -75 -50 -45 -40 -35 -30 -25 -20 -15 -14 -13 -12 -11 -10 -9 -8 -7 -6 "
    "-5 -4 -3 -2 -1"
).split()]

# Feet/fathom isobaths: a second contour set at the classic fathom curves. Friendly feet depths
# (6, 12, 18, 30, 60, 120, 180, 300, 600 ft …) are exactly whole fathoms in feet, so one geometry
# labels as either — the viewer picks feet or fathoms. In metres (negative, positive-down) for
# gdal_contour. Full depth range so feet/fathom mode has isobaths everywhere, not just the shelf.
FATHOM_CURVES = [1, 2, 3, 5, 10, 20, 30, 50, 100, 200, 300, 500, 1000, 2000, 3000, 5000]
# Ascending (deepest first, like CONTOUR_LEVELS) — gdal_contour -fl needs strictly increasing.
CONTOUR_LEVELS_FT = sorted(round(-fm * 1.8288, 4) for fm in FATHOM_CURVES)


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
