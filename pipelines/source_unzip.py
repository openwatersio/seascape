"""Extract GeoTIFF tiles from any .zip archives in store/download/<id>/.

The normalize-phase stage for zip sources (e.g. the GEBCO global grid): flattens the
archive's *.tif/*.tiff members by basename into store/source/<id>/, leaving the archive
untouched in the download cache so re-running normalize needs no re-fetch. Clears the
source work dir first so a rerun starts from clean raw. No ±85° clamp needed — the
aggregation warp to EPSG:3857 clips the poles.
"""

import os
import shutil
import sys
import zipfile
from glob import glob


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: source_unzip.py <source-id>")
    source = sys.argv[1]
    src = f"store/download/{source}"
    dst = f"store/source/{source}"
    zips = sorted(glob(f"{src}/*.zip"))
    if not zips:
        sys.exit(f"no archives in {src}/ (run source-download first?)")
    shutil.rmtree(dst, ignore_errors=True)
    os.makedirs(dst, exist_ok=True)
    print(f"unzip {source}: {len(zips)} archive(s) {src}/ -> {dst}/")
    for zpath in zips:
        with zipfile.ZipFile(zpath) as z:
            members = [n for n in z.namelist() if n.lower().endswith((".tif", ".tiff"))]
            print(f"  {zpath}: {len(members)} tif(s)")
            for name in members:
                with open(f"{dst}/{os.path.basename(name)}", "wb") as f:
                    f.write(z.read(name))


if __name__ == "__main__":
    main()
