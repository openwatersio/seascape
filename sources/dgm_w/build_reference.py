"""Build the low-water (chart datum) reference surface for the DGM-W tidal reaches.

Bespoke to this source, so it lives here rather than in pipelines/. Run from pipelines/ (the
Justfile does): it writes ``store/source/dgm_w/reference/skn_reference.tif`` — the height of
Seekartennull (SKN, the German chart datum ~= LAT) in NHN, which ``source_datum
--offset-surface`` subtracts from the NHN riverbed to get depth below chart datum. The subdir
keeps it out of the pipeline's ``store/source/<id>/*.tif`` globs.

Two pieces, merged into one EPSG:4326 raster:

  1. **Outer estuaries + open German Bight** — the BSH "SKN-Fläche Nordsee 2026" grid
     (CC-BY 4.0), a published surface of SKN in NHN, fetched here. Covers the sea, Watten, and
     the outer estuaries, but its east edge is ~9.5 deg E, short of the inner tidal Elbe.

  2. **Inner tidal Elbe (Hamburg reach, east of the grid up to the Geesthacht weir)** — no
     packaged grid reaches here, so it is assembled from the per-gauge SKN values in the
     checked-in ``tideelbe_skn.csv`` (transcribed from the GDWS Tidepegel table; see that file
     for the source links), interpolated along the gauge polyline. Above the weir is non-tidal
     (no SKN), so the profile is clamped at the most-upstream gauge.

Refresh: re-download the BSH grid edition and update tideelbe_skn.csv together. Not for navigation.
"""

import csv
import io
import os
import zipfile

import numpy as np
import rasterio
import requests
import shapely
from shapely import LineString, points

BSH_ZIP = "https://gdi.bsh.de/de/data/Chart-datum-for-the-German-Bight-2026.zip"
BSH_MEMBER = "SKN-Flaeche_Nordsee_2026_NHN.tif"
GAUGE_CSV = os.path.join(os.path.dirname(__file__), "tideelbe_skn.csv")

CORRIDOR_DEG = 0.06   # ~6 km: only fill cells this close to the gauge line (keeps it to the river)
EAST_MARGIN = 0.25    # extend the canvas this far east of the most-upstream gauge


def read_gauges():
    """[(lon, lat, skn_nhn, km)] downstream -> upstream, from the checked-in CSV."""
    rows = []
    with open(GAUGE_CSV, encoding="utf-8") as f:
        for r in csv.DictReader(line for line in f if not line.lstrip().startswith("#")):
            rows.append((float(r["lon"]), float(r["lat"]), float(r["skn_nhn_m"]), float(r["km"])))
    return sorted(rows, key=lambda g: -g[3])  # km descending


def main():
    out_dir = "store/source/dgm_w/reference"
    os.makedirs(out_dir, exist_ok=True)
    ref_path = f"{out_dir}/skn_reference.tif"

    # 1) BSH SKN grid — extract the NHN band from the zip in memory (no cached upstream archive)
    r = requests.get(BSH_ZIP, timeout=120)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        with z.open(BSH_MEMBER) as m, open(f"{out_dir}/_skn_bsh.tif", "wb") as f:
            f.write(m.read())
    bsh_path = f"{out_dir}/_skn_bsh.tif"

    gauges = read_gauges()
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

    # 2) widen the canvas east to cover the inner Elbe, paste BSH (west), fill the corridor east
    east_edge = max(g[0] for g in gauges) + EAST_MARGIN
    new_width = max(int(np.ceil((east_edge - west) / xres)), bsh_data.shape[1])
    out = np.full((height, new_width), nodata, dtype="float32")
    out[:, : bsh_data.shape[1]] = bsh_data

    line = LineString([(g[0], g[1]) for g in gauges])
    gauge_dist = shapely.line_locate_point(line, points(np.array([[g[0], g[1]] for g in gauges])))
    gauge_skn = np.array([g[2] for g in gauges], dtype="float64")

    lons = west + (np.arange(new_width) + 0.5) * xres
    lats = north + (np.arange(height) + 0.5) * yres
    east_col0 = int((9.45 - west) / xres)
    for row in np.nonzero((lats >= 53.30) & (lats <= 53.95))[0]:
        need = np.nonzero(out[row, east_col0:] == nodata)[0] + east_col0
        if need.size == 0:
            continue
        pts = points(np.column_stack([lons[need], np.full(need.shape, lats[row])]))
        near = shapely.distance(pts, line) < CORRIDOR_DEG
        if not near.any():
            continue
        proj = shapely.line_locate_point(line, pts[near])
        out[row, need[near]] = np.interp(proj, gauge_dist, gauge_skn).astype("float32")

    prof.update(width=new_width, compress="deflate", tiled=True, blockxsize=512, blockysize=512)
    with rasterio.open(ref_path, "w", **prof) as dst:
        dst.write(out, 1)
    valid = out[out != nodata]
    print(f"wrote {ref_path}: {new_width}x{height}, SKN-in-NHN "
          f"{valid.min():.2f}..{valid.max():.2f} m over {valid.size:,} cells ({len(gauges)} inner-Elbe gauges)")


if __name__ == "__main__":
    main()
