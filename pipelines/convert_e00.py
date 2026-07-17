"""ARC/INFO .e00 (GRD section) → GeoTIFF — a content-keyed format converter.

The GDAL E00GRID driver isn't in the stock builds (only AIG binary + AAIGrid), so the
GRD interchange section is parsed directly. The format is simple and self-describing:

    line 1: EXP 0 <path>
    line 2: GRD <n>
    line 3: <ncols> <nrows> <type> <nodata>   (fixed-width columns)
    line 4: <cellsize_x> <cellsize_y>
    line 5: <minx> <miny>          (lower-left cell edge)
    line 6: <maxx> <maxy>          (upper-right cell edge)
    line 7..EOG: values, row-major top-to-bottom, 5 fixed-width E-notation floats/line

Called from staging (source_prep) once a raw asset sniffs as an .e00 export. No CRS is
assigned here — source_normalize assigns the horizontal CRS from metadata.json — so this
stays a pure format converter, not a source-specific script. Absorbed verbatim from the
GRD parser in source_download_tahoe.py (the legacy downloader stays for the Justfile chain
until cutover A; the duplication is bounded and noted in the migration plan).
"""

import numpy as np
import rasterio
from rasterio.transform import from_origin


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


def e00_to_tif(e00_path, tif_path):
    """Convert an ARC/INFO .e00 export to a tiled Float32 GeoTIFF (no CRS assigned)."""
    arr, transform, nodata = parse_e00_grd(e00_path)
    valid = arr[arr != nodata]
    print(f"  e00 grid {arr.shape[1]}x{arr.shape[0]}  range {valid.min():.3f}..{valid.max():.3f}"
          f"  nodata {nodata} -> {tif_path}")
    with rasterio.open(tif_path, "w", driver="GTiff", height=arr.shape[0], width=arr.shape[1],
                       count=1, dtype="float32", nodata=nodata, transform=transform,
                       tiled=True, blockxsize=512, blockysize=512, compress="deflate") as dst:
        dst.write(arr, 1)
