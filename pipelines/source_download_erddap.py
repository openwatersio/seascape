"""Fetch an ERDDAP griddap source as a fetch_bbox NetCDF subset, then to GeoTIFF.

For datasets served by an ERDDAP griddap endpoint (e.g. EMODnet 2024, whose full
grid is ~136 GB): download only the bbox subset. file_list.txt holds the griddap
base URL; metadata.json carries fetch_bbox (W,S,E,N lon/lat) and erddap_var
(default ``elevation``). ERDDAP wants axis ranges in stored (ascending) order.
"""

import argparse
import os
import subprocess

import config
import utils


def main():
    p = argparse.ArgumentParser(description="Fetch an ERDDAP griddap bbox subset as GeoTIFF.")
    p.add_argument("source")
    p.add_argument("--bbox", required=True, help="W,S,E,N in lon/lat")
    p.add_argument("--var", default="elevation", help="griddap variable name")
    a = p.parse_args()
    source, var = a.source, a.var
    w, s, e, n = (float(x) for x in a.bbox.split(","))
    os.makedirs(f"store/source/{source}", exist_ok=True)

    for i, url in enumerate(config.file_list(source)):
        nc = f"store/source/{source}/{source}_{i}.nc"
        out = f"store/source/{source}/{source}_{i}.tif"
        print(f"  [{i}] erddap subset {url} ({bbox}) -> {out}")
        utils.http_download(f"{url}.nc?{var}[({s}):({n})][({w}):({e})]", nc)
        subprocess.run(["gdal_translate", f"NETCDF:{nc}:{var}", out], check=True)
        os.remove(nc)


if __name__ == "__main__":
    main()
