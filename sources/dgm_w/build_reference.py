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
MAIN_ZS_CSV = os.path.join(HERE, "main_zs.csv")
MAIN_STAU_CSV = os.path.join(HERE, "main_stau.csv")
MAIN_CENTERLINE_WKT = os.path.join(HERE, "main_centerline.wkt")

CORRIDOR_DEG = 0.06   # ~6 km: only fill cells this close to the gauge line (keeps it to the river)
MAIN_CORRIDOR = 0.015  # ~1.5 km: the Main is a narrow river — hug it (the estuary width isn't needed)
EAST_MARGIN = 0.25    # extend the SKN canvas this far east of the most-upstream inner-Elbe gauge
REACH_RES = 0.001     # ~110 m: a smooth ramp / flat pools resample cleanly onto 2 m tiles
REACH_NODATA = -9999.0


def read_gauges(path, value_col):
    """[(lon, lat, value_nhn, km)], km descending, from a checked-in gauge CSV."""
    rows = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(line for line in f if not line.lstrip().startswith("#")):
            rows.append((float(r["lon"]), float(r["lat"]), float(r[value_col]), float(r["km"])))
    return sorted(rows, key=lambda g: -g[3])


def read_barrages(path):
    """[{km, stau, weir}] sorted by km, from main_stau.csv. weir is ((lon1,lat1),(lon2,lat2)) —
    the channel-crossing weir line used as the pool divider — or None where OSM has no weir way."""
    rows = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(line for line in f if not line.lstrip().startswith("#")):
            weir = None
            if r.get("lon1", "").strip():
                weir = ((float(r["lon1"]), float(r["lat1"])), (float(r["lon2"]), float(r["lat2"])))
            rows.append({"km": float(r["km"]), "stau": float(r["stauziel_nhn_m"]), "weir": weir})
    return sorted(rows, key=lambda b: b["km"])


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
        # arc-length along the spine (only the ramp reaches need it; a MultiLineString corridor
        # geometry — the Main's real centerline — has no single arc-length, and value_at ignores it)
        proj = shapely.line_locate_point(line, pts[near]) if line.geom_type == "LineString" \
            else np.zeros(int(near.sum()))
        out[row, need[near]] = value_at(proj, plon[near], np.full(int(near.sum()), lats[row])).astype("float32")


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


def _corridor_reach(spine_pts, value_at_factory, ref_path, label, corridor_deg=CORRIDOR_DEG):
    """Fill a corridor reference raster sized to the spine bbox. value_at_factory(line, gdist)
    returns the value_at(arc_length, lon, lat) function; corridor_deg hugs narrow rivers tighter."""
    lons_g = [p[0] for p in spine_pts]
    lats_g = [p[1] for p in spine_pts]
    west = min(lons_g) - corridor_deg - REACH_RES
    north = max(lats_g) + corridor_deg + REACH_RES
    width = int(np.ceil((max(lons_g) + corridor_deg - west) / REACH_RES))
    height = int(np.ceil((north - (min(lats_g) - corridor_deg)) / REACH_RES))

    out = np.full((height, width), REACH_NODATA, dtype="float32")
    line, gdist = _spine(spine_pts)
    fill_corridor(out, REACH_NODATA, west, north, REACH_RES, -REACH_RES, line,
                  value_at_factory(line, gdist), corridor_deg)

    prof = dict(driver="GTiff", dtype="float32", count=1, width=width, height=height,
                crs="EPSG:4326", nodata=REACH_NODATA,
                transform=rasterio.transform.from_origin(west, north, REACH_RES, REACH_RES),
                compress="deflate", tiled=True, blockxsize=512, blockysize=512)
    with rasterio.open(ref_path, "w", **prof) as dst:
        dst.write(out, 1)
    valid = out[out != REACH_NODATA]
    print(f"wrote {ref_path}: {width}x{height}, {label} "
          f"{valid.min():.2f}..{valid.max():.2f} m over {valid.size:,} cells ({len(spine_pts)} spine pts)")
    return ref_path


def build_rhein(out_dir):
    """GlW surface: free-flowing Rhein gauge corridor, linearly interpolated -> glw_rhein.tif."""
    gauges = read_gauges(RHEIN_CSV, "datum_nhn_m")
    gval = np.array([g[2] for g in gauges])
    factory = lambda line, gdist: (lambda proj, plon, plat: np.interp(proj, gdist, gval))
    return _corridor_reach(gauges, factory, f"{out_dir}/glw_rhein.tif", "GlW-in-NHN")


def build_main(out_dir):
    """Stauziel surface: impounded Main as pool steps divided at the actual weirs -> zs_main.tif.

    Pool levels come from the complete per-barrage Stauziel table (main_stau.csv), since the DEM
    reach has more pools than PEGELONLINE gauges; the gauges (main_zs.csv) are only a cross-check.
    Each barrage's OSM weir line is the exact pool divider — a pixel takes the Stauziel of the
    barrage whose weir is immediately downstream of it (the count of weir lines it lies upstream of).
    The corridor follows the real OSM Main centerline (main_centerline.wkt) so it hugs every meander
    rather than a coarse gauge chord that the river escapes at bends."""
    gauges = read_gauges(MAIN_ZS_CSV, "datum_nhn_m")
    barrages = read_barrages(MAIN_STAU_CSV)
    bkm = np.array([b["km"] for b in barrages])
    ball = np.array([b["stau"] for b in barrages])

    def stau_at_km(km):  # cross-check helper: pool level by km
        return ball[np.clip(np.searchsorted(bkm, km, side="right") - 1, 0, len(bkm) - 1)]

    for lon, lat, zs_i, km in gauges:  # each gauge's ZS_I must match its pool's Stauziel
        assert abs(zs_i - stau_at_km(km)) < 0.15, \
            f"main gauge km{km} ZS_I {zs_i} != pool Stauziel {stau_at_km(km):.2f} — stale table?"

    center = shapely.from_wkt(open(MAIN_CENTERLINE_WKT, encoding="utf-8").read())

    # one divider per barrage weir in the DEM reach; the upstream side is the one toward the next
    # barrage upstream (its weir midpoint), so no river-km projection or spine is needed.
    div = [b for b in barrages if b["weir"] and b["km"] <= 105]
    mids = [np.array([(b["weir"][0][0] + b["weir"][1][0]) / 2,
                      (b["weir"][0][1] + b["weir"][1][1]) / 2]) for b in div]
    dividers, dstau = [], []
    for i, b in enumerate(div):
        a, bb = np.array(b["weir"][0]), np.array(b["weir"][1])
        up = mids[i + 1] if i + 1 < len(div) else mids[i] + (mids[i] - mids[i - 1])
        d = bb - a
        dividers.append((a, bb, np.sign(d[0] * (up[1] - a[1]) - d[1] * (up[0] - a[0]))))
        dstau.append(b["stau"])
    dstau = np.array(dstau)

    def value_at(proj, plon, plat):
        n_up = np.zeros(plon.shape)
        for a, bb, usign in dividers:
            d = bb - a
            n_up += np.sign(d[0] * (plat - a[1]) - d[1] * (plon - a[0])) == usign
        return dstau[np.clip(n_up.astype(int) - 1, 0, len(dividers) - 1)]

    w, s, e, n = center.bounds
    west, north = w - MAIN_CORRIDOR - REACH_RES, n + MAIN_CORRIDOR + REACH_RES
    width = int(np.ceil((e + MAIN_CORRIDOR - west) / REACH_RES))
    height = int(np.ceil((north - (s - MAIN_CORRIDOR)) / REACH_RES))
    out = np.full((height, width), REACH_NODATA, dtype="float32")
    fill_corridor(out, REACH_NODATA, west, north, REACH_RES, -REACH_RES, center, value_at, MAIN_CORRIDOR)

    ref_path = f"{out_dir}/zs_main.tif"
    prof = dict(driver="GTiff", dtype="float32", count=1, width=width, height=height, crs="EPSG:4326",
                nodata=REACH_NODATA, transform=rasterio.transform.from_origin(west, north, REACH_RES, REACH_RES),
                compress="deflate", tiled=True, blockxsize=512, blockysize=512)
    with rasterio.open(ref_path, "w", **prof) as dst:
        dst.write(out, 1)
    valid = out[out != REACH_NODATA]
    print(f"wrote {ref_path}: {width}x{height}, Stauziel-in-NHN "
          f"{valid.min():.2f}..{valid.max():.2f} m over {valid.size:,} cells ({len(dividers)} pools)")
    return ref_path


def main():
    out_dir = "store/source/dgm_w/reference"
    os.makedirs(out_dir, exist_ok=True)
    parts = [build_tidal(out_dir), build_rhein(out_dir), build_main(out_dir)]
    vrt = f"{out_dir}/reference.vrt"
    subprocess.run(["gdalbuildvrt", "-overwrite", vrt, *parts], check=True)
    print(f"wrote {vrt} ({len(parts)} reach(es))")


if __name__ == "__main__":
    main()
