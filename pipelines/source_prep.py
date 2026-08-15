"""Prepare one source from its fetched raw assets: stage → datum → normalize.

The Snakemake lane's single prep entry point, driven entirely by metadata.json —
the per-source knobs that live in Justfile flags on the legacy chain:

  crs             horizontal CRS to assign (source_normalize --crs)
  nodata          nodata value to assign (source_normalize --nodata)
  negate          raw values are positive-down depth → flip (source_datum --negate)
  datum_offset_m  constant shift to the target datum (source_datum --offset)
  offset_surface  reference raster subtracted per pixel for a spatially-varying datum
                  separation (source_datum --offset-surface); names a raster in the datum
                  store, e.g. "navd88_chart" (config.DATUM_BUILDERS maps it to its composer)
  clamp_positive  drop cells above the water surface (source_datum --clamp-positive)
  valid_range     [min, max] in the source's FINAL frame — values outside it are voided
                  (source_datum --valid-range); the only defence against an upstream that
                  ships impossible depths as ordinary data rather than as its nodata
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
  asc-tile[:<glob>] a zip OR 7z of ESRI ASCII .asc grids → mosaic THIS asset's members to
                    one tif, one per asset (uk_multibeam's per-OS-tile zips; Litto3D's
                    per-prépaquet 7z). The container is sniffed, so one mode serves both.
                    <glob> selects among the members — omit it to take every .asc, give it
                    when an asset carries more than one grid of the SAME area at different
                    resolutions (Litto3D ships MNT1m and MNT5m side by side, and mosaicking
                    the two together would interleave 1 m and 5 m pixels)
  e00               gunzip → ARC/INFO .e00 export → convert to <id>.tif (Lake Tahoe;
                    the export is gzip-wrapped and the unpacker handles the wrapper)
  gldb              the GLDB v2 tar.gz → one <id>_<lake>.tif per documented-bathymetry
                    lake, cut from the global binary depth grid (convert_gldb)
  netcdf            gdal_translate to a GeoTIFF, per-file CRS preserved (NOAA estuaries)
  (absent)          a bare raster: hardlink to <url-filename>_<item-hash>.<ext> (staged_name)

Content sniffing (`_kind`) survives ONLY as validation: an asset whose leading bytes
contradict its declaration (a truncated download, an upstream 200-with-error-page) is
a corrupt raw — deleted with a refetch message, the same self-heal as before. A bare
raster is validated by header-opening it (`_check_raster`).

Staged basenames are tracked across every raw: two members (from any archives or nested
paths) sharing a basename is a hard error, never a silent overwrite. The in-place
datum/normalize steps os.replace onto fresh inodes, so they can never write through into
raw/. Every derived intermediate (tifs, .nc, gzip spools, VRT/7z scratch, asc/ tiles) is
removed at entry — all are re-derivable from raw/ + this module — and orphan raws (a hash no
longer enumerated, or a stale legacy index name) are deleted rather than wedging the source.

Staging is serial (basename collisions and corrupt-raw self-heal are order-dependent, and it is
a hardlink or an archive read per asset); everything after it — datum, CRS flatten, normalize —
runs one worker per staged file.

Run from pipelines/:  uv run python source_prep.py <source-id> [workers]
"""

import fnmatch
import functools
import gzip
import lzma
import os
import re
import shutil
import sys
import tarfile
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from glob import glob

import config
import utils
from convert_e00 import e00_to_tif
from convert_gldb import gldb_to_tifs
from source_datum import (assign_declared_crs, coverage_report, dome_report,
                          flatten_compound_crs, surface_path, transform_file, write_sidecar)
from source_normalize import normalize_file

# Only trust a URL's trailing extension when it names a real data/archive format;
# otherwise (e.g. a weblink ending in ...html?...) stage as .tif — GDAL reads by
# content, not by name.
DATA_EXTS = {"tif", "tiff", "zip", "nc", "asc", "xyz", "img", "gz", "7z", "grd"}


def _url_name(url):
    """The URL's filename: its last path segment, query + fragment stripped."""
    return url.split("?")[0].split("#")[0].rsplit("/", 1)[-1]


def ext_for(url):
    last = _url_name(url)
    ext = last.rsplit(".", 1)[-1].lower() if "." in last else ""
    if ext == "tiff":  # canonicalize to .tif — the staged extension the rest of the lane globs
        return "tif"
    return ext if ext in DATA_EXTS else "tif"


# Cap on the legible half of a staged name: identity rides in the hash, so truncating a
# pathological upstream filename can't collide, and the name stays inside the 255-byte limit.
STEM_MAX = 100


def staged_name(source, url, ext=None):
    """The staged basename for a bare raster: the URL's own filename beside the item hash that
    names its raw/ download (config.item_hash), so the staged file and raw/<hash> read as a pair.

    Derived from the item URL alone, never from its position in the enumeration: a positional
    name re-keys every later file when upstream inserts or drops one, which re-registers the
    whole source and re-aggregates its tiles. The hash also keeps two items that share a
    filename apart — LINZ tiles the same national grid cell in adjacent surveys (23 such pairs
    in nz_coastal), and NCEI names a tile only within its region directory. A URL that names no
    data file (a weblink gateway, e.g. ddm's) has no filename to carry, so it takes the source id.

    ``ext`` overrides the URL's extension for a mode that CONVERTS rather than extracts:
    asc-tile mosaics an archive into a GeoTIFF, and inheriting the archive's own ".7z" would
    leave a raster the rest of the lane — which globs ``*.tif`` — never sees.
    """
    name, _, url_ext = _url_name(url).rpartition(".")
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip(".") if url_ext.lower() in DATA_EXTS else ""
    return f"{(stem or source)[:STEM_MAX]}_{config.item_hash(url)}.{ext or ext_for(url)}"


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


# The magic byte kinds each declared format's raw asset may present — sniffing kept only to
# validate the declaration (e00/tar.gz arrive gzip-wrapped, asc-mosaic as a zip). asc-tile is
# the one mode that accepts either container: what it declares is the PAYLOAD (ESRI ASCII
# grids to mosaic per asset), and whether an upstream ships them zipped or 7z-ed is packaging.
_EXPECT_KIND = {
    "zip": frozenset({"zip"}),
    "asc-mosaic": frozenset({"zip"}),
    "asc-tile": frozenset({"zip", "7z"}),
    "tar.gz": frozenset({"gzip"}),
    "e00": frozenset({"gzip"}),
    "gldb": frozenset({"gzip"}),
    "7z": frozenset({"7z"}),
    "netcdf": frozenset({"netcdf"}),
}


# Archive formats select members by glob; the rest transform the whole asset. asc-tile sits
# between: every .asc by default, narrowed by a glob when an asset carries more than one grid.
_GLOB_FORMATS = {"zip", "tar.gz", "7z"}
_OPTIONAL_GLOB_FORMATS = {"asc-tile"}


def _parse_unpack(spec):
    """Parse a metadata `unpack` string `format[:glob][!N]` → (format, glob, expect).
    `expect` is the exact per-archive match count asserted by `!N` (else None). Archive
    formats require a member glob, asc-tile accepts one, and the rest (e00/netcdf/asc-mosaic/
    gldb) reject one."""
    fmt, _, rest = spec.partition(":")
    members_glob, expect = (rest or None), None
    if members_glob and "!" in members_glob:
        members_glob, _, n = members_glob.partition("!")
        members_glob = members_glob or None
        expect = int(n)
    if fmt not in _EXPECT_KIND:
        sys.exit(f"unknown unpack format {fmt!r} (expected one of {sorted(_EXPECT_KIND)})")
    if fmt in _GLOB_FORMATS and not members_glob:
        sys.exit(f"unpack {spec!r}: {fmt} needs a member glob, e.g. {fmt!r}:*.tif")
    if fmt not in _GLOB_FORMATS | _OPTIONAL_GLOB_FORMATS and (members_glob or expect is not None):
        sys.exit(f"unpack {spec!r}: {fmt} takes no member glob or !N")
    return fmt, members_glob, expect


def _claim(seen, name, origin):
    """Register a staged basename; hard-error on a collision (two archive members, nested
    paths, or duplicate item URLs sharing a basename would silently overwrite each other)."""
    if name in seen:
        sys.exit(f"{origin}: staged filename collision on {name!r} — two inputs would "
                 "overwrite each other; they need distinct basenames")
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


def _dedupe_entries(entries, origin):
    """Collapse literal duplicate ENTRIES — the same full member path listed more than once in
    one archive's directory — to a single name, first-seen order preserved.

    Legal but sloppy, and real: the Environment Agency's 5 km packages list 3 of their 9
    members twice over (TG5500/2014 ships `tg5400/1/2_20140821mb.asc` duplicated). That is one
    path naming one file, not two inputs racing for a name, so it must not read as a collision.
    Byte-identity is checked through the directory's CRCs where the container records them; two
    entries claiming one path with DIFFERENT bytes stay fatal, because nothing here can know
    which copy the upstream meant."""
    first, order = {}, []
    for name, crc in entries:
        if name not in first:
            first[name] = crc
            order.append(name)
        elif first[name] is not None and crc is not None and crc != first[name]:
            sys.exit(f"{origin}: archive lists {name!r} more than once with different bytes "
                     f"(CRC {first[name]:#010x} vs {crc:#010x}) — which copy is right is not "
                     "knowable here; the upstream archive needs fixing")
    return order


def _asc_members(raw, kind, members_glob, expect, dest_dir, origin):
    """Write this archive's selected ESRI ASCII members flat into ``dest_dir``, returning the
    count. Container-agnostic: the caller has already sniffed zip vs 7z, and only the read
    mechanics differ (py7zr has no random-access member read, so a 7z extracts to scratch and
    the members are moved out).

    Members are matched case-insensitively against the FULL member path, so a glob can select
    on the directory an upstream sorts by — Litto3D's resolutions are `MNT1m/` vs `MNT5m/`
    directories as much as they are `_MNT1M_` vs `_MNT5M_` filenames.

    Three ways two members can share a name, and they are not the same thing: the SAME path
    twice is a duplicate entry and collapses (`_dedupe_entries`); two DIFFERENT paths sharing a
    basename would overwrite each other in this asset's scratch and stay fatal; and the same
    basename in ANOTHER asset's archive is fine, because each asset mosaics into its own
    directory — a national grid legitimately repeats tile ids across neighbouring packages."""
    picks_glob = members_glob or "*.asc"
    os.makedirs(dest_dir, exist_ok=True)
    claimed = set()

    def take(name, data):
        member = os.path.basename(name)
        _claim(claimed, member, origin)
        with open(f"{dest_dir}/{member}", "wb") as f:
            f.write(data)

    if kind == "zip":
        with zipfile.ZipFile(raw) as z:
            names = _dedupe_entries(
                [(i.filename, i.CRC) for i in z.infolist() if not i.is_dir()], origin)
            picks = _members(names, picks_glob, expect, origin)
            for name in picks:
                take(name, z.read(name))
    else:
        import py7zr
        with py7zr.SevenZipFile(raw) as z:
            names = _dedupe_entries(
                [(i.filename, getattr(i, "crc32", None)) for i in z.list()
                 if not i.is_directory], origin)
            picks = _members(names, picks_glob, expect, origin)
            tmp = f"{dest_dir}/_7z_extract"
            shutil.rmtree(tmp, ignore_errors=True)
            z.extract(path=tmp, targets=picks)
        for name in picks:
            with open(f"{tmp}/{name}", "rb") as f:
                take(name, f.read())
        shutil.rmtree(tmp, ignore_errors=True)
    return len(picks)


def _stage_asc_tile(raw, root, source, url, kind, members_glob, expect, seen, origin):
    """A zip or 7z of ESRI ASCII grids → mosaic THIS asset's members into one GeoTIFF. A tiled
    national product can't take the per-source asc-mosaic: its members span the whole country,
    so one mosaic of them all would be a single country-sized raster with the gaps between
    tiles filled in."""
    base = staged_name(source, url, ext="tif")
    _claim(seen, base, f"{origin} {url}")
    # Per-asset scratch, not the shared asc/ dir: asc-tile mosaics each asset on its own, and
    # a shared dir would let one asset's leftovers join the next asset's mosaic.
    asc_dir = f"{root}/_asc_{config.item_hash(url)}"
    shutil.rmtree(asc_dir, ignore_errors=True)
    try:
        n = _asc_members(raw, kind, members_glob, expect, asc_dir, origin)
        _mosaic_asc(root, asc_dir, f"{root}/{base}", origin)
    finally:
        shutil.rmtree(asc_dir, ignore_errors=True)
    return f"asc-tile ({kind}), {n} tile(s) -> {base}"


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
        # validate the inner bytes too — a gzip of not-an-e00 must self-heal, not crash the parser
        with open(inner, "rb") as f:
            if _kind(f.read(512)) != "e00":
                raise CorruptRaw("gzip inner content is not an ARC/INFO e00 export")
        _claim(seen, f"{source}.tif", origin)
        try:
            e00_to_tif(inner, f"{root}/{source}.tif")
        except (AssertionError, ValueError, IndexError) as e:
            raise CorruptRaw(f"e00 parse failed: {e}") from e
        return "e00 → tif"
    finally:
        if os.path.exists(inner):
            os.remove(inner)


def _stage_gldb(raw, root, source, seen, origin):
    """The GLDB v2 archive → one <id>_<lake>.tif per documented-bathymetry lake; the lake set
    comes out of the archive, so each name is claimed as the converter reaches it. The converter
    reads the tar.gz itself and fails closed on any change to its members, dimensions or counts;
    a digest mismatch (truncated download or upstream republish) arrives as a ValueError and
    self-heals as a corrupt raw."""
    try:
        paths = gldb_to_tifs(raw, root, source,
                             claim=lambda base: _claim(seen, base, origin))
    except (tarfile.TarError, ValueError) as e:
        raise CorruptRaw(f"gldb: {e}") from e
    return f"gldb → {len(paths)} lake tif(s)"


def _stage_netcdf(raw, root, url, seen, origin):
    """Translate a netCDF to a GeoTIFF, preserving the file's embedded CRS (no -a_srs) —
    a mixed-CRS source keeps each file's zone. Named after the URL stem so the registration
    stays legible. A file with no embedded CRS is assigned EPSG:4326 (else the catalog scan fails)."""
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


# No real depth is 99 km. A grid declaring NODATA_value -9999 can still carry earlier vintages'
# void markers as ordinary pixels (EA multibeam holds -99999, -999999 and -1e7 among its 0.5 m
# soundings), and at that resolution a single one wins every merge it touches. Everything above
# this floor is left alone — the declared value stays authoritative, and the positive outliers
# the same sweep found are not provably markers.
SENTINEL_FLOOR = -99999.0


def _float32_inputs(ascs, label):
    """The mosaic's inputs, with any non-Float32 grid promoted through a Float32 VRT.

    gdalbuildvrt REFUSES to mix band types: it keeps the first file's type, SKIPS every input
    that disagrees, and still exits 0 with only a warning. AAIGrid infers Int32 for a grid whose
    every value is a bare integer — which is what an all-void EA member looks like — so one of
    those sorting first pinned the VRT to Int32 and silently dropped the real Float32 soundings
    beside it (3 of 5 members, 355,796 valid pixels, on TQ9015 alone). Promoting is a header
    rewrite, no pixels copied."""
    import rasterio
    out, promoted = [], 0
    for asc in ascs:
        with rasterio.open(asc) as src:
            if src.dtypes[0] == "float32":
                out.append(asc)
                continue
        vrt = asc + ".f32.vrt"
        utils.run_command(f"gdal_translate -q -of VRT -ot Float32 {asc} {vrt}", silent=True)
        out.append(vrt)
        promoted += 1
    if promoted:
        print(f"{label}: promoted {promoted}/{len(ascs)} asc tile(s) to Float32 "
              "(gdalbuildvrt would have skipped them)")
    return out


def _mask_sentinels(tif, label):
    """Void every pixel at or below SENTINEL_FLOOR, in place. Returns how many were masked."""
    import rasterio
    masked = 0
    with rasterio.open(tif, "r+") as ds:
        nodata = ds.nodata if ds.nodata is not None else -9999.0
        for _, window in ds.block_windows(1):
            block = ds.read(1, window=window)
            hit = (block <= SENTINEL_FLOOR) & (block != nodata)
            if hit.any():
                block[hit] = nodata
                ds.write(block, 1, window=window)
                masked += int(hit.sum())
    if masked:
        print(f"{label}: voided {masked} pixel(s) at or below {SENTINEL_FLOOR:g} "
              "(stray void markers, not soundings)")
    return masked


def _mosaic_asc(root, asc_dir, tif, label):
    """Mosaic the staged ESRI ASCII tiles into one GeoTIFF via a VRT (no -a_srs;
    source_normalize assigns the CRS from metadata), then drop the tiles."""
    import rasterio
    ascs = sorted(glob(f"{asc_dir}/*.asc"))
    inputs = _float32_inputs(ascs, label)
    listfile = f"{root}/tiles.txt"
    with open(listfile, "w") as f:
        f.write("\n".join(inputs) + "\n")
    vrt = f"{os.path.splitext(tif)[0]}.vrt"
    print(f"{label}: mosaicking {len(ascs)} asc tile(s) -> {tif}")
    # No -srcnodata: gdalbuildvrt already reads each member's OWN NODATA_value and masks it,
    # while -srcnodata forces one value across every member — a member declaring -32768 then
    # enters with its voids as ordinary -32768 readings. Measured both ways on members that
    # disagree: the default masks all voids, -srcnodata leaks them in.
    utils.run_command(f"gdalbuildvrt -overwrite -input_file_list {listfile} {vrt}", silent=False)
    # Assign -9999 ONLY when the grids declare no NODATA_value of their own. -a_nodata relabels
    # without touching pixels, so forcing it over a grid that says -99999 (Litto3D) leaves every
    # void as a -99999 m reading that the datum step then "corrects" into a plausible depth.
    with rasterio.open(vrt) as src:
        declared = src.nodata
    nodata_arg = "" if declared is not None else "-a_nodata -9999 "
    # -ot Float32: depth is a float measurement, and an all-integer grid must not stage as one.
    utils.run_command(
        f"gdal_translate -q -of GTiff -ot Float32 {nodata_arg}-co TILED=YES "
        f"-co COMPRESS=DEFLATE -co NUM_THREADS=ALL_CPUS {vrt} {tif}", silent=False)
    _mask_sentinels(tif, label)
    os.remove(vrt)
    os.remove(listfile)
    shutil.rmtree(asc_dir, ignore_errors=True)


def _raw_hashes(source, root, items):
    """Validate raw/ against items.txt and return [(hash, url), …] in enumeration order.
    Orphans — a hash no longer listed, or a stale legacy raw/<index> whose item was dropped
    — are deleted (re-fetchable derived state; leaving them wedges the source). A name that
    is neither a listed hash nor a legacy index is an anomaly (a crashed tool's leftovers)
    and errors out; a missing hash names exactly what to fetch."""
    wanted = {config.item_hash(u): u for u in items}
    present = set()
    for name in (p.rsplit("/", 1)[-1] for p in glob(f"{root}/raw/*")):
        if name in wanted:
            present.add(name)
        elif re.fullmatch(r"[0-9a-f]{16}", name) or name.isdigit():
            print(f"{source}: deleting orphan raw/{name} (not in the current enumeration)")
            os.remove(f"{root}/raw/{name}")
        else:
            sys.exit(f"{source}: unexpected file raw/{name} — raw/ holds only enumerated "
                     "item downloads; remove it")
    missing = [u for h, u in wanted.items() if h not in present]
    if missing:
        sys.exit(f"{source}: raw asset(s) missing for {len(missing)} enumerated item(s) "
                 f"(e.g. {missing[0]}) — fetch them before prep")
    return [(config.item_hash(u), u) for u in items]


def _clear_stale(root):
    """Remove every derived/intermediate artifact a prior prep may have left: staged tifs,
    netCDF translations, gzip spool files, VRT/mosaic scratch, and the asc tile dirs (stale
    .asc tiles would otherwise join the next mosaic; a stale .nc breaks the netCDF hardlink)."""
    for stale in (glob(f"{root}/*.tif") + glob(f"{root}/*.tiff") + glob(f"{root}/*.nc")
                  + glob(f"{root}/_gz_*") + glob(f"{root}/*.vrt") + glob(f"{root}/tiles.txt")):
        os.remove(stale)
    # asc/ is asc-mosaic's shared dir; _asc_<hash>/ is asc-tile's per-asset scratch, left
    # behind only by a crash mid-mosaic.
    for stale_dir in [f"{root}/asc"] + glob(f"{root}/_asc_*"):
        shutil.rmtree(stale_dir, ignore_errors=True)
    shutil.rmtree(f"{root}/_7z_extract", ignore_errors=True)
    for scratch in glob(f"{root}/seascape-shoal-*"):  # a crashed normalize's pyramid levels
        shutil.rmtree(scratch, ignore_errors=True)


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
        base = staged_name(source, url)
        _claim(seen, base, f"{origin} {url}")  # name the URL: a collision here is a duplicate item
        dest = f"{root}/{base}"
        if os.path.exists(dest):
            os.remove(dest)
        os.link(raw, dest)
        _check_raster(dest)  # an upstream error page saved as .tif dies here
        return f"-> {base}"

    fmt, members_glob, expect = unpack
    with open(raw, "rb") as f:
        kind = _kind(f.read(512))
    if kind not in _EXPECT_KIND[fmt]:
        raise CorruptRaw(f"declared unpack {fmt!r} expects "
                         f"{'/'.join(sorted(_EXPECT_KIND[fmt]))} bytes, got {kind}")
    if fmt == "zip":
        return _stage_zip(raw, root, seen, origin, members_glob, expect)
    if fmt == "asc-mosaic":
        return _stage_asc(raw, asc_dir, seen, origin)
    if fmt == "asc-tile":
        return _stage_asc_tile(raw, root, source, url, kind, members_glob, expect, seen, origin)
    if fmt == "7z":
        return _stage_7z(raw, root, seen, origin, members_glob, expect)
    if fmt == "tar.gz":
        return _stage_targz(raw, root, index, seen, origin, members_glob, expect)
    if fmt == "e00":
        return _stage_e00(raw, root, source, index, seen, origin)
    if fmt == "gldb":
        return _stage_gldb(raw, root, source, seen, origin)
    return _stage_netcdf(raw, root, url, seen, origin)  # fmt == "netcdf"


def _staged_now(root):
    """The staged rasters currently in the source dir, as basenames."""
    return {os.path.basename(p) for p in glob(f"{root}/*.tif") + glob(f"{root}/*.tiff")}


def stage(source):
    """Materialize every raw, returning {staged basename: the raw path it came from}.

    The map is built by diffing the directory around each unpack rather than by threading a
    return value through every stager: an ARCHIVE member's staged name is invented by the
    upstream, derivable from neither the item URL nor an inode (only a bare raster is a hardlink
    to its raw). Without it a corrupt extracted member could be quarantined while its archive
    survived, re-extracting the same bad bytes every run under a message promising a refetch."""
    root = f"store/source/{source}"
    spec = config.load_metadata(source).get("unpack")
    unpack = _parse_unpack(spec) if spec else None
    hashes = _raw_hashes(source, root, config.items(source))
    _clear_stale(root)
    asc_dir = f"{root}/asc"
    seen = set()  # staged basenames — collisions across raws/archives hard-error
    corrupt = []
    produced = {}
    # `pos` (enumeration position) names archive scratch + the error origin only; staged
    # basenames derive from the item URL, and the raw itself lives at raw/<hash>.
    for pos, (h, url) in enumerate(hashes):
        raw = f"{root}/raw/{h}"
        origin = f"{source}[{pos}]"
        before = _staged_now(root)
        try:
            note = _unpack_one(unpack, raw, root, source, pos, url, asc_dir, seen, origin)
        except _CORRUPT as e:
            print(f"{origin}: corrupt raw ({e}) — deleted, a rerun refetches it")
            os.remove(raw)
            corrupt.append(h)
            continue
        for name in _staged_now(root) - before:
            produced[name] = raw
        print(f"{origin}: {note}")
    if corrupt:
        sys.exit(f"{source}: deleted {len(corrupt)} corrupt raw asset(s) {corrupt} — "
                 "rerun to refetch them")
    if os.path.isdir(asc_dir):  # asc-mosaic only — asc-tile mosaics per asset and cleans up
        # Every raw fed this one mosaic, so no single raw owns it: quarantining it would have
        # to delete the whole source's downloads, which is a decision for an operator.
        _mosaic_asc(root, asc_dir, f"{root}/{source}.tif", source)
        produced.pop(f"{source}.tif", None)
    return produced


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


class CorruptStaged(Exception):
    """A staged raster whose header parses but whose PIXELS are unreadable. Carries the paths
    already quarantined, so a pass over many files can report them as one list."""

    def __init__(self, message, removed=()):
        super().__init__(message)
        self.removed = list(removed)


def _raw_index(root, source, staged_map=None):
    """(by-inode, by-name) lookups from a staged file back to the raw it came from.

    By inode is exact and needs no naming convention: a bare raster IS its raw, one hardlink to
    one set of bytes. By name covers the modes that CONVERT, whose staged file is a fresh inode —
    both names staging can give it, since asc-tile forces the .tif extension. ``staged_map``
    (what stage() actually produced) covers the rest: an archive member's staged name comes from
    the upstream and is derivable from neither, so it has to be observed, not computed."""
    by_inode, by_name = {}, {}
    for url in config.items(source):
        raw = f"{root}/raw/{config.item_hash(url)}"
        if not os.path.exists(raw):
            continue
        st = os.stat(raw)
        by_inode[(st.st_dev, st.st_ino)] = raw
        for name in (staged_name(source, url), staged_name(source, url, ext="tif")):
            by_name[name] = raw
    by_name.update(staged_map or {})  # observed beats derived
    return by_inode, by_name


def _quarantine(tif, raw_index):
    """Delete an unreadable staged raster together with the raw behind it, returning what went.

    Staging validates a bare raster by HEADER-opening it, so a download truncated inside its
    pixel data passes that gate and fails later, in the transform. The raw is the bad bytes, and
    `_clear_stale` re-links the staged file from it every run — so removing only the staged copy
    reproduces the same failure forever. Removing the raw makes fetch_item reschedule, and the
    source heals on the next run."""
    by_inode, by_name = raw_index
    raw = None
    try:
        st = os.stat(tif)
        raw = by_inode.get((st.st_dev, st.st_ino))
    except OSError:
        pass
    raw = raw or by_name.get(os.path.basename(tif))
    removed = []
    for path in (tif, raw):
        if path and os.path.exists(path):
            os.remove(path)
            removed.append(path)
    return removed


def prep_file(tif, transform, negate, offset, clamp, surface, crs, nodata, valid_range,
              raw_index):
    """One staged raster end to end: datum transform → compound-CRS flatten → normalize to a COG.
    The unit the pool fans out over, so it touches no file but its own — every step writes a
    sibling temp and os.replaces it, and rasterio's GDAL config Env is thread-local.
    Returns (basename, reference-corrected px, valid px, whether a vertical CRS was dropped,
    interpolation-dome candidates)."""
    import rasterio.errors
    corrected = valid = 0
    domes = None  # only the value transform streams the pixels; without it nothing is scored
    try:
        if transform:
            # Before the transform, not after: an offset surface is resampled onto this file's
            # own grid, which normalize would not have declared yet.
            assign_declared_crs(tif, crs)
            corrected, valid, domes = transform_file(tif, negate, offset, clamp, surface,
                                                     valid_range)
        # After the transform, which already reduces the CRS on the files it rewrites; this
        # catches the rest, so no staged raster reaches a warp carrying a vertical CRS.
        flattened = flatten_compound_crs(tif)
        normalize_file(tif, crs, nodata)
    except rasterio.errors.RasterioIOError as e:
        # ONLY this: bad bytes, never a bad recipe. A value error, a missing nodata or an
        # unbuildable COG are all reasons to stop and fix the source, not to delete a download.
        removed = _quarantine(tif, raw_index)
        raise CorruptStaged(f"{os.path.basename(tif)}: unreadable pixels ({e})", removed) from e
    return os.path.basename(tif), corrected, valid, flattened, domes


# The per-file pipeline is embarrassingly parallel, and THREADS carry it: GDAL's read/write and
# numpy's ufuncs both drop the GIL, and the COG write is a subprocess. Measured on 16 real 1/9"
# CUDEM tiles (3.3 GB), 4 workers: 70s threaded vs 84s with a process pool, at 2.5 GB peak RSS
# vs 3.2 GB — one shared GDAL block cache instead of one per worker, and no start-method
# portability surface (fork vs spawn vs forkserver) around the worker bootstrap.
#
# Each worker still runs GDAL with its own utils.GDAL_WORKER_THREADS compressor pool, so a prep
# job's real thread demand is workers x GDAL_WORKER_THREADS: half the cores keeps that product at
# the 2x oversubscription the box already declares (--cores 16 on 8 vCPU), and lands on the same
# 4 the Snakemake rule passes as {threads}. Past that it flattens — 8 workers bought 5%.
DEFAULT_WORKERS = max(1, (os.cpu_count() or 4) // 2)

PROGRESS_EVERY = 25  # a listed source is ~1,000 files and hours long; log that it is moving


def _fan_out(source, tifs, workers, job):
    """Run `job(tif)` over every staged tif, `workers` at a time, and return the results in
    `tifs` order — the aggregates (coverage report, sidecar) must not depend on who finished
    first.

    Two failure kinds, deliberately unalike. Any ordinary failure stops the source at the FIRST
    one, by name: it means the recipe is wrong, every remaining file would fail the same way,
    and a partially prepped source that registered anyway would ship a datum-uncorrected tile as
    if it were corrected. A CorruptStaged does NOT stop the pass — its bytes are already
    quarantined, and a volume can hold several truncated downloads (cudem had at least two), so
    stopping at the first would cost one dispatch per bad file. Collect them all, then fail once
    with the whole list."""
    results, corrupt, done_n = {}, [], 0

    def record(tif, call):
        """Run one file, keeping a quarantined one out of the results without stopping."""
        try:
            results[tif] = call()
        except CorruptStaged as e:
            corrupt.append(e)

    if workers == 1:
        for tif in tifs:
            record(tif, lambda t=tif: job(t))
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(job, tif): tif for tif in tifs}
            for future in as_completed(futures):
                tif = futures[future]
                try:
                    record(tif, future.result)
                except BaseException as e:
                    for pending in futures:
                        pending.cancel()
                    raise RuntimeError(f"{source}: {os.path.basename(tif)}: {e}") from e
                done_n += 1
                if len(tifs) >= PROGRESS_EVERY * 2 and done_n % PROGRESS_EVERY == 0:
                    print(f"{source}: prepped {done_n}/{len(tifs)} file(s)", flush=True)
    if corrupt:
        removed = [path for e in corrupt for path in e.removed]
        raise CorruptStaged(
            f"quarantined {len(corrupt)} unreadable file(s) — "
            + "; ".join(str(e) for e in corrupt)
            + f". Removed {len(removed)} path(s): {', '.join(removed)} — "
            "the next run refetches them", removed)
    return [results[tif] for tif in tifs]


def prep(source, workers=DEFAULT_WORKERS):
    meta = config.load_metadata(source)
    staged_map = stage(source)
    tifs = sorted(glob(f"store/source/{source}/*.tif"))  # staging canonicalizes .tiff -> .tif (ext_for)

    negate = bool(meta.get("negate", False))
    offset = float(meta.get("datum_offset_m", 0.0))
    clamp = bool(meta.get("clamp_positive", False))
    name = meta.get("offset_surface")
    band = meta.get("valid_range")
    if band is not None and (len(band) != 2 or band[0] >= band[1]):
        sys.exit(f"{source}: valid_range must be [min, max] with min < max, got {band}")
    write_sidecar(source, negate, offset, clamp, name,  # even for a no-op: the catalog's invariant
                  valid_range=band)
    surface = surface_path(name) if name else None
    if surface and not os.path.isfile(surface):
        sys.exit(f"{source}: offset surface {surface} is not in the store — "
                 f"build it with {config.datum_builder(name)}")
    transform = bool(negate or offset or clamp or surface or band)
    if transform:
        print(f"{source}: datum negate={negate} offset={offset} surface={name} "
              f"clamp_positive={clamp} valid_range={band}")
    crs, nodata = meta.get("crs"), meta.get("nodata")
    print(f"{source}: prep {len(tifs)} file(s) on {workers} worker(s) "
          f"(crs={crs} nodata={nodata})", flush=True)

    job = functools.partial(prep_file, transform=transform, negate=negate, offset=offset,
                            clamp=clamp, surface=surface, crs=crs, nodata=nodata,
                            valid_range=band,
                            raw_index=_raw_index(f"store/source/{source}", source, staged_map))
    try:
        per_file = _fan_out(source, tifs, workers, job)
    # A quarantined raw is an operational fact, not a stack trace: the source is red until the
    # next run refetches, and the message is the whole diagnosis. Both arms are needed — the
    # serial path raises it bare, the pool path re-raises it wrapped.
    except CorruptStaged as e:
        sys.exit(f"{source}: {e}")
    except RuntimeError as e:
        if isinstance(e.__cause__, CorruptStaged):
            sys.exit(str(e))
        raise

    if transform:
        # Rewritten with what the pass measured: how much of the source actually moved, and its
        # dome-candidate count, are only known once every file is transformed.
        write_sidecar(source, negate, offset, clamp, name,
                      coverage_report(source, [r[:3] for r in per_file]) if surface else None,
                      dome_report(source, [(r[0], r[4]) for r in per_file]),
                      valid_range=band)
    flattened = sum(r[3] for r in per_file)
    if flattened:
        print(f"{source}: dropped the vertical CRS from {flattened}/{len(tifs)} file(s)")


def main():
    if len(sys.argv) not in (2, 3):
        sys.exit("usage: source_prep.py <source-id> [workers]")
    prep(sys.argv[1], int(sys.argv[2]) if len(sys.argv) == 3 else DEFAULT_WORKERS)


def _check():
    """Synthetic sources end to end, driven by declared `unpack`. Raws live at raw/<hash>
    (config.item_hash of the item URL); items.txt is the enumeration. Common path: a declared
    zip extracts its glob members, an undeclared raw hardlinks under its URL-derived
    staged_name — which an upstream insertion or removal leaves untouched, while the dropped
    item's staged file goes and a duplicated item URL hard-errors by URL —
    stale root tifs are cleared, the metadata knobs drive datum + normalize. Failure
    modes: a missing raw names what to fetch, an unexpected non-hash file in raw/ is a distinct
    error, an orphan (a hash no longer listed, or a stale legacy index) is deleted, and a
    staged-basename collision hard-errors. Format registry: a gzipped tar with `!1` stages
    exactly its one *_lld.tif (0 → error), a 7z glob filters its members, a gzipped .e00 stages
    to <id>.tif (pure-Python), and — when the GDAL CLI is present — an asc-mosaic zip mosaics to
    <id>.tif. Corrupt raws self-heal: a truncated declared zip, a declared zip whose bytes are
    not a zip, and an undeclared raw that is a server error page are all deleted with a refetch.
    Fan-out: results come back in input order however the workers finish, a multi-file source
    preps identically on 1 and 4 workers (pixels and the coverage fraction alike), and one
    unpreppable file fails the whole source by name on either."""
    import io
    import json
    import tempfile
    import warnings

    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    # ext_for canonicalizes both GeoTIFF spellings to the staged .tif the lane globs, and
    # falls back to .tif for a non-data extension (GDAL reads by content, not name).
    assert ext_for("https://x/a.tiff") == "tif" and ext_for("https://x/a.tif") == "tif"
    assert ext_for("https://x/a.TIFF?k=v") == "tif" and ext_for("https://x/page.html?z") == "tif"
    assert ext_for("https://x/a.zip") == "zip" and ext_for("https://x/a.nc") == "nc"

    # staged_name: the URL's filename + the item hash naming its raw/ download, canonical
    # extension, path-unsafe characters folded, and the source id where the URL names no data
    # file. Two items sharing a filename (the real nz_coastal shape) get distinct names.
    H = config.item_hash
    u_nz = ["https://b/auckland/mangawhai_2025/dem_1m/2193/AY31_10000_0103.tiff",
            "https://b/northland/whangarei_2025/dem_1m/2193/AY31_10000_0103.tiff"]
    assert staged_name("s", u_nz[0]) == f"AY31_10000_0103_{H(u_nz[0])}.tif"
    assert staged_name("s", u_nz[0]) != staged_name("s", u_nz[1]), "same filename, distinct items"
    u_weblink = "https://f/main.html?weblink=abc"  # ddm's shape: a gateway URL, no filename
    assert staged_name("ddm", u_weblink) == f"ddm_{H(u_weblink)}.tif"
    assert staged_name("s", "https://x/a b%20c.tif").startswith("a_b_20c_"), \
        staged_name("s", "https://x/a b%20c.tif")
    assert staged_name("s", "https://x/../.tif").startswith("s_"), "no traversal out of the store"
    long_url = "https://x/" + "n" * 300 + ".tif"
    assert len(staged_name("s", long_url)) == STEM_MAX + len(H(long_url)) + len("_.tif")

    # _parse_unpack splits format/glob/!N and rejects an unknown format.
    assert _parse_unpack("zip:*.tif") == ("zip", "*.tif", None)
    assert _parse_unpack("tar.gz:*_lld.tif!1") == ("tar.gz", "*_lld.tif", 1)
    assert _parse_unpack("e00") == ("e00", None, None)
    assert _parse_unpack("gldb") == ("gldb", None, None)
    try:
        _parse_unpack("rar:*.tif")
        assert False, "expected an unknown unpack format to exit"
    except SystemExit as e:
        assert "unknown unpack format" in str(e), e
    # archive formats require a glob ("zip"/"zip:" would crash in _members later);
    # glob-less formats reject one.
    for bad, msg in (("zip", "needs a member glob"), ("zip:", "needs a member glob"),
                     ("e00:*.tif", "takes no member glob"), ("netcdf:!1", "takes no member glob"),
                     ("gldb:*.tif", "takes no member glob")):
        try:
            _parse_unpack(bad)
            assert False, f"expected {bad!r} to exit"
        except SystemExit as e:
            assert msg in str(e), (bad, e)

    def seed(sid, urls, meta):
        """A synthetic source: metadata.json + items.txt (the enumeration) + an empty raw/."""
        os.makedirs(f"sources/{sid}", exist_ok=True)
        with open(f"sources/{sid}/metadata.json", "w") as f:
            json.dump(meta, f)
        os.makedirs(f"store/source/{sid}/raw", exist_ok=True)
        with open(f"store/source/{sid}/items.txt", "w") as f:
            f.write("".join(u + "\n" for u in urls))

    def raw_of(sid, url):
        return f"store/source/{sid}/raw/{H(url)}"

    d = tempfile.mkdtemp()
    cwd, saved = os.getcwd(), config.SOURCES_DIR
    try:
        os.chdir(d)
        config.SOURCES_DIR = "sources"
        sid = "_prep_selfcheck"
        u0, u1 = "https://x/archive.zip", "https://x/plain.zip"
        seed(sid, [u0, u1], {"name": "Synth", "unpack": "zip:*.tif", "negate": True,
                             "datum_offset_m": -1.0, "crs": "EPSG:28992"})

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
        with open(raw_of(sid, u0), "wb") as f:
            f.write(zbuf.getvalue())
        with open(f"store/source/{sid}/stale.tif", "w") as f:
            f.write("old")
        # `unpack` applies to every item, so u1 is a zip too (+10 m depth member).
        zbuf1 = io.BytesIO()
        with zipfile.ZipFile(zbuf1, "w") as z:
            z.writestr("b.tif", tif_bytes(10.0))
        with open(raw_of(sid, u1), "wb") as f:
            f.write(zbuf1.getvalue())

        prep(sid)
        assert not os.path.exists(f"store/source/{sid}/stale.tif"), "stale tif must be cleared"
        assert not os.path.exists(f"store/source/{sid}/readme.txt")
        assert open(raw_of(sid, u1), "rb").read() == zbuf1.getvalue(), \
            "in-place steps must never write through into raw/"
        with open(f"store/source/{sid}/datum.json") as f:
            sidecar = json.load(f)
        # dome_candidates is 0, not None: the staged tifs carry no CRS of their own, but the
        # metadata one is stamped on before the transform, so the detector can size itself in
        # metres and actually runs.
        assert sidecar == {"negate": True, "offset_m": -1.0, "clamp_positive": False,
                           "offset_surface": None, "valid_range": None,
                           "corrected_fraction": None, "dome_candidates": 0}, sidecar
        for name, want in (("a.tif", -6.0), ("b.tif", -11.0)):  # -(v) - 1
            with rasterio.open(f"store/source/{sid}/{name}") as src:
                assert src.crs.to_epsg() == 28992, (name, src.crs)
                assert src.read(1)[0, 0] == want, (name, src.read(1)[0, 0])

        os.remove(raw_of(sid, u1))
        try:
            prep(sid)
            assert False, "expected a missing raw to exit"
        except SystemExit as e:
            assert "missing" in str(e) and "plain.zip" in str(e) and "fetch them" in str(e), e
        with open(raw_of(sid, u1), "wb") as f:  # restore, then add an anomalous file
            f.write(zbuf1.getvalue())
        # An unexpected (non-hash, non-index) file in raw/ is a distinct, named anomaly.
        with open(f"store/source/{sid}/raw/junk!", "w") as f:
            f.write("?")
        try:
            prep(sid)
            assert False, "expected an unexpected raw file to exit"
        except SystemExit as e:
            assert "unexpected file" in str(e) and "junk!" in str(e), e
        os.remove(f"store/source/{sid}/raw/junk!")

        # Orphans are deleted, not a wedge: a hash no longer listed AND a stale legacy index.
        with open(f"store/source/{sid}/items.txt", "w") as f:
            f.write(u0 + "\n")  # enumeration shrank 2 → 1
        with open(raw_of(sid, u1), "wb") as f:
            f.write(zbuf1.getvalue())  # u1's hash is now an orphan
        with open(f"store/source/{sid}/raw/7", "wb") as f:
            f.write(b"legacy index leftover")  # a stale pre-hash index name
        prep(sid)
        assert not os.path.exists(raw_of(sid, u1)), "orphan hash must be deleted"
        assert not os.path.exists(f"store/source/{sid}/raw/7"), "stale legacy index must be deleted"

        # A bare raster (no `unpack`) hardlinks to its URL-derived name, and an upstream
        # insertion or removal leaves the other files' names — and their inodes — alone. A
        # positional name would re-key every file after the insertion, re-registering the source.
        bid = "_prep_bare"
        bus = [f"https://x/tiles/dem_{i}.tif" for i in range(3)]
        seed(bid, bus, {"name": "Bare", "crs": "EPSG:4326"})
        for u in bus:
            with open(raw_of(bid, u), "wb") as f:
                f.write(tif_bytes(7.0))
        stage(bid)
        staged = {u: f"store/source/{bid}/{staged_name(bid, u)}" for u in bus}
        for u, path in staged.items():
            assert os.path.isfile(path), (u, path)
            assert os.path.basename(path).endswith(f"_{H(u)}.tif"), path  # raw/<hash> ↔ staged
            assert os.stat(path).st_ino == os.stat(raw_of(bid, u)).st_ino, "staged must hardlink raw"
        inserted = "https://x/tiles/dem_new.tif"
        with open(raw_of(bid, inserted), "wb") as f:
            f.write(tif_bytes(8.0))
        with open(f"store/source/{bid}/items.txt", "w") as f:  # inserted FIRST: the drift case
            f.write("".join(u + "\n" for u in [inserted] + bus))
        stage(bid)
        for u, path in staged.items():
            assert os.path.isfile(path), f"insertion re-keyed {u}"
        assert os.path.isfile(f"store/source/{bid}/{staged_name(bid, inserted)}")
        # Dropping an item clears its staged file (and its now-orphan raw) while the rest stand.
        with open(f"store/source/{bid}/items.txt", "w") as f:
            f.write("".join(u + "\n" for u in bus))
        stage(bid)
        assert not os.path.exists(f"store/source/{bid}/{staged_name(bid, inserted)}"), \
            "a dropped item's staged file must be cleared"
        assert not os.path.exists(raw_of(bid, inserted)), "orphan raw must be deleted"
        for u, path in staged.items():
            assert os.path.isfile(path), f"removal re-keyed {u}"
        # A duplicated item URL would stage twice onto one name — hard-error, naming the URL.
        with open(f"store/source/{bid}/items.txt", "w") as f:
            f.write("".join(u + "\n" for u in bus + bus[:1]))
        try:
            stage(bid)
            assert False, "expected a duplicated item URL to exit"
        except SystemExit as e:
            assert "collision" in str(e) and bus[0] in str(e), e
        with open(f"store/source/{bid}/items.txt", "w") as f:
            f.write("".join(u + "\n" for u in bus))

        # A source with NO datum knobs (the CUDEM-territory shape) still loses a compound
        # CRS's vertical half, with its values untouched — nothing downstream may see one.
        # A bare raster stages as a HARDLINK to its raw, so this also pins the raw bytes:
        # an in-place header edit here would rewrite the verbatim download.
        # Three files, so the flatten runs in the workers rather than only on the serial path.
        vid = "_prep_vertical"
        vus = [f"https://x/compound{i}.tif" for i in range(3)]
        seed(vid, vus, {"name": "Compound"})
        for vu in vus:
            with rasterio.open(raw_of(vid, vu), "w", driver="GTiff", height=2, width=2, count=1,
                               dtype="float32", nodata=-9999.0,
                               crs=rasterio.crs.CRS.from_epsg(5498),  # NAD83 + NAVD88 height
                               transform=from_origin(0, 2, 1, 1)) as dst:
                dst.write(np.full((2, 2), -4.5, dtype="float32"), 1)
        raw_bytes = open(raw_of(vid, vus[0]), "rb").read()
        prep(vid, 4)
        for vu in vus:
            with rasterio.open(f"store/source/{vid}/{staged_name(vid, vu)}") as src:
                assert src.crs.to_epsg() == 4269, (vu, src.crs)
                assert (src.read(1) == -4.5).all(), (vu, src.read(1))
        assert open(raw_of(vid, vus[0]), "rb").read() == raw_bytes, \
            "flattening a hardlinked bare raster must not write through into raw/"

        # `offset_surface` drives the per-pixel datum correction (the CUDEM shape: bare rasters,
        # no crs/nodata override, a reference from the datum store) and lands in the sidecar.
        sid_s = "_prep_surface"
        su = "https://x/tile.tif"
        seed(sid_s, [su], {"name": "Surface", "offset_surface": "synth"})
        with rasterio.open(raw_of(sid_s, su), "w", driver="GTiff", height=2, width=2, count=1,
                           dtype="float32", nodata=-9999.0, crs="EPSG:4326",
                           transform=from_origin(0, 2, 1, 1)) as dst:
            dst.write(np.full((2, 2), -10.0, dtype="float32"), 1)  # bed 10 m below zero
        os.makedirs("store/datum", exist_ok=True)
        with rasterio.open("store/datum/synth.tif", "w", driver="GTiff", height=2, width=2,
                           count=1, dtype="float32", crs="EPSG:4326", nodata=-9999.0,
                           transform=from_origin(0, 2, 1, 1)) as dst:
            # Chart datum 1 m lower over the western column only: the coverage edge a real
            # boundary tile sits on, so the source's corrected fraction is a ratio, not 1.
            dst.write(np.array([[-1.0, -9999.0]] * 2, dtype="float32"), 1)
        prep(sid_s)
        with open(f"store/source/{sid_s}/datum.json") as f:
            sidecar_s = json.load(f)
        assert sidecar_s["offset_surface"] == "synth", sidecar_s
        assert sidecar_s["corrected_fraction"] == 0.5, sidecar_s
        with rasterio.open(f"store/source/{sid_s}/{staged_name(sid_s, su)}") as src:
            assert src.read(1)[0, 0] == -9.0, src.read(1)   # bed - (-1): shallower
            assert src.read(1)[0, 1] == -10.0, src.read(1)  # no coverage: passed through
        # A declared surface that isn't in the store must name itself, not silently no-op —
        # and point at the builder that composes THAT surface, since naming the wrong one
        # sends you to the composer for another datum entirely.
        os.remove("store/datum/synth.tif")
        try:
            prep(sid_s)
            assert False, "expected a missing offset surface to exit"
        except SystemExit as e:
            assert "offset surface" in str(e) and "none is registered" in str(e), e
        registered = next(iter(config.DATUM_BUILDERS))
        meta_path = f"sources/{sid_s}/metadata.json"
        with open(meta_path) as f:
            meta = json.load(f)
        with open(meta_path, "w") as f:
            json.dump({**meta, "offset_surface": registered}, f)
        try:
            prep(sid_s)
            assert False, "expected a missing offset surface to exit"
        except SystemExit as e:
            assert config.DATUM_BUILDERS[registered] in str(e), e

        # The EXTRACT modes land every member in the one source dir, so two archives sharing a
        # member basename really would overwrite each other: hard-error. (asc-tile scopes this
        # per-archive instead — its members never share a directory. Pinned in its own case.)
        cid = "_prep_collide"
        cu = ["https://x/a.zip", "https://x/b.zip"]
        seed(cid, cu, {"name": "Collide", "unpack": "zip:*.tif"})
        for i, u in enumerate(cu):
            zb = io.BytesIO()
            with zipfile.ZipFile(zb, "w") as z:
                z.writestr(f"pack{i}/dup.tif", tif_bytes(1.0))
            with open(raw_of(cid, u), "wb") as f:
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
        lu = "https://x/huron_lld.geotiff.tar.gz"
        seed(lid, [lu], {"name": "LLD", "unpack": "tar.gz:*_lld.tif!1"})

        def targz_bytes(members):
            tb = io.BytesIO()
            with _tar.open(fileobj=tb, mode="w") as t:
                for name, data in members:
                    info = _tar.TarInfo(name)
                    info.size = len(data)
                    t.addfile(info, io.BytesIO(data))
            return _gz.compress(tb.getvalue())

        with open(raw_of(lid, lu), "wb") as f:
            f.write(targz_bytes([("huron_lld/huron_lld.tif", tif_bytes(2.0)),
                                 ("huron_lld/huron_lld.prj", b"PROJCS[...]"),
                                 ("huron_lld/extra.tif", tif_bytes(3.0))]))  # non-matching: ignored
        stage(lid)
        assert os.path.isfile(f"store/source/{lid}/huron_lld.tif")
        assert not os.path.exists(f"store/source/{lid}/extra.tif"), "tar stages matches only"
        with open(raw_of(lid, lu), "wb") as f:
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
        zu = "https://x/rasters.7z"
        seed(zid, [zu], {"name": "7Z", "unpack": "7z:*_ras.tif"})
        with py7zr.SevenZipFile(raw_of(zid, zu), "w") as z:
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
        eu = "https://x/grid.e00.gz"
        seed(eid, [eu], {"name": "E00", "unpack": "e00", "crs": "EPSG:32610"})
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
        with _gz.open(raw_of(eid, eu), "wb") as f:
            f.write(e00_text.encode())
        stage(eid)
        with rasterio.open(f"store/source/{eid}/{eid}.tif") as src:
            assert src.shape == (2, 2) and src.read(1)[0, 0] == 1.0, src.read(1)
        # a gzip whose inner bytes are NOT an e00 export self-heals as a corrupt raw
        with _gz.open(raw_of(eid, eu), "wb") as f:
            f.write(b"<html>503 not an e00</html>")
        try:
            stage(eid)
            assert False, "expected a non-e00 gzip payload to exit as corrupt"
        except SystemExit as e:
            assert "corrupt raw" in str(e), e
        assert not os.path.exists(raw_of(eid, eu)), "corrupt e00 raw must be deleted"

        # Corrupt raws self-heal: a truncated declared zip (PK magic intact), a declared zip
        # whose bytes are not a zip at all (an error page), and — with no `unpack` — a server
        # error page routed as a raster: all deleted with a refetch; a rerun with good bytes
        # then succeeds. The exact truncated-download / 200-with-garbage cases.
        cid = "_prep_corrupt"
        c0, c1 = "https://x/a.zip", "https://x/b.zip"
        seed(cid, [c0, c1], {"name": "Corrupt", "unpack": "zip:*.tif"})
        good_zip = io.BytesIO()
        with zipfile.ZipFile(good_zip, "w") as z:
            z.writestr("a.tif", tif_bytes(1.0))
        with open(raw_of(cid, c0), "wb") as f:
            f.write(good_zip.getvalue()[: len(good_zip.getvalue()) // 2])  # truncated, PK intact
        with open(raw_of(cid, c1), "wb") as f:
            f.write(b"<html>503 Service Unavailable</html>")  # not zip bytes → declaration contradicted
        try:
            stage(cid)
            assert False, "expected corrupt raws to exit"
        except SystemExit as e:
            assert "deleted 2 corrupt raw asset(s)" in str(e), e
        assert not os.path.exists(raw_of(cid, c0)), "truncated zip raw must be deleted"
        assert not os.path.exists(raw_of(cid, c1)), "non-zip raw must be deleted"
        with open(raw_of(cid, c0), "wb") as f:
            f.write(good_zip.getvalue())
        good_zip1 = io.BytesIO()
        with zipfile.ZipFile(good_zip1, "w") as z:
            z.writestr("b.tif", tif_bytes(2.0))
        with open(raw_of(cid, c1), "wb") as f:
            f.write(good_zip1.getvalue())
        stage(cid)  # refetched good bytes stage cleanly
        assert os.path.isfile(f"store/source/{cid}/a.tif")
        assert os.path.isfile(f"store/source/{cid}/b.tif")

        # An undeclared raw that is a server error page (not a raster) also self-heals.
        gid = "_prep_garbage"
        gu = "https://x/dem.tif"
        seed(gid, [gu], {"name": "Garbage"})
        with open(raw_of(gid, gu), "wb") as f:
            f.write(b"<html>503 Service Unavailable</html>")
        try:
            stage(gid)
            assert False, "expected a garbage bare raster to exit"
        except SystemExit as e:
            assert "deleted 1 corrupt raw asset(s)" in str(e), e
        assert not os.path.exists(raw_of(gid, gu)), "garbage raster raw must be deleted"
        assert not os.path.exists(f"store/source/{gid}/{gid}_0.tif"), "bad staged tif must be removed"

        # _fan_out returns in INPUT order however the workers finish — the coverage report and
        # the sidecar fraction it feeds must not depend on the race. Reversed completion order.
        import time
        order = ["c", "a", "b", "d"]
        assert _fan_out("s", order, 4, lambda x: (time.sleep(0.05 * order.index(x)), x)[1]) == order

        # The parallel path must be indistinguishable from the serial one: same pixels, same
        # ordered aggregates. Three tiles sit exactly on the reference grid and two sit far
        # outside it, so the source's corrected fraction is a ratio a reordering would disturb.
        pid = "_prep_parallel"
        pu = [f"https://x/t{i}.tif" for i in range(5)]
        seed(pid, pu, {"name": "Parallel", "offset_surface": "synth2"})
        with rasterio.open("store/datum/synth2.tif", "w", driver="GTiff", height=4, width=4,
                           count=1, dtype="float32", crs="EPSG:4326", nodata=-9999.0,
                           transform=from_origin(0.0, 4.0, 1.0, 1.0)) as dst:
            dst.write(np.full((4, 4), -1.0, dtype="float32"), 1)  # chart datum 1 m below zero
        origins = [(0.0, 4.0), (2.0, 4.0), (0.0, 2.0), (50.0, 4.0), (60.0, 4.0)]
        depths = [-10.0, -20.0, -30.0, -40.0, -50.0]  # distinct, so a crossed result is visible
        for u, (x, y), v in zip(pu, origins, depths):
            with rasterio.open(raw_of(pid, u), "w", driver="GTiff", height=2, width=2, count=1,
                               dtype="float32", nodata=-9999.0, crs="EPSG:4326",
                               transform=from_origin(x, y, 1.0, 1.0)) as dst:
                dst.write(np.full((2, 2), v, dtype="float32"), 1)

        def prepped(workers):
            prep(pid, workers)
            with open(f"store/source/{pid}/datum.json") as f:
                sidecar = json.load(f)
            out = []
            for u in pu:
                with rasterio.open(f"store/source/{pid}/{staged_name(pid, u)}") as src:
                    out.append(src.read(1).tolist())
            return sidecar, out

        serial, parallel = prepped(1), prepped(4)
        assert serial == parallel, (serial, parallel)
        assert serial[0]["corrected_fraction"] == 0.6, serial[0]  # 12 of 20 px reached
        for i, want in enumerate([-9.0, -19.0, -29.0, -40.0, -50.0]):  # covered rise 1 m
            assert serial[1][i] == [[want] * 2] * 2, (i, serial[1][i])

        # Interpolation-dome candidates aggregate over the source and are a sum, not a race: three
        # tiles carrying 1, 0 and 2 planted domes report 3 on one worker and on four.
        did = "_prep_domes"
        dus = [f"https://x/d{i}.tif" for i in range(3)]
        seed(did, dus, {"name": "Domes", "datum_offset_m": 0.5})
        PX = 3.0  # metres, close to CUDEM 1/9 arc-second
        for u, centres in zip(dus, ([(100, 100)], [], [(60, 60), (60, 160)])):
            field = np.full((220, 220), -16.5, dtype="float32")
            rr, cc = np.ogrid[:220, :220]
            for row, col in centres:  # a 70 m cone peaking at datum once the +0.5 m offset lands
                dist = np.hypot(rr - row, cc - col) * PX
                np.maximum(field, field + (0.0 - field) * np.clip(1 - dist / 70.0, 0, 1),
                           out=field)
            with rasterio.open(raw_of(did, u), "w", driver="GTiff", height=220, width=220,
                               count=1, dtype="float32", nodata=-9999.0,
                               crs=rasterio.crs.CRS.from_epsg(32618),
                               transform=from_origin(500000.0, 4000000.0, PX, PX)) as dst:
                dst.write(field, 1)

        def dome_count(workers):
            prep(did, workers)
            with open(f"store/source/{did}/datum.json") as f:
                return json.load(f)["dome_candidates"]

        assert dome_count(1) == 3, dome_count(1)
        assert dome_count(4) == 3, dome_count(4)

        # One bad file fails the SOURCE, named — never a partial prep that registers anyway.
        fid = "_prep_fail"
        fu = [f"https://x/f{i}.tif" for i in range(3)]
        seed(fid, fu, {"name": "Fail", "clamp_positive": True})
        for i, u in enumerate(fu):
            with rasterio.open(raw_of(fid, u), "w", driver="GTiff", height=2, width=2, count=1,
                               dtype="float32", crs="EPSG:4326",  # the middle tile declares none
                               nodata=None if i == 1 else -9999.0,
                               transform=from_origin(0, 2, 1, 1)) as dst:
                dst.write(np.full((2, 2), -5.0, dtype="float32"), 1)
        for workers in (1, 4):
            try:
                prep(fid, workers)
                assert False, f"expected a file with no nodata to fail prep (workers={workers})"
            except (RuntimeError, ValueError) as e:
                assert staged_name(fid, fu[1]) in str(e), (workers, e)

        # A staged raster whose HEADER parses but whose pixels are truncated is quarantined with
        # its raw, not merely failed: staging validates by header-open, so these bytes get past
        # it every run, and _clear_stale re-links the staged copy from the same bad raw — the
        # source would fail identically forever. Removing the raw makes fetch_item reschedule.
        # ONE pass heals EVERY corrupt file: a volume can hold several truncated downloads
        # (cudem had at least two), and stopping at the first costs one dispatch per bad file.
        qid = "_prep_quarantine"
        bad_urls = [f"https://x/truncated{i}.tif" for i in range(2)]
        ok_urls = [f"https://x/whole{i}.tif" for i in range(2)]
        qus = bad_urls + ok_urls
        seed(qid, qus, {"name": "Quarantine", "negate": True})  # negate: forces a pixel read
        good = io.BytesIO()
        with rasterio.open(good, "w", driver="GTiff", height=256, width=256, count=1,
                           dtype="float32", nodata=-9999.0, crs="EPSG:4326",
                           transform=from_origin(0, 1, 0.001, 0.001),
                           tiled=True, blockxsize=128, blockysize=128, compress="deflate") as dst:
            dst.write(np.full((256, 256), -5.0, dtype="float32"), 1)
        whole = good.getvalue()
        staged_of = {u: f"store/source/{qid}/{staged_name(qid, u)}" for u in qus}
        # Both fan-out paths: serial raises CorruptStaged bare, the pool re-raises it wrapped,
        # and only one of those was reaching the clean exit.
        for workers in (1, 4):
            for u in bad_urls:  # header + directory survive, tile data does not
                with open(raw_of(qid, u), "wb") as f:
                    f.write(whole[:len(whole) // 2])
            for u in ok_urls:
                with open(raw_of(qid, u), "wb") as f:
                    f.write(whole)
            try:
                prep(qid, workers)
                assert False, f"expected truncated staged rasters to exit (workers={workers})"
            except SystemExit as e:
                assert "quarantined 2 unreadable file(s)" in str(e), (workers, e)
                assert "refetches" in str(e), (workers, e)
                for u in bad_urls:  # every bad file named, not just the first to fail
                    assert staged_of[u] in str(e) and raw_of(qid, u) in str(e), (workers, u, e)
            for u in bad_urls:
                assert not os.path.exists(staged_of[u]), (workers, u, "staged must be gone")
                assert not os.path.exists(raw_of(qid, u)), (workers, u, "raw must be gone")
            for u in ok_urls:  # the readable files still prepped, and keep their raws
                assert os.path.exists(staged_of[u]), (workers, u, "good file must survive")
                assert os.path.exists(raw_of(qid, u)), (workers, u, "good raw must survive")
        # An EXTRACTED member's staged name is the upstream's invention — no inode shared with
        # the raw, no URL to derive it from — so the quarantine has to learn it from staging.
        # Without that it deleted the tif and left the archive, which re-extracted the same bad
        # bytes every run while the message promised a refetch.
        zqid = "_prep_quarantine_zip"
        zqu = "https://x/pack.zip"
        seed(zqid, [zqu], {"name": "Quarantine zip", "unpack": "zip:*.tif", "negate": True})
        zb = io.BytesIO()
        with zipfile.ZipFile(zb, "w") as z:
            z.writestr("inner/member.tif", whole[:len(whole) // 2])  # header ok, pixels cut
        with open(raw_of(zqid, zqu), "wb") as f:
            f.write(zb.getvalue())
        staged_member = f"store/source/{zqid}/member.tif"
        try:
            prep(zqid, 1)
            assert False, "expected a truncated extracted member to exit"
        except SystemExit as e:
            assert "quarantined 1 unreadable file(s)" in str(e), e
            assert staged_member in str(e) and raw_of(zqid, zqu) in str(e), e
        assert not os.path.exists(staged_member), "the staged member must go"
        assert not os.path.exists(raw_of(zqid, zqu)), \
            "the ARCHIVE behind it must go too, else it re-extracts forever"

        # …and with the bad raws gone, the next prep asks for a refetch rather than repeating.
        try:
            prep(qid)
            assert False, "expected the missing raws to exit"
        except SystemExit as e:
            assert "missing" in str(e) and "fetch" in str(e), e

        # Format registry: an asc-mosaic zip of ESRI ASCII tiles mosaics to <id>.tif (GDAL CLI).
        if shutil.which("gdalbuildvrt") and shutil.which("gdal_translate"):
            aid = "_prep_asc"
            au = "https://x/tiles.esriasciigrid.zip"
            seed(aid, [au], {"name": "ASC", "unpack": "asc-mosaic", "crs": "EPSG:2056"})
            asc = ("ncols 2\nnrows 2\nxllcorner 0\nyllcorner 0\ncellsize 1\n"
                   "NODATA_value -9999\n1 2\n3 4\n")
            zb = io.BytesIO()
            with zipfile.ZipFile(zb, "w") as z:
                z.writestr("swissbathy_1.asc", asc)
            with open(raw_of(aid, au), "wb") as f:
                f.write(zb.getvalue())
            stage(aid)
            assert os.path.isfile(f"store/source/{aid}/{aid}.tif"), "asc zip must mosaic to <id>.tif"
            with rasterio.open(f"store/source/{aid}/{aid}.tif") as src:
                assert src.shape == (2, 2), src.shape

            def asc_grid(x_off, value, nodata=-9999):
                head = (f"ncols 2\nnrows 2\nxllcorner {x_off}\nyllcorner 0\ncellsize 1\n"
                        + (f"NODATA_value {nodata}\n" if nodata is not None else ""))
                return head + f"{value} {value}\n{value} {value}\n"

            # asc-tile mosaics each asset on its own, so two disjoint archives stay two
            # rasters rather than one bounding-box-sized mosaic spanning the gap between them.
            tid = "_prep_asc_tile"
            tu = [f"https://x/tiles/0.5/T{i}" for i in range(2)]
            seed(tid, tu, {"name": "ASC tile", "unpack": "asc-tile", "crs": "EPSG:27700"})
            for i, u in enumerate(tu):
                zb = io.BytesIO()
                with zipfile.ZipFile(zb, "w") as z:
                    for j in range(2):  # two members per asset, side by side
                        z.writestr(f"t{i}{j}mb.asc", asc_grid(i * 1000 + j * 2, -1))
                with open(raw_of(tid, u), "wb") as f:
                    f.write(zb.getvalue())
            stage(tid)
            staged = sorted(glob(f"store/source/{tid}/*.tif"))
            assert [os.path.basename(p) for p in staged] == sorted(
                staged_name(tid, u) for u in tu), staged
            for p in staged:
                with rasterio.open(p) as src:
                    assert src.shape == (2, 4), src.shape  # its own two members, not the gap
                    assert src.read(1).max() < 0, "seafloor stays negative"
                    # grids declaring no NODATA_value fall back to -9999
                    assert src.nodata == -9999, (p, src.nodata)
            assert not glob(f"store/source/{tid}/_asc_*"), "asc-tile must consume its scratch"

            # Collision scope for asc-tile is PER ARCHIVE, unlike the extract modes. Adjacent
            # EA 5 km tiles genuinely ship the same 1 km cell (the API packages by
            # intersection), and that is harmless here: members go to per-asset scratch and
            # mosaic per asset, so the shared cell correctly lands in both neighbours' rasters.
            shid = "_prep_asc_tile_shared"
            shu = [f"https://x/tiles/0.5/TG54{i}" for i in range(2)]
            seed(shid, shu, {"name": "ASC shared", "unpack": "asc-tile", "crs": "EPSG:27700"})
            for i, u in enumerate(shu):
                zb = io.BytesIO()
                with zipfile.ZipFile(zb, "w") as z:
                    z.writestr("tg5402mb.asc", asc_grid(0, -5))          # the shared cell
                    z.writestr(f"tg54{i}9mb.asc", asc_grid(2 + i * 2, -6))
                with open(raw_of(shid, u), "wb") as f:
                    f.write(zb.getvalue())
            stage(shid)
            assert len(glob(f"store/source/{shid}/*.tif")) == 2, \
                "a member basename shared ACROSS archives must not fail asc-tile"

            # …but two members sharing a basename at DIFFERENT paths inside one archive would
            # overwrite each other in that asset's scratch, so it stays fatal.
            dupid = "_prep_asc_tile_dup"
            dupu = "https://x/tiles/0.5/TG55"
            seed(dupid, [dupu], {"name": "ASC dup", "unpack": "asc-tile", "crs": "EPSG:27700"})
            zb = io.BytesIO()
            with zipfile.ZipFile(zb, "w") as z:
                z.writestr("a/tg5502mb.asc", asc_grid(0, -5))
                z.writestr("b/tg5502mb.asc", asc_grid(2, -6))
            with open(raw_of(dupid, dupu), "wb") as f:
                f.write(zb.getvalue())
            try:
                stage(dupid)
                assert False, "expected a within-archive basename collision to exit"
            except SystemExit as e:
                assert "collision" in str(e) and "tg5502mb.asc" in str(e), e

            # A literal duplicate ENTRY — the same full path listed twice, which the EA's 5 km
            # packages really do ship — is one file listed twice, not two inputs colliding.
            # It stages, and the mosaic sees one copy of it.
            twiceid = "_prep_asc_tile_twice"
            twiceu = "https://x/tiles/0.5/TG56"
            seed(twiceid, [twiceu], {"name": "ASC twice", "unpack": "asc-tile",
                                     "crs": "EPSG:27700"})
            body = asc_grid(0, -5)
            zb = io.BytesIO()
            with warnings.catch_warnings():  # zipfile warns on the duplicate it is asked to write
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(zb, "w") as z:
                    z.writestr("tg5602mb.asc", body)
                    z.writestr("tg5602mb.asc", body)     # byte-identical, same path
                    z.writestr("tg5609mb.asc", asc_grid(2, -6))
            with open(raw_of(twiceid, twiceu), "wb") as f:
                f.write(zb.getvalue())
            stage(twiceid)
            with rasterio.open(
                    f"store/source/{twiceid}/{staged_name(twiceid, twiceu, ext='tif')}") as src:
                assert src.shape == (2, 4), src.shape  # two distinct cells, not three
                assert src.read(1).max() < 0, "seafloor stays negative"

            # …but the same path twice with DIFFERENT bytes is ambiguous, and stays fatal.
            forkid = "_prep_asc_tile_forked"
            forku = "https://x/tiles/0.5/TG57"
            seed(forkid, [forku], {"name": "ASC forked", "unpack": "asc-tile",
                                   "crs": "EPSG:27700"})
            zb = io.BytesIO()
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(zb, "w") as z:
                    z.writestr("tg5702mb.asc", asc_grid(0, -5))
                    z.writestr("tg5702mb.asc", asc_grid(0, -6))  # same path, other bytes
            with open(raw_of(forkid, forku), "wb") as f:
                f.write(zb.getvalue())
            try:
                stage(forkid)
                assert False, "expected a CRC-mismatched duplicate entry to exit"
            except SystemExit as e:
                assert "more than once with different bytes" in str(e), e
                assert "tg5702mb.asc" in str(e), e

            # A grid declaring NODATA_value -9999 can still carry an older vintage's void
            # marker as an ordinary pixel. At 0.5 m one of those wins every merge it touches,
            # so anything at or below the sentinel floor is voided whatever the header says —
            # while a merely deep value, and the unprovable positive outliers, are left alone.
            sentid = "_prep_asc_sentinel"
            sentu = "https://x/tiles/0.5/SS01"
            seed(sentid, [sentu], {"name": "ASC sentinel", "unpack": "asc-tile",
                                   "crs": "EPSG:27700"})
            zb = io.BytesIO()
            with zipfile.ZipFile(zb, "w") as z:
                z.writestr("ss0102mb.asc",
                           "ncols 2\nnrows 2\nxllcorner 0\nyllcorner 0\ncellsize 1\n"
                           "NODATA_value -9999\n-999999 -5.5\n-99999 99\n")
            with open(raw_of(sentid, sentu), "wb") as f:
                f.write(zb.getvalue())
            stage(sentid)
            with rasterio.open(
                    f"store/source/{sentid}/{staged_name(sentid, sentu, ext='tif')}") as src:
                band = src.read(1)
                assert band[0][0] == src.nodata, ("-999999 must be void", band)
                assert band[1][0] == src.nodata, ("-99999 must be void", band)
                assert band[0][1] == np.float32(-5.5), ("a real sounding survives", band)
                assert band[1][1] == 99, ("a positive outlier is not provably a marker", band)

            # AAIGrid reads a grid whose every value is a bare integer as Int32, and
            # gdalbuildvrt SKIPS inputs whose type disagrees with the first file's — warning
            # only, exit 0. An all-void integer member sorting first therefore used to drop
            # every real Float32 grid beside it. Both must survive, as float32.
            mixid = "_prep_asc_mixed_dtype"
            mixu = "https://x/tiles/0.5/MX01"
            seed(mixid, [mixu], {"name": "ASC mixed", "unpack": "asc-tile",
                                 "crs": "EPSG:27700"})
            zb = io.BytesIO()
            with zipfile.ZipFile(zb, "w") as z:
                z.writestr("mx0101mb.asc",  # sorts FIRST, all void, no decimal point anywhere
                           "ncols 2\nnrows 2\nxllcorner 0\nyllcorner 0\ncellsize 1\n"
                           "NODATA_value -9999\n-9999 -9999\n-9999 -9999\n")
                z.writestr("mx0102mb.asc",  # the real soundings, float
                           "ncols 2\nnrows 2\nxllcorner 2\nyllcorner 0\ncellsize 1\n"
                           "NODATA_value -9999\n-3.25 -3.5\n-3.75 -4.0\n")
            with open(raw_of(mixid, mixu), "wb") as f:
                f.write(zb.getvalue())
            stage(mixid)
            with rasterio.open(
                    f"store/source/{mixid}/{staged_name(mixid, mixu, ext='tif')}") as src:
                assert src.dtypes[0] == "float32", src.dtypes
                assert src.shape == (2, 4), (src.shape, "the float member must not be skipped")
                got = src.read(1, masked=True)
                assert got.count() == 4, (got.count(), "all four soundings survive")
                assert float(got.min()) == -4.0 and float(got.max()) == -3.25, got

            # Voids must never enter the pixel values, and the mosaic must not let a later
            # member's holes overpaint an earlier one's soundings — the shape EA ships when it
            # packages one cell from two survey dates. This is the invariant the whole
            # near-nodata question turns on, so it is pinned rather than assumed.
            ovid = "_prep_asc_overlap"
            ovu = "https://x/tiles/0.5/OV01"
            seed(ovid, [ovu], {"name": "ASC overlap", "unpack": "asc-tile",
                               "crs": "EPSG:27700"})
            zb = io.BytesIO()
            with zipfile.ZipFile(zb, "w") as z:
                z.writestr("ov0101_20140101mb.asc",  # sorts first: real soundings
                           "ncols 2\nnrows 2\nxllcorner 0\nyllcorner 0\ncellsize 1\n"
                           "NODATA_value -9999\n-5.5 -6.5\n-7.5 -8.5\n")
                z.writestr("ov0101_20140202mb.asc",  # sorts last, same ground, all void
                           "ncols 2\nnrows 2\nxllcorner 0\nyllcorner 0\ncellsize 1\n"
                           "NODATA_value -9999\n-9999 -9999\n-9999 -9999\n")
            with open(raw_of(ovid, ovu), "wb") as f:
                f.write(zb.getvalue())
            stage(ovid)
            with rasterio.open(
                    f"store/source/{ovid}/{staged_name(ovid, ovu, ext='tif')}") as src:
                got = src.read(1, masked=True)
                assert got.count() == 4, (got.count(), "a later member's voids must not "
                                          "overpaint an earlier member's soundings")
                assert float(got.min()) == -8.5 and float(got.max()) == -5.5, got
                # nothing may sit in the empty gap between the void marker and the real data
                between = got[(got > src.nodata) & (got < -100)]
                assert between.count() == 0, ("nodata blended into a neighbour", between)

            # A grid that declares no NODATA_value at all still gets the -9999 fallback.
            nid = "_prep_asc_no_nodata"
            nu = "https://x/tiles/0.5/N0"
            seed(nid, [nu], {"name": "ASC no nodata", "unpack": "asc-tile",
                             "crs": "EPSG:27700"})
            zb = io.BytesIO()
            with zipfile.ZipFile(zb, "w") as z:
                z.writestr("n0mb.asc", asc_grid(0, -4, nodata=None))
            with open(raw_of(nid, nu), "wb") as f:
                f.write(zb.getvalue())
            stage(nid)
            with rasterio.open(f"store/source/{nid}/{staged_name(nid, nu, ext='tif')}") as src:
                assert src.nodata == -9999, src.nodata

            # The SAME mode over a 7z, narrowed by a glob: the container is sniffed, and the
            # glob keeps a second resolution of the same ground out of the mosaic (Litto3D
            # ships MNT1m beside MNT5m). Only the 5 m member may reach the raster.
            sid_7z = "_prep_asc_tile_7z"
            u7 = "https://x/prepaquet/0225_6770.7z"
            seed(sid_7z, [u7], {"name": "ASC tile 7z", "unpack": "asc-tile:*_MNT5M_*.asc",
                                "crs": "EPSG:2154"})
            import py7zr
            with py7zr.SevenZipFile(raw_of(sid_7z, u7), "w") as z:
                for j in range(2):
                    z.writestr(asc_grid(j * 2, -7, nodata=-99999),
                               f"d{j}/MNT5m/L3D_{j}_MNT5M_LAMB93.asc")
                    z.writestr(asc_grid(j * 2, -1, nodata=-99999),
                               f"d{j}/MNT1m/L3D_{j}_MNT1M_LAMB93.asc")
                z.writestr("read me", "d0/DC_LITTO3D.txt")
            stage(sid_7z)
            # …and it is named for what it IS (a tif), not for the .7z it came out of —
            # otherwise prep()'s *.tif glob registers nothing at all.
            staged7 = glob(f"store/source/{sid_7z}/*.tif")
            assert staged7 == [f"store/source/{sid_7z}/{staged_name(sid_7z, u7, ext='tif')}"], \
                staged7
            assert staged7[0].endswith(".tif") and "0225_6770" in staged7[0], staged7
            with rasterio.open(staged7[0]) as src:
                assert src.shape == (2, 4), src.shape  # the two 5 m members only
                assert (src.read(1) == -7).all(), "the 1 m members must not join the mosaic"
                # the grids' OWN nodata survives — relabelling it would turn every void into
                # a plausible depth once the datum step shifts it
                assert src.nodata == -99999, src.nodata
            print("source_prep.py self-check ok (incl. asc mosaic + asc tile over zip/7z)")
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
