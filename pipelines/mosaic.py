"""Stage 2 — persist each aggregation tile's merged Float32 DEM as the durable MOSAIC product.

The aggregate computes a priority-resolved, seam-feathered, datum-offset, land-clamped merged DEM
transiently inside every tile's tmp folder (aggregation_reproject + aggregation_merge). This module
persists that exact array — unsmoothed, unencoded — as the survey-faithful truth layer stage 3 reads:

  store/mosaic/
    tiles/<stem>.tif          one Float32 COG per aggregation tile: the merged DEM cropped to the
                              tile's EXACT bounds (halo removed so the tiles partition the plane with
                              no overlap — a GTI requirement), nodata = -9999 (the sentinel the
                              merge's hole detection uses), nodata-aware `average` internal overviews
                              native -> z8 (the COG driver stops at the 512 block = one z8 tile).
                              `average`, never `nearest`: decimation is the anti-alias prefilter.
    index/covering.parquet    a GeoParquet tile index doubling as the manifest: rows are the tile
                              COGs with ABSOLUTE `location`s (+ per-tile resx/resy) the GTI driver
                              reads, plus `seascape:`-prefixed provenance columns it ignores (sources,
                              datum, offset, priority, maxzoom).
    planet-z8.tif             the whole mosaic decimated to the GEBCO-native z8 base, registered as
                              the mosaic's overview (GTI <Overview>) so a z0-z4 open reads it, not
                              thousands of tile-COG top overviews.
    mosaic.gti                the pointer — a small XML naming the index + the z8 overview; GDAL opens
                              the planet mosaic straight from it. Written LAST.

No smoothing, no encoding: that is stage-3 display generalization. The mosaic is the layer to QA,
diff between builds, and could publish as an open dataset.

R2-agnostic like the rest of pipelines/: the writer writes the LOCAL store; the GTI `location` base
comes from MOSAIC_VSI_BASE (CI: a /vsicurl bucket URL), else the tiles dir's local abspath.

Artifacts take plain stable names; the publish command content-addresses the finished COGs by
hashing their bytes at R2-publish time, so a name's presence on R2 proves its content.

  python mosaic.py tile <covering-csv>   one tile: reproject + merge + persist, merge ONLY
                                         (no smoothing, no vector forks — stage 3 reads windows)
  python mosaic.py index --stable        build the index + planet z8 COG + mosaic.gti
  python mosaic.py publish               content-address the plain COGs to R2 + a CANDIDATE
                                         pointer; the serving pointer mosaic.gti is never touched
  python mosaic.py --check               self-check
"""

import glob
import hashlib
import json
import os
import shutil
import sys

import mercantile
import rasterio

import aggregation_covering
import aggregation_merge
import aggregation_reproject
import config
import smooth
import utils

NODATA = -9999

# The per-source build props that shape the merged surface (priority + maxzoom set merge order;
# offset is the recorded datum shift; land_clamp/negate/band/mixed_crs change the warped values).
_PROPS = ("priority", "max_zoom", "land_clamp", "offset", "negate", "band", "mixed_crs")


def tiles_dir():
    return "store/mosaic/tiles"


def index_dir():
    return "store/mosaic/index"


def gti_path():
    return "store/mosaic/mosaic.gti"


def _stem(filepath):
    return filepath.split("/")[-1].replace("-aggregation.csv", "")


def tile_artifact(stem):
    """The tile-COG path for a stem. Plain, stable name; publish content-addresses the R2 copy by
    hashing its bytes."""
    return f"{tiles_dir()}/{stem}.tif"


def planet_artifact():
    """The planet-z8 overview COG path. Content-addressed only in the R2 copy, at publish time."""
    return "store/mosaic/planet-z8.tif"


def vsi_base():
    """The absolute base the GTI `location` column resolves against — MOSAIC_VSI_BASE when set (CI
    passes a /vsicurl bucket URL for the mosaic tiles prefix), else the tiles dir's local abspath so
    a laptop's GTI opens straight off disk. Mirrors config.source_path reading SOURCE_VSI_BASE per
    call — R2-agnostic, no bucket name in the pipeline."""
    base = os.environ.get("MOSAIC_VSI_BASE")
    return base if base else os.path.abspath(tiles_dir())


def _tile_sources(filepath):
    with open(filepath) as f:
        rows = f.read().splitlines()[1:]  # skip header
    return sorted({r.split(",")[0] for r in rows if r.strip()})


def _merged_dem(tmp_folder):
    """The merged DEM the aggregate produced: the highest-index N-3857.tiff (the merge output for a
    multi-source tile, or the lone reprojected source for a single-source one)."""
    n = len(glob.glob(f"{tmp_folder}/*.tiff"))
    return f"{tmp_folder}/{n - 1}-3857.tiff"


def _translate(filepath, tmp_folder):
    """Crop the halo off the tile's merged DEM and write the mosaic COG to a transient path inside
    tmp_folder. Crops buffer_pixels off every side so the tile carries EXACTLY its mercantile bounds
    (the non-overlapping partition GTI needs). Float32, nodata = -9999, ZSTD, nodata-aware `average`
    overviews (COG driver, native -> the 512 block = z8)."""
    stem = _stem(filepath)
    with open(f"{tmp_folder}/reprojection.json") as f:
        buffer_pixels = json.load(f)["buffer_pixels"]
    merged = _merged_dem(tmp_folder)
    with rasterio.open(merged) as src:
        width, height = src.width, src.height
    w = width - 2 * buffer_pixels
    h = height - 2 * buffer_pixels
    # De-buffered extent is span*512 (span = 2**(child_z - z)); a fractional result means the halo
    # math drifted from the tiling math — fail loudly rather than emit a misregistered tile.
    if w <= 0 or h <= 0 or w % 512 or h % 512:
        raise ValueError(f"mosaic {stem}: de-buffered size {w}x{h} is not a positive multiple of 512")

    tmp_cog = f"{tmp_folder}/mosaic.tif"
    # -b 1: drop any alpha band; -a_nodata -9999 + -ot Float32: one uniform sentinel + dtype across
    # every tile (GEBCO-only tiles are Int16 upstream). -srcwin adjusts the geotransform, so the
    # output origin is the tile's exact bounds. COG default overviews stop at the 512 block (= z8),
    # AVERAGE + the nodata make them nodata-aware; the transient smooth/encode never see this file.
    utils.run_command(
        f"GDAL_CACHEMAX=512 gdal_translate -q -of COG -b 1 -ot Float32 -a_nodata {NODATA} "
        f"-srcwin {buffer_pixels} {buffer_pixels} {w} {h} "
        "-co COMPRESS=ZSTD -co PREDICTOR=3 -co BLOCKSIZE=512 "
        "-co RESAMPLING=AVERAGE -co OVERVIEW_RESAMPLING=AVERAGE -co NUM_THREADS=ALL_CPUS "
        f"{merged} {tmp_cog}")
    return tmp_cog


def tile(filepath):
    """One covering tile's merge: reproject each intersecting source, feather-merge, persist the
    unsmoothed COG at store/mosaic/tiles/<stem>.tif. No smoothing, no vector forks (stage 3 reads
    this tile back through windowed VRTs). The tmp folder is cleared first so a retried job never
    reuses a half-written reproject."""
    stem = _stem(filepath)
    tmp_folder = filepath.replace("-aggregation.csv", "-tmp")
    shutil.rmtree(tmp_folder, ignore_errors=True)
    aggregation_reproject.reproject(filepath)
    aggregation_merge.merge(filepath)
    tmp_cog = _translate(filepath, tmp_folder)
    os.makedirs(tiles_dir(), exist_ok=True)
    out = tile_artifact(stem)
    with open(tmp_cog, "rb") as f:
        os.fsync(f.fileno())  # rename is metadata-only; a hard box teardown must not strand garbage
    os.replace(tmp_cog, out)       # atomic: the stable name only ever appears complete
    if not os.environ.get("KEEP_TMP"):  # KEEP_TMP=1 preserves the merged DEM for debugging
        shutil.rmtree(tmp_folder)
    print(f"mosaic tile {stem}: {out}", flush=True)
    return out


# ── the covering-level index / planet-z8 / pointer (mosaic.py index) ──────────────────────────────


def _datum_name(source):
    """The source's recorded vertical datum name (catalog seascape:vertical_datum, else
    metadata.json `datum`), for the manifest — None when unrecorded (e.g. GEBCO, MSL-ish)."""
    cat = config.load_catalog(source)
    if cat is not None:
        v = cat.get("properties", {}).get("seascape:vertical_datum")
        if v is not None:
            return v
    return config.load_metadata(source).get("datum")


def _feature(filepath, cpath, location, extra_props=None):
    """One index row: geometry (exact 3857 tile bounds — a non-overlapping partition, so GTI needs
    no z-order), location, resx/resy, and the seascape: provenance columns composited from the
    tile's catalog items. Geometry + resolution come from the produced COG's OWN georeferencing,
    not recomputed from mercantile: the two agree to the metre, but a float-epsilon disagreement
    makes GTI ceil the virtual extent to a 1-pixel overhang (the exact-grid-registration
    invariant). Reading the COG makes the index describe the raster exactly."""
    stem = _stem(filepath)
    child_z = int(stem.split("-")[3])
    with rasterio.open(cpath) as src:
        t = src.transform
        res = t.a
        left, top = t.c, t.f
        right, bottom = left + src.width * res, top - src.height * res

    sources = _tile_sources(filepath)
    props = {
        "location": location,
        "resx": res,
        "resy": res,
        **(extra_props or {}),
        "seascape:sources": ",".join(sources),
        "seascape:priority": max((int(config.source_property(s, "priority", 0) or 0) for s in sources), default=0),
        "seascape:maxzoom": child_z,
        "seascape:datum": json.dumps({s: _datum_name(s) for s in sources}, sort_keys=True),
        "seascape:offset": json.dumps({s: float(config.source_property(s, "offset", 0.0) or 0.0) for s in sources}, sort_keys=True),
    }
    geom = {"type": "Polygon", "coordinates": [[
        [left, bottom], [right, bottom], [right, top], [left, top], [left, bottom]]]}
    return {"type": "Feature", "properties": props, "geometry": geom}


def _tile_row_plain(filepath):
    """One index row for a stem's tile COG. Refuses a missing tile — an index must never vouch for
    a half-built mosaic."""
    stem = _stem(filepath)
    cpath = tile_artifact(stem)
    if not os.path.isfile(cpath):
        raise SystemExit(f"mosaic index incomplete: missing {cpath} — the mosaic_tile rules "
                         f"produce it; refusing to publish an index")
    return _feature(filepath, cpath, f"{vsi_base()}/{stem}.tif")


def _write_index(filepath_out, features):
    """Write the GeoParquet index via ogr2ogr (GDAL's Parquet driver — no pyarrow dep, per the
    build's dep discipline: prefer the present GDAL over a new Python dep). Goes through a temp
    GeoJSON so the colon-bearing seascape: field names survive verbatim into Parquet."""
    os.makedirs(os.path.dirname(filepath_out), exist_ok=True)
    tmp_geojson = filepath_out + ".geojson"
    with open(tmp_geojson, "w") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)
    tmp_parquet = filepath_out + ".tmp.parquet"
    if os.path.exists(tmp_parquet):
        os.remove(tmp_parquet)
    utils.run_command(
        f"ogr2ogr -f Parquet -a_srs EPSG:3857 {tmp_parquet} {tmp_geojson}")
    os.replace(tmp_parquet, filepath_out)  # index only ever appears complete
    os.remove(tmp_geojson)


def _warp_planet(index_path, out_tmp):
    """Decimate the whole mosaic (via the index-as-GTI, so the warp picks each tile's own
    `average` overviews — never a full-res read) to the GEBCO-native z8 base."""
    res8 = aggregation_reproject.get_resolution(utils.macrotile_z)
    if os.path.exists(out_tmp):
        os.remove(out_tmp)
    utils.run_command(
        f"GDAL_CACHEMAX=512 gdalwarp -q -overwrite -r average -tr {res8} {res8} "
        f"-dstnodata {NODATA} -of COG -co BIGTIFF=YES -co COMPRESS=ZSTD -co PREDICTOR=3 "
        "-co BLOCKSIZE=512 -co OVERVIEW_RESAMPLING=AVERAGE -co NUM_THREADS=ALL_CPUS "
        f"GTI:{index_path} {out_tmp}")


def _gti_xml(index_ref, planet_ref, resolution):
    """The GTI pointer XML naming an index + z8 overview. index_ref / planet_ref are written
    verbatim: RELATIVE paths for the local serving pointer (portable across store prefixes),
    ABSOLUTE public URLs for the publish command's candidate pointer (resolves off the bucket)."""
    return (
        "<GDALTileIndexDataset>\n"
        f"  <IndexDataset>{index_ref}</IndexDataset>\n"
        "  <LocationField>location</LocationField>\n"
        "  <SRS>EPSG:3857</SRS>\n"
        f"  <ResX>{resolution}</ResX>\n"
        f"  <ResY>{resolution}</ResY>\n"
        f"  <NoDataValue>{NODATA}</NoDataValue>\n"
        "  <Overview>\n"
        f"    <Dataset>{planet_ref}</Dataset>\n"
        "  </Overview>\n"
        "</GDALTileIndexDataset>\n")


def _write_gti(index_path, planet_path, resolution):
    """Write the mosaic.gti pointer LAST: a small XML naming the current index + the z8 overview.
    IndexDataset / Overview are stored RELATIVE to the .gti file so the store prefix is portable
    (rclone carries it as-is); GDAL opens the planet mosaic straight from this file. One atomic
    rename, so the pointer only ever names a complete world.

    The R2 push publishes the mosaic/ prefix (tiles, index, planet-z8) BEFORE this pointer — same
    artifacts-before-pointer discipline as bounds.csv-last — then PUTs mosaic.gti last. This module
    only writes it locally."""
    rel_index = os.path.relpath(index_path, os.path.dirname(gti_path()))
    rel_planet = os.path.relpath(planet_path, os.path.dirname(gti_path()))
    xml = _gti_xml(rel_index, rel_planet, resolution)
    tmp = gti_path() + ".tmp"
    with open(tmp, "w") as f:
        f.write(xml)
    os.replace(tmp, gti_path())


# ── the content-addressed R2 publish (mosaic.py publish) ──────────────────────────────────────────
#
# Hash the finished plain COGs, hardlink them under content names, and push only what R2 lacks
# (content-addressing makes existence == correctness). The parquet + GTI it uploads are a CANDIDATE
# pointer; the serving pointer mosaic.gti is never produced from this path — promotion is separate.

SERVING_GTI_NAME = "mosaic.gti"  # the serving pointer — promotion writes it; this path must not


def publish_dir():
    return "store/mosaic/publish"


def public_base():
    """The public base the CANDIDATE index locations + GTI refs resolve against — PUBLIC_BASE when
    set (the workflow passes it), else the production bucket base."""
    return os.environ.get("PUBLIC_BASE", "https://data.openwaters.io/bathymetry")


def _hash12(path):
    """12 hex of the plain COG's content hash — the R2 basename discriminator. Fails loudly on a
    missing/remote path: publish hashes local plain COGs the mosaic rules just wrote."""
    h = utils.file_hash(path)
    if h is None:
        sys.exit(f"mosaic publish: cannot hash {path} — missing or remote; run `snakemake mosaic` first")
    return h[:12]


HASH_CACHE = "store/mosaic/hash-cache.json"


class _HashCache:
    """mtime_ns+size-keyed content-hash cache: a no-op publish re-hashed the whole ~360 GB tile
    store (~40 min); unchanged files answer from here in microseconds. The mosaic writer fsyncs
    before its rename, so a replaced tile always presents a new (mtime, size) identity."""

    def __init__(self):
        try:
            with open(HASH_CACHE) as f:
                self.entries = json.load(f)
        except (OSError, ValueError):
            self.entries = {}
        self.dirty = False

    def hash12(self, path):
        st = os.stat(path)
        cached = self.entries.get(path)
        if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            return cached[2]
        h = _hash12(path)
        self.entries[path] = [st.st_mtime_ns, st.st_size, h]
        self.dirty = True
        return h

    def save(self):
        if not self.dirty:
            return
        tmp = HASH_CACHE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.entries, f)
        os.replace(tmp, HASH_CACHE)


def _stage_publish():
    """Stage the content-addressed publish set LOCALLY — no network, so the test drives it directly.
    Recreate publish/ each run; hardlink every covering tile COG to publish/tiles/<stem>-<hash12>.tif
    and planet-z8 to publish/planet-z8-<hash12>.tif; build the CANDIDATE index (its `location` column
    the PUBLIC hashed tile URLs) named by content (<idxhash12> over the sorted tile hashes); and write
    the CANDIDATE GTI referencing the PUBLIC index + planet URLs (absolute, so it resolves off the
    bucket). The serving name mosaic.gti is never produced. Returns (publish_dir, features, names)."""
    stems = _stable_stems()
    pubdir = publish_dir()
    tiles_stage = f"{pubdir}/tiles"
    shutil.rmtree(pubdir, ignore_errors=True)
    os.makedirs(tiles_stage)

    cache = _HashCache()
    features, tile_files, tile_hashes = [], [], []
    for stem in stems:
        fp = f"store/aggregation/{stem}-aggregation.csv"
        cpath = tile_artifact(stem)
        if not os.path.isfile(cpath):
            sys.exit(f"mosaic publish: missing {cpath} — run `snakemake mosaic` first")
        h = cache.hash12(cpath)
        tile_hashes.append(h)
        name = f"{stem}-{h}.tif"
        tile_files.append(name)
        os.link(cpath, f"{tiles_stage}/{name}")
        features.append(_feature(fp, cpath, f"/vsicurl/{public_base()}/mosaic/tiles/{name}"))

    planet = planet_artifact()
    if not os.path.isfile(planet):
        sys.exit(f"mosaic publish: missing {planet} — run `snakemake mosaic` first")
    planet_name = f"planet-z8-{cache.hash12(planet)}.tif"
    cache.save()
    os.link(planet, f"{pubdir}/{planet_name}")

    # Hash the pointer's SEMANTIC content (locations + refs), not just the tile hashes: a
    # location-format fix must mint a NEW candidate name, or --ignore-existing keeps serving
    # the defective object under the unchanged name.
    locations = sorted(f["properties"]["location"] for f in features)
    idxhash = hashlib.sha256("\n".join(locations + [public_base()]).encode()).hexdigest()[:12]
    index_name = f"{idxhash}.parquet"
    index_stage = f"{pubdir}/index/{index_name}"
    _write_index(index_stage, features)

    gti_name = f"mosaic-candidate-{idxhash}.gti"
    assert gti_name != SERVING_GTI_NAME, "the candidate GTI must never be the serving pointer"
    # /vsicurl/ prefix: a bare https URL makes GDAL fetch whole objects instead of
    # range-reading — an open would download entire tile COGs.
    index_ref = f"/vsicurl/{public_base()}/mosaic/index/{index_name}"
    planet_ref = f"/vsicurl/{public_base()}/mosaic/{planet_name}"
    child_z = max(int(s.split("-")[3]) for s in stems)
    resolution = aggregation_reproject.get_resolution(child_z)
    gti_stage = f"{pubdir}/{gti_name}"
    with open(gti_stage, "w") as f:
        f.write(_gti_xml(index_ref, planet_ref, resolution))

    names = {"tiles_dir": tiles_stage, "tile_files": tile_files, "planet": planet_name,
             "index": index_name, "index_ref": index_ref, "planet_ref": planet_ref,
             "gti": gti_name, "gti_path": gti_stage, "idxhash": idxhash}
    return pubdir, features, names


def candidate_gti_name():
    """The candidate GTI object name for the CURRENT local store, computed without staging:
    the same location list + public-base hash _stage_publish mints. stage_build embeds this in
    the release manifest so release.yml can promote the matching pointer to mosaic.gti."""
    cache = _HashCache()
    locations = sorted(
        f"/vsicurl/{public_base()}/mosaic/tiles/{stem}-{cache.hash12(tile_artifact(stem))}.tif"
        for stem in _stable_stems())
    cache.save()
    idxhash = hashlib.sha256("\n".join(locations + [public_base()]).encode()).hexdigest()[:12]
    return f"mosaic-candidate-{idxhash}.gti"


def _publish_guard():
    """rclone + R2 creds are required — publish runs on the box only."""
    if shutil.which("rclone") is None:
        sys.exit("rclone not found — mosaic publish runs on the box only")
    if not os.environ.get("RCLONE_CONFIG_R2_TYPE"):
        sys.exit("RCLONE_CONFIG_R2_TYPE unset (no R2 creds) — mosaic publish runs on the box only")


def publish():
    """Push the content-addressed publish set to R2 and print a summary. One `rclone copy
    --ignore-existing` per prefix moves only objects R2 lacks; the CANDIDATE GTI never overwrites
    the serving pointer. $DATA_BUCKET stays a shell env var (rclone's env-var remote is `r2`)."""
    _publish_guard()
    pubdir, features, names = _stage_publish()
    dest = "r2:$DATA_BUCKET/bathymetry/mosaic"

    # Count new-vs-skipped from the remote's existing basenames before copying — content-addressing
    # makes a name's presence proof of its bytes, so a listed name is already-published.
    lsf, _ = utils.run_command(f"rclone lsf {dest}/tiles --files-only 2>/dev/null || true")
    existing = set(lsf.split())
    new = [n for n in names["tile_files"] if n not in existing]

    utils.run_command(f"rclone copy --ignore-existing {names['tiles_dir']} {dest}/tiles "
                      "--retries 5 --stats 60s --stats-one-line", silent=False)
    utils.run_command(f"rclone copyto --ignore-existing {pubdir}/{names['planet']} "
                      f"{dest}/{names['planet']} --retries 5", silent=False)
    utils.run_command(f"rclone copyto {pubdir}/index/{names['index']} "
                      f"{dest}/index/{names['index']} --retries 5", silent=False)
    utils.run_command(f"rclone copyto {pubdir}/{names['gti']} {dest}/{names['gti']} --retries 5",
                      silent=False)

    # The staging dir pins the current tile inodes via hardlinks: left behind, a later
    # re-merge strands whole superseded generations as sole-owner copies (~170 GB observed).
    shutil.rmtree(pubdir, ignore_errors=True)

    print(f"mosaic publish: {len(features)} tiles staged, {len(new)} uploaded new, "
          f"{len(features) - len(new)} already present; candidate GTI "
          f"{public_base()}/mosaic/{names['gti']}", flush=True)


def covering_stems(covering_path="store/aggregation/covering.txt"):
    """covering.txt filtered to the BBOX env (empty = every stem) — the ONE scope rule the
    build invocation's parse (STEMS) and `index --stable` share. The covering is the full
    on-disk inventory (write-if-changed keeps out-of-window tiles), so a bbox build must
    scope here, not trust the file's extent."""
    with open(covering_path) as f:
        stems = sorted(f.read().split())
    clip = aggregation_covering.bbox_3857()
    if clip is None:
        return stems
    boxes = aggregation_covering.split_at_antimeridian(clip)

    def hits(stem):
        z, x, y, _cz = (int(a) for a in stem.split("-"))
        b = mercantile.xy_bounds(mercantile.Tile(x=x, y=y, z=z))
        return any(b.left < r and b.right > l and b.bottom < t and b.top > bo
                   for (l, bo, r, t) in boxes)

    return [s for s in stems if hits(s)]


def window_buffer_3857(stem):
    """The stage-3 read buffer in metres: the fixed macrotile buffer, or the smooth halo in metres
    at the stem's child-zoom resolution when that's larger. At coarse zooms the 150 m fixed buffer
    is under a pixel, so a sub-halo window lets the smooth perturb band edges at coarse-tile seams;
    scaling by the halo keeps neighbour truth in reach at every zoom. One source of truth for both
    the window materialize (window_dem) and the fork input tracking (intersecting_tiles)."""
    child_z = int(stem.split("-")[3])
    return max(utils.macrotile_buffer_3857,
               smooth.halo_px() * aggregation_reproject.get_resolution(child_z))


def intersecting_tiles(stem, buffer_3857=None):
    """The covering stems whose tiles intersect this stem's BUFFERED bounds — the stage-3
    windowed-read input set. From the (BBOX-scoped) covering only, never the global GTI:
    a stem's tile jobs depend on their neighborhood, not on every tile."""
    z, x, y, _cz = (int(a) for a in stem.split("-"))
    l, b, r, t = aggregation_reproject.buffered_bounds(
        mercantile.Tile(x=x, y=y, z=z),
        window_buffer_3857(stem) if buffer_3857 is None else buffer_3857)
    out = []
    for s in covering_stems():
        sz, sx, sy, _scz = (int(a) for a in s.split("-"))
        sb = mercantile.xy_bounds(mercantile.Tile(x=sx, y=sy, z=sz))
        if sb.left < r and sb.right > l and sb.bottom < t and sb.top > b:
            out.append(s)
    return out


def window_dem(stem, out_tif):
    """Materialize the stem's BUFFERED window from the plain mosaic tiles — the stage-3 read
    primitive: a throwaway VRT over the intersecting tiles, translated to one bounded raster at the
    stem's native resolution. A coarser neighbor's halo pixels upsample bilinearly (seam_check gates
    the continuity this can perturb)."""
    z, x, y, child_z = (int(a) for a in stem.split("-"))
    l, b, r, t = aggregation_reproject.buffered_bounds(
        mercantile.Tile(x=x, y=y, z=z), window_buffer_3857(stem))
    res = aggregation_reproject.get_resolution(child_z)
    tiles = " ".join(tile_artifact(s) for s in intersecting_tiles(stem))
    vrt = out_tif + ".vrt"
    utils.run_command(f"gdalbuildvrt -overwrite -te {l} {b} {r} {t} -tr {res} {res} "
                      f"-r bilinear {vrt} {tiles}")
    utils.run_command(f"GDAL_CACHEMAX=512 gdal_translate -q -ot Float32 -a_nodata {NODATA} "
                      "-co TILED=YES -co BLOCKSIZE=512 -co COMPRESS=ZSTD -co PREDICTOR=3 "
                      f"{vrt} {out_tif}")
    os.remove(vrt)
    return out_tif


def _stable_stems():
    """The --stable covering scoped to the BBOX env — the stem set index and publish both walk.
    Refuses (naming the catalogs invocation) rather than build nothing."""
    try:
        stems = covering_stems()
    except FileNotFoundError:
        sys.exit("mosaic: no covering — run `snakemake catalogs` first")
    if not stems:
        sys.exit("mosaic: no covering tiles in BBOX — run `snakemake catalogs` first")
    return stems


def build_index_stable():
    """The index + planet z8 + pointer, off the --stable covering (BBOX-scoped like the build
    invocation): plain names throughout. Same artifacts-before-pointer discipline — the .gti is
    written last."""
    stems = _stable_stems()
    csvs = [f"store/aggregation/{s}-aggregation.csv" for s in stems]
    features = [_tile_row_plain(fp) for fp in csvs]
    index_path = f"{index_dir()}/covering.parquet"
    _write_index(index_path, features)
    planet_path = planet_artifact()
    tmp = planet_path + ".tmp"
    _warp_planet(index_path, tmp)
    os.replace(tmp, planet_path)
    child_z = max(int(s.split("-")[3]) for s in stems)
    resolution = aggregation_reproject.get_resolution(child_z)
    _write_gti(index_path, planet_path, resolution)  # pointer LAST
    print(f"mosaic index: {len(features)} tile(s) -> {index_path}; planet z8 {planet_path}; "
          f"pointer {gti_path()} at z{child_z} ({resolution} m)")


def _check():
    """One synthetic aggregation tile: persist its merged DEM to the plain tile COG (Float32,
    nodata -9999, nodata-aware average overviews down to the 512 block), build the
    --stable index + planet z8 + .gti, confirm the .gti opens as one raster with the z8 overview
    and the parquet carries the seascape: provenance columns (no key column), then stage the
    content-addressed publish set and assert its names/refs never touch the serving pointer."""
    import shutil
    import tempfile

    import numpy as np
    from rasterio.transform import from_origin

    saved_dir, cwd = config.SOURCES_DIR, os.getcwd()
    saved_env = {k: os.environ.pop(k, None) for k in
                 ("LANDMASK", "WATERMASK", "MOSAIC_VSI_BASE", "SOURCE_VSI_BASE", "BBOX")}
    d = tempfile.mkdtemp()
    try:
        os.chdir(d)
        config.SOURCES_DIR = "sources"
        config._catalog_cache.clear()
        os.makedirs("sources/gebco")
        with open("sources/gebco/metadata.json", "w") as f:
            json.dump({"name": "gebco", "priority": 0, "max_zoom": 9, "land_clamp": True}, f)
        os.makedirs("sources/reef")
        with open("sources/reef/metadata.json", "w") as f:
            json.dump({"name": "reef", "priority": 5, "max_zoom": 10, "datum": "MLLW"}, f)

        # A z8 covering tile with child_z=10 -> de-buffered 2048px (span=4), 2 overviews to 512.
        z, x, y, child_z = 8, 75, 96, 10
        stem = f"{z}-{x}-{y}-{child_z}"
        os.makedirs("store/aggregation")
        fp = f"store/aggregation/{stem}-aggregation.csv"
        with open(fp, "w") as f:
            f.write("source,filename,maxzoom\ngebco,gebco_0.tif,9\nreef,reef_0.tif,10\n")
        with open("store/aggregation/covering.txt", "w") as f:
            f.write(stem + "\n")

        # Build a synthetic merged DEM at the tile's buffered extent (buffer_pixels=31), Float32,
        # nodata -9999: a valid depth field with a nodata hole, at the exact reproject geotransform.
        buffer_pixels = 31
        res = aggregation_reproject.get_resolution(child_z)
        span = 2 ** (child_z - z)
        core = span * 512
        full = core + 2 * buffer_pixels
        b = mercantile.xy_bounds(mercantile.Tile(x=x, y=y, z=z))
        origin_x = b.left - buffer_pixels * res
        origin_y = b.top + buffer_pixels * res
        arr = np.full((full, full), -20.0, dtype="float32")
        arr[: full // 2, : full // 2] = -50.0
        arr[full - 100:, full - 100:] = NODATA  # a nodata hole
        tmp_folder = f"store/aggregation/{stem}-tmp"
        os.makedirs(tmp_folder)
        with open(f"{tmp_folder}/reprojection.json", "w") as f:
            json.dump({"buffer_pixels": buffer_pixels}, f)
        # two *.tiff so _merged_dem picks 1-3857.tiff (the "merge output")
        for i in (0,):
            with rasterio.open(f"{tmp_folder}/{i}-3857.tiff", "w", driver="GTiff", height=full,
                               width=full, count=1, dtype="float32", nodata=NODATA, crs="EPSG:3857",
                               transform=from_origin(origin_x, origin_y, res, res)) as dst:
                dst.write(np.zeros((full, full), "float32"), 1)
        with rasterio.open(f"{tmp_folder}/1-3857.tiff", "w", driver="GTiff", height=full, width=full,
                           count=1, dtype="float32", nodata=NODATA, crs="EPSG:3857",
                           transform=from_origin(origin_x, origin_y, res, res),
                           tiled=True, blockxsize=512, blockysize=512) as dst:
            dst.write(arr, 1)

        # tile()'s reproject/merge needs real sources, so exercise its persist half directly
        # (_translate + atomic replace — exactly tile()'s tail) and then the stable index.
        os.makedirs(tiles_dir(), exist_ok=True)
        os.replace(_translate(fp, tmp_folder), tile_artifact(stem))
        with rasterio.open(tile_artifact(stem)) as src:
            assert src.dtypes[0] == "float32", src.dtypes
            assert src.nodata == NODATA, src.nodata
            assert (src.width, src.height) == (core, core), (src.width, src.height)
            # exact grid registration: origin == the tile's mercantile bounds (halo cropped)
            assert abs(src.transform.c - b.left) < 1e-3 and abs(src.transform.f - b.top) < 1e-3, src.transform
            ovc = src.overviews(1)
            assert ovc == [2, 4], f"expected native->z8 overviews [2,4], got {ovc}"
            # nodata-aware: the interior -50 quadrant averages to -50 in the coarsest overview (no
            # -9999 / 0 contamination), and the pure-nodata corner stays nodata.
            ov = src.read(1, out_shape=(1, src.height // 4, src.width // 4))
            assert abs(float(ov[10, 10]) - (-50.0)) < 1e-3, ov[10, 10]
            assert float(ov[-1, -1]) == NODATA, ov[-1, -1]

        build_index_stable()
        assert os.path.isfile(planet_artifact()), "the plain-named planet z8 must exist"
        assert os.path.isfile(f"{index_dir()}/covering.parquet")
        # The .gti opens as one raster with the z8 overview registered. Verified via the system
        # gdalinfo — the toolchain the pipeline shells out to — not rasterio's bundled GDAL: the
        # .gti's IndexDataset / Overview are RELATIVE (portable across store prefixes), which GDAL
        # >=3.11 resolves against the .gti's own directory. The check mirrors the real consumer.
        info, _ = utils.run_command(f"gdalinfo {os.path.abspath(gti_path())}")
        assert "Driver: GTI/GDAL Raster Tile Index" in info and f"Size is {core}, {core}" in info, \
            info[:400]
        assert f"Pixel Size = ({res:.15f},-{res:.15f})" in info, info[:500]
        assert "Type=Float32" in info and f"NoData Value={NODATA}" in info, info[:400]
        assert "Overviews:" in info, "the plain planet z8 must register as the mosaic overview"
        out, _ = utils.run_command(f"ogrinfo -q -al {index_dir()}/covering.parquet")
        assert "seascape:key" not in out, "the index carries no key column"
        for col in ("seascape:sources", "seascape:priority", "seascape:maxzoom",
                    "seascape:datum", "seascape:offset"):
            assert col in out, f"index missing column {col}"
        assert "gebco,reef" in out, "provenance columns must survive in the stable index"
        assert '"reef": "MLLW"' in out or 'reef\\":\\"MLLW' in out or "MLLW" in out, "datum must be composited"

        # ── publish: stage content-addressed names LOCALLY, assert names + refs, no
        # network (rclone is never called from _stage_publish; the serving pointer is unreachable) ──
        pub_base = "https://data.example/bathymetry"
        os.environ["PUBLIC_BASE"] = pub_base
        try:
            pubdir, feats, names = _stage_publish()
        finally:
            os.environ.pop("PUBLIC_BASE", None)
        # hardlinks exist under the hashed names — same inode as the plain COGs, no byte copy
        thash = _hash12(tile_artifact(stem))
        staged_tile = f"{pubdir}/tiles/{stem}-{thash}.tif"
        assert os.path.isfile(staged_tile), staged_tile
        assert os.stat(staged_tile).st_ino == os.stat(tile_artifact(stem)).st_ino, "tile must be a hardlink"
        phash = _hash12(planet_artifact())
        staged_planet = f"{pubdir}/planet-z8-{phash}.tif"
        assert os.path.isfile(staged_planet), staged_planet
        assert os.stat(staged_planet).st_ino == os.stat(planet_artifact()).st_ino, "planet must be a hardlink"
        # the candidate index locations carry PUBLIC_BASE + the hashed tile name
        assert [f["properties"]["location"] for f in feats] == [f"/vsicurl/{pub_base}/mosaic/tiles/{stem}-{thash}.tif"], \
            [f["properties"]["location"] for f in feats]
        # the candidate GTI references the PUBLIC index + planet URLs (absolute), not a local path
        gti_xml = open(names["gti_path"]).read()
        assert f"<IndexDataset>/vsicurl/{pub_base}/mosaic/index/{names['index']}</IndexDataset>" in gti_xml, gti_xml
        assert f"<Dataset>/vsicurl/{pub_base}/mosaic/planet-z8-{phash}.tif</Dataset>" in gti_xml, gti_xml
        # the index name is content-addressed by the sorted tile hashes
        exp_loc = f"/vsicurl/{pub_base}/mosaic/tiles/{stem}-{thash}.tif"
        exp_hash = hashlib.sha256("\n".join([exp_loc, pub_base]).encode()).hexdigest()[:12]
        assert names["index"] == exp_hash + ".parquet", names["index"]
        # the serving pointer name is unreachable from this path — nowhere in the staged outputs
        assert names["gti"] == f"mosaic-candidate-{names['idxhash']}.gti" and names["gti"] != SERVING_GTI_NAME
        for root, _dirs, sfiles in os.walk(pubdir):
            assert SERVING_GTI_NAME not in sfiles, f"the serving pointer must never be staged ({root})"
        print("mosaic.py self-check ok")
    finally:
        config.SOURCES_DIR = saved_dir
        config._catalog_cache.clear()
        os.chdir(cwd)
        for kk, vv in saved_env.items():
            if vv is not None:
                os.environ[kk] = vv
        shutil.rmtree(d, ignore_errors=True)


def verify_tiles():
    """Delete unreadable tile COGs and empty render pmtiles (a hard box teardown can strand
    renamed files whose data blocks never flushed); the engine rebuilds anything missing.
    Empty contour/soundings outputs are legitimate dry-tile sentinels — never swept."""
    bad = 0
    for path in sorted(glob.glob(f"{tiles_dir()}/*.tif")):
        try:
            with rasterio.open(path):
                pass
        except rasterio.errors.RasterioIOError:
            print(f"mosaic verify: deleting unreadable {path}", flush=True)
            os.remove(path)
            bad += 1
    for path in sorted(glob.glob("store/pmtiles/*.pmtiles")):
        if os.path.getsize(path) == 0:
            print(f"mosaic verify: deleting empty {path}", flush=True)
            os.remove(path)
            bad += 1
    print(f"mosaic verify: {bad} stranded artifact(s) removed", flush=True)


def main(argv):
    if argv == ["index", "--stable"]:
        build_index_stable()
    elif argv == ["publish"]:
        publish()
    elif argv == ["verify"]:
        verify_tiles()
    elif argv[:1] == ["tile"] and len(argv) == 2:
        tile(argv[1])
    elif argv[:1] == ["--check"]:
        _check()
    else:
        sys.exit("usage: mosaic.py tile <covering-csv> | index --stable | publish | verify | --check")


if __name__ == "__main__":
    main(sys.argv[1:])
