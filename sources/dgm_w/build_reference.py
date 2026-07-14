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
MAIN_ZS_CSV = os.path.join(HERE, "main_zs.csv")
MAIN_STAU_CSV = os.path.join(HERE, "main_stau.csv")
MAIN_CENTERLINE_WKT = os.path.join(HERE, "main_centerline.wkt")
RHEIN_STAU_CSV = os.path.join(HERE, "rhein_stau.csv")
RHEIN_CENTERLINE_WKT = os.path.join(HERE, "rhein_centerline.wkt")

CORRIDOR_DEG = 0.06   # ~6 km: only fill cells this close to the gauge line (keeps it to the river)
MAIN_CORRIDOR = 0.015  # ~1.5 km: the Main is a narrow river — hug it (the estuary width isn't needed)
RHEIN_UP_CORRIDOR = 0.02  # ~2 km: the upper Rhein channel/braids are wider than the Main
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


def build_freeflowing(out_dir, name, gauge_csv, river_wkt, corridor, label):
    """Ramp surface for a free-flowing river: a low-water datum interpolated along the gauges and
    filled over the OSM river corridor.

    The corridor is the river union (``river_wkt``), NOT the gauge polyline: gauges are ~30 km apart,
    so a corridor around their chord misses the wide outer channel at the big meanders (the Rhein's
    Düsseldorf/Kaiserswerth bends) and the river there falls outside the mask. The value is still the
    gauge ramp — each pixel projected onto the gauge line, the datum linearly interpolated by
    arc-length (low water drifts ~0.02 m/km, so the coarse chord is fine for the *value*; only the
    fill needed the river). Used for both the Rhein (GlW) and the Elbe (MNW proxy for the un-published
    GlW), the two un-impounded reaches."""
    gauges = read_gauges(gauge_csv, "datum_nhn_m")
    gline, gdist = _spine(gauges)
    gval = np.array([g[2] for g in gauges])

    def value_at(proj, plon, plat):  # interp the datum at each pixel's projection onto the gauge line
        s = np.asarray(shapely.line_locate_point(gline, points(np.column_stack([plon, plat]))))
        return np.interp(s, gdist, gval)

    river = shapely.from_wkt(open(river_wkt, encoding="utf-8").read())
    return _paint_corridor(river, value_at, corridor, f"{out_dir}/ramp_{name}.tif",
                           label, f"{len(gauges)} gauges")


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


def _paint_corridor(center, value_at, corridor, ref_path, label, tail):
    """Rasterize value_at over a corridor around `center` onto a bbox-sized EPSG:4326 grid (nodata
    outside the corridor). `center` may be a LineString (fill_corridor then passes each pixel's
    arc-length as proj) or a MultiLineString (proj is 0; value_at must key off lon/lat). `tail` is
    a short count string for the log line (e.g. "10 pools")."""
    w, s, e, n = center.bounds
    west, north = w - corridor - REACH_RES, n + corridor + REACH_RES
    width = int(np.ceil((e + corridor - west) / REACH_RES))
    height = int(np.ceil((north - (s - corridor)) / REACH_RES))
    out = np.full((height, width), REACH_NODATA, dtype="float32")
    fill_corridor(out, REACH_NODATA, west, north, REACH_RES, -REACH_RES, center, value_at, corridor)
    prof = dict(driver="GTiff", dtype="float32", count=1, width=width, height=height, crs="EPSG:4326",
                nodata=REACH_NODATA, transform=rasterio.transform.from_origin(west, north, REACH_RES, REACH_RES),
                compress="deflate", tiled=True, blockxsize=512, blockysize=512)
    with rasterio.open(ref_path, "w", **prof) as dst:
        dst.write(out, 1)
    valid = out[out != REACH_NODATA]
    print(f"wrote {ref_path}: {width}x{height}, {label} "
          f"{valid.min():.2f}..{valid.max():.2f} m over {valid.size:,} cells ({tail})")
    return ref_path


def build_rhein_upper(out_dir):
    """Stauziel surface: the impounded upper Rhein (Basel→Iffezheim) as pool steps -> zs_rhein.tif.

    Ten barrages from Kembs to Iffezheim hold a staircase of pools, each at its barrage's normal
    retention level (rhein_stau.csv). Unlike the Main, no weir lines are needed: this reach flows
    due north, so the barrage latitudes (strictly increasing downstream) are the pool dividers — a
    pixel takes the retention level of the first barrage at or north of it. The corridor follows the
    OSM navigation centerline (rhein_centerline.wkt: Grand Canal d'Alsace + canalised Rhine), which
    keeps the fill on the impounded channel and off the low Restrhein running alongside it."""
    barrages = []
    with open(RHEIN_STAU_CSV, encoding="utf-8") as f:
        for r in csv.DictReader(line for line in f if not line.lstrip().startswith("#")):
            barrages.append((float(r["lat"]), float(r["stauziel_nhn_m"])))
    barrages.sort()  # by latitude = upstream -> downstream
    blat = np.array([b[0] for b in barrages])
    blev = np.array([b[1] for b in barrages])
    assert (np.diff(blat) > 0).all(), "barrage latitudes must strictly increase downstream"

    def value_at(proj, plon, plat):  # first barrage at/north of the pixel -> its pool's Stauziel
        return blev[np.clip(np.searchsorted(blat, plat, side="left"), 0, len(blat) - 1)]

    center = shapely.from_wkt(open(RHEIN_CENTERLINE_WKT, encoding="utf-8").read())
    return _paint_corridor(center, value_at, RHEIN_UP_CORRIDOR,
                           f"{out_dir}/zs_rhein.tif", "Stauziel-in-NHN", f"{len(barrages)} pools")


def _read_stau_csv(path):
    """[(km, lat, lon, level_nhn)] from a barrage/weir CSV (columns km, lat, lon, stauziel_nhn_m)."""
    rows = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(line for line in f if not line.lstrip().startswith("#")):
            rows.append((float(r["km"]), float(r["lat"]), float(r["lon"]), float(r["stauziel_nhn_m"])))
    return rows


def build_impounded(out_dir, name, corridor_wkt, centerline_wkt, stau_csv, corridor, zs_csv=None):
    """Stauziel surface for a canalised river: pool steps assigned by arc-length along the river.

    The general form of build_main/build_rhein_upper, for a river of any orientation. Two geometries:
    the **corridor** is the raw OSM river union (``corridor_wkt`` — a MultiLineString is fine) and
    bounds the fill to the channel at full width; the **centerline** is a single ordered LineString
    following the channel (shortest path through the river graph), so a pixel's arc-length projected
    onto it places it in a pool. A coarse barrage chord fails at big meanders — the Cochem loop
    projects a mid-pool point past the next barrage — so the meander-following centerline is needed.
    The pixel takes the retention level of the barrage bounding its pool downstream; flow direction
    is inferred from the levels (they fall downstream), so river-km may run either way. With a ZS_I
    gauge CSV, each gauge pixel's assigned pool level is asserted against the gauge (0.5 m) —
    build_main's cross-check, which caught exactly the Cochem-loop misprojection above."""
    river = shapely.from_wkt(open(corridor_wkt, encoding="utf-8").read())
    center = shapely.from_wkt(open(centerline_wkt, encoding="utf-8").read())
    assert center.geom_type == "LineString", f"{name}: centerline must be one LineString"
    barr = _read_stau_csv(stau_csv)
    barc = np.asarray(shapely.line_locate_point(center, points([(b[2], b[1]) for b in barr])))
    lev = np.array([b[3] for b in barr])
    order = np.argsort(barc)  # by arc along the centerline (downstream->upstream, either km sense)
    barc, lev = barc[order], lev[order]
    falls = lev[0] >= lev[-1]  # level falls as arc grows -> downstream is +arc, else -arc

    def value_at(proj, plon, plat):  # arc-length on the centerline places the pixel in a pool
        s = np.asarray(shapely.line_locate_point(center, points(np.column_stack([plon, plat]))))
        idx = np.searchsorted(barc, s, side="left") if falls else np.searchsorted(barc, s, side="right") - 1
        return lev[np.clip(idx, 0, len(barc) - 1)]

    if zs_csv:  # a gauge sits in the pool whose retention level it reports
        for lon, lat, zs, km in read_gauges(zs_csv, "datum_nhn_m"):
            got = float(value_at(None, np.array([lon]), np.array([lat]))[0])
            assert abs(got - zs) < 0.5, f"{name} ZS_I km{km}: pool {got:.2f} != gauge {zs:.2f} — stale table/centerline?"

    return _paint_corridor(river, value_at, corridor, f"{out_dir}/zs_{name}.tif", "Stauziel-in-NHN",
                           f"{len(barr)} pools")


# Free-flowing (un-impounded) reaches built by build_freeflowing: name, gauge CSV (low-water datum
# in NHN), OSM-river corridor, corridor half-width (deg), log label. GlW for the Rhein; MNW for the
# Elbe (open proxy for the un-published GlW — see harvest_gauges.py).
FREEFLOWING = [
    ("rhein", "rhein_glw.csv", "rhein_river.wkt", 0.025, "GlW-in-NHN"),
    ("elbe",  "elbe_mnw.csv",  "elbe_river.wkt",  0.03,  "MNW-in-NHN"),
]

# Canalised Stauziel rivers built by build_impounded: name, OSM-river corridor, centerline (shortest
# path for arc-length pools), barrage table, corridor half-width (deg), ZS_I cross-check CSV.
IMPOUNDED = [
    ("mosel", "mosel_river.wkt", "mosel_centerline.wkt", "mosel_stau.csv", 0.015, "mosel_zs.csv"),
    ("saar",  "saar_river.wkt",  "saar_centerline.wkt",  "saar_stau.csv",  0.012, "saar_zs.csv"),
    ("lahn",  "lahn_river.wkt",  "lahn_centerline.wkt",  "lahn_stau.csv",  0.008, "lahn_zs.csv"),
]


def main():
    out_dir = "store/source/dgm_w/reference"
    os.makedirs(out_dir, exist_ok=True)
    parts = [build_tidal(out_dir), build_main(out_dir), build_rhein_upper(out_dir)]
    parts += [build_freeflowing(out_dir, name, os.path.join(HERE, gauge), os.path.join(HERE, riv), corr, label)
              for name, gauge, riv, corr, label in FREEFLOWING]
    parts += [build_impounded(out_dir, name, os.path.join(HERE, riv), os.path.join(HERE, cl),
                              os.path.join(HERE, stau), corr, os.path.join(HERE, zs))
              for name, riv, cl, stau, corr, zs in IMPOUNDED]
    vrt = f"{out_dir}/reference.vrt"
    subprocess.run(["gdalbuildvrt", "-overwrite", vrt, *parts], check=True)
    print(f"wrote {vrt} ({len(parts)} reach(es))")


if __name__ == "__main__":
    main()
