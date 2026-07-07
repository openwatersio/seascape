"""Extract GeoTIFF tiles from any .zip archives in store/source/<id>/.

For sources fetched as a zip (e.g. the GEBCO global grid). Flattens the archive's
*.tif/*.tiff members into store/source/<id>/ and removes the zip. No ±85° clamp
needed — the aggregation warp to EPSG:3857 clips the poles.

Member filters (case-insensitive substring, for zips that bundle byproducts):
  --exclude S   skip members containing S (e.g. ``_hs.tif`` hillshades)
  --prefer S    if any member contains S, extract only those — for zips that ship
                the same grid twice (AusSeabed: a raw tif beside its ``_cog`` twin)
"""

import argparse
import os
import zipfile
from glob import glob


def select_members(names, exclude=None, prefer=None):
    tifs = [n for n in names if n.lower().endswith((".tif", ".tiff"))]
    if exclude:
        tifs = [n for n in tifs if exclude.lower() not in n.lower()]
    if prefer:
        preferred = [n for n in tifs if prefer.lower() in n.lower()]
        if preferred:
            tifs = preferred
    return tifs


def dest_name(member):
    """Flattened on-disk basename with a lowercase ``.tif`` extension — every
    downstream step globs ``*.tif``, so a ``.tiff``/``.TIF`` member would
    silently vanish from the source."""
    return os.path.basename(member).rsplit(".", 1)[0] + ".tif"


def main():
    p = argparse.ArgumentParser(description="Flatten zip archives' tif members into the source dir.")
    p.add_argument("source")
    p.add_argument("--exclude", help="skip members containing this substring")
    p.add_argument("--prefer", help="if any member contains this substring, extract only those")
    a = p.parse_args()
    zips = sorted(glob(f"store/source/{a.source}/*.zip"))
    print(f"unzip {a.source}: {len(zips)} archive(s)")
    for zpath in zips:
        with zipfile.ZipFile(zpath) as z:
            members = select_members(z.namelist(), a.exclude, a.prefer)
            print(f"  {zpath}: {len(members)} tif(s)")
            for name in members:
                with open(f"store/source/{a.source}/{dest_name(name)}", "wb") as f:
                    f.write(z.read(name))
        os.remove(zpath)


def _check():
    names = ["d/a_cog.tif", "d/a_raw.tiff", "d/a_hs.tiff", "meta/x.txt"]
    assert select_members(names) == ["d/a_cog.tif", "d/a_raw.tiff", "d/a_hs.tiff"]
    assert select_members(names, exclude="_hs.tif") == ["d/a_cog.tif", "d/a_raw.tiff"]
    assert select_members(names, exclude="_hs.tif", prefer="_cog.tif") == ["d/a_cog.tif"]
    # prefer is a no-op when nothing matches — a cog-less zip keeps its members
    assert select_members(["d/b.tif"], exclude="_hs.tif", prefer="_cog.tif") == ["d/b.tif"]
    assert dest_name("d/a_cog.tiff") == "a_cog.tif" and dest_name("d/a.TIFF") == "a.tif"
    assert dest_name("d/a_cog.tif") == "a_cog.tif"
    print("source_unzip.py self-check ok")


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["--check"]:
        _check()
    else:
        main()
