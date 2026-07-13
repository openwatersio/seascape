#!/usr/bin/env python3
"""Regenerate rhein_glw.csv — the free-flowing Rhein low-water (GlW) reference gauges for dgm_w.

NOT part of the build. The build reads the committed rhein_glw.csv; this script just refreshes
it from PEGELONLINE. Re-run when the WSV republishes GlW (a new "gleichwertiger Wasserstand"
edition, every several years):

  python harvest_rhein_glw.py            # rewrites rhein_glw.csv
  python harvest_rhein_glw.py --check    # offline self-test

Why this reach: below the Iffezheim barrage (Rhein-km 336) the Rhein runs free, so its low-water
datum is GlW (gleichwertiger Wasserstand), a longitudinal profile of gauge stages. build_reference.py
turns these per-gauge points into a reference surface that source_datum subtracts, giving depth below
low water. (The impounded reach above Iffezheim uses Stauziel, not GlW — a separate step.)

PEGELONLINE carries everything needed, no PDF tables:

  stations: GET .../stations.json?waters=RHEIN            → km, longitude, latitude, uuid per gauge
  datum+GlW: GET .../stations/<uuid>/W.json?includeCharacteristicValues=true
             → gaugeZero (Pegelnullpunkt height) + characteristicValues[] incl. GlW (cm above PNP)

Low water in NHN is then gaugeZero + GlW/100. We keep only gauges that are (a) below Iffezheim,
(b) referenced to NHN — the Swiss upper gauges report gaugeZero in "mü.M." and the Dutch ones in NAP,
neither of which is NHN — and (c) actually publish a GlW (impounded/tidal gauges don't). Stdlib only.
"""

import csv
import json
import sys
import urllib.request

BASE = "https://pegelonline.wsv.de/webservices/rest-api/v2"
NHN_UNIT = "m. ü. NHN"   # gaugeZero unit that marks a gauge as referenced to German NHN
IFFEZHEIM_KM = 336.0     # last barrage; below it the Rhein is free-flowing (GlW applies)


def low_water_nhn(gauge_zero_m, glw_cm):
    """GlW height in NHN: the gauge datum (m NHN) plus the GlW stage (cm above it)."""
    return round(gauge_zero_m + glw_cm / 100.0, 3)


def pick(station, timeseries):
    """Return (lon, lat, km, glw_nhn_m) for a free-flowing NHN gauge with a GlW, else None."""
    km = station.get("km")
    if km is None or km < IFFEZHEIM_KM:
        return None
    gz = timeseries.get("gaugeZero") or {}
    if gz.get("unit") != NHN_UNIT or gz.get("value") is None:
        return None
    glw = next((c["value"] for c in timeseries.get("characteristicValues", [])
                if c["shortname"] == "GlW"), None)
    if glw is None:
        return None
    return (station["longitude"], station["latitude"], km,
            low_water_nhn(gz["value"], glw))


def _get(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)


def harvest():
    stations = _get(f"{BASE}/stations.json?waters=RHEIN")
    rows = []
    for s in stations:
        try:
            ts = _get(f"{BASE}/stations/{s['uuid']}/W.json?includeCharacteristicValues=true")
        except Exception as ex:
            print(f"  {s.get('shortname')}: fetch failed ({ex})")
            continue
        picked = pick(s, ts)
        if picked:
            lon, lat, km, glw = picked
            rows.append((s["shortname"], lon, lat, km, glw))
    return sorted(rows, key=lambda r: r[3])  # upstream -> downstream by km


def write_csv(rows, path):
    header = (
        "# Free-flowing Rhein low-water (GlW — gleichwertiger Wasserstand) reference gauges for dgm_w.\n"
        "# Used by build_reference.py to build the GlW-in-NHN surface subtracted from the NHN riverbed,\n"
        "# giving depth below low water. Free-flowing reach only: below the Iffezheim barrage (km 336).\n"
        "#\n"
        "# glw_nhn_m = GlW height in NHN = gaugeZero + GlW/100. Source: PEGELONLINE\n"
        "#   https://www.pegelonline.wsv.de/webservices/rest-api/v2/ (water=RHEIN, gaugeZero unit\n"
        "#   'm. ü. NHN', characteristicValues GlW). Regenerate with harvest_rhein_glw.py when the WSV\n"
        "#   republishes GlW. Attribution: © Wasserstraßen- und Schifffahrtsverwaltung des Bundes (WSV) / BfG.\n"
    )
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(header)
        w = csv.writer(f)
        w.writerow(["gauge", "lon", "lat", "km", "glw_nhn_m"])
        w.writerows(rows)


def _check():
    # arithmetic: Maxau's real values (gaugeZero 97.721 m NHN, GlW 372 cm) -> 101.441 m NHN
    assert low_water_nhn(97.721, 372.0) == 101.441
    keep = pick({"km": 362.3, "longitude": 8.3, "latitude": 49.0},
                {"gaugeZero": {"unit": NHN_UNIT, "value": 97.721},
                 "characteristicValues": [{"shortname": "GlW", "value": 372.0}]})
    assert keep == (8.3, 49.0, 362.3, 101.441), keep
    # dropped: above Iffezheim, non-NHN datum, and no-GlW gauge
    glw_cv = {"characteristicValues": [{"shortname": "GlW", "value": 300.0}]}
    assert pick({"km": 227.6, "longitude": 7.5, "latitude": 48.0},
                {"gaugeZero": {"unit": NHN_UNIT, "value": 180.0}, **glw_cv}) is None  # above barrage
    assert pick({"km": 500.0, "longitude": 8.0, "latitude": 50.0},
                {"gaugeZero": {"unit": "mü.M.", "value": 240.0}, **glw_cv}) is None   # Swiss datum
    assert pick({"km": 500.0, "longitude": 8.0, "latitude": 50.0},
                {"gaugeZero": {"unit": NHN_UNIT, "value": 78.0},
                 "characteristicValues": []}) is None                                # no GlW
    print("harvest_rhein_glw.py self-check ok")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        _check()
    else:
        path = f"{sys.path[0]}/rhein_glw.csv"
        rows = harvest()
        if not rows:
            sys.exit("no GlW gauges found — the API may have moved or GlW was retired; check the site.")
        write_csv(rows, path)
        print(f"wrote {len(rows)} gauges to {path} (km {rows[0][3]:.0f}–{rows[-1][3]:.0f})")
