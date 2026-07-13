"""Build the low-water (chart datum) reference surface for the DGM-W tidal reaches.

Bespoke to this source, so it lives here rather than in pipelines/. Run from pipelines/ (the
Justfile does): it writes ``store/source/dgm_w/reference/skn_reference.tif`` — the height of
Seekartennull (SKN, the German chart datum ~= LAT) in NHN, which ``source_datum
--offset-surface`` subtracts from the NHN riverbed to get depth below chart datum. The subdir
keeps it out of the pipeline's ``store/source/<id>/*.tif`` globs.

Every value is pulled from an authoritative source at build time (nothing hard-coded but the
source URLs and the list of tidal-Elbe gauge names):

  1. **Outer estuaries + open German Bight** — the BSH "SKN-Fläche Nordsee 2026" grid
     (CC-BY 4.0), a published surface of SKN in NHN. Covers the sea, Watten, and the outer
     estuaries, but its east edge is ~9.5 deg E, short of the inner tidal Elbe.

  2. **Inner tidal Elbe (Hamburg reach, east of the grid up to the Geesthacht weir)** — no
     packaged grid reaches here, so it is assembled from the GDWS per-gauge SKN table
     ("Aktuelles Seekartennull an den Tidepegeln ... ab 2026", parsed from the PDF) placed at
     each gauge's position + river-km (PEGELONLINE) and interpolated along the gauge polyline.
     Above the weir is non-tidal (no SKN), so the profile is clamped at the most-upstream gauge.

Refresh happens automatically when BSH/GDWS republish (the URLs point at the current edition).
Not for navigation.
"""

import io
import os
import re
import sys
import zipfile

import numpy as np
import pdfplumber
import rasterio
import requests
import shapely
from shapely import LineString, points

BSH_ZIP = "https://gdi.bsh.de/de/data/Chart-datum-for-the-German-Bight-2026.zip"
BSH_MEMBER = "SKN-Flaeche_Nordsee_2026_NHN.tif"
GDWS_PDF = "https://www.gdws.wsv.bund.de/SharedDocs/Downloads/DE/Karten/SKN/SKN_Nordsee.pdf?__blob=publicationFile&v=2"
PEGEL = "https://www.pegelonline.wsv.de/webservices/rest-api/v2/stations.json?waters=ELBE"

# Tidal-Elbe reference gauges, downstream -> upstream. Each key matches both the GDWS PDF row
# and the PEGELONLINE longname (case-insensitive substring); this is a selection of gauges, not
# a data value. SKN comes from the PDF, position/km from PEGELONLINE.
GAUGE_KEYS = ["Cuxhaven", "Glückstadt", "Stadersand", "Seemannshöft",
              "St. Pauli", "Harburg", "Over", "Zollenspieker"]

CORRIDOR_DEG = 0.06   # ~6 km: only fill cells this close to the gauge line (keeps it to the river)
EAST_MARGIN = 0.25    # extend the canvas this far east of the most-upstream gauge


def _get(url):
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    return r.content


def gauge_skn_from_pdf(pdf_bytes):
    """{gauge_key: skn_nhn_m} — SKN height in NHN (negative = below NHN). The PDF lists SKN as
    a positive depth below NHN (e.g. "2,01"); the only decimal-comma number on a gauge row."""
    out = {}
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            for line in (page.extract_text() or "").splitlines():
                for key in GAUGE_KEYS:
                    if key.lower() in line.lower() and key not in out:
                        m = re.search(r"(\d),(\d{2})\b", line)
                        if m:
                            out[key] = -float(f"{m.group(1)}.{m.group(2)}")
    missing = [k for k in GAUGE_KEYS if k not in out]
    if missing:
        sys.exit(f"GDWS PDF: no SKN parsed for {missing}")
    return out


def gauge_positions(stations):
    """{gauge_key: (lon, lat, km)} from PEGELONLINE. 'Over' is matched exactly (it is a
    substring of many names)."""
    out = {}
    for key in GAUGE_KEYS:
        k = key.lower()
        cands = [s for s in stations
                 if (s["longname"].lower() == "over" if key == "Over" else k in s["longname"].lower())]
        if not cands:
            sys.exit(f"PEGELONLINE: no station for {key}")
        s = cands[0]
        out[key] = (s["longitude"], s["latitude"], s.get("km"))
    return out


def main():
    out_dir = "store/source/dgm_w/reference"
    os.makedirs(out_dir, exist_ok=True)
    ref_path = f"{out_dir}/skn_reference.tif"

    # 1) BSH SKN grid — extract the NHN band straight from the zip in memory (no cached archive)
    with zipfile.ZipFile(io.BytesIO(_get(BSH_ZIP))) as z:
        with z.open(BSH_MEMBER) as m, open(f"{out_dir}/_skn_bsh.tif", "wb") as f:
            f.write(m.read())
    bsh_path = f"{out_dir}/_skn_bsh.tif"

    # 2) per-gauge SKN (GDWS PDF) + positions (PEGELONLINE), joined and ordered downstream->up
    skn = gauge_skn_from_pdf(_get(GDWS_PDF))
    pos = gauge_positions(requests.get(PEGEL, timeout=120).json())
    gauges = sorted(((pos[k][0], pos[k][1], skn[k], pos[k][2]) for k in GAUGE_KEYS),
                    key=lambda g: -(g[3] if g[3] is not None else 0))  # km descending
    print("tidal-Elbe SKN gauges (lon, lat, skn_nhn, km):")
    for g in gauges:
        print(f"  {g[0]:.3f} {g[1]:.3f}  SKN {g[2]:+.2f}  km {g[3]}")

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

    # 3) widen the canvas east to cover the inner Elbe, paste BSH (west), fill the corridor east
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
    for r in np.nonzero((lats >= 53.30) & (lats <= 53.95))[0]:
        need = np.nonzero(out[r, east_col0:] == nodata)[0] + east_col0
        if need.size == 0:
            continue
        pts = points(np.column_stack([lons[need], np.full(need.shape, lats[r])]))
        near = shapely.distance(pts, line) < CORRIDOR_DEG
        if not near.any():
            continue
        proj = shapely.line_locate_point(line, pts[near])
        out[r, need[near]] = np.interp(proj, gauge_dist, gauge_skn).astype("float32")

    prof.update(width=new_width, compress="deflate", tiled=True, blockxsize=512, blockysize=512)
    with rasterio.open(ref_path, "w", **prof) as dst:
        dst.write(out, 1)
    valid = out[out != nodata]
    print(f"wrote {ref_path}: {new_width}x{height}, SKN-in-NHN "
          f"{valid.min():.2f}..{valid.max():.2f} m over {valid.size:,} cells")


if __name__ == "__main__":
    main()
