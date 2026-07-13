"""Build the low-water (chart datum) reference surface for the DGM-W tidal reaches.

Writes ``store/source/dgm_w/skn_reference.tif`` — the height of Seekartennull (SKN, the
German chart datum ~= LAT) in NHN, which ``source_datum --offset-surface`` subtracts from
the NHN riverbed to get depth below chart datum.

Two pieces, merged into one EPSG:4326 raster:

  1. **Outer estuaries + open German Bight** — the BSH "SKN-Fläche Nordsee 2026" grid
     (CC-BY 4.0), a published surface of SKN in NHN. Covers the sea, Watten, and the outer
     estuaries, but its east edge is ~9.5 deg E, so it stops short of the inner tidal Elbe.

  2. **Inner tidal Elbe (Hamburg reach, east of the grid up to the Geesthacht weir)** — no
     packaged grid reaches here, so we assemble it from the GDWS per-gauge SKN values
     ("Aktuelles Seekartennull an den Tidepegeln ... ab 2026") placed at each gauge's
     river-km / position (PEGELONLINE), interpolated along the gauge polyline. SKN in the
     tidal Elbe is ~-1.9 m NHN, tapering to ~-1.2 m near Zollenspieker; above the weir it is
     non-tidal (no SKN), so the profile is clamped at the most-upstream gauge.

Both are the 2026 vintage; when BSH republishes, refresh the grid URL and the gauge table
together. Not for navigation.
"""

import os
import sys
import zipfile

import numpy as np
import rasterio
import shapely
from shapely import LineString, points

import utils

BSH_ZIP = "https://gdi.bsh.de/de/data/Chart-datum-for-the-German-Bight-2026.zip"
BSH_MEMBER = "SKN-Flaeche_Nordsee_2026_NHN.tif"

# Tideelbe gauges, downstream -> upstream. skn_nhn = SKN height in NHN (m, negative = below
# NHN). SKN from the GDWS 2026 Tidepegel table; km + lon/lat from PEGELONLINE.
GAUGES = [
    # (lon,       lat,        skn_nhn)   # gauge (Elbe-km)
    (8.717425, 53.867686, -2.01),        # Cuxhaven Steubenhöft (724.0)
    (9.409430, 53.784361, -1.93),        # Glückstadt (674.0)
    (9.526602, 53.629729, -1.92),        # Stadersand (654.9)
    (9.880859, 53.540164, -1.92),        # Seemannshöft (628.9)
    (9.969965, 53.545442, -1.90),        # Hamburg St. Pauli (623.1)
    (9.991826, 53.472726, -1.92),        # Hamburg-Harburg (615.0)
    (10.101120, 53.428692, -1.73),       # Over (605.3)
    (10.185374, 53.398709, -1.23),       # Zollenspieker (598.2, most-upstream tidal gauge)
]

CORRIDOR_DEG = 0.06   # ~6 km: only fill cells this close to the gauge line (keeps it to the river)
EAST_EDGE = 10.45     # extend the canvas east far enough to cover the Zollenspieker reach


def main():
    # A subdir, so the pipeline's non-recursive store/source/<id>/*.tif globs (download,
    # datum, normalize, bounds) never pick the reference up as a data tile.
    out_dir = "store/source/dgm_w/reference"
    os.makedirs(out_dir, exist_ok=True)
    ref_path = f"{out_dir}/skn_reference.tif"

    # 1) fetch + extract the BSH SKN-Fläche NHN grid
    zip_path = f"{out_dir}/_skn_bsh.zip"
    if not os.path.exists(zip_path):
        print(f"fetching {BSH_ZIP}")
        utils.http_download(BSH_ZIP, zip_path)
    with zipfile.ZipFile(zip_path) as z:
        with z.open(BSH_MEMBER) as src, open(f"{out_dir}/_skn_bsh.tif", "wb") as dst:
            dst.write(src.read())
    bsh_path = f"{out_dir}/_skn_bsh.tif"

    with rasterio.open(bsh_path) as bsh:
        prof = bsh.profile
        xres = prof["transform"].a
        west = prof["transform"].c
        north = prof["transform"].f
        yres = prof["transform"].e  # negative
        bsh_data = bsh.read(1)
        bsh_nodata = bsh.nodata
        height = bsh.height

    # 2) new canvas: same origin/res as BSH, widened east to EAST_EDGE
    new_width = int(np.ceil((EAST_EDGE - west) / xres))
    new_width = max(new_width, bsh_data.shape[1])
    nodata = np.float32(bsh_nodata)
    out = np.full((height, new_width), nodata, dtype="float32")
    out[:, : bsh_data.shape[1]] = bsh_data  # paste BSH values (west)

    # 3) fill the inner-Elbe corridor where the canvas is still nodata
    line = LineString([(g[0], g[1]) for g in GAUGES])
    gpts = points(np.array([[g[0], g[1]] for g in GAUGES]))
    gauge_dist = shapely.line_locate_point(line, gpts)   # along-line distance of each gauge
    gauge_skn = np.array([g[2] for g in GAUGES], dtype="float64")

    cols = np.arange(new_width)
    lons = west + (cols + 0.5) * xres
    rows = np.arange(height)
    lats = north + (rows + 0.5) * yres

    # restrict work to the eastern band (lon >= BSH east edge) and the Elbe latitude range
    east_col0 = int((9.45 - west) / xres)
    lat_lo, lat_hi = 53.30, 53.95
    row_mask = (lats >= lat_lo) & (lats <= lat_hi)
    for r in np.nonzero(row_mask)[0]:
        need = np.nonzero((out[r, east_col0:] == nodata))[0] + east_col0
        if need.size == 0:
            continue
        cell_lon = lons[need]
        cell_lat = np.full(cell_lon.shape, lats[r])
        pts = points(np.column_stack([cell_lon, cell_lat]))
        d = shapely.distance(pts, line)
        near = d < CORRIDOR_DEG
        if not near.any():
            continue
        proj = shapely.line_locate_point(line, pts[near])
        skn = np.interp(proj, gauge_dist, gauge_skn)  # clamps to end gauges (weir end -> Zollenspieker)
        cols_to_set = need[near]
        out[r, cols_to_set] = skn.astype("float32")

    prof.update(width=new_width, compress="deflate", tiled=True,
                blockxsize=512, blockysize=512)
    with rasterio.open(ref_path, "w", **prof) as dst:
        dst.write(out, 1)

    valid = out[out != nodata]
    print(f"wrote {ref_path}: {out.shape[1]}x{out.shape[0]}, "
          f"SKN-in-NHN {valid.min():.2f}..{valid.max():.2f} m over {valid.size:,} cells")
    os.remove(bsh_path)


if __name__ == "__main__":
    main()
