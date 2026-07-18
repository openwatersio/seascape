"""Assemble store/source/<id>/catalog.json — the generated machine-facing view of a source.

One flat, readable JSON item per source, shaped like a STAC Item (``id``/``bbox``/
``properties``) so interop stays open, but with no STAC library dependency and no spec
validation — project-specific fields live under ``seascape:`` keys. It consolidates what
today is scattered across ``metadata.json`` (hand-edited attribution + flags), ``bounds.csv``
(per-file bounds → overall bbox + file count), the normalized COG's CRS, the datum sidecar
``source_datum`` records at prep time (``negate`` + applied ``offset`` + ``clamp_positive``),
and the recipe content hash the caller supplies via ``RECIPE_HASH`` (empty locally).

Composed into the shared source tail (``just source <id>``) after the per-source recipe, so
every source — prepared or mirrored — gets one without editing each recipe. Runs after
``bounds.csv`` exists; downstream still reads ``metadata.json`` for now, so this is additive.

Validation (registration-time, not aggregation): a source whose recipe runs ``source_datum``
but left no datum sidecar, or one missing ``metadata.json``, is a hard error here — a stale or
absent item must fail the source's registration, never surface later as a silent merge default.

Run from pipelines/:  uv run python source_catalog.py <source-id>
"""

import json
import os
import sys
from glob import glob

import rasterio
from rasterio.warp import transform_bounds

import config
import utils


def _bbox_and_count(source):
    """Overall EPSG:4326 bbox [w, s, e, n] + file count from bounds.csv (rows are EPSG:3857
    left,bottom,right,top per file). 4326 because that's the STAC bbox frame; the per-file
    3857 rows stay in bounds.csv. A source with no data rows has no bbox (None).

    Latitudes union through one inverse-mercator transform; longitudes are linear in 3857 x.
    A row with left > right crosses the antimeridian (the wrap_antimeridian convention the
    registration modules write — CUDEM/S-102 cover the Aleutians), so when any row wraps the
    longitude union is taken on the 0..360 circle and comes out west > east — the STAC
    wraparound signal — instead of a naive min/max that silently swallows the wrapped tiles.
    The circular union is coarse (min start / max end, not a true interval union); fine for a
    catalog summary — a consumer needing exact coverage reads bounds.csv itself."""
    path = f"store/source/{source}/bounds.csv"
    if not os.path.isfile(path):
        raise SystemExit(f"{source}: bounds.csv missing — run the source recipe before the catalog")
    with open(path) as f:
        rows = [l.strip() for l in f.readlines()[1:] if l.strip()]
    if not rows:
        return None, 0
    lons, bottoms, tops = [], [], []
    for row in rows:
        _, left, bottom, right, top = row.split(",")[:5]
        lons.append((float(left) / utils.X_MAX_3857 * 180.0,
                     float(right) / utils.X_MAX_3857 * 180.0))
        bottoms.append(float(bottom)); tops.append(float(top))
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
    so no single code) and for a mirrored source (no local COG — its CRS lives in the remote
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
    when it's true — so it must mean "negate at aggregation", which only a mirrored/streamed
    source (no sidecar; negates nothing at prep) ever needs. A sidecar means source_datum
    already baked the transform into the prepared COGs, so negate is False regardless of what
    was applied — publishing the applied flag here made aggregation flip prepared depths back
    to positive (the african_great_lakes/ddm double-negation). ``offset_m``/``clamp_positive``
    stay the applied-at-prep record (provenance + tile keying; nothing re-applies them).
    Enforces the invariant that a recipe running source_datum must leave a sidecar behind."""
    sidecar = f"store/source/{source}/datum.json"
    if os.path.isfile(sidecar):
        with open(sidecar) as f:
            d = json.load(f)
        return False, float(d.get("offset_m", 0.0)), bool(d.get("clamp_positive", False))
    justfile = f"{config.SOURCES_DIR}/{source}/Justfile"
    if os.path.isfile(justfile):
        with open(justfile) as f:
            # Recipe lines only — header comments legitimately mention source_datum
            # (noaa_s102's explains why mirrored sources skip it) without running it.
            runs_datum = any("source_datum" in line for line in f
                             if not line.lstrip().startswith("#"))
        if runs_datum:
            raise SystemExit(
                f"{source}: recipe runs source_datum but store/source/{source}/datum.json is "
                "missing — the transform wasn't recorded; failing registration, not aggregation")
    meta = config.load_metadata(source)
    return bool(meta.get("negate", False)), 0.0, False


def build_item(source, recipe_hash=None):
    """The catalog item dict for <source>. Raises if metadata.json is missing or the datum
    invariant is violated — a source can't register without a complete item."""
    meta = config.load_metadata(source)  # raises FileNotFoundError if absent
    bbox, file_count = _bbox_and_count(source)
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
            "seascape:volatile": bool(meta.get("volatile", False)),
            "seascape:mixed_crs": bool(meta.get("mixed_crs", False)),
            "seascape:band": meta.get("band"),
            "seascape:vertical_datum": meta.get("datum"),
            "seascape:datum_offset_m": offset_m,
            "seascape:negate": negate,  # negate at aggregation — false once baked at prep
            "seascape:clamp_positive": clamp_positive,
            "seascape:file_count": file_count,
            "seascape:recipe_hash": recipe_hash or None,
        },
    }


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: source_catalog.py <source-id>")
    source = sys.argv[1]
    item = build_item(source, os.environ.get("RECIPE_HASH") or None)
    out = f"store/source/{source}/catalog.json"
    with open(out, "w") as f:
        json.dump(item, f, indent=2)
    props = item["properties"]
    print(f"{source}: catalog.json ({props['seascape:file_count']} file(s), "
          f"datum_offset={props['seascape:datum_offset_m']}, negate={props['seascape:negate']}, "
          f"epsg={props['proj:epsg']}, bbox={item['bbox']})")


def _check():
    """Offline synthetic self-check: an item assembles from a synthetic source, the recorded
    datum offset round-trips, an antimeridian-wrapped bounds row is represented (west > east),
    a comment-only source_datum mention doesn't trip the sidecar invariant (the real noaa_s102
    Justfile is the regression case), and the two hard-error paths (missing metadata.json, a
    recipe that ran source_datum with no sidecar) both fail registration."""
    import shutil
    import tempfile

    d = tempfile.mkdtemp()
    cwd, saved_sources_dir = os.getcwd(), config.SOURCES_DIR
    # The real repo sources dir, resolved before the chdir — the noaa_s102 case below reads
    # its actual Justfile (offline file read; its header comment mentions source_datum).
    real_sources = os.path.abspath(saved_sources_dir)
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
        # bounds.csv: two EPSG:3857 rows so the bbox is a real union (roughly 0..~2°E, 0..~1°N).
        with open(f"store/source/{sid}/bounds.csv", "w") as f:
            f.write("filename,left,bottom,right,top,width,height\n"
                    "a.tif,0.0,0.0,111319.49,111325.14,10,10\n"
                    "b.tif,111319.49,0.0,222638.98,55787.5,10,10\n")

        item = build_item(sid, recipe_hash="abc123")
        p = item["properties"]
        assert item["id"] == sid and item["type"] == "Feature", item
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
        with open(f"store/source/{sid2}/bounds.csv", "w") as f:
            f.write("filename,left,bottom,right,top,width,height\n"
                    f"mainland.tif,{-160 * x},6000000.0,{-150 * x},7000000.0,10,10\n"
                    f"wrapped.tif,{175 * x},6000000.0,{-170 * x},7000000.0,10,10\n")
        w2, s2, e2, n2 = build_item(sid2)["bbox"]
        assert w2 > e2, f"wrapped union must signal wraparound (west > east): {(w2, e2)}"
        assert abs(w2 - 175.0) < 0.01 and abs(e2 - -150.0) < 0.01, (w2, e2)
        assert s2 < n2, (s2, n2)

        # A Justfile whose COMMENT mentions source_datum but never invokes it must not trip
        # the sidecar invariant (sid2 has no sidecar and no datum need)…
        with open(f"sources/{sid2}/Justfile", "w") as f:
            f.write("# mirrored sources skip source_datum — see the module docstring\n"
                    "default:\n    uv run python source_mirror.py x\n")
        assert build_item(sid2)["properties"]["seascape:negate"] is False
        # …and the real noaa_s102 recipe (header comment mentions it, never runs it) is the
        # live regression: _datum must fall through to metadata (negate=true, no offset).
        assert os.path.isfile(f"{real_sources}/noaa_s102/Justfile"), \
            f"run --check from pipelines/ ({real_sources}/noaa_s102/Justfile not found)"
        config.SOURCES_DIR = real_sources
        try:
            assert _datum("noaa_s102") == (True, 0.0, False)
        finally:
            config.SOURCES_DIR = "sources"

        # missing datum sidecar for a recipe that runs source_datum → hard error
        with open(f"sources/{sid}/Justfile", "w") as f:
            f.write("default:\n    uv run python source_datum.py x --negate\n")
        os.remove(f"store/source/{sid}/datum.json")
        try:
            build_item(sid)
            assert False, "expected a missing datum sidecar to fail"
        except SystemExit as ex:
            assert "datum.json is missing" in str(ex), ex

        # missing metadata.json → hard error
        shutil.rmtree(f"sources/{sid}")
        try:
            build_item("_no_such_source")
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
