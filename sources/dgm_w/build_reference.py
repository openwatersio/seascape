"""Build the SKN (chart datum) reference surface for the DGM-W tidal reaches.

Bespoke to this source, so it lives here rather than in pipelines/. The Snakefile's
``datum_surface_dgm_w`` rule runs it to compose ``store/datum/dgm_w_lowwater.tif`` — the height
of the local low-water datum in NHN, which the prep subtracts from the NHN riverbed
(``source_datum --offset-surface``) to get depth below low water.

Tidal — Seekartennull (SKN ~= LAT): the BSH "SKN-Fläche Nordsee 2026" grid (CC-BY 4.0), fetched
here, covers the sea/Watten/outer estuaries but stops at ~9.5 deg E; east of it the inner tidal
Elbe (Hamburg reach to the Geesthacht weir) is filled from the per-gauge SKN values in
``tideelbe_skn.csv`` via ``fill_corridor``: interpolate the per-gauge SKN along the gauge
polyline and paint it into a corridor around the river (beyond the corridor stays nodata, so
source_datum leaves those bed cells un-referenced and drops them). Interpolation is on arc-length
along the gauge line, not river-km; for a monotonic profile between the same gauge anchors the
two agree to a few cm — negligible against a datum that drifts ~0.02 m/km.

Refresh: re-download the BSH grid edition when BSH republishes; update tideelbe_skn.csv
alongside it. Not for navigation.
"""

import argparse
import csv
import os
import shutil
import zipfile

import numpy as np
import rasterio
import requests
import shapely
from shapely import LineString, points

BSH_ZIP = "https://gdi.bsh.de/de/data/Chart-datum-for-the-German-Bight-2026.zip"
BSH_MEMBER = "SKN-Flaeche_Nordsee_2026_NHN.tif"
HERE = os.path.dirname(__file__)
ELBE_CSV = os.path.join(HERE, "tideelbe_skn.csv")

CORRIDOR_DEG = 0.06   # ~6 km: only fill cells this close to the gauge line (keeps it to the river)
EAST_MARGIN = 0.25    # extend the SKN canvas this far east of the most-upstream inner-Elbe gauge


def read_gauges(path, value_col):
    """[(lon, lat, value_nhn, km)], km descending, from a checked-in gauge CSV."""
    rows = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(line for line in f if not line.lstrip().startswith("#")):
            rows.append((float(r["lon"]), float(r["lat"]), float(r[value_col]), float(r["km"])))
    return sorted(rows, key=lambda g: -g[3])


def _spine(gauges):
    """Gauge polyline (spine) + each gauge's arc-length along it, for projecting pixels on."""
    line = LineString([(g[0], g[1]) for g in gauges])
    gdist = np.asarray(shapely.line_locate_point(line, points(np.array([[g[0], g[1]] for g in gauges]))))
    return line, gdist


def fill_corridor(out, nodata, west, north, xres, yres, line, value_at, corridor_deg,
                  lat_range=None, col0=0):
    """Paint each nodata cell within corridor_deg of the spine with value_at(arc_length, lon, lat),
    the low-water datum along the river. In place. col0/lat_range bound the scan for speed."""
    height, width = out.shape
    lons = west + (np.arange(width) + 0.5) * xres
    lats = north + (np.arange(height) + 0.5) * yres
    if lat_range is not None:
        rows = np.nonzero((lats >= lat_range[0]) & (lats <= lat_range[1]))[0]
    else:
        rows = range(height)
    for row in rows:
        need = np.nonzero(out[row, col0:] == nodata)[0] + col0
        if need.size == 0:
            continue
        plon = lons[need]
        pts = points(np.column_stack([plon, np.full(need.shape, lats[row])]))
        near = shapely.distance(pts, line) < corridor_deg
        if not near.any():
            continue
        proj = shapely.line_locate_point(line, pts[near])
        out[row, need[near]] = value_at(proj, plon[near], np.full(int(near.sum()), lats[row])).astype("float32")


def build_tidal(ref_path):
    """SKN surface: BSH grid (west) + inner-Elbe gauge corridor (east) -> ref_path."""
    # BSH SKN grid — stream the zip to disk and copy the NHN member out, so memory stays
    # bounded regardless of the grid edition's size (no cached upstream archive)
    zip_path = f"{ref_path}.bsh.zip"
    with requests.get(BSH_ZIP, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(zip_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    bsh_path = f"{ref_path}.bsh.tif"
    with zipfile.ZipFile(zip_path) as z:
        with z.open(BSH_MEMBER) as m, open(bsh_path, "wb") as f:
            shutil.copyfileobj(m, f)
    os.remove(zip_path)

    gauges = read_gauges(ELBE_CSV, "skn_nhn_m")
    with rasterio.open(bsh_path) as bsh:
        prof = bsh.profile
        xres = prof["transform"].a
        west = prof["transform"].c
        north = prof["transform"].f
        yres = prof["transform"].e  # negative
        bsh_data = bsh.read(1)
        nodata = np.float32(bsh.nodata)
        height = bsh.height
    os.remove(bsh_path)

    # widen the canvas east to cover the inner Elbe, paste BSH (west), fill the corridor east
    east_edge = max(g[0] for g in gauges) + EAST_MARGIN
    new_width = max(int(np.ceil((east_edge - west) / xres)), bsh_data.shape[1])
    out = np.full((height, new_width), nodata, dtype="float32")
    out[:, : bsh_data.shape[1]] = bsh_data
    line, gdist = _spine(gauges)
    gval = np.array([g[2] for g in gauges])
    fill_corridor(out, nodata, west, north, xres, yres, line,
                  lambda proj, pl, pa: np.interp(proj, gdist, gval), CORRIDOR_DEG,
                  lat_range=(53.30, 53.95), col0=int((9.45 - west) / xres))

    prof.update(width=new_width, compress="deflate", tiled=True, blockxsize=512, blockysize=512)
    with rasterio.open(ref_path, "w", **prof) as dst:
        dst.write(out, 1)
    valid = out[out != nodata]
    print(f"wrote {ref_path}: {new_width}x{height}, SKN-in-NHN "
          f"{valid.min():.2f}..{valid.max():.2f} m over {valid.size:,} cells ({len(gauges)} inner-Elbe gauges)")
    return ref_path


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True, help="output GeoTIFF, e.g. store/datum/dgm_w_lowwater.tif")
    a = p.parse_args()
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    build_tidal(a.out)


if __name__ == "__main__":
    main()
