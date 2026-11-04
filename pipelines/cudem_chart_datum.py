"""Measure where CUDEM ships on CHS chart datum, for ``datum_grid.CHART_DATUM_CELLS``.

CUDEM in the Bay of Fundy approaches is labelled NAVD88, but NCEI left the CHS surveys there on
chart datum and converted only the NOS ones, so the datum follows survey provenance, which CUDEM
does not publish. Subtracting the NAVD88 reference from a chart-datum bed corrects it twice and
charts it about 3 m too shallow; clearing the reference under a NAVD88 bed charts it 3 m too deep.
So the split is measured: raw CUDEM against CHS NONNA-10 (chart datum), per 0.01 deg cell, over
subtidal pixels both cover. Agreement within 1 m is chart datum; 2 m or more deeper than NONNA is
NAVD88 (S is about 3 m here). A cell counts as chart datum when at least half its measured
pixels agree, under a tenth read NAVD88 and no neighbour is 30% NAVD88; the rest keep the
reference, which errs shallow. Only subtidal pixels are measured, and only they qualify: the
drying fringe mixes in NAVD88 lidar, so datum_grid keeps the correction for beds above its floor.

The cells hold only for the tiles measured, so prep corrects any other tile over them in full
and warns. Re-run when it does, or when NONNA republishes here, and paste the two literals into datum_grid.py:
    uv run python cudem_chart_datum.py <work_dir>   # prints CHART_DATUM_CELLS and _TILES
"""

import os
import re
import subprocess
import sys

import numpy as np
import rasterio
from scipy.ndimage import binary_dilation

BOUNDS = (-67.25, 44.0, -66.70, 45.25)  # every CUDEM tile reaching Canadian Fundy waters
CELL = 0.01
GRID = 0.0002  # ~20 m: NONNA-10 and CUDEM both averaged onto it
# The CONUS urllists sources/cudem/file_list.txt fetches, both bands.
URLLISTS = ["https://noaa-nos-coastal-lidar-pds.s3.amazonaws.com/dem/"
            "NCEI_third_Topobathy_2014_8580/urllist8580.txt",
            "https://noaa-nos-coastal-lidar-pds.s3.amazonaws.com/dem/"
            "NCEI_ninth_Topobathy_2014_8483/urllist8483.txt"]
TILE = re.compile(r"/(ncei(\d+)_n(\d+)x(\d+)_w(\d+)x(\d+)_\w+)\.tif$")
NONNA = ("https://nonna-geoserver.data.chs-shc.ca/geoserver/wcs?service=WCS&version=2.0.1"
         "&request=GetCoverage&coverageId=nonna__NONNA%2010%20Coverage&format=image/tiff"
         "&subsettingCrs=http://www.opengis.net/def/crs/EPSG/0/4326"
         "&subset=Long({0},{2})&subset=Lat({1},{3})")
MIN_PX = 50       # ~0.02 km2 of overlap; NONNA-10 is patchy, and its gaps are where CUDEM shows
MIN_AGREE = 0.5   # share of a cell's pixels within 1 m of NONNA
MAX_NAVD88 = 0.1  # share reading 2 m or more deeper than NONNA
NEAR_NAVD88 = 0.3  # a neighbour this NAVD88 vetoes the cell


def tiles():
    """Every CUDEM tile URL over BOUNDS, from the live urllists: a tile is named by its
    north-west corner and spans 0.25 deg."""
    west, south, east, north = BOUNDS
    urls = []
    for listing in URLLISTS:
        text = subprocess.run(["curl", "-fsS", listing], check=True, capture_output=True,
                              text=True).stdout
        for url in text.split():
            m = TILE.search(url)
            if not m:
                continue
            n = int(m[3]) + int(m[4]) / 100
            w = -(int(m[5]) + int(m[6]) / 100)
            if w < east and w + 0.25 > west and n - 0.25 < north and n > south:
                urls.append(url)
    # 1/3" first, so the 1/9" tiles land on top where both cover.
    return sorted(urls, key=lambda u: TILE.search(u)[2] == "19")


def fetch(work):
    os.makedirs(work, exist_ok=True)
    paths = []
    for url in tiles():
        path = os.path.join(work, os.path.basename(url))
        if not os.path.isfile(path):
            subprocess.run(["curl", "-fsS", "-o", path, url], check=True)
        paths.append(path)
    nonna = os.path.join(work, "nonna10.tif")
    if not os.path.isfile(nonna):
        subprocess.run(["curl", "-fsS", "-o", nonna, NONNA.format(*BOUNDS)], check=True)
    vrt = os.path.join(work, "cudem.vrt")
    subprocess.run(["gdalbuildvrt", "-q", "-overwrite", vrt, *paths], check=True)
    return vrt, nonna, [os.path.splitext(os.path.basename(p))[0] for p in paths]


def on_grid(src, dst):
    subprocess.run(["gdalwarp", "-q", "-overwrite", "-t_srs", "EPSG:4326",
                    "-te", *map(str, BOUNDS), "-tr", str(GRID), str(GRID),
                    "-r", "average", "-dstnodata", "nan", "-ot", "Float32", src, dst], check=True)
    with rasterio.open(dst) as ds:
        return ds.read(1)


def classify(cudem, nonna, cell_px):
    """Chart-datum cells as a bool array (row 0 = north), from two co-registered beds."""
    ok = np.isfinite(cudem) & np.isfinite(nonna) & (cudem < -1) & (nonna < -1)
    d = np.where(ok, cudem - nonna, np.nan)
    rows, cols = d.shape[0] // cell_px, d.shape[1] // cell_px

    def per_cell(a):
        return a[:rows * cell_px, :cols * cell_px].reshape(
            rows, cell_px, cols, cell_px).sum(axis=(1, 3))
    n = per_cell(ok)
    agree = per_cell(ok & (np.abs(d) < 1))
    navd88 = per_cell(ok & (d <= -2))
    chart = (n >= MIN_PX) & (agree >= MIN_AGREE * n) & (navd88 <= MAX_NAVD88 * n)
    # A cell beside NAVD88 evidence keeps the reference: the survey seam runs through cells.
    return chart & ~binary_dilation((n >= MIN_PX) & (navd88 >= NEAR_NAVD88 * n),
                                    np.ones((3, 3), bool))


def runs(cells):
    """{south edge of a cell row: [(west, east), ...]} in degrees, rounded to the cell."""
    west, _, _, north = BOUNDS
    out = {}
    for r, row in enumerate(cells):
        edges = np.flatnonzero(np.diff(np.concatenate(([0], row.astype(np.int8), [0]))))
        if edges.size:
            south = round(north - (r + 1) * CELL, 2)
            out[south] = [(round(west + int(a) * CELL, 2), round(west + int(b) * CELL, 2))
                          for a, b in edges.reshape(-1, 2)]
    return out


def _check():
    cell_px = 2
    cudem = np.full((4, 10), -10.0)
    nonna = cudem.copy()
    nonna[:, 4:6] += 3.0  # CUDEM 3 m deeper: NAVD88, vetoing its neighbours
    nonna[0, 0] = np.nan  # unmeasured pixel: the cell still has 3 of 4
    cudem[2:, 8:] = 5.0   # land: unmeasured cell
    global MIN_PX
    MIN_PX, saved = 3, MIN_PX
    try:
        cells = classify(cudem, nonna, cell_px)
    finally:
        MIN_PX = saved
    assert cells.tolist() == [[True, False, False, False, True],
                              [True, False, False, False, False]], cells
    assert runs(np.array([[True, True, False, True]])) == {
        45.24: [(-67.25, -67.23), (-67.22, -67.21)]}
    print("cudem_chart_datum: ok")


def main():
    if sys.argv[1:] == ["--check"]:
        return _check()
    work = sys.argv[1]
    vrt, nonna, measured = fetch(work)
    cudem = on_grid(vrt, os.path.join(work, "cudem_grid.tif"))
    chs = on_grid(nonna, os.path.join(work, "nonna_grid.tif"))
    cells = classify(cudem, chs, round(CELL / GRID))
    print(f"# {int(cells.sum())} chart-datum cells of {cells.size}", file=sys.stderr)
    print("CHART_DATUM_CELLS = {")
    for south, spans in runs(cells).items():
        print(f"    {south}: {spans},")
    print("}")
    print(f"CHART_DATUM_TILES = {sorted(measured)}")


if __name__ == "__main__":
    main()
