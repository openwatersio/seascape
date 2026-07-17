"""Prepare one source from its fetched raw assets: stage → datum → normalize.

The Snakemake lane's single prep entry point, driven entirely by metadata.json —
the per-source knobs that live in Justfile flags on the legacy chain:

  crs             horizontal CRS to assign (source_normalize --crs)
  nodata          nodata value to assign (source_normalize --nodata)
  negate          raw values are positive-down depth → flip (source_datum --negate)
  datum_offset_m  constant shift to ~MSL (source_datum --offset)
  clamp_positive  drop cells above the water surface (source_datum --clamp-positive)
  archive_members fnmatch glob selecting which archive members to extract (all archive
                  kinds: zip/tar/7z); absent = every .tif/.tiff member

Staging is CONTENT-KEYED, never source-keyed: every prepped source is fetched the same
way (one raw/<index> per file_list.txt entry, extensionless — source_fetch.py doesn't
trust URL extensions), and stage() routes each raw by sniffing its magic bytes:

  zip      → extract member rasters flat (archive_members filters when declared); a zip
             of ESRI ASCII .asc grids is mosaicked into one store/source/<id>/<id>.tif
             (swissBATHY / Bodensee)
  7z       → extract the selected members flat via py7zr (African Great Lakes)
  gzip     → decompress, then re-sniff the inner bytes (an .e00 ARC/INFO export →
             convert_e00; a tar → extract members, exactly ONE match required per
             tarball when archive_members is declared — the NGDC Great Lakes layout;
             a bare GeoTIFF → keep)
  netCDF   → gdal_translate to a GeoTIFF, per-file CRS preserved (assign EPSG:4326 only
             when the file embeds none), named after the URL stem (NOAA estuaries)
  anything else → hardlink to <id>_<index>.<ext> under the extension source_download
             would have chosen, so staged names match the legacy chain

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
from source_download import ext_for
from source_normalize import normalize_file


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


def _claim(seen, name, origin):
    """Register a staged basename; hard-error on a collision (two archive members or
    nested paths sharing a basename would silently overwrite each other)."""
    if name in seen:
        sys.exit(f"{origin}: staged filename collision on {name!r} — two members would "
                 "overwrite each other; the archives need distinct basenames")
    seen.add(name)


def _members(names, members_glob, origin):
    """Archive members to extract: fnmatch of metadata's `archive_members` glob against
    the full member path (case-insensitive) when declared — zero matches is a hard error
    (the upstream layout changed under the recipe) — else every .tif/.tiff member."""
    if members_glob:
        picks = [n for n in names if fnmatch.fnmatchcase(n.lower(), members_glob.lower())]
        if not picks:
            sys.exit(f"{origin}: no archive member matches archive_members={members_glob!r}")
        return picks
    return [n for n in names if n.lower().endswith((".tif", ".tiff"))]


def _extract_members(members_reader, names, root, seen, origin):
    """Write each selected member flat into root by its basename. members_reader(name)
    returns the member's bytes."""
    for name in names:
        base = os.path.basename(name)
        _claim(seen, base, origin)
        with open(f"{root}/{base}", "wb") as f:
            f.write(members_reader(name))
    return len(names)


def _stage_zip(raw, root, asc_dir, seen, origin, members_glob):
    """A zip of GeoTIFFs → extract them flat (archive_members filters when declared).
    A zip of ESRI ASCII .asc grids → stash the tiles under asc_dir for a single mosaic
    after every asset is staged."""
    with zipfile.ZipFile(raw) as z:
        names = z.namelist()
        picks = _members(names, members_glob, origin)
        if picks:
            n = _extract_members(z.read, picks, root, seen, origin)
            return f"zip, {n} member(s)"
        ascs = [n for n in names if n.lower().endswith(".asc")]
        if ascs:
            os.makedirs(asc_dir, exist_ok=True)
            for name in ascs:
                base = os.path.basename(name)
                _claim(seen, base, origin)
                with open(f"{asc_dir}/{base}", "wb") as f:
                    f.write(z.read(name))
            return f"zip, {len(ascs)} asc tile(s) staged for mosaic"
    return "zip, no raster members"


def _stage_7z(raw, root, seen, origin, members_glob):
    """A 7z archive → extract the selected members flat (archive_members filters when
    declared; the African Great Lakes .7z carries four per-lake Analytical rasters).
    py7zr, not GDAL /vsi7z — the CI image's GDAL lacks the libarchive backend."""
    import py7zr
    with py7zr.SevenZipFile(raw) as z:
        picks = _members(z.getnames(), members_glob, origin)
        tmp = f"{root}/_7z_extract"
        shutil.rmtree(tmp, ignore_errors=True)
        z.extract(path=tmp, targets=picks)
    for name in picks:
        base = os.path.basename(name)
        _claim(seen, base, origin)
        os.replace(f"{tmp}/{name}", f"{root}/{base}")
    shutil.rmtree(tmp, ignore_errors=True)
    return f"7z, {len(picks)} member(s)"


def _stage_gzip(raw, root, source, index, seen, members_glob):
    """Decompress a gzip member, then route the inner bytes by content: .e00 → convert,
    tar → extract members, bare GeoTIFF → keep. A tar WITH archive_members must match
    exactly one member per tarball — the NGDC Great Lakes layout (one <lake>_lld.tif +
    sidecars per tarball); 0 or 2+ matches means the upstream layout changed."""
    origin = f"{source}[{index}]"
    inner = f"{root}/_gz_{index}"
    with gzip.open(raw, "rb") as fin, open(inner, "wb") as fout:
        shutil.copyfileobj(fin, fout)
    try:
        with open(inner, "rb") as f:
            kind = _kind(f.read(512))
        if kind == "e00":
            _claim(seen, f"{source}.tif", origin)
            e00_to_tif(inner, f"{root}/{source}.tif")
            return "gzip → e00 → tif"
        if kind == "tar":
            with tarfile.open(inner) as t:
                names = [m.name for m in t.getmembers() if m.isfile()]
                if members_glob:
                    picks = [n for n in names
                             if fnmatch.fnmatchcase(n.lower(), members_glob.lower())]
                    if len(picks) != 1:
                        sys.exit(f"{origin}: expected exactly one member matching "
                                 f"{members_glob!r} in the tarball, found {len(picks)}")
                else:
                    picks = [n for n in names if n.lower().endswith((".tif", ".tiff"))]
                for name in picks:
                    base = os.path.basename(name)
                    _claim(seen, base, origin)
                    with t.extractfile(name) as src, open(f"{root}/{base}", "wb") as dst:
                        shutil.copyfileobj(src, dst)  # stream to disk, don't buffer the raster
            return f"gzip → tar, {len(picks)} member(s)"
        if kind == "tif":
            _claim(seen, f"{source}_{index}.tif", origin)
            os.replace(inner, f"{root}/{source}_{index}.tif")
            inner = None  # moved, don't remove below
            return "gzip → tif"
        sys.exit(f"{origin}: gzip inner content not recognized (e00/tar/tif)")
    finally:
        if inner and os.path.exists(inner):
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


def stage(source):
    root = f"store/source/{source}"
    urls = config.file_list(source)
    members_glob = config.load_metadata(source).get("archive_members")
    indices = _raw_indices(source, root, len(urls))
    _clear_stale(root)
    asc_dir = f"{root}/asc"
    seen = set()  # staged basenames — collisions across raws/archives hard-error
    for index in indices:
        raw = f"{root}/raw/{index}"
        origin = f"{source}[{index}]"
        with open(raw, "rb") as f:
            kind = _kind(f.read(512))
        if kind == "zip":
            note = _stage_zip(raw, root, asc_dir, seen, origin, members_glob)
        elif kind == "7z":
            note = _stage_7z(raw, root, seen, origin, members_glob)
        elif kind == "gzip":
            note = _stage_gzip(raw, root, source, index, seen, members_glob)
        elif kind == "netcdf":
            note = _stage_netcdf(raw, root, urls[index], seen, origin)
        else:
            base = f"{source}_{index}.{ext_for(urls[index])}"
            _claim(seen, base, origin)
            dest = f"{root}/{base}"
            if os.path.exists(dest):
                os.remove(dest)
            os.link(raw, dest)
            note = f"-> {base}"
        print(f"{origin}: {note}")
    if os.path.isdir(asc_dir):
        _mosaic_asc(root, source, asc_dir)


def prep(source):
    meta = config.load_metadata(source)
    stage(source)
    # *.tif only — exact legacy parity: source_datum/source_normalize glob *.tif, so
    # extracted .tiff members (ausbathytopo's raw twins) stay raw there and here.
    tifs = sorted(glob(f"store/source/{source}/*.tif"))

    negate = bool(meta.get("negate", False))
    offset = float(meta.get("datum_offset_m", 0.0))
    clamp = bool(meta.get("clamp_positive", False))
    # The sidecar is written even for a no-op, so the catalog's invariant holds
    # uniformly — and negate publishes False downstream once it's baked here.
    write_sidecar(source, negate, offset, clamp)
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
    """Synthetic sources end to end. Common path: a zip raw extracts its .tif members, a
    plain raw hardlinks under the legacy name, stale root tifs are cleared, the metadata
    knobs drive datum + normalize. Failure modes: a missing raw index names what to fetch,
    a non-index file in raw/ is a distinct error, an orphan index beyond a shrunken
    file_list is deleted, and a staged-basename collision hard-errors. Format registry: a
    gzipped .e00 stages to <id>.tif (pure-Python), a gzipped tar stages exactly its one
    *_lld.tif (0 → error), and — when the GDAL CLI is present — a zip of .asc tiles
    mosaics to <id>.tif."""
    import io
    import json
    import tempfile

    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

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
            json.dump({"name": "Synth", "negate": True, "datum_offset_m": -1.0,
                       "crs": "EPSG:28992"}, f)

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
        with open(f"store/source/{sid}/raw/1", "wb") as f:
            f.write(tif_bytes(10.0))  # +10 m depth
        with open(f"store/source/{sid}/stale.tif", "w") as f:
            f.write("old")

        prep(sid)
        assert not os.path.exists(f"store/source/{sid}/stale.tif"), "stale tif must be cleared"
        assert not os.path.exists(f"store/source/{sid}/readme.txt")
        assert open(f"store/source/{sid}/raw/1", "rb").read() == tif_bytes(10.0), \
            "in-place steps must never write through into raw/"
        with open(f"store/source/{sid}/datum.json") as f:
            sidecar = json.load(f)
        assert sidecar == {"negate": True, "offset_m": -1.0, "clamp_positive": False}, sidecar
        for name, want in (("a.tif", -6.0), (f"{sid}_1.tif", -11.0)):  # -(v) - 1
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
            f.write(tif_bytes(10.0))  # now an orphan
        prep(sid)
        assert not os.path.exists(f"store/source/{sid}/raw/1"), "orphan raw must be deleted"

        # Two archives whose members share a basename must hard-error, not silently overwrite.
        cid = "_prep_collide"
        os.makedirs(f"sources/{cid}")
        os.makedirs(f"store/source/{cid}/raw")
        with open(f"sources/{cid}/file_list.txt", "w") as f:
            f.write("https://x/a.zip\nhttps://x/b.zip\n")
        with open(f"sources/{cid}/metadata.json", "w") as f:
            json.dump({"name": "Collide"}, f)
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

        # archive_members on a gzipped tar: stages ONLY the single matching member (the
        # Great Lakes *_lld.tif layout), and 0 (or 2+) matches per tarball is a hard error.
        import gzip as _gz
        import tarfile as _tar
        lid = "_prep_lld"
        os.makedirs(f"sources/{lid}")
        os.makedirs(f"store/source/{lid}/raw")
        with open(f"sources/{lid}/file_list.txt", "w") as f:
            f.write("https://x/huron_lld.geotiff.tar.gz\n")
        with open(f"sources/{lid}/metadata.json", "w") as f:
            json.dump({"name": "LLD", "archive_members": "*_lld.tif"}, f)

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
            assert "exactly one" in str(e), e

        # archive_members on a 7z: only matching members are extracted (multiple matches
        # OK — the African Great Lakes .7z carries four per-lake rasters).
        import py7zr
        zid = "_prep_7z"
        os.makedirs(f"sources/{zid}")
        os.makedirs(f"store/source/{zid}/raw")
        with open(f"sources/{zid}/file_list.txt", "w") as f:
            f.write("https://x/rasters.7z\n")
        with open(f"sources/{zid}/metadata.json", "w") as f:
            json.dump({"name": "7Z", "archive_members": "*_ras.tif"}, f)
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
            json.dump({"name": "E00", "crs": "EPSG:32610"}, f)
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
        import gzip as _gz
        with _gz.open(f"store/source/{eid}/raw/0", "wb") as f:
            f.write(e00_text.encode())
        stage(eid)
        with rasterio.open(f"store/source/{eid}/{eid}.tif") as src:
            assert src.shape == (2, 2) and src.read(1)[0, 0] == 1.0, src.read(1)

        # Format registry: a zip of ESRI ASCII .asc tiles mosaics to <id>.tif (needs GDAL CLI).
        if shutil.which("gdalbuildvrt") and shutil.which("gdal_translate"):
            aid = "_prep_asc"
            os.makedirs(f"sources/{aid}")
            os.makedirs(f"store/source/{aid}/raw")
            with open(f"sources/{aid}/file_list.txt", "w") as f:
                f.write("https://x/tiles.esriasciigrid.zip\n")
            with open(f"sources/{aid}/metadata.json", "w") as f:
                json.dump({"name": "ASC", "crs": "EPSG:2056"}, f)
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

