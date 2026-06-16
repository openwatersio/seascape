"""Pack the redistributable per-source artifact: store/tar/<id>.tar.

Adapted from mapterhorn (BSD-3). Bundles metadata + bounds + coverage polygon +
the normalized DEM files, streaming an MD5. Local changes: reads from
``../sources`` (renamed); LICENSE.pdf is optional; includes file_list.txt.
"""

from glob import glob
import sys
import tarfile
import os
import json

import config
import utils


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: source_create_tarball.py <source-id>")
    source = sys.argv[1]
    print(f"creating tarball for {source}...")

    src_dir = f"{config.SOURCES_DIR}/{source}"
    utils.create_folder("store/tar/")
    filepath = f"store/tar/{source}.tar"
    with open(filepath, "wb") as f:
        writer = utils.HashWriter(f)
        with tarfile.open(fileobj=writer, mode="w") as tar:
            for optional in ("LICENSE.pdf", "file_list.txt"):
                if os.path.isfile(f"{src_dir}/{optional}"):
                    tar.add(f"{src_dir}/{optional}", optional)
            tar.add(f"{src_dir}/metadata.json", "metadata.json")
            tar.add(f"store/source/{source}/bounds.csv", "bounds.csv")
            tar.add(f"store/polygon/{source}.gpkg", "coverage.gpkg")
            tifs = glob(f"store/source/{source}/*.tif")
            for j, tif in enumerate(tifs, 1):
                if j % 1000 == 0:
                    print(f"{j:_} / {len(tifs):_}")
                tar.add(tif, f"files/{tif.split('/')[-1]}")
        checksum = writer.md5.hexdigest()

    utils.create_folder("store/meta/tar/")
    with open(f"store/meta/tar/{source}.json", "w") as f:
        json.dump({"size": os.path.getsize(filepath), "md5sum": checksum}, f, indent=2)
    print(f"store/tar/{source}.tar ({os.path.getsize(filepath):_} bytes, md5 {checksum})")


if __name__ == "__main__":
    main()
