#!/usr/bin/env python3
"""Regenerate sources/nonna_10 and sources/nonna_100 file_list.txt — a one-off tool (NOT part of
the build). It lives outside both source dirs so editing it changes neither recipe hash (raws are
temp, so a recipe change refetches the whole national WCS pull).

CHS publishes NONNA through a public GeoServer WCS (no account) and its tile indexes as public
shapefiles on the open.canada.ca record. Each file_list line is one WCS GetCoverage request,
subset in the coverage's native EPSG:3857, so the engine fetches, retries and resumes them like
any static URL. Re-run when CHS republishes the index (the record's "Date Modified"):

  python scripts/nonna_harvest.py                  # all of Canada, rewrites both lists
  python scripts/nonna_harvest.py --allow-shrink   # ...accepting cells CHS withdrew
  python scripts/nonna_harvest.py -67,44,-66,45         # W,S,E,N — print a regional prototype's URLs only

Request units: NONNA-100 one per 1° index cell (~150 m px, small); NONNA-10 one per 0.5° bin
that holds a 0.1° index cell (~50 MB / ~60 s each at 45°N). Stdlib + the `ogr2ogr` CLI.
"""

import csv
import io
import math
import os
import subprocess
import sys
import tempfile
import urllib.request

INDEX = "https://api-proxy.edh-cde.dfo-mpo.gc.ca/catalogue/records/d3881c4c-650d-4070-bf9b-1e00aabf0a1d/attachments/{0}.zip"
WCS = ("https://nonna-geoserver.data.chs-shc.ca/geoserver/wcs?service=WCS&version=2.0.1"
       "&request=GetCoverage&coverageId=nonna__NONNA%20{0}%20Coverage&format=image/tiff"
       # lossless; the sparse survey bins shrink up to ~450x and dense ones ~2x in transfer
       "&geotiff:compression=DEFLATE"
       "&subset=X({1:.2f},{3:.2f})&subset=Y({2:.2f},{4:.2f})")
SOURCES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sources")
R = 6378137.0


def mercator(lon, lat):
    lat = max(-85.05112878, min(85.05112878, lat))
    return R * math.radians(lon), R * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def cells(layer):
    """(w, s, e, n) of every index cell, read via the ogr2ogr CLI."""
    sql = (f"select MbrMinX(geometry) w, MbrMinY(geometry) s, MbrMaxX(geometry) e, "
           f"MbrMaxY(geometry) n from {layer}")
    with tempfile.NamedTemporaryFile(suffix=".zip") as z:
        # the API proxy serves no byte ranges, so /vsicurl can't open the zip in place
        z.write(urllib.request.urlopen(INDEX.format(layer), timeout=300).read())
        z.flush()
        out = subprocess.run(["ogr2ogr", "-f", "CSV", "/vsistdout/", f"/vsizip/{z.name}",
                              "-dialect", "sqlite", "-sql", sql],
                             check=True, capture_output=True, text=True).stdout
    return [tuple(float(r[k]) for k in "wsen") for r in csv.DictReader(io.StringIO(out))]


def bins(cells, step):
    """Every step-degree grid square a cell overlaps (Arctic cells are 0.2-0.4° wide and cross
    bin edges); step=None keeps each cell as its own unit."""
    if step is None:
        return sorted(set(cells))
    lo = lambda v: math.floor(v / step + 1e-9)
    hi = lambda v: math.ceil(v / step - 1e-9)
    keys = {(i, j) for w, s, e, n in cells
            for i in range(lo(w), hi(e)) for j in range(lo(s), hi(n))}
    return sorted((i * step, j * step, (i + 1) * step, (j + 1) * step) for i, j in keys)


def intersects(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def urls(res, units):
    # The WCS grid is fixed Web Mercator, ~2x finer than NONNA's native cells north of 68°N
    return [WCS.format(res, *mercator(w, s), *mercator(e, n)) + ("&scaleFactor=0.5" if s >= 68 else "")
            for w, s, e, n in units]


def write(source, lines, allow_shrink):
    """Replace the list below the header. A static list has no enumerate-time shrink guard, and
    a shrunken one makes prep drop the missing COGs and publish sync them away, so a shrink needs
    --allow-shrink (CHS does withdraw cells for reprocessing)."""
    path = os.path.join(SOURCES, source, "file_list.txt")
    text = open(path).read()
    header, _, body = text.partition("\n# --\n")
    old = sum(1 for l in body.splitlines() if l.startswith("https"))
    if len(lines) < old and not allow_shrink:
        sys.exit(f"{source}: index gives {len(lines)} requests, list has {old}; "
                 "rerun with --allow-shrink to drop the rest")
    with open(path, "w") as f:
        f.write(header + "\n# --\n" + "\n".join(lines) + "\n")
    print(f"{source}: {len(lines)} request(s)")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    units = {100: bins(cells("NONNA100"), None), 10: bins(cells("NONNA10"), 0.5)}
    if args:
        bbox = tuple(float(x) for x in args[0].split(","))
        for res, us in units.items():
            print(f"# nonna_{res}", *urls(res, [u for u in us if intersects(u, bbox)]), sep="\n")
        return
    for res, us in units.items():
        write(f"nonna_{res}", urls(res, us), "--allow-shrink" in sys.argv)


def _check():
    x, y = mercator(-66.2, 44.6)
    assert abs(x - -7369350.29) < 0.01 and abs(y - 5558767.96) < 0.01, (x, y)  # gdaltransform
    assert bins([(-66.95, 44.05, -66.85, 44.15), (-66.6, 44.4, -66.5, 44.5)], 0.5) == \
        [(-67.0, 44.0, -66.5, 44.5)]
    assert bins([(-66.4, 44.6, -66.3, 44.7)], 0.5) == [(-66.5, 44.5, -66.0, 45.0)]
    # an Arctic 0.2° cell across a 0.5° edge lands in both bins
    assert bins([(-133.6, 69.7, -133.4, 69.8)], 0.5) == \
        [(-134.0, 69.5, -133.5, 70.0), (-133.5, 69.5, -133.0, 70.0)]
    assert urls(10, [(-67.0, 68.0, -66.5, 68.5)])[0].endswith("&scaleFactor=0.5")
    print("harvest self-check: ok")


if __name__ == "__main__":
    _check() if sys.argv[1:2] == ["--check"] else main()
