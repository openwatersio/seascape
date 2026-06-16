"""Extract GeoTIFF tiles from any .zip archives in store/source/<id>/.

For sources fetched as a zip (e.g. the GEBCO global grid). Flattens the archive's
*.tif/*.tiff members into store/source/<id>/ and removes the zip. No ±85° clamp
needed — the aggregation warp to EPSG:3857 clips the poles.
"""

import os
import sys
import zipfile
from glob import glob


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: source_unzip.py <source-id>")
    source = sys.argv[1]
    zips = sorted(glob(f"store/source/{source}/*.zip"))
    print(f"unzip {source}: {len(zips)} archive(s)")
    for zpath in zips:
        with zipfile.ZipFile(zpath) as z:
            members = [n for n in z.namelist() if n.lower().endswith((".tif", ".tiff"))]
            print(f"  {zpath}: {len(members)} tif(s)")
            for name in members:
                with open(f"store/source/{source}/{os.path.basename(name)}", "wb") as f:
                    f.write(z.read(name))
        os.remove(zpath)


if __name__ == "__main__":
    main()
