"""Assemble store/source/<id>/catalog.json — the generated machine-facing view of a source.

One flat, readable JSON item per source, shaped like a STAC Item (``id``/``bbox``/
``properties``) so interop stays open, but with no STAC library dependency and no spec
validation — project-specific fields live under ``seascape:`` keys. It is a source's SINGLE
registration artifact: it consolidates what once was scattered across ``metadata.json``
(hand-edited attribution + flags), the per-file bounds that ``bounds.csv`` used to hold, the
normalized COG's CRS, the datum sidecar ``source_datum`` records at prep time (``negate`` +
applied ``offset`` + ``clamp_positive``), and the recipe content hash (``--hash-recipe``
computes it; the ``RECIPE_HASH`` env is the legacy override). Missing metadata.json is a hard
error — an incomplete item must fail registration, never surface later as a silent merge default.

``seascape:files`` is the per-file registration: a compact list of 7-element arrays
``[filename, left, bottom, right, top, width, height]`` — bounds in EPSG:3857, filename
resolvable by ``config.source_path``, in exactly the column order the retired bounds.csv used.
``config.source_files`` is the single reader. A processed source's rows are scanned from its
normalized COGs here; a raw source's rows arrive via source_mirror's .rows.csv handoff.

Run from pipelines/:  uv run python source_catalog.py <source-id> [--hash-recipe]
"""

import hashlib
import json
import math
import os
import sys
from glob import glob

import rasterio
from rasterio.warp import transform_bounds

import config
import utils
from source_remote import HANDOFF, read_rows


def recipe_hash(source):
    """Content hash of sources/<id>/** — what RECIPE_HASH carries in the legacy workflow
    (there via GitHub's hashFiles), computed here directly so the Snakemake lane stamps it
    without a CI env var. Path-relative + content, sorted, so it's stable across checkouts.
    Not equal to hashFiles' value — each lane compares only against its own stamps, and the
    one-time mismatch at cutover re-aggregates affected tiles once, like any re-registration."""
    root = f"{config.SOURCES_DIR}/{source}"
    h = hashlib.sha256()
    for dirpath, _dirnames, filenames in sorted(os.walk(root)):
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            h.update(os.path.relpath(path, root).encode())
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
    return h.hexdigest()


def scan_local_files(source):
    """The per-file 3857 registration rows for a PROCESSED source, scanned from its normalized
    COGs (store/source/<id>/*.tif). Moved here from the retired source_bounds.py; keeps its
    antimeridian flip + non-finite guard. Raw sources don't scan — source_mirror hands their
    rows off via the .rows.csv handoff. Each row is (filename, left, bottom, right, top, width,
    height), the column order seascape:files stores."""
    rows = []
    filepaths = sorted(glob(f"store/source/{source}/*.tif"))
    for j, filepath in enumerate(filepaths):
        with rasterio.open(filepath) as src:
            if src.crs is None:
                raise ValueError(f"crs not defined on {filepath}")
            left, bottom, right, top = transform_bounds(src.crs, "EPSG:3857", *src.bounds)
            if right - left > 0.9 * 2 * utils.X_MAX_3857:
                left, right = right, left  # antimeridian: transform_bounds flips l/r
            for num in (left, bottom, right, top):
                if not math.isfinite(num):
                    raise ValueError(f"non-finite bound: src.bounds={src.bounds} crs={src.crs}")
            rows.append((os.path.basename(filepath), left, bottom, right, top,
                         src.width, src.height))
        if j % 100 == 0:
            print(f"{j} / {len(filepaths)}")
    return rows


def registration_rows(source):
    """This source's per-file rows for the catalog. A raw source's rows arrive via the
    .rows.csv handoff source_mirror wrote (consumed + deleted here); a processed source scans
    its normalized COGs. One mode per lane, chosen by the handoff's presence."""
    handoff = f"store/source/{source}/{HANDOFF}"
    if os.path.isfile(handoff):
        rows = read_rows(handoff)
        os.remove(handoff)
        return rows
    return scan_local_files(source)


def _bbox_and_count(rows):
    """Overall EPSG:4326 bbox [w, s, e, n] + file count from the 3857 registration rows
    (left,bottom,right,top per file). 4326 because that's the STAC bbox frame. A source with
    no data rows has no bbox (None).

    Latitudes union through one inverse-mercator transform; longitudes are linear in 3857 x.
    A row with left > right crosses the antimeridian (the wrap_antimeridian convention the
    registration modules write — CUDEM/S-102 cover the Aleutians), so when any row wraps the
    longitude union is taken on the 0..360 circle and comes out west > east — the STAC
    wraparound signal — instead of a naive min/max that silently swallows the wrapped tiles.
    The circular union is coarse (min start / max end, not a true interval union); fine for a
    catalog summary — a consumer needing exact coverage reads seascape:files itself."""
    if not rows:
        return None, 0
    lons, bottoms, tops = [], [], []
    for _filename, left, bottom, right, top, _w, _h in rows:
        lons.append((left / utils.X_MAX_3857 * 180.0, right / utils.X_MAX_3857 * 180.0))
        bottoms.append(bottom); tops.append(top)
    _, s, _, n = transform_bounds("EPSG:3857", "EPSG:4326", 0.0, min(bottoms), 0.0, max(tops))
    if any(w > e for w, e in lons):
        starts = [w % 360.0 for w, _ in lons]
        # % 360 turns a full-planet row (e - w == 360) into width 0 — restore it.
        widths = [((e - w) % 360.0) or (360.0 if e > w else 0.0) for w, e in lons]
        w360 = min(starts)
        e360 = max(start + width for start, width in zip(starts, widths))
        if e360 - w360 >= 360.0:
            west, east = -180.0, 180.0
        else:
            west = (w360 + 180.0) % 360.0 - 180.0
            east = (e360 + 180.0) % 360.0 - 180.0
    else:
        west, east = min(w for w, _ in lons), max(e for _, e in lons)
    return [round(v, 6) for v in (west, s, east, n)], len(rows)


def _crs_epsg(source, mixed_crs):
    """EPSG code of the normalized COG, or None. None for a mixed_crs source (per-file CRS,
    so no single code) and for a raw source (no local COG — its CRS lives in the remote
    objects). Reads the first local .tif's assigned CRS otherwise."""
    if mixed_crs:
        return None
    tifs = sorted(glob(f"store/source/{source}/*.tif"))
    if not tifs:
        return None
    with rasterio.open(tifs[0]) as src:
        return src.crs.to_epsg() if src.crs else None


def _datum(source):
    """The datum facts downstream reads: (negate, offset_m, clamp_positive).

    ``negate`` is the one field with a machine consumer — aggregation_reproject flips band 1
    when it's true — so it must mean "negate at aggregation", which only a raw/streamed
    source (no sidecar; negates nothing at prep) ever needs. A sidecar means the transform is
    already baked into the prepared COGs, so negate is False regardless of what was applied —
    publishing the applied flag here made aggregation flip prepared depths back to positive
    (the african_great_lakes/ddm double-negation). ``offset_m``/``clamp_positive`` stay the
    applied-at-prep record (provenance + tile keying; nothing re-applies them). Every processed
    source has a sidecar (source_prep writes one even for a no-op transform); no sidecar
    means a raw source, whose negate comes from metadata."""
    sidecar = f"store/source/{source}/datum.json"
    if os.path.isfile(sidecar):
        with open(sidecar) as f:
            d = json.load(f)
        return False, float(d.get("offset_m", 0.0)), bool(d.get("clamp_positive", False))
    meta = config.load_metadata(source)
    return bool(meta.get("negate", False)), 0.0, False


def build_item(source, rows, recipe_hash=None):
    """The catalog item dict for <source>, built from its per-file registration ``rows``
    (from registration_rows). Raises if metadata.json is missing or the datum invariant is
    violated — a source can't register without a complete item."""
    meta = config.load_metadata(source)  # raises FileNotFoundError if absent
    bbox, file_count = _bbox_and_count(rows)
    negate, offset_m, clamp_positive = _datum(source)
    producer = meta.get("producer")
    website = meta.get("website")
    return {
        "type": "Feature",
        "stac_version": "1.0.0",
        "id": source,
        "bbox": bbox,
        "geometry": None,  # bbox only; the dissolved footprint lives in the coverage gpkg
        "properties": {
            "title": meta.get("name"),
            "license": meta.get("license"),
            "providers": [{"name": producer, "url": website,
                           "roles": ["producer", "licensor"]}] if producer else [],
            "proj:epsg": _crs_epsg(source, bool(meta.get("mixed_crs"))),
            "seascape:producer": producer,
            "seascape:website": website,
            "seascape:attribution": meta.get("attribution"),
            "seascape:priority": int(meta.get("priority", 0)),
            "seascape:max_zoom": meta.get("max_zoom"),
            "seascape:land_clamp": bool(meta.get("land_clamp", False)),
            "seascape:raw": bool(meta.get("raw", False)),
            "seascape:mixed_crs": bool(meta.get("mixed_crs", False)),
            "seascape:band": meta.get("band"),
            "seascape:vertical_datum": meta.get("datum"),
            "seascape:datum_offset_m": offset_m,
            "seascape:negate": negate,  # negate at aggregation — false once baked at prep
            "seascape:clamp_positive": clamp_positive,
            "seascape:file_count": file_count,
            # The per-file registration (the retired bounds.csv absorbed): compact arrays in
            # the documented [filename, left, bottom, right, top, width, height] 3857 order.
            "seascape:files": [list(r) for r in rows],
            "seascape:recipe_hash": recipe_hash or None,
        },
    }


def main():
    args = [a for a in sys.argv[1:] if a != "--hash-recipe"]
    if len(args) != 1:
        sys.exit("usage: source_catalog.py <source-id> [--hash-recipe]")
    source = args[0]
    rh = recipe_hash(source) if "--hash-recipe" in sys.argv else os.environ.get("RECIPE_HASH")
    item = build_item(source, registration_rows(source), rh or None)
    out = f"store/source/{source}/catalog.json"
    # Write-if-changed: an unchanged item keeps its mtime, so a no-op re-catalog
    # doesn't cascade into cover/coverage/publish.
    utils.write_if_changed(out, json.dumps(item, indent=2))
    props = item["properties"]
    print(f"{source}: catalog.json ({props['seascape:file_count']} file(s), "
          f"datum_offset={props['seascape:datum_offset_m']}, negate={props['seascape:negate']}, "
          f"epsg={props['proj:epsg']}, bbox={item['bbox']})")


def _check():
    """Offline synthetic self-check: an item assembles from a synthetic source, the per-file
    rows round-trip into seascape:files, the recorded datum offset round-trips (sidecar →
    negate publishes False), a sidecar-less source takes negate from metadata (the raw model),
    ``raw`` round-trips to seascape:raw and the retired seascape:volatile key still reads back
    through source_property, an antimeridian-wrapped bounds row is represented (west > east),
    scan_local_files reads a real COG, and missing metadata.json fails registration."""
    import shutil
    import tempfile

    d = tempfile.mkdtemp()
    cwd, saved_sources_dir = os.getcwd(), config.SOURCES_DIR
    try:
        os.chdir(d)
        config.SOURCES_DIR = "sources"
        sid = "_catalog_selfcheck"
        os.makedirs(f"sources/{sid}")
        os.makedirs(f"store/source/{sid}")
        with open(f"sources/{sid}/metadata.json", "w") as f:
            json.dump({"name": "Synth Source", "producer": "Test Co", "website": "https://x",
                       "license": "CC0 1.0", "max_zoom": 12, "land_clamp": True,
                       "datum": "MSL (approx)"}, f)
        # A datum sidecar as source_datum would record it (negate + a −1 m offset).
        with open(f"store/source/{sid}/datum.json", "w") as f:
            json.dump({"negate": True, "offset_m": -1.0, "clamp_positive": False}, f)
        # Two EPSG:3857 rows so the bbox is a real union (roughly 0..~2°E, 0..~1°N).
        rows = [("a.tif", 0.0, 0.0, 111319.49, 111325.14, 10, 10),
                ("b.tif", 111319.49, 0.0, 222638.98, 55787.5, 10, 10)]

        # recipe_hash(): deterministic, and sensitive to both content and filename
        h1 = recipe_hash(sid)
        assert h1 == recipe_hash(sid)
        with open(f"sources/{sid}/file_list.txt", "w") as f:
            f.write("https://x/a.tif\n")
        h2 = recipe_hash(sid)
        assert h1 != h2, "recipe hash must change when a recipe file changes"

        item = build_item(sid, rows, recipe_hash="abc123")
        p = item["properties"]
        assert item["id"] == sid and item["type"] == "Feature", item
        # the rows round-trip verbatim into seascape:files (the retired bounds.csv's payload)
        assert p["seascape:files"] == [list(r) for r in rows], p["seascape:files"]
        # A sidecar means the transform is baked into the COGs: the offset is recorded as
        # provenance, but negate must publish False — aggregation would otherwise flip the
        # already-negated values back to positive depth (the double-negation regression).
        assert p["seascape:datum_offset_m"] == -1.0 and p["seascape:negate"] is False, p
        assert p["seascape:clamp_positive"] is False, p
        assert p["seascape:land_clamp"] is True and p["seascape:max_zoom"] == 12, p
        assert p["seascape:vertical_datum"] == "MSL (approx)", p
        assert p["seascape:file_count"] == 2, p
        assert p["seascape:recipe_hash"] == "abc123", p
        assert p["providers"][0]["name"] == "Test Co", p
        # bbox is 4326 and covers the union (w≈0, e≈2, s≈0, n≈0.5), west of east, south of north
        w, s, e, n = item["bbox"]
        assert w < e and s < n and -0.01 < w < 0.01 and 1.9 < e < 2.1, item["bbox"]

        # An antimeridian-wrapped row (left > right in 3857, the wrap_antimeridian convention;
        # Aleutian CUDEM/S-102 tiles) must be represented, not swallowed by a naive min/max:
        # one mainland row (-160..-150) + one wrapped row (175..-170) → west 175 > east -150.
        sid2 = "_catalog_wrap"
        os.makedirs(f"sources/{sid2}")
        os.makedirs(f"store/source/{sid2}")
        with open(f"sources/{sid2}/metadata.json", "w") as f:
            json.dump({"name": "Wrap Source"}, f)
        x = utils.X_MAX_3857 / 180.0  # metres per degree of longitude at the equator
        wrap_rows = [("mainland.tif", -160 * x, 6000000.0, -150 * x, 7000000.0, 10, 10),
                     ("wrapped.tif", 175 * x, 6000000.0, -170 * x, 7000000.0, 10, 10)]
        w2, s2, e2, n2 = build_item(sid2, wrap_rows)["bbox"]
        assert w2 > e2, f"wrapped union must signal wraparound (west > east): {(w2, e2)}"
        assert abs(w2 - 175.0) < 0.01 and abs(e2 - -150.0) < 0.01, (w2, e2)
        assert s2 < n2, (s2, n2)

        # No sidecar = the raw model: negate comes from metadata (a processed source
        # always has a sidecar — source_prep writes one even for a no-op transform). The
        # declared `raw` flag round-trips to seascape:raw.
        with open(f"sources/{sid2}/metadata.json", "w") as f:
            json.dump({"name": "Wrap Source", "negate": True, "raw": True}, f)
        p2 = build_item(sid2, wrap_rows)["properties"]
        assert p2["seascape:negate"] is True and p2["seascape:raw"] is True, p2

        # Cutover fallback: a catalog published before the rename carries seascape:volatile and
        # no seascape:raw; source_property("raw") must still read it (delete with the fallback).
        sid3 = "_catalog_volatile_fallback"
        os.makedirs(f"sources/{sid3}")
        os.makedirs(f"store/source/{sid3}")
        with open(f"sources/{sid3}/metadata.json", "w") as f:
            json.dump({"name": "Legacy Source"}, f)
        with open(f"store/source/{sid3}/catalog.json", "w") as f:
            json.dump({"properties": {"seascape:volatile": True}}, f)
        assert config.source_property(sid3, "raw") is True, "seascape:volatile must read back as raw"

        # registration_rows: a raw source's .rows.csv handoff is consumed + deleted; a
        # processed source scans its local COG. Both feed build_item the same shape.
        from source_remote import write_rows, HANDOFF
        sid4 = "_catalog_handoff"
        os.makedirs(f"store/source/{sid4}")
        write_rows(sid4, [("objects/x.h5", 1.0, 2.0, 3.0, 4.0, 5, 6)])
        got = registration_rows(sid4)
        assert got == [("objects/x.h5", 1.0, 2.0, 3.0, 4.0, 5, 6)], got
        assert not os.path.isfile(f"store/source/{sid4}/{HANDOFF}"), "handoff must be deleted"

        import numpy as np
        from rasterio.transform import from_origin
        sid5 = "_catalog_scan"
        os.makedirs(f"store/source/{sid5}")
        with rasterio.open(f"store/source/{sid5}/s_0.tif", "w", driver="GTiff", height=10,
                           width=10, count=1, dtype="float32", crs="EPSG:3857", nodata=-9999,
                           transform=from_origin(0.0, 111325.14, x / 100, x / 100)) as dst:
            dst.write(np.full((10, 10), -5.0, dtype="float32"), 1)
        scanned = scan_local_files(sid5)
        assert len(scanned) == 1 and scanned[0][0] == "s_0.tif", scanned

        # missing metadata.json → hard error
        shutil.rmtree(f"sources/{sid}")
        try:
            build_item("_no_such_source", [])
            assert False, "expected missing metadata.json to fail"
        except (FileNotFoundError, SystemExit):
            pass
        print("source_catalog.py self-check ok")
    finally:
        os.chdir(cwd)
        config.SOURCES_DIR = saved_sources_dir
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        _check()
    else:
        main()
