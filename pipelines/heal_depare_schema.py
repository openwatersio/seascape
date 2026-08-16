"""Repair depare FGBs whose drval columns are typed String instead of numeric.

OGR types a GeoJSONSeq field from the VALUES it sees, so a tile whose rows are ALL nodata — inland
water the DEM holds no bathymetry for, which carries no drval at all — used to land drval1/drval2
as String. contour_run reads those rows back with a numeric filter ("drval1 < 0"), which is then
invalid SQL, and the tile fails a whole stage downstream of the write that caused it.

depare_run pins the schema on write now. This repairs the artifacts already on the store volume,
which the writer fix cannot reach: nothing about a depare rule's inputs or params changed, so
Snakemake considers them up to date. The geometry in them is CORRECT — only the field type is
wrong — so this rewrites the schema in place rather than re-running the partition, which would
recompute contours for no geometric gain.

Idempotent and cheap: a clean store costs one read_info per file and rewrites nothing.

    heal_depare_schema.py [<dir>]   heal every .fgb under <dir> (default store/depare)
    heal_depare_schema.py --check   self-check (synthetic poisoned file, no network)
"""

import glob
import os
import subprocess
import sys

import pyogrio

import depare_run

# The fields the writer casts to a numeric type; pyogrio reports an OGR String field as "object".
NUMERIC_FIELDS = tuple(n for n, t in depare_run._RowSink.FIELDS if t in ("float", "integer"))
NUMERIC_DTYPES = ("float64", "float32", "int64", "int32")

DEFAULT_DIR = "store/depare"


def _schema(path):
    """{field: dtype} as pyogrio reports it — metadata only, the features are never read."""
    info = pyogrio.read_info(path)
    return dict(zip(info["fields"], info["dtypes"])), int(info["features"])


def needs_heal(path):
    """The numeric fields this file types as something else. Empty means it is already correct.

    A field that is ABSENT is not reported: casting one would fail, and a depare layer without
    drval1 is a different defect than a mistyped one."""
    types, _ = _schema(path)
    return [n for n in NUMERIC_FIELDS
            if n in types and types[n] not in NUMERIC_DTYPES]


def heal(path):
    """Rewrite `path` with the numeric schema, atomically. Returns the fields that were fixed.

    The rewrite is a straight copy through the writer's own SELECT, so NULLs stay NULL (absence is
    what tippecanoe encodes as a missing property) and the geometry is untouched. Written beside
    the original and renamed, so an interrupted heal cannot leave a truncated artifact in place."""
    bad = needs_heal(path)
    if not bad:
        return []
    layer = pyogrio.list_layers(path)[0][0]
    before_types, before_n = _schema(path)
    tmp = f"{path}.heal.tmp.fgb"
    try:
        subprocess.run(["ogr2ogr", "-f", "FlatGeobuf", "-overwrite",
                        "-sql", depare_run.row_select(layer), tmp, path],
                       check=True, capture_output=True, text=True)
        after_types, after_n = _schema(tmp)
        # Never replace a good artifact with a short one: a partial rewrite would silently delete
        # depth areas, which is the failure this whole pass exists to undo.
        if after_n != before_n:
            raise AssertionError(f"{path}: heal wrote {after_n} of {before_n} features")
        still = [n for n in NUMERIC_FIELDS
                 if n in after_types and after_types[n] not in NUMERIC_DTYPES]
        if still:
            raise AssertionError(f"{path}: still non-numeric after heal: {still}")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return bad


def run(folder=DEFAULT_DIR):
    """Heal every FGB under `folder`. Prints one line per repair plus a summary."""
    paths = sorted(glob.glob(os.path.join(folder, "*.fgb")))
    healed = []
    for p in paths:
        fixed = heal(p)
        if fixed:
            healed.append(os.path.basename(p))
            print(f"healed {os.path.basename(p)}: {', '.join(fixed)} -> numeric", flush=True)
    print(f"depare schema: {len(paths)} scanned, {len(healed)} healed"
          + (f" ({', '.join(healed)})" if healed else ""), flush=True)
    return healed


def _poisoned(path, rows):
    """Write an FGB the way the pre-fix writer did — GeoJSONSeq staged, types left to inference."""
    import geopandas as gpd
    seq = f"{path}.geojsons"
    gpd.GeoDataFrame(rows, crs="EPSG:4326").to_file(seq, driver="GeoJSONSeq")
    # No -sql: this is exactly the conversion that typed an all-null column String.
    subprocess.run(["ogr2ogr", "-f", "FlatGeobuf", "-overwrite", "-nln", "depare-rows", path, seq],
                   check=True, capture_output=True, text=True)
    os.remove(seq)


def _check():
    """A synthetic all-nodata tile is typed String, heals to numeric, answers the reader's filter,
    keeps its geometry/NULLs/feature count, and heals to a no-op the second time."""
    import tempfile

    import geopandas as gpd
    from shapely.geometry import box

    d = tempfile.mkdtemp()
    path = f"{d}/8-69-88-8.fgb"
    rows = [{"geometry": box(i, 0, i + 1, 1), "drval1": None, "drval2": None,
             "sys": None, "kind": "lake", "rank": depare_run.NODATA_RANK} for i in range(8)]
    _poisoned(path, rows)
    assert needs_heal(path) == ["drval1", "drval2"], \
        f"the fixture must reproduce the String schema, got {_schema(path)[0]}"
    where = "sys = 'm' OR drval1 < 0"  # contour_run's metre-pass filter, verbatim
    try:
        gpd.read_file(path, where=where)
        raise AssertionError("the poisoned fixture must fail the reader's numeric filter")
    except ValueError:
        pass

    original = gpd.read_file(path)
    assert heal(path) == ["drval1", "drval2"], "heal must report the fields it fixed"
    types, n = _schema(path)
    assert all(types[f] in NUMERIC_DTYPES for f in NUMERIC_FIELDS), \
        f"healed schema must be numeric, got {types}"
    assert n == len(rows), f"heal must keep every feature, got {n} of {len(rows)}"
    after = gpd.read_file(path)
    assert bool(original.geometry.equals(after.geometry)), "heal must not touch the geometry"
    assert bool(after["drval1"].isna().all()), "an absent drval must stay NULL, not become 0"
    assert bool((original["kind"] == after["kind"]).all()), "heal must carry the other fields"
    assert len(gpd.read_file(path, where=where)) == 0, \
        "the healed layer must answer the reader's numeric filter"
    assert pyogrio.list_layers(path)[0][0] == "depare-rows", "heal must keep the layer name"

    assert heal(path) == [], "a healed file must heal to a no-op"
    assert not glob.glob(f"{d}/*.tmp.fgb"), "heal must leave no temp artifact behind"

    # A tile that always had numeric drvals is left alone, and run() reports it scanned-not-healed
    good = f"{d}/8-1-1-8.fgb"
    _poisoned(good, [{"geometry": box(0, 0, 1, 1), "drval1": -10.0, "drval2": -5.0,
                      "sys": "m", "kind": None, "rank": depare_run.BAND_RANK}])
    assert needs_heal(good) == [], "a numeric tile must not be rewritten"
    assert run(d) == [], "a clean folder must heal nothing"
    print("heal_depare_schema self-check ok")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--check"]:
        _check()
    else:
        run(args[0] if args else DEFAULT_DIR)
