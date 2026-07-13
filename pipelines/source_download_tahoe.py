"""Fetch USGS DDS-55 Lake Tahoe bathymetry and convert its ARC/INFO .e00 export to a tif.

The authoritative grid (lt_bathy.e00.gz, 10 m, Float32, 1400.017-1898.999 m) is an
ARC/INFO GRID interchange export. GDAL's E00GRID driver isn't in the stock Homebrew/
Ubuntu builds (only AIG binary + AAIGrid), so we parse the GRD section directly — the
format is simple and fully self-describing:

    line 1: EXP 0 <path>
    line 2: GRD <n>
    line 3: <ncols> <nrows> <type> <nodata>
    line 4: <cellsize_x> <cellsize_y>
    line 5: <minx> <miny>          (lower-left cell edge)
    line 6: <maxx> <maxy>          (upper-right cell edge)
    line 7..EOG: values, row-major top-to-bottom, 5 per line, fixed E-notation
    PRJ section: Projection UTM / Zone 10 / Datum WGS84 / Units METERS  -> EPSG:32610

Values are bed ELEVATION in metres above sea level (lake surface ~1898.999, deepest
bed 1400.017). source_datum --offset -1899 (the survey's reference lake elevation,
which is also the grid max) brings the surface to ~0 and the bed negative.
"""

import gzip
import os
import sys

import numpy as np
import rasterio
from rasterio.transform import from_origin

import config
import utils

EPSG = "EPSG:32610"  # WGS84 / UTM zone 10N — from the e00 PRJ section


def parse_e00_grd(path):
    """Parse the GRD section of an ARC/INFO .e00 export into (array, transform, nodata)."""
    with open(path) as f:
        assert f.readline().startswith("EXP"), "not an .e00 export"
        assert f.readline().split()[0] == "GRD", "no GRD section"
        # Header line 3 is fixed-width: ncols[1:10] nrows[11:20] type[21:22] nodata[23:43]
        # — the type digit and the nodata's leading '-' are fused (no space), so a plain
        # split() mis-counts; read by column.
        h = f.readline()
        ncols, nrows = int(h[0:10]), int(h[10:20])
        nodata = float(h[22:])
        csx, csy = (float(x) for x in f.readline().split())
        minx, _miny = (float(x) for x in f.readline().split())
        _maxx, maxy = (float(x) for x in f.readline().split())
        # Values follow row-major, top row first, 5 fixed-width (14-char) E-notation
        # floats per line. ESRI's GRD export pads each grid row out to a whole number
        # of 5-value lines, so the on-disk stride is ceil(ncols/5)*5 (e.g. 1992 -> 1995,
        # 3 pad values per row); slice each row back to ncols.
        toks = []
        for line in f:
            if line.startswith("EOG"):
                break
            s = line.rstrip("\n")
            toks.extend(s[i:i + 14] for i in range(0, len(s), 14))
        stride = -(-ncols // 5) * 5  # ceil(ncols/5)*5
        flat = np.array(toks, dtype="float32").reshape(nrows, stride)
        arr = np.ascontiguousarray(flat[:, :ncols])  # drop the per-row pad
    transform = from_origin(minx, maxy, csx, csy)
    return arr, transform, nodata


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: source_download_tahoe.py <source-id>")
    source = sys.argv[1]
    urls = config.file_list(source)
    if not urls:
        sys.exit(f"no URLs in {config.SOURCES_DIR}/{source}/file_list.txt")
    out_dir = f"store/download/{source}"
    os.makedirs(out_dir, exist_ok=True)

    gz = f"{out_dir}/lt_bathy.e00.gz"
    e00 = f"{out_dir}/lt_bathy.e00"
    print(f"downloading {source}: {urls[0]}")
    utils.http_download(urls[0], gz)
    with gzip.open(gz, "rb") as fin, open(e00, "wb") as fout:
        fout.write(fin.read())

    print("parsing ARC/INFO .e00 GRD section ...")
    arr, transform, nodata = parse_e00_grd(e00)
    print(f"  grid {arr.shape[1]}x{arr.shape[0]}  range {np.nanmin(arr[arr != nodata]):.3f}"
          f"..{arr[arr != nodata].max():.3f}  nodata {nodata}")

    tif = f"{out_dir}/lake_tahoe.tif"
    with rasterio.open(tif, "w", driver="GTiff", height=arr.shape[0], width=arr.shape[1],
                       count=1, dtype="float32", crs=EPSG, nodata=nodata, transform=transform,
                       tiled=True, blockxsize=512, blockysize=512, compress="deflate") as dst:
        dst.write(arr, 1)
    os.remove(gz)
    os.remove(e00)
    print(f"  wrote {tif}")


if __name__ == "__main__":
    main()
