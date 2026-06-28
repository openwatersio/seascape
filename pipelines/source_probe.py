"""source_probe.py — inspect a candidate source raster and print the facts an
ingest recipe needs, so a new ``sources/<id>/`` can be written without hand-running
gdalinfo. Reuses the pipeline's OWN native-zoom derivation (aggregation_covering),
so the suggested cap matches what ``cover`` will actually compute.

Handles local files, http(s) URLs (wrapped in ``/vsicurl``), and zip archives
(``/vsizip``: lists members, probes the .tif). Stats are read decimated (uses COG
overviews), so probing a remote COG doesn't pull the whole file.

Usage:
  uv run python source_probe.py <path|url>
  uv run python source_probe.py <archive.zip> [--inner NAME.tif]
  uv run python source_probe.py --check
"""

import argparse
import math
import re
import subprocess
import sys

import numpy as np
import rasterio
from rasterio.warp import transform_bounds

import utils
from aggregation_covering import get_mercator_resolutions, get_smallest_overzoom

RASTER_EXTS = (".tif", ".tiff", ".nc", ".img", ".grd", ".vrt", ".asc")


def vsi_path(target, inner=None):
    """Build a GDAL VSI path from a local path / http url / zip (+ optional member).
    Returns (path, is_zip)."""
    p = target
    if p.startswith(("http://", "https://")):
        p = "/vsicurl/" + p
    if ".zip" in p.lower():
        base = "/vsizip/{" + p + "}"
        return (base + "/" + inner if inner else base), True
    return p, False


def list_zip_members(zip_vsi):
    """Raster members inside a zip — via one gdalinfo (works for remote zips too:
    GDAL prints the member list when handed a multi-file archive)."""
    r = subprocess.run(["gdalinfo", zip_vsi], capture_output=True, text=True)
    members = re.findall(r"/vsizip/\{[^}]+\}/\S+", r.stdout + r.stderr)
    seen, out = set(), []
    for m in members:
        if m.lower().endswith(RASTER_EXTS) and m not in seen:
            seen.add(m)
            out.append(m)
    return out


def infer_negate(mn, mx):
    """Map sampled value range -> whether the source needs ``source_datum --negate``
    to reach elevation convention (water negative, land positive, like GEBCO)."""
    if mn is None or not math.isfinite(mn):
        return None, "no valid pixels sampled — inspect manually"
    if mn >= 0:
        return True, ("all sampled values >= 0 -> positive-down depth -> NEGATE "
                      "(verify: an inland/lake bed on a local datum wants --offset, not negate)")
    if mx <= 0:
        return False, "all sampled values <= 0 -> ocean elevation (seafloor negative) -> no negate"
    return False, "mixed sign -> elevation (seafloor negative, land/reef positive) -> no negate"


def probe(vsi):
    with rasterio.open(vsi) as src:
        epsg = src.crs.to_epsg() if src.crs else None
        dtype = src.dtypes[0]
        nodata = src.nodata
        oh, ow = min(src.height, 512), min(src.width, 512)
        arr = src.read(1, out_shape=(oh, ow), masked=True).astype("float64")
        valid = np.ma.compressed(arr)
        valid = valid[np.isfinite(valid)]
        if nodata is not None:  # belt-and-suspenders for unset masks
            valid = valid[valid != nodata]
        mn = float(valid.min()) if valid.size else None
        mx = float(valid.max()) if valid.size else None
        mean = float(valid.mean()) if valid.size else None
        left, bottom, right, top = transform_bounds(src.crs, "EPSG:3857", *src.bounds)
        if right - left > 0.9 * 2 * utils.X_MAX_3857:  # antimeridian flip (see source_bounds)
            left, right = right, left
        resolutions = get_mercator_resolutions(0, 24)
        native_z = get_smallest_overzoom(left, bottom, right, top, src.width, src.height, resolutions)
        return {"driver": src.driver, "width": src.width, "height": src.height,
                "epsg": epsg, "dtype": dtype, "nodata": nodata,
                "min": mn, "max": mx, "mean": mean, "native_z": native_z}


def report(target, vsi, info):
    negate, why = infer_negate(info["min"], info["max"])
    crs = f"EPSG:{info['epsg']}" if info["epsg"] else "UNKNOWN (pass --crs)"
    floored = max(info["native_z"], utils.macrotile_z)
    print(f"\n=== {target} ===")
    if vsi != target:
        print(f"  opened:      {vsi}")
    print(f"  driver/size: {info['driver']}  {info['width']} x {info['height']}")
    print(f"  CRS:         {crs}")
    print(f"  dtype:       {info['dtype']}")
    print(f"  NoData:      {info['nodata']}")
    rng = (f"min {info['min']:.2f}  mean {info['mean']:.2f}  max {info['max']:.2f}"
           if info["min"] is not None else "(no valid pixels sampled)")
    print(f"  values:      {rng}")
    print(f"  sign:        {'NEGATE' if negate else 'no negate'}  ({why})")
    print(f"  native zoom: {info['native_z']}  (floored to macrotile_z={utils.macrotile_z}: {floored})"
          f"  -> suggested max_zoom: {info['native_z']}")
    print("  recipe:")
    print(f"    source_normalize <id> --crs {crs}")
    neg = "    source_datum <id> --negate   # values are positive-down depth" if negate else \
          "    # no --negate (already elevation; for an inland/lake bed use --offset to its surface level)"
    print(neg)
    print(f"    # metadata.json: \"max_zoom\": {info['native_z']}, record the vertical datum")


def main():
    p = argparse.ArgumentParser(description="Probe a candidate bathymetry source for ingest params.")
    p.add_argument("target", nargs="?", help="local path, http(s) URL, or .zip")
    p.add_argument("--inner", help="member name inside a zip (default: probe each raster member)")
    p.add_argument("--check", action="store_true", help="run the self-check")
    a = p.parse_args()
    if a.check:
        _check()
        return
    if not a.target:
        p.error("target required (or --check)")
    vsi, is_zip = vsi_path(a.target, a.inner)
    targets = [vsi]
    if is_zip and not a.inner:
        members = list_zip_members(vsi)
        if not members:
            sys.exit(f"no raster members found in {a.target}")
        print(f"{a.target}: {len(members)} raster member(s); probing each")
        targets = members
    for t in targets:
        report(a.target, t, probe(t))


def _check():
    import os
    import tempfile

    def write(path, values, nodata, crs="EPSG:4326"):
        data = np.array(values, dtype="float32")
        with rasterio.open(path, "w", driver="GTiff", height=data.shape[0], width=data.shape[1],
                           count=1, dtype="float32", crs=crs, nodata=nodata,
                           transform=rasterio.transform.from_bounds(0, 50, 1, 51, data.shape[1], data.shape[0])) as dst:
            dst.write(data, 1)

    with tempfile.TemporaryDirectory() as d:
        elev = os.path.join(d, "elev.tif")
        write(elev, [[-100, -50, -9999], [10, 30, 50]], nodata=-9999)
        i = probe(elev)
        assert i["epsg"] == 4326, i["epsg"]
        assert i["min"] == -100 and i["max"] == 50, (i["min"], i["max"])  # nodata excluded
        neg, _ = infer_negate(i["min"], i["max"])
        assert neg is False, "mixed-sign elevation must NOT negate"

        depth = os.path.join(d, "depth.tif")
        write(depth, [[5, 20, 80], [120, 160, 200]], nodata=-9999)
        j = probe(depth)
        neg2, _ = infer_negate(j["min"], j["max"])
        assert neg2 is True, "all-positive depth must negate"
        assert isinstance(j["native_z"], int) and j["native_z"] >= 0

    print("source_probe self-check: ok")


if __name__ == "__main__":
    main()
