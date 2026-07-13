"""Build the low-water (chart datum) reference surface for the DGM-W waterways.

Bespoke to this source, so it lives here rather than in pipelines/. Run from pipelines/ (the
Justfile does): it writes ``store/source/dgm_w/reference/`` — the height of the local low-water
datum in NHN, which ``source_datum --offset-surface`` subtracts from the NHN riverbed to get depth
below low water. The subdir keeps it out of the pipeline's ``store/source/<id>/*.tif`` globs.

Two independent reaches, each its own GeoTIFF, stitched into ``reference.vrt`` (the file the
Justfile passes to source_datum — a VRT so each reach keeps its own extent/resolution and warp
reads whichever one overlaps a tile):

  1. **Tidal — Seekartennull (SKN ~= LAT)**, ``skn_reference.tif``. The BSH "SKN-Fläche Nordsee
     2026" grid (CC-BY 4.0), fetched here, covers the sea/Watten/outer estuaries but stops at
     ~9.5 deg E; east of it the inner tidal Elbe (Hamburg reach to the Geesthacht weir) is filled
     from the per-gauge SKN values in ``tideelbe_skn.csv``, interpolated along the gauge line.

  2. **Free-flowing Rhein — GlW (gleichwertiger Wasserstand)**, ``glw_rhein.tif``. No packaged
     grid; the low-water longitudinal profile is assembled from the per-gauge GlW-in-NHN values in
     ``rhein_glw.csv`` (harvested from PEGELONLINE — see harvest_rhein_glw.py), interpolated along
     the gauge line the same way as the inner Elbe.

Both fills share ``fill_corridor``: interpolate the per-gauge low-water height along the gauge
polyline and paint it into a corridor around the river (beyond the corridor stays nodata, so
source_datum leaves those bed cells un-referenced and drops them). Interpolation is on arc-length
along the gauge line, not river-km; for a monotonic profile between the same gauge anchors the two
agree to a few cm — negligible against a datum that drifts ~0.02 m/km.

Refresh: re-download the BSH grid edition and re-run harvest_rhein_glw.py when BSH/GDWS/WSV
republish; update tideelbe_skn.csv alongside the BSH grid. Not for navigation.
"""

import csv
import io
import os
import subprocess
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
RHEIN_CSV = os.path.join(HERE, "rhein_glw.csv")

CORRIDOR_DEG = 0.06   # ~6 km: only fill cells this close to the gauge line (keeps it to the river)
EAST_MARGIN = 0.25    # extend the SKN canvas this far east of the most-upstream inner-Elbe gauge
RHEIN_RES = 0.001     # ~110 m: GlW is a smooth ramp, so a coarse grid resamples cleanly onto 2 m tiles
RHEIN_NODATA = -9999.0


def read_gauges(path, value_col):
    """[(lon, lat, value_nhn, km)], km descending, from a checked-in gauge CSV."""
    rows = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(line for line in f if not line.lstrip().startswith("#")):
            rows.append((float(r["lon"]), float(r["lat"]), float(r[value_col]), float(r["km"])))
    return sorted(rows, key=lambda g: -g[3])


def fill_corridor(out, nodata, west, north, xres, yres, gauges, corridor_deg,
                  lat_range=None, col0=0):
    """Paint each nodata cell within corridor_deg of the gauge line with the gauge low-water
    height, interpolated along the line. In place. col0/lat_range bound the scan for speed."""
    height, width = out.shape
    line = LineString([(g[0], g[1]) for g in gauges])
    gauge_dist = shapely.line_locate_point(line, points(np.array([[g[0], g[1]] for g in gauges])))
    gauge_val = np.array([g[2] for g in gauges], dtype="float64")

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
        pts = points(np.column_stack([lons[need], np.full(need.shape, lats[row])]))
        near = shapely.distance(pts, line) < corridor_deg
        if not near.any():
            continue
        proj = shapely.line_locate_point(line, pts[near])
        out[row, need[near]] = np.interp(proj, gauge_dist, gauge_val).astype("float32")


def build_tidal(out_dir):
    """SKN surface: BSH grid (west) + inner-Elbe gauge corridor (east) -> skn_reference.tif."""
    ref_path = f"{out_dir}/skn_reference.tif"

    # BSH SKN grid — extract the NHN band from the zip in memory (no cached upstream archive)
    r = requests.get(BSH_ZIP, timeout=120)
    r.raise_for_status()
    bsh_path = f"{out_dir}/_skn_bsh.tif"
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        with z.open(BSH_MEMBER) as m, open(bsh_path, "wb") as f:
            f.write(m.read())

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
    fill_corridor(out, nodata, west, north, xres, yres, gauges, CORRIDOR_DEG,
                  lat_range=(53.30, 53.95), col0=int((9.45 - west) / xres))

    prof.update(width=new_width, compress="deflate", tiled=True, blockxsize=512, blockysize=512)
    with rasterio.open(ref_path, "w", **prof) as dst:
        dst.write(out, 1)
    valid = out[out != nodata]
    print(f"wrote {ref_path}: {new_width}x{height}, SKN-in-NHN "
          f"{valid.min():.2f}..{valid.max():.2f} m over {valid.size:,} cells ({len(gauges)} inner-Elbe gauges)")
    return ref_path


def build_rhein(out_dir):
    """GlW surface: free-flowing Rhein gauge corridor on its own grid -> glw_rhein.tif."""
    ref_path = f"{out_dir}/glw_rhein.tif"
    gauges = read_gauges(RHEIN_CSV, "datum_nhn_m")

    lons_g = [g[0] for g in gauges]
    lats_g = [g[1] for g in gauges]
    west = min(lons_g) - CORRIDOR_DEG - RHEIN_RES
    north = max(lats_g) + CORRIDOR_DEG + RHEIN_RES
    width = int(np.ceil((max(lons_g) + CORRIDOR_DEG - west) / RHEIN_RES))
    height = int(np.ceil((north - (min(lats_g) - CORRIDOR_DEG)) / RHEIN_RES))

    out = np.full((height, width), RHEIN_NODATA, dtype="float32")
    fill_corridor(out, RHEIN_NODATA, west, north, RHEIN_RES, -RHEIN_RES, gauges, CORRIDOR_DEG)

    prof = dict(driver="GTiff", dtype="float32", count=1, width=width, height=height,
                crs="EPSG:4326", nodata=RHEIN_NODATA,
                transform=rasterio.transform.from_origin(west, north, RHEIN_RES, RHEIN_RES),
                compress="deflate", tiled=True, blockxsize=512, blockysize=512)
    with rasterio.open(ref_path, "w", **prof) as dst:
        dst.write(out, 1)
    valid = out[out != RHEIN_NODATA]
    print(f"wrote {ref_path}: {width}x{height}, GlW-in-NHN "
          f"{valid.min():.2f}..{valid.max():.2f} m over {valid.size:,} cells ({len(gauges)} Rhein gauges)")
    return ref_path


def main():
    out_dir = "store/source/dgm_w/reference"
    os.makedirs(out_dir, exist_ok=True)
    parts = [build_tidal(out_dir), build_rhein(out_dir)]
    vrt = f"{out_dir}/reference.vrt"
    subprocess.run(["gdalbuildvrt", "-overwrite", vrt, *parts], check=True)
    print(f"wrote {vrt} ({len(parts)} reach(es))")


if __name__ == "__main__":
    main()
