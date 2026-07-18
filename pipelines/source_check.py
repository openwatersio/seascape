"""Validate one source's contract — the registration-time gate the plan's check_source
describes. Run after the catalog item exists; a source that fails here fails registration,
not a later aggregation. Every failure is a hard error with a readable message.

Checks, in order:
  1. metadata.json carries name + license + producer + website (attribution basics).
  2. a datum note (metadata ``datum``) is present — an "unknown"/"unverified" value counts
     (the fact is recorded, even when it can't be pinned down), an absent one does not.
  3. store/source/<id>/catalog.json exists, parses, and its bbox + file count agree with
     bounds.csv recomputed via source_catalog._bbox_and_count.
  4. every local bounds.csv row's file exists under store/source/<id>/, opens with rasterio,
     and carries a CRS and a nodata value. Rows whose filename starts with ``objects/`` or
     ``/vsi`` are remote (mirrored/streamed collections) and skip the on-disk raster checks —
     their bytes live outside the store.

Run from pipelines/:  uv run python source_check.py <source-id>
"""

import json
import os
import sys

import rasterio

import config
from source_catalog import _bbox_and_count


def fail(source, msg):
    raise SystemExit(f"{source}: {msg}")


def check(source):
    meta_path = f"{config.SOURCES_DIR}/{source}/metadata.json"
    if not os.path.isfile(meta_path):
        fail(source, f"{meta_path} missing")
    meta = config.load_metadata(source)

    for field in ("name", "license", "producer", "website"):
        if not meta.get(field):
            fail(source, f"metadata.json missing {field!r} (name + license + producer + website required)")
    if not str(meta.get("datum", "")).strip():
        fail(source, "metadata.json has no datum note — record the vertical datum, or 'unknown'/'unverified'")

    catalog_path = f"store/source/{source}/catalog.json"
    if not os.path.isfile(catalog_path):
        fail(source, f"{catalog_path} missing — run the catalog step before check")
    try:
        with open(catalog_path) as f:
            catalog = json.load(f)
    except json.JSONDecodeError as e:
        fail(source, f"catalog.json does not parse: {e}")

    bbox, count = _bbox_and_count(source)
    cat_bbox = catalog.get("bbox")
    if bbox != cat_bbox:
        fail(source, f"catalog bbox {cat_bbox} disagrees with bounds.csv recompute {bbox}")
    cat_count = catalog.get("properties", {}).get("seascape:file_count")
    if cat_count != count:
        fail(source, f"catalog file_count {cat_count} disagrees with bounds.csv rows {count}")

    with open(f"store/source/{source}/bounds.csv") as f:
        rows = [l.strip() for l in f.readlines()[1:] if l.strip()]
    checked = 0
    for row in rows:
        filename = row.split(",")[0]
        if filename.startswith("objects/") or filename.startswith("/vsi"):
            continue  # remote object — not on disk to open
        path = f"store/source/{source}/{filename}"
        if not os.path.isfile(path):
            fail(source, f"bounds.csv references {filename} but {path} is missing")
        with rasterio.open(path) as src:
            if src.crs is None:
                fail(source, f"{filename} has no CRS")
            if src.nodata is None:
                fail(source, f"{filename} has no nodata value")
        checked += 1
    print(f"{source}: contract ok ({count} file(s), {checked} local raster(s) verified, bbox={cat_bbox})")


def main():
    args = sys.argv[1:]
    if len(args) != 1:
        sys.exit("usage: source_check.py <source-id>")
    check(args[0])


def _check():
    """Offline: a synthetic source passes, then each of two contract violations (a missing
    attribution field, a catalog bbox that disagrees with bounds.csv, and a raster with no
    nodata) is caught as a hard error."""
    import shutil
    import tempfile

    import numpy as np
    from rasterio.transform import from_origin

    import source_catalog
    import utils

    d = tempfile.mkdtemp()
    cwd, saved = os.getcwd(), config.SOURCES_DIR
    try:
        os.chdir(d)
        config.SOURCES_DIR = "sources"
        sid = "_check_selfcheck"
        os.makedirs(f"sources/{sid}")
        os.makedirs(f"store/source/{sid}")
        with open(f"sources/{sid}/metadata.json", "w") as f:
            json.dump({"name": "Synth", "license": "CC0 1.0", "producer": "Test Co",
                       "website": "https://x", "datum": "MSL (approx)", "crs": "EPSG:3857"}, f)

        # One real raster tile at a known 3857 origin, and a bounds.csv row that matches it.
        x = utils.X_MAX_3857 / 180.0
        transform = from_origin(0.0, 111325.14, x / 100, x / 100)  # ~ small cell
        tif = f"store/source/{sid}/a.tif"

        def write_tif(path, nodata):
            with rasterio.open(path, "w", driver="GTiff", height=10, width=10, count=1,
                               dtype="float32", crs="EPSG:3857", nodata=nodata,
                               transform=transform) as dst:
                dst.write(np.full((10, 10), -5.0, dtype="float32"), 1)

        write_tif(tif, -9999.0)
        # bounds.csv row must reproduce _bbox_and_count → build the catalog from it so they agree.
        with rasterio.open(tif) as src:
            b = src.bounds
        with open(f"store/source/{sid}/bounds.csv", "w") as f:
            f.write("filename,left,bottom,right,top,width,height\n"
                    f"a.tif,{b.left},{b.bottom},{b.right},{b.top},10,10\n")
        with open(f"store/source/{sid}/datum.json", "w") as f:
            json.dump({"negate": False, "offset_m": 0.0, "clamp_positive": False}, f)
        item = source_catalog.build_item(sid)
        with open(f"store/source/{sid}/catalog.json", "w") as f:
            json.dump(item, f)

        check(sid)  # passes

        # (1) missing attribution field → hard error
        with open(f"sources/{sid}/metadata.json", "w") as f:
            json.dump({"name": "Synth", "license": "CC0 1.0", "producer": "Test Co",
                       "datum": "MSL"}, f)  # no website
        try:
            check(sid); assert False, "expected missing-website to fail"
        except SystemExit as e:
            assert "website" in str(e), e
        with open(f"sources/{sid}/metadata.json", "w") as f:
            json.dump({"name": "Synth", "license": "CC0 1.0", "producer": "Test Co",
                       "website": "https://x", "datum": "MSL", "crs": "EPSG:3857"}, f)

        # (2) catalog bbox disagreeing with bounds.csv → hard error
        bad = dict(item); bad["bbox"] = [0, 0, 1, 1]
        with open(f"store/source/{sid}/catalog.json", "w") as f:
            json.dump(bad, f)
        try:
            check(sid); assert False, "expected bbox mismatch to fail"
        except SystemExit as e:
            assert "bbox" in str(e), e
        with open(f"store/source/{sid}/catalog.json", "w") as f:
            json.dump(item, f)

        # (3) a raster with no nodata → hard error
        write_tif(tif, None)
        try:
            check(sid); assert False, "expected missing nodata to fail"
        except SystemExit as e:
            assert "nodata" in str(e), e
        print("source_check.py self-check ok")
    finally:
        os.chdir(cwd)
        config.SOURCES_DIR = saved
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        _check()
    else:
        main()
