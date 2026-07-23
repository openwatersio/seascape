"""Prepare one source from its fetched raw assets: stage → datum → normalize.

The Snakemake lane's single prep entry point, driven entirely by metadata.json —
the per-source knobs that live in Justfile flags on the legacy chain:

  crs             horizontal CRS to assign (source_normalize --crs)
  nodata          nodata value to assign (source_normalize --nodata)
  negate          raw values are positive-down depth → flip (source_datum --negate)
  datum_offset_m  constant shift to ~MSL (source_datum --offset)
  clamp_positive  drop cells above the water surface (source_datum --clamp-positive)
  unpack          how to turn each raw asset into staged raster(s); absent = a bare
                  raster (see below)

Staging is DECLARED, never sniffed: the source author knows each asset's shape at
add time, so `unpack` names it and stage() dispatches on the declaration. The
micro-syntax is `format[:glob][!N]`:

  zip:<glob>        extract the archive members matching <glob> flat (GEBCO/EMODnet/
                    the INFOMAR & Aussie zips — one or more .tif members)
  tar.gz:<glob>!N   gunzip → tar → extract the <glob> members flat; `!N` asserts
                    exactly N matches per archive (great_lakes: one *_lld.tif each)
  7z:<glob>         extract the <glob> members flat via py7zr (African Great Lakes) —
                    py7zr, not GDAL /vsi7z (the CI image's GDAL lacks libarchive)
  asc-mosaic        a zip of ESRI ASCII .asc grids → mosaic to one store/source/<id>/
                    <id>.tif (swissBATHY / Bodensee)
  e00               gunzip → ARC/INFO .e00 export → convert to <id>.tif (Lake Tahoe;
                    the export is gzip-wrapped and the unpacker handles the wrapper)
  netcdf            gdal_translate to a GeoTIFF, per-file CRS preserved (NOAA estuaries)
  (absent)          a bare raster: hardlink to <id>_<index>.<ext> with the URL-derived
                    extension (ext_for), keeping the store's historical file naming

Content sniffing (`_kind`) survives ONLY as validation: an asset whose leading bytes
contradict its declaration (a truncated download, an upstream 200-with-error-page) is
a corrupt raw — deleted with a refetch message, the same self-heal as before. A bare
raster is validated by header-opening it (`_check_raster`).

Staged basenames are tracked across every raw: two members (from any archives or nested
paths) sharing a basename is a hard error, never a silent overwrite. The in-place
datum/normalize steps os.replace onto fresh inodes, so they can never write through into
raw/. Every derived intermediate (tifs, .nc, gzip spools, VRT/7z scratch, asc/ tiles) is
removed at entry — all are re-derivable from raw/ + this module — and orphan raw indices
beyond a shrunken file_list.txt are deleted rather than wedging the source.

Run from pipelines/:  uv run python source_prep.py <source-id>
"""

import fnmatch
import gzip
import lzma
import os
import shutil
import sys
import tarfile
import zipfile
from glob import glob

import config
import utils
from convert_e00 import e00_to_tif
from source_datum import transform_file, write_sidecar
from source_normalize import normalize_file

# Only trust a URL's trailing extension when it names a real data/archive format;
# otherwise (e.g. a weblink ending in ...html?...) stage as .tif — GDAL reads by
# content, not by name.
DATA_EXTS = {"tif", "tiff", "zip", "nc", "asc", "xyz", "img", "gz", "7z", "grd"}


def ext_for(url):
    last = url.split("?")[0].split("#")[0].rsplit("/", 1)[-1]
    ext = last.rsplit(".", 1)[-1].lower() if "." in last else ""
    if ext == "tiff":  # canonicalize to .tif — the staged extension the rest of the lane globs
        return "tif"
    return ext if ext in DATA_EXTS else "tif"


def _kind(head):
    """Classify a raw asset by its leading bytes (>= 512 read, for the tar magic at 257)."""
    if head[:4] == b"PK\x03\x04":
        return "zip"
    if head[:6] == b"7z\xbc\xaf\x27\x1c":
        return "7z"
    if head[:2] == b"\x1f\x8b":
        return "gzip"
    if head[257:262] == b"ustar":
        return "tar"
    if head[:3] == b"EXP":  # ARC/INFO .e00 export ("EXP 0 ...")
        return "e00"
    if head[:3] == b"CDF" or head[:4] == b"\x89HDF":  # classic netCDF or netCDF-4/HDF5
        return "netcdf"
    if head[:2] in (b"II", b"MM"):  # TIFF (little/big endian)
        return "tif"
    return "other"


# The magic byte kind each declared format's raw asset must present — sniffing kept only
# to validate the declaration (e00/tar.gz arrive gzip-wrapped, asc-mosaic as a zip).
_EXPECT_KIND = {
    "zip": "zip",
    "asc-mosaic": "zip",
    "tar.gz": "gzip",
    "e00": "gzip",
    "7z": "7z",
    "netcdf": "netcdf",
}


def _parse_unpack(spec):
    """Parse a metadata `unpack` string `format[:glob][!N]` → (format, glob, expect).
    `expect` is the exact per-archive match count asserted by `!N` (else None); `glob`
    is None for the glob-less formats (e00/netcdf/asc-mosaic)."""
    fmt, _, rest = spec.partition(":")
    members_glob, expect = (rest or None), None
    if members_glob and "!" in members_glob:
        members_glob, _, n = members_glob.partition("!")
        expect = int(n)
    if fmt not in _EXPECT_KIND:
        sys.exit(f"unknown unpack format {fmt!r} (expected one of {sorted(_EXPECT_KIND)})")
    return fmt, members_glob, expect


def _claim(seen, name, origin):
    """Register a staged basename; hard-error on a collision (two archive members or
    nested paths sharing a basename would silently overwrite each other)."""
    if name in seen:
        sys.exit(f"{origin}: staged filename collision on {name!r} — two members would "
                 "overwrite each other; the archives need distinct basenames")
    seen.add(name)


def _members(names, members_glob, expect, origin):
    """Archive members to extract: fnmatch of the declared glob against the full member
    path (case-insensitive). Zero matches is a hard error (the upstream layout changed
    under the recipe); `expect` (from `!N`) asserts an exact count per archive."""
    picks = [n for n in names if fnmatch.fnmatchcase(n.lower(), members_glob.lower())]
    if expect is not None and len(picks) != expect:
        sys.exit(f"{origin}: expected exactly {expect} member(s) matching "
                 f"{members_glob!r}, found {len(picks)}")
    if not picks:
        sys.exit(f"{origin}: no archive member matches unpack glob {members_glob!r}")
    return picks


def _extract_members(members_reader, names, root, seen, origin):
    """Write each selected member flat into root by its basename. members_reader(name)
    returns the member's bytes."""
    for name in names:
        base = os.path.basename(name)
        _claim(seen, base, origin)
        with open(f"{root}/{base}", "wb") as f:
            f.write(members_reader(name))
    return len(names)


def _stage_zip(raw, root, seen, origin, members_glob, expect):
    """A zip of GeoTIFFs → extract the declared members flat."""
    with zipfile.ZipFile(raw) as z:
        picks = _members(z.namelist(), members_glob, expect, origin)
        n = _extract_members(z.read, picks, root, seen, origin)
    return f"zip, {n} member(s)"


def _stage_asc(raw, asc_dir, seen, origin):
    """A zip of ESRI ASCII .asc grids → stash the tiles under asc_dir for a single mosaic
    after every asset is staged."""
    with zipfile.ZipFile(raw) as z:
        ascs = [n for n in z.namelist() if n.lower().endswith(".asc")]
        if not ascs:
            sys.exit(f"{origin}: asc-mosaic zip has no .asc members")
        os.makedirs(asc_dir, exist_ok=True)
        for name in ascs:
            base = os.path.basename(name)
            _claim(seen, base, origin)
            with open(f"{asc_dir}/{base}", "wb") as f:
                f.write(z.read(name))
    return f"asc-mosaic, {len(ascs)} tile(s) staged"


def _stage_7z(raw, root, seen, origin, members_glob, expect):
    """A 7z archive → extract the declared members flat (the African Great Lakes .7z
    carries four per-lake Analytical rasters). py7zr, not GDAL /vsi7z — the CI image's
    GDAL lacks the libarchive backend."""
    import py7zr
    with py7zr.SevenZipFile(raw) as z:
        picks = _members(z.getnames(), members_glob, expect, origin)
        tmp = f"{root}/_7z_extract"
        shutil.rmtree(tmp, ignore_errors=True)
        z.extract(path=tmp, targets=picks)
    for name in picks:
        base = os.path.basename(name)
        _claim(seen, base, origin)
        os.replace(f"{tmp}/{name}", f"{root}/{base}")
    shutil.rmtree(tmp, ignore_errors=True)
    return f"7z, {len(picks)} member(s)"


def _stage_targz(raw, root, index, seen, origin, members_glob, expect):
    """A gzipped tar → gunzip, then extract the declared members flat. `!N` keeps the
    NGDC Great Lakes exactly-one-*_lld.tif-per-tarball guard (0 or 2+ matches errors)."""
    inner = f"{root}/_gz_{index}"
    with gzip.open(raw, "rb") as fin, open(inner, "wb") as fout:
        shutil.copyfileobj(fin, fout)
    try:
        with tarfile.open(inner) as t:
            names = [m.name for m in t.getmembers() if m.isfile()]
            picks = _members(names, members_glob, expect, origin)
            for name in picks:
                base = os.path.basename(name)
                _claim(seen, base, origin)
                with t.extractfile(name) as src, open(f"{root}/{base}", "wb") as dst:
                    shutil.copyfileobj(src, dst)  # stream to disk, don't buffer the raster
        return f"tar.gz, {len(picks)} member(s)"
    finally:
        if os.path.exists(inner):
            os.remove(inner)


def _stage_e00(raw, root, source, index, seen, origin):
    """A gzipped ARC/INFO .e00 export → gunzip, then convert the GRD section to <id>.tif
    (pure-Python convert_e00 — GDAL here has no E00GRID driver)."""
    inner = f"{root}/_gz_{index}"
    with gzip.open(raw, "rb") as fin, open(inner, "wb") as fout:
        shutil.copyfileobj(fin, fout)
    try:
        _claim(seen, f"{source}.tif", origin)
        e00_to_tif(inner, f"{root}/{source}.tif")
        return "e00 → tif"
    finally:
        if os.path.exists(inner):
            os.remove(inner)


def _stage_netcdf(raw, root, url, seen, origin):
    """Translate a netCDF to a GeoTIFF, preserving the file's embedded CRS (no -a_srs) —
    a mixed-CRS source keeps each file's zone. Named after the URL stem so bounds.csv stays
    legible. A file with no embedded CRS is assigned EPSG:4326 (else source_bounds fails)."""
    import rasterio
    stem = url.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    _claim(seen, f"{stem}.tif", origin)
    nc = f"{root}/{stem}.nc"
    tif = f"{root}/{stem}.tif"
    os.link(raw, nc)  # gdal recognizes netCDF by extension + content
    utils.run_command(
        f"gdal_translate -q -of GTiff -co TILED=YES -co COMPRESS=DEFLATE {nc} {tif}",
        silent=False)
    os.remove(nc)
    with rasterio.open(tif, "r+") as src:
        if src.crs is None:
            src.crs = rasterio.crs.CRS.from_epsg(4326)
    return f"netCDF → {stem}.tif"


def _mosaic_asc(root, source, asc_dir):
    """Mosaic the staged ESRI ASCII tiles into one GeoTIFF via a VRT (no -a_srs;
    source_normalize assigns the CRS from metadata), then drop the tiles."""
    ascs = sorted(glob(f"{asc_dir}/*.asc"))
    listfile = f"{root}/tiles.txt"
    with open(listfile, "w") as f:
        f.write("\n".join(ascs) + "\n")
    vrt = f"{root}/{source}.vrt"
    tif = f"{root}/{source}.tif"
    print(f"{source}: mosaicking {len(ascs)} asc tile(s) -> {tif}")
    utils.run_command(f"gdalbuildvrt -overwrite -input_file_list {listfile} {vrt}", silent=False)
    utils.run_command(
        f"gdal_translate -q -of GTiff -a_nodata -9999 -co TILED=YES -co COMPRESS=DEFLATE "
        f"-co NUM_THREADS=ALL_CPUS {vrt} {tif}", silent=False)
    os.remove(vrt)
    os.remove(listfile)
    shutil.rmtree(asc_dir, ignore_errors=True)


def _raw_indices(source, root, n_urls):
    """Validate raw/ against file_list.txt and return the indices 0..n-1 in order.
    Orphan indices >= n (the list shrank) are deleted — they're re-fetchable derived
    state, and leaving them wedges the source until hand-cleanup. Non-index names are
    an anomaly (a crashed tool's leftovers, a stray file) and error out explicitly;
    missing indices name exactly what to fetch."""
    names = [p.rsplit("/", 1)[-1] for p in glob(f"{root}/raw/*")]
    odd = sorted(n for n in names if not n.isdigit())
    if odd:
        sys.exit(f"{source}: unexpected file(s) in raw/ (not list indices): {odd} — "
                 "remove them; raw/ holds only file_list.txt downloads")
    present = {int(n) for n in names}
    for orphan in sorted(i for i in present if i >= n_urls):
        print(f"{source}: deleting orphan raw/{orphan} (file_list.txt has {n_urls} entries)")
        os.remove(f"{root}/raw/{orphan}")
        present.discard(orphan)
    missing = sorted(set(range(n_urls)) - present)
    if missing:
        sys.exit(f"{source}: raw asset(s) missing for file_list.txt entries {missing} — "
                 "fetch them before prep")
    return sorted(present)


def _clear_stale(root):
    """Remove every derived/intermediate artifact a prior prep may have left: staged tifs,
    netCDF translations, gzip spool files, VRT/mosaic scratch, and the asc/ tile dir (stale
    .asc tiles would otherwise join the next mosaic; a stale .nc breaks the netCDF hardlink)."""
    for stale in (glob(f"{root}/*.tif") + glob(f"{root}/*.tiff") + glob(f"{root}/*.nc")
                  + glob(f"{root}/_gz_*") + glob(f"{root}/*.vrt") + glob(f"{root}/tiles.txt")):
        os.remove(stale)
    shutil.rmtree(f"{root}/asc", ignore_errors=True)
    shutil.rmtree(f"{root}/_7z_extract", ignore_errors=True)


class CorruptRaw(Exception):
    """A staged file whose bytes are unreadable — an upstream 200-with-error-page."""


# Errors that mean "this raw's BYTES are bad" (truncated archive, an upstream error page
# saved as a raster, bytes that contradict the declaration) — never a code bug, so deleting
# the raw and refetching is the remedy. Deliberately narrow: our own hard errors (collisions,
# zero glob matches, unknown format) sys.exit past it.
_CORRUPT = (zipfile.BadZipFile, gzip.BadGzipFile, tarfile.TarError, EOFError,
            lzma.LZMAError, CorruptRaw)


def _unpack_one(unpack, raw, root, source, index, url, asc_dir, seen, origin):
    """Materialize one raw asset per its declaration. No declaration = a bare raster.
    The declared format's magic bytes are validated first, so bytes that contradict the
    declaration self-heal as a corrupt raw (raising CorruptRaw)."""
    if unpack is None:
        base = f"{source}_{index}.{ext_for(url)}"
        _claim(seen, base, origin)
        dest = f"{root}/{base}"
        if os.path.exists(dest):
            os.remove(dest)
        os.link(raw, dest)
        _check_raster(dest)  # an upstream error page saved as .tif dies here
        return f"-> {base}"

    fmt, members_glob, expect = unpack
    with open(raw, "rb") as f:
        kind = _kind(f.read(512))
    if kind != _EXPECT_KIND[fmt]:
        raise CorruptRaw(f"declared unpack {fmt!r} expects {_EXPECT_KIND[fmt]} bytes, got {kind}")
    if fmt == "zip":
        return _stage_zip(raw, root, seen, origin, members_glob, expect)
    if fmt == "asc-mosaic":
        return _stage_asc(raw, asc_dir, seen, origin)
    if fmt == "7z":
        return _stage_7z(raw, root, seen, origin, members_glob, expect)
    if fmt == "tar.gz":
        return _stage_targz(raw, root, index, seen, origin, members_glob, expect)
    if fmt == "e00":
        return _stage_e00(raw, root, source, index, seen, origin)
    return _stage_netcdf(raw, root, url, seen, origin)  # fmt == "netcdf"


def stage(source):
    root = f"store/source/{source}"
    urls = config.file_list(source)
    spec = config.load_metadata(source).get("unpack")
    unpack = _parse_unpack(spec) if spec else None
    indices = _raw_indices(source, root, len(urls))
    _clear_stale(root)
    asc_dir = f"{root}/asc"
    seen = set()  # staged basenames — collisions across raws/archives hard-error
    corrupt = []
    for index in indices:
        raw = f"{root}/raw/{index}"
        origin = f"{source}[{index}]"
        try:
            note = _unpack_one(unpack, raw, root, source, index, urls[index], asc_dir, seen, origin)
        except _CORRUPT as e:
            print(f"{origin}: corrupt raw ({e}) — deleted, a rerun refetches it")
            os.remove(raw)
            corrupt.append(index)
            continue
        print(f"{origin}: {note}")
    if corrupt:
        sys.exit(f"{source}: deleted {len(corrupt)} corrupt raw asset(s) {corrupt} — "
                 "rerun to refetch them")
    if os.path.isdir(asc_dir):
        _mosaic_asc(root, source, asc_dir)


def _check_raster(path):
    """Header-open the staged file; unreadable bytes (a server's 200-with-error-page)
    surface here as a corrupt raw instead of a normalize crash naming only the staged tif."""
    import rasterio
    try:
        with rasterio.open(path):
            pass
    except rasterio.errors.RasterioIOError as e:
        os.remove(path)
        raise CorruptRaw(f"not a readable raster: {e}") from e


def prep(source):
    meta = config.load_metadata(source)
    stage(source)
    tifs = sorted(glob(f"store/source/{source}/*.tif"))  # staging canonicalizes .tiff -> .tif (ext_for)

    negate = bool(meta.get("negate", False))
    offset = float(meta.get("datum_offset_m", 0.0))
    clamp = bool(meta.get("clamp_positive", False))
    write_sidecar(source, negate, offset, clamp)  # even for a no-op: the catalog's invariant
    if negate or offset or clamp:
        print(f"{source}: datum negate={negate} offset={offset} clamp_positive={clamp}")
        for tif in tifs:
            transform_file(tif, negate, offset, clamp)

    crs, nodata = meta.get("crs"), meta.get("nodata")
    print(f"{source}: normalize {len(tifs)} file(s) (crs={crs} nodata={nodata})")
    for tif in tifs:
        normalize_file(tif, crs, nodata)


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: source_prep.py <source-id>")
    prep(sys.argv[1])


def _check():
    """Synthetic sources end to end, driven by declared `unpack`. Common path: a declared
    zip extracts its glob members, an undeclared raw hardlinks under the legacy name, stale
    root tifs are cleared, the metadata knobs drive datum + normalize. Failure modes: a
    missing raw index names what to fetch, a non-index file in raw/ is a distinct error, an
    orphan index beyond a shrunken file_list is deleted, and a staged-basename collision
    hard-errors. Format registry: a gzipped tar with `!1` stages exactly its one *_lld.tif
    (0 → error), a 7z glob filters its members, a gzipped .e00 stages to <id>.tif
    (pure-Python), and — when the GDAL CLI is present — an asc-mosaic zip mosaics to <id>.tif.
    Corrupt raws self-heal: a truncated declared zip, a declared zip whose bytes are not a
    zip, and an undeclared raw that is a server error page are all deleted with a refetch."""
    import io
    import json
    import tempfile

    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    # ext_for canonicalizes both GeoTIFF spellings to the staged .tif the lane globs, and
    # falls back to .tif for a non-data extension (GDAL reads by content, not name).
    assert ext_for("https://x/a.tiff") == "tif" and ext_for("https://x/a.tif") == "tif"
    assert ext_for("https://x/a.TIFF?k=v") == "tif" and ext_for("https://x/page.html?z") == "tif"
    assert ext_for("https://x/a.zip") == "zip" and ext_for("https://x/a.nc") == "nc"

    # _parse_unpack splits format/glob/!N and rejects an unknown format.
    assert _parse_unpack("zip:*.tif") == ("zip", "*.tif", None)
    assert _parse_unpack("tar.gz:*_lld.tif!1") == ("tar.gz", "*_lld.tif", 1)
    assert _parse_unpack("e00") == ("e00", None, None)
    try:
        _parse_unpack("rar:*.tif")
        assert False, "expected an unknown unpack format to exit"
    except SystemExit as e:
        assert "unknown unpack format" in str(e), e

    d = tempfile.mkdtemp()
    cwd, saved = os.getcwd(), config.SOURCES_DIR
    try:
        os.chdir(d)
        config.SOURCES_DIR = "sources"
        sid = "_prep_selfcheck"
        os.makedirs(f"sources/{sid}")
        os.makedirs(f"store/source/{sid}/raw")
        with open(f"sources/{sid}/file_list.txt", "w") as f:
            f.write("https://x/archive.zip\nhttps://x/plain.tif\n")
        with open(f"sources/{sid}/metadata.json", "w") as f:
            json.dump({"name": "Synth", "unpack": "zip:*.tif", "negate": True,
                       "datum_offset_m": -1.0, "crs": "EPSG:28992"}, f)

        def tif_bytes(value):
            buf = io.BytesIO()
            with rasterio.open(buf, "w", driver="GTiff", height=2, width=2, count=1,
                               dtype="float32", nodata=-9999.0, crs=None,
                               transform=from_origin(0, 2, 1, 1)) as dst:
                dst.write(np.full((2, 2), value, dtype="float32"), 1)
            return buf.getvalue()

        zbuf = io.BytesIO()
        with zipfile.ZipFile(zbuf, "w") as z:
            z.writestr("nested/a.tif", tif_bytes(5.0))  # +5 m depth
            z.writestr("readme.txt", b"skip me")
        with open(f"store/source/{sid}/raw/0", "wb") as f:
            f.write(zbuf.getvalue())
        with open(f"store/source/{sid}/stale.tif", "w") as f:
            f.write("old")
        # `unpack` applies to every raw, so index 1 is a zip too (+10 m depth member).
        zbuf1 = io.BytesIO()
        with zipfile.ZipFile(zbuf1, "w") as z:
            z.writestr("b.tif", tif_bytes(10.0))
        with open(f"store/source/{sid}/raw/1", "wb") as f:
            f.write(zbuf1.getvalue())

        prep(sid)
        assert not os.path.exists(f"store/source/{sid}/stale.tif"), "stale tif must be cleared"
        assert not os.path.exists(f"store/source/{sid}/readme.txt")
        assert open(f"store/source/{sid}/raw/1", "rb").read() == zbuf1.getvalue(), \
            "in-place steps must never write through into raw/"
        with open(f"store/source/{sid}/datum.json") as f:
            sidecar = json.load(f)
        assert sidecar == {"negate": True, "offset_m": -1.0, "clamp_positive": False}, sidecar
        for name, want in (("a.tif", -6.0), ("b.tif", -11.0)):  # -(v) - 1
            with rasterio.open(f"store/source/{sid}/{name}") as src:
                assert src.crs.to_epsg() == 28992, (name, src.crs)
                assert src.read(1)[0, 0] == want, (name, src.read(1)[0, 0])

        os.remove(f"store/source/{sid}/raw/1")
        try:
            prep(sid)
            assert False, "expected a missing raw index to exit"
        except SystemExit as e:
            assert "missing" in str(e) and "[1]" in str(e) and "fetch them" in str(e), e
        # …and a non-index file in raw/ is a distinct, named anomaly (never a bare int() crash).
        with open(f"store/source/{sid}/raw/junk", "w") as f:
            f.write("?")
        try:
            prep(sid)
            assert False, "expected a non-index raw file to exit"
        except SystemExit as e:
            assert "not list indices" in str(e) and "junk" in str(e), e
        os.remove(f"store/source/{sid}/raw/junk")

        # Orphan raw indices beyond the (shrunk) file_list are deleted, not a wedge.
        with open(f"sources/{sid}/file_list.txt", "w") as f:
            f.write("https://x/archive.zip\n")  # list shrank 2 → 1
        with open(f"store/source/{sid}/raw/1", "wb") as f:
            f.write(zbuf1.getvalue())  # now an orphan
        prep(sid)
        assert not os.path.exists(f"store/source/{sid}/raw/1"), "orphan raw must be deleted"

        # A bare raster (no `unpack`): the raw hardlinks under the legacy <id>_<index>.<ext>.
        bid = "_prep_bare"
        os.makedirs(f"sources/{bid}")
        os.makedirs(f"store/source/{bid}/raw")
        with open(f"sources/{bid}/file_list.txt", "w") as f:
            f.write("https://x/dem.tif\n")
        with open(f"sources/{bid}/metadata.json", "w") as f:
            json.dump({"name": "Bare", "crs": "EPSG:4326"}, f)
        with open(f"store/source/{bid}/raw/0", "wb") as f:
            f.write(tif_bytes(7.0))
        stage(bid)
        assert os.path.isfile(f"store/source/{bid}/{bid}_0.tif"), "bare raster stages under legacy name"

        # Two archives whose members share a basename must hard-error, not silently overwrite.
        cid = "_prep_collide"
        os.makedirs(f"sources/{cid}")
        os.makedirs(f"store/source/{cid}/raw")
        with open(f"sources/{cid}/file_list.txt", "w") as f:
            f.write("https://x/a.zip\nhttps://x/b.zip\n")
        with open(f"sources/{cid}/metadata.json", "w") as f:
            json.dump({"name": "Collide", "unpack": "zip:*.tif"}, f)
        for i in range(2):
            zb = io.BytesIO()
            with zipfile.ZipFile(zb, "w") as z:
                z.writestr(f"pack{i}/dup.tif", tif_bytes(1.0))
            with open(f"store/source/{cid}/raw/{i}", "wb") as f:
                f.write(zb.getvalue())
        try:
            stage(cid)
            assert False, "expected a basename collision to exit"
        except SystemExit as e:
            assert "collision" in str(e) and "dup.tif" in str(e), e

        # tar.gz:<glob>!1 stages ONLY the single matching member (the Great Lakes *_lld.tif
        # layout), and 0 (or 2+) matches per tarball is a hard error.
        import gzip as _gz
        import tarfile as _tar
        lid = "_prep_lld"
        os.makedirs(f"sources/{lid}")
        os.makedirs(f"store/source/{lid}/raw")
        with open(f"sources/{lid}/file_list.txt", "w") as f:
            f.write("https://x/huron_lld.geotiff.tar.gz\n")
        with open(f"sources/{lid}/metadata.json", "w") as f:
            json.dump({"name": "LLD", "unpack": "tar.gz:*_lld.tif!1"}, f)

        def targz_bytes(members):
            tb = io.BytesIO()
            with _tar.open(fileobj=tb, mode="w") as t:
                for name, data in members:
                    info = _tar.TarInfo(name)
                    info.size = len(data)
                    t.addfile(info, io.BytesIO(data))
            return _gz.compress(tb.getvalue())

        with open(f"store/source/{lid}/raw/0", "wb") as f:
            f.write(targz_bytes([("huron_lld/huron_lld.tif", tif_bytes(2.0)),
                                 ("huron_lld/huron_lld.prj", b"PROJCS[...]"),
                                 ("huron_lld/extra.tif", tif_bytes(3.0))]))  # non-matching: ignored
        stage(lid)
        assert os.path.isfile(f"store/source/{lid}/huron_lld.tif")
        assert not os.path.exists(f"store/source/{lid}/extra.tif"), "tar stages matches only"
        with open(f"store/source/{lid}/raw/0", "wb") as f:
            f.write(targz_bytes([("huron_lld/readme.txt", b"no dem here")]))
        try:
            stage(lid)
            assert False, "expected a no-match tarball to exit"
        except SystemExit as e:
            assert "exactly 1" in str(e), e

        # 7z:<glob> extracts only matching members (multiple matches OK — the African Great
        # Lakes .7z carries four per-lake rasters).
        import py7zr
        zid = "_prep_7z"
        os.makedirs(f"sources/{zid}")
        os.makedirs(f"store/source/{zid}/raw")
        with open(f"sources/{zid}/file_list.txt", "w") as f:
            f.write("https://x/rasters.7z\n")
        with open(f"sources/{zid}/metadata.json", "w") as f:
            json.dump({"name": "7Z", "unpack": "7z:*_ras.tif"}, f)
        with py7zr.SevenZipFile(f"store/source/{zid}/raw/0", "w") as z:
            z.writestr(tif_bytes(1.0), "Rasters/Lake_A_ras.tif")
            z.writestr(tif_bytes(2.0), "Rasters/Lake_B_ras.tif")
            z.writestr(tif_bytes(3.0), "Rasters/hillshade.tif")  # non-matching: ignored
        stage(zid)
        assert os.path.isfile(f"store/source/{zid}/Lake_A_ras.tif")
        assert os.path.isfile(f"store/source/{zid}/Lake_B_ras.tif")
        assert not os.path.exists(f"store/source/{zid}/hillshade.tif"), "7z stages matches only"
        assert not os.path.exists(f"store/source/{zid}/_7z_extract"), "7z scratch must be cleaned"

        # Format registry: a gzipped ARC/INFO .e00 export stages to <id>.tif (no GDAL CLI).
        eid = "_prep_e00"
        os.makedirs(f"sources/{eid}")
        os.makedirs(f"store/source/{eid}/raw")
        with open(f"sources/{eid}/file_list.txt", "w") as f:
            f.write("https://x/grid.e00.gz\n")
        with open(f"sources/{eid}/metadata.json", "w") as f:
            json.dump({"name": "E00", "unpack": "e00", "crs": "EPSG:32610"}, f)
        # Fixed-width GRD: ncols[0:10] nrows[10:20], one space + type digit at [20:22], then
        # nodata at [22:]; values are 14-char E-notation, 5/line, each grid row padded out to
        # ceil(ncols/5)*5 = 5 tokens. A 2x2 grid of [[1,2],[3,4]] with -3.4e38 nodata + pad.
        nd = -3.4e38
        rows = [[1.0, 2.0, nd, nd, nd], [3.0, 4.0, nd, nd, nd]]
        e00_text = (
            "EXP  0 GRID\nGRD  2\n"
            f"{2:10d}{2:10d} 2{nd:.7E}\n"
            f"{1.0:.7E} {1.0:.7E}\n0.0 0.0\n2.0 2.0\n"
            + "".join("".join(f"{v:14.7E}" for v in r) + "\n" for r in rows)
            + "EOG\nEOI\n")
        with _gz.open(f"store/source/{eid}/raw/0", "wb") as f:
            f.write(e00_text.encode())
        stage(eid)
        with rasterio.open(f"store/source/{eid}/{eid}.tif") as src:
            assert src.shape == (2, 2) and src.read(1)[0, 0] == 1.0, src.read(1)

        # Corrupt raws self-heal: a truncated declared zip (PK magic intact), a declared zip
        # whose bytes are not a zip at all (an error page), and — with no `unpack` — a server
        # error page routed as a raster: all deleted with a refetch; a rerun with good bytes
        # then succeeds. The exact truncated-download / 200-with-garbage cases.
        cid = "_prep_corrupt"
        os.makedirs(f"sources/{cid}")
        os.makedirs(f"store/source/{cid}/raw")
        with open(f"sources/{cid}/file_list.txt", "w") as f:
            f.write("https://x/a.zip\nhttps://x/b.zip\n")
        with open(f"sources/{cid}/metadata.json", "w") as f:
            json.dump({"name": "Corrupt", "unpack": "zip:*.tif"}, f)
        good_zip = io.BytesIO()
        with zipfile.ZipFile(good_zip, "w") as z:
            z.writestr("a.tif", tif_bytes(1.0))
        with open(f"store/source/{cid}/raw/0", "wb") as f:
            f.write(good_zip.getvalue()[: len(good_zip.getvalue()) // 2])  # truncated, PK intact
        with open(f"store/source/{cid}/raw/1", "wb") as f:
            f.write(b"<html>503 Service Unavailable</html>")  # not zip bytes → declaration contradicted
        try:
            stage(cid)
            assert False, "expected corrupt raws to exit"
        except SystemExit as e:
            assert "corrupt raw asset(s) [0, 1]" in str(e), e
        assert not os.path.exists(f"store/source/{cid}/raw/0"), "truncated zip raw must be deleted"
        assert not os.path.exists(f"store/source/{cid}/raw/1"), "non-zip raw must be deleted"
        with open(f"store/source/{cid}/raw/0", "wb") as f:
            f.write(good_zip.getvalue())
        good_zip1 = io.BytesIO()
        with zipfile.ZipFile(good_zip1, "w") as z:
            z.writestr("b.tif", tif_bytes(2.0))
        with open(f"store/source/{cid}/raw/1", "wb") as f:
            f.write(good_zip1.getvalue())
        stage(cid)  # refetched good bytes stage cleanly
        assert os.path.isfile(f"store/source/{cid}/a.tif")
        assert os.path.isfile(f"store/source/{cid}/b.tif")

        # An undeclared raw that is a server error page (not a raster) also self-heals.
        gid = "_prep_garbage"
        os.makedirs(f"sources/{gid}")
        os.makedirs(f"store/source/{gid}/raw")
        with open(f"sources/{gid}/file_list.txt", "w") as f:
            f.write("https://x/dem.tif\n")
        with open(f"sources/{gid}/metadata.json", "w") as f:
            json.dump({"name": "Garbage"}, f)
        with open(f"store/source/{gid}/raw/0", "wb") as f:
            f.write(b"<html>503 Service Unavailable</html>")
        try:
            stage(gid)
            assert False, "expected a garbage bare raster to exit"
        except SystemExit as e:
            assert "corrupt raw asset(s) [0]" in str(e), e
        assert not os.path.exists(f"store/source/{gid}/raw/0"), "garbage raster raw must be deleted"
        assert not os.path.exists(f"store/source/{gid}/{gid}_0.tif"), "bad staged tif must be removed"

        # Format registry: an asc-mosaic zip of ESRI ASCII tiles mosaics to <id>.tif (GDAL CLI).
        if shutil.which("gdalbuildvrt") and shutil.which("gdal_translate"):
            aid = "_prep_asc"
            os.makedirs(f"sources/{aid}")
            os.makedirs(f"store/source/{aid}/raw")
            with open(f"sources/{aid}/file_list.txt", "w") as f:
                f.write("https://x/tiles.esriasciigrid.zip\n")
            with open(f"sources/{aid}/metadata.json", "w") as f:
                json.dump({"name": "ASC", "unpack": "asc-mosaic", "crs": "EPSG:2056"}, f)
            asc = ("ncols 2\nnrows 2\nxllcorner 0\nyllcorner 0\ncellsize 1\n"
                   "NODATA_value -9999\n1 2\n3 4\n")
            zb = io.BytesIO()
            with zipfile.ZipFile(zb, "w") as z:
                z.writestr("swissbathy_1.asc", asc)
            with open(f"store/source/{aid}/raw/0", "wb") as f:
                f.write(zb.getvalue())
            stage(aid)
            assert os.path.isfile(f"store/source/{aid}/{aid}.tif"), "asc zip must mosaic to <id>.tif"
            with rasterio.open(f"store/source/{aid}/{aid}.tif") as src:
                assert src.shape == (2, 2), src.shape
            print("source_prep.py self-check ok (incl. asc mosaic)")
        else:
            print("source_prep.py self-check ok (asc mosaic skipped — no GDAL CLI)")
    finally:
        os.chdir(cwd)
        config.SOURCES_DIR = saved
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        _check()
    else:
        main()
