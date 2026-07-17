"""Offline end-to-end self-check for the source stage.

Pre-places a synthetic positive-depth raster in store/source (the fetch steps are
network-bound and validated against live data), then runs the transform chain
(datum -> normalize -> bounds -> polygonize -> tarball) in an isolated tmp dir and
asserts the artifacts + the bathymetry value transform (negate + offset on valid
pixels, nodata preserved).

Run from pipelines/:  uv run python test_source_stage.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np
import rasterio
from rasterio.transform import from_origin

PIPE = os.path.dirname(os.path.abspath(__file__))


def run(tmp, *args):
    env = {**os.environ, "SOURCES_DIR": "sources", "PYTHONPATH": PIPE}
    subprocess.run([sys.executable, os.path.join(PIPE, args[0]), *args[1:]],
                   cwd=tmp, env=env, check=True)


def check_remote_parsers():
    """Mirrored-source URL/name parsing — the bits that bite (virtual-host vs s3://
    URLs, the fixed-width S-102 issue field that defeats a trailing-digits strip).
    Import-level sanity too: source_mirror's own --check covers its behavior in depth."""
    import source_mirror as sm
    import source_remote as sr
    assert sr.to_vsicurl("https://noaa-nos-coastal-lidar-pds.s3.amazonaws.com/dem/X/t.tif") \
        == "/vsicurl/https://noaa-nos-coastal-lidar-pds.s3.amazonaws.com/dem/X/t.tif"
    assert sr.to_vsicurl("s3://b/k/t.tif") == "/vsicurl/https://b.s3.amazonaws.com/k/t.tif"
    assert sm.cell_key("ed3.0.0/x/102US004LA1DO2622F7.h5") == "102US004LA1DO"
    assert sm._split_bucket_key("https://noaa-s102-pds.s3.amazonaws.com/ed3.0.0/") \
        == ("noaa-s102-pds", "ed3.0.0/")
    print("remote parsers ok")


def main():
    check_remote_parsers()
    tmp = tempfile.mkdtemp()
    try:
        sid = "_synth"
        os.makedirs(f"{tmp}/sources/{sid}")
        os.makedirs(f"{tmp}/store/source/{sid}")
        nodata = -9999.0
        arr = np.array([[5, 10, nodata], [0, 2.5, 100]], dtype="float32")  # +depth
        # Pre-place the "downloaded" raster directly in store/source.
        with rasterio.open(f"{tmp}/store/source/{sid}/{sid}_0.tif", "w", driver="GTiff",
                           height=2, width=3, count=1, dtype="float32", nodata=nodata,
                           crs="EPSG:4326", transform=from_origin(-74.0, 40.7, 0.001, 0.001)) as d:
            d.write(arr, 1)

        with open(f"{tmp}/sources/{sid}/metadata.json", "w") as f:
            json.dump({"name": "synth", "max_zoom": 13}, f)

        for step in (["source_datum.py", sid, "--negate", "--offset", "-1"],
                     ["source_normalize.py", sid, "--crs", "EPSG:4326", "--nodata", "-9999"],
                     ["source_bounds.py", sid], ["source_polygonize.py", sid, "2"],
                     ["source_create_tarball.py", sid], ["source_catalog.py", sid]):
            run(tmp, *step)

        norm = f"{tmp}/store/source/{sid}/{sid}_0.tif"
        assert os.path.exists(norm), norm
        with rasterio.open(norm) as s:
            out = s.read(1)
            valid = s.read_masks(1) != 0
        assert out[0, 0] == -6.0 and out[1, 2] == -101.0, out  # negate then -1 m
        assert not valid[0, 2], "nodata pixel should remain nodata"
        for artifact in (f"{tmp}/store/source/{sid}/bounds.csv",
                         f"{tmp}/store/source/{sid}/datum.json",
                         f"{tmp}/store/source/{sid}/catalog.json",
                         f"{tmp}/store/polygon/{sid}.gpkg",
                         f"{tmp}/store/tar/{sid}.tar"):
            assert os.path.exists(artifact), artifact
        # The catalog folds in the recorded offset, the assigned CRS, and the file count — but
        # negate must publish False: source_datum baked the flip into the COGs, and aggregation
        # consumes seascape:negate as "negate at reproject" (republishing the applied flag
        # double-negated african_great_lakes/ddm back to positive depth).
        with open(f"{tmp}/store/source/{sid}/catalog.json") as jf:
            item = json.load(jf)
        p = item["properties"]
        assert item["id"] == sid and item["bbox"], item
        assert p["seascape:negate"] is False and p["seascape:datum_offset_m"] == -1.0, p
        assert p["proj:epsg"] == 4326 and p["seascape:file_count"] == 1, p
        print("source stage e2e ok")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
