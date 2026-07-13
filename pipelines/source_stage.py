"""Stage raw rasters from the download cache into the source work dir.

The normalize-phase entry for non-zip sources (plain .tif/.nc — GEBCO's siblings aside:
DDM, the lakes, the estuarine netCDFs, …). source_datum / source_normalize mutate tifs
in place, so they must run on a *copy*: store/download/<id>/ stays pristine and
source-normalize is re-runnable with no network. Clears the source work dir first, then
copies *.tif/*.nc — the files the transform chain globs. Download-only junk (archives,
.vrt, intermediate dirs a bespoke downloader left behind) stays in the cache.

Zip sources use source_unzip instead — extraction *is* their stage.
"""

import os
import shutil
import sys
from glob import glob

RASTER_GLOBS = ("*.tif", "*.tiff", "*.nc")


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: source_stage.py <source-id>")
    source = sys.argv[1]
    src = f"store/download/{source}"
    dst = f"store/source/{source}"
    files = sorted(f for pat in RASTER_GLOBS for f in glob(f"{src}/{pat}"))
    if not files:
        sys.exit(f"no rasters to stage in {src}/ (run source-download first?)")
    shutil.rmtree(dst, ignore_errors=True)
    os.makedirs(dst, exist_ok=True)
    print(f"stage {source}: {len(files)} file(s) {src}/ -> {dst}/")
    for f in files:
        shutil.copy2(f, f"{dst}/{os.path.basename(f)}")


def _check():
    import tempfile
    d = tempfile.mkdtemp()
    os.chdir(d)
    os.makedirs(f"store/download/x")
    for n in ("x_0.tif", "x_1.nc", "x.vrt", "x.zip"):  # only tif/nc stage; vrt/zip stay
        open(f"store/download/x/{n}", "wb").close()
    os.makedirs("store/source/x")
    open("store/source/x/stale.tif", "wb").close()  # cleared before staging
    sys.argv = ["source_stage.py", "x"]
    main()
    staged = sorted(os.path.basename(p) for p in glob("store/source/x/*"))
    assert staged == ["x_0.tif", "x_1.nc"], staged
    assert sorted(os.path.basename(p) for p in glob("store/download/x/*")) == \
        ["x.vrt", "x.zip", "x_0.tif", "x_1.nc"]  # cache untouched
    print("source_stage.py self-check ok")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        _check()
    else:
        main()
