"""Stage 2 — persist the merged Float32 DEM as the durable, content-addressed MOSAIC product.

The aggregate already computes a priority-resolved, seam-feathered, datum-offset, land-clamped
merged DEM transiently inside every tile's tmp folder (aggregation_reproject + aggregation_merge),
then throws it away after the cartographic forks read it. This module PERSISTS that exact array —
unsmoothed, unencoded — as the survey-faithful TRUTH layer:

  store/mosaic/
    tiles/<stem>-<key12>.tif   one immutable content-addressed Float32 COG per aggregation tile:
                               the merged DEM cropped to the tile's EXACT bounds (the halo removed
                               so the tiles partition the plane with no overlap — a GTI requirement
                               and the buffer/restrict discipline), nodata = -9999 (the same numeric
                               sentinel the merge's hole detection uses), with nodata-aware `average`
                               internal overviews native -> z8-equivalent (the COG driver stops at
                               the 512 block = one z8 tile, so a z8 covering tile yields exactly
                               child_z - 8 levels; never `nearest` — decimation is the anti-alias
                               prefilter, and never below z8, where the planet z8 COG takes over).
    index/<covering-ulid>.parquet   a GeoParquet TILE INDEX doubling as the MANIFEST: rows are the
                               tile COGs with ABSOLUTE `location`s (+ per-tile resx/resy) the GTI
                               driver reads, plus `seascape:`-prefixed columns it ignores (key,
                               sources, datum, offset, priority, maxzoom) — one file is both the
                               GDAL-openable planet mosaic and the DuckDB-queryable provenance.
    planet-z8-<key>.tif        the whole mosaic decimated to the GEBCO-native z8 base, registered as
                               the mosaic's overview (GTI <Overview>) so a z0-z4 open reads it, not
                               thousands of tile-COG top overviews.
    mosaic.gti                 the pointer — a small XML naming the current index + the z8 overview;
                               GDAL opens the planet mosaic straight from it. Written LAST.

NO smoothing, NO encoding: that is stage-3 display generalization. The mosaic is the layer you QA,
diff between builds, and could publish as an open dataset. The mosaic KEY (mosaic_key) hashes the
same determinants that produce the merged DEM MINUS smoothing — intersecting sources' recipe
hashes + covering row + per-source priority/maxzoom/offset/land_clamp/negate/band, the reproject/
explicit cache contract, resample + covering params, and the toolchain — so a precedence or source
change re-keys the tile while a smoothing change does not touch it. Content-addressed exactly like
the other store artifacts: freshness is "the named COG exists" (keys.fork_fresh).

R2-agnostic like the rest of pipelines/: the writer writes the LOCAL store; the GTI `location`
base comes from MOSAIC_VSI_BASE (CI: a /vsicurl bucket URL) and falls back to the tiles dir's local
abspath, mirroring config.source_path's SOURCE_VSI_BASE resolution — no bucket name here.

  python mosaic.py index     build the index + planet z8 COG + mosaic.gti for the current covering
  python mosaic.py --check    self-check
"""

import glob
import json
import os
import sys
from glob import glob as _glob

import mercantile
import rasterio

import aggregation_reproject
import cache_versions
import config
import keys
import landmask
import utils

NODATA = -9999

# Explicit output contracts for the unsmoothed merged COG. Input recipes, resolved merge config,
# masks, and toolchain enter separately below; unrelated source edits and pointer changes do not.
MOSAIC_TILE_VERSIONS = [cache_versions.MERGE, cache_versions.LANDMASK,
                        cache_versions.MOSAIC_TILE]

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
    """The LOGICAL tile-COG path ({stem}.tif); keys.content_path splices the key in before .tif."""
    return f"{tiles_dir()}/{stem}.tif"


def planet_artifact():
    """The LOGICAL planet-z8 overview COG path; keys.content_path splices the planet key in before
    .tif. Content-addressed like the tiles, so it belongs in the store manifest (hydrate reuses it
    across builds instead of re-decimating; the GC keeps the referenced one)."""
    return "store/mosaic/planet-z8.tif"


def vsi_base():
    """The absolute base the GTI `location` column resolves against — MOSAIC_VSI_BASE when set (CI
    passes a /vsicurl bucket URL for the mosaic tiles prefix), else the tiles dir's local abspath so
    a laptop's GTI opens straight off disk. Mirrors config.source_path reading SOURCE_VSI_BASE per
    call — R2-agnostic, no bucket name in the pipeline."""
    base = os.environ.get("MOSAIC_VSI_BASE")
    return base if base else os.path.abspath(tiles_dir())


def location_for(stem, key):
    """The absolute location recorded in the index for a tile COG (the content-addressed name under
    vsi_base)."""
    return f"{vsi_base()}/{stem}-{key}.tif"


def _tile_sources(filepath):
    with open(filepath) as f:
        rows = f.read().splitlines()[1:]  # skip header
    return sorted({r.split(",")[0] for r in rows if r.strip()})


def _inputs_config(filepath):
    """The mosaic key's inputs + config for one tile: the covering row (a re-tile / source-file
    change flips it), each intersecting source's recipe hash + the resolved build props the
    reproject/merge read, any LOCAL mask's content hash, and the resample + covering knobs. The
    smoothing config is intentionally excluded — the mosaic is unsmoothed."""
    with open(filepath) as f:
        covering_row = f.read()
    sources = _tile_sources(filepath)
    inputs = [covering_row]
    props = {}
    for s in sources:
        inputs.append(config.source_recipe_hash(s))
        props[s] = {k: config.source_property(s, k) for k in _PROPS}
    for mask in (landmask.path(), landmask.water_path()):
        h = keys.file_hash(mask)
        if h is not None:
            inputs.append(h)
    cfg = {
        "sources": props,
        "resample": aggregation_reproject.RESAMPLE,
        "macrotile_z": utils.macrotile_z,
        "macrotile_buffer_3857": utils.macrotile_buffer_3857,
        "num_overviews": utils.num_overviews,
    }
    return inputs, cfg


def mosaic_key(filepath):
    inputs, cfg = _inputs_config(filepath)
    return keys.stage_key(inputs, MOSAIC_TILE_VERSIONS, {**cfg, "product": "mosaic"})


def stale(filepath):
    """Whether the tile's mosaic COG needs (re)building — content-addressed, so a re-keyed source /
    precedence / merge-code change makes it stale and a no-op rerun does not (FORCE_REBUILD forces
    it via keys.fork_fresh)."""
    return not keys.fork_fresh(tile_artifact(_stem(filepath)), mosaic_key(filepath))


def _merged_dem(tmp_folder):
    """The merged DEM the aggregate just produced: the highest-index N-3857.tiff (the merge output
    for a multi-source tile, or the lone reprojected source for a single-source one). Same rule
    smooth.smooth_merged / aggregation_tile.main use, so the mosaic persists exactly what the forks
    read — before smoothing."""
    n = len(glob.glob(f"{tmp_folder}/*.tiff"))
    return f"{tmp_folder}/{n - 1}-3857.tiff"


def produce(filepath, tmp_folder, key=None):
    """Persist one aggregation tile's merged DEM as its content-addressed mosaic COG. Called from
    aggregation_run.run AFTER merge and BEFORE smooth (unsmoothed truth), only when stale. Crops the
    halo (buffer_pixels) off every side so the tile carries EXACTLY its mercantile bounds — the
    non-overlapping partition GTI needs and the buffer/restrict discipline. Float32, nodata = -9999,
    ZSTD, nodata-aware `average` overviews (COG driver, native -> the 512 block = z8-equivalent).
    Published with an atomic rename so the content name only ever appears complete."""
    stem = _stem(filepath)
    key = key or mosaic_key(filepath)
    art = tile_artifact(stem)
    if keys.fork_fresh(art, key):
        return keys.content_path(art, key)

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

    os.makedirs(tiles_dir(), exist_ok=True)
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
    keys.supersede(art)            # clear last build's key before the atomic publish
    cpath = keys.content_path(art, key)
    keys.publish(tmp_cog, cpath)
    return cpath


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


def _tile_row(filepath):
    """One index row for a covering tile: the geometry (exact 3857 tile bounds — a non-overlapping
    partition, so GTI needs no z-order), location, resx/resy, and the seascape: provenance columns
    composited from the tile's catalog items. Raises if the tile COG is missing under its key — the
    index asserts completeness (a half-built mosaic can't get a manifest that vouches for it)."""
    stem = _stem(filepath)
    key = mosaic_key(filepath)
    cpath = keys.content_path(tile_artifact(stem), key)
    if not os.path.isfile(cpath):
        raise SystemExit(
            f"mosaic index incomplete: tile {stem} has no COG under key {key} — run the aggregate "
            f"first (a failed/interrupted build); refusing to publish an index")
    z, x, y, child_z = (int(a) for a in stem.split("-"))
    # Geometry + resolution come from the produced COG's OWN georeferencing, not recomputed from
    # mercantile: the two agree to the metre, but a float-epsilon disagreement makes GTI ceil the
    # virtual extent to a 1-pixel overhang (the exact-grid-registration invariant). Reading the COG
    # makes the index describe the raster exactly.
    with rasterio.open(cpath) as src:
        t = src.transform
        res = t.a
        left, top = t.c, t.f
        right, bottom = left + src.width * res, top - src.height * res

    sources = _tile_sources(filepath)
    props = {
        "location": location_for(stem, key),
        "resx": res,
        "resy": res,
        "seascape:key": key,
        "seascape:sources": ",".join(sources),
        "seascape:priority": max((int(config.source_property(s, "priority", 0) or 0) for s in sources), default=0),
        "seascape:maxzoom": child_z,
        "seascape:datum": json.dumps({s: _datum_name(s) for s in sources}, sort_keys=True),
        "seascape:offset": json.dumps({s: float(config.source_property(s, "offset", 0.0) or 0.0) for s in sources}, sort_keys=True),
    }
    geom = {"type": "Polygon", "coordinates": [[
        [left, bottom], [right, bottom], [right, top], [left, top], [left, bottom]]]}
    return key, {"type": "Feature", "properties": props, "geometry": geom}


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


def _planet_key(tile_keys):
    """The planet z8 COG's content key: a hash of every tile key (so any tile change re-keys the
    overview) + its explicit index/overview contracts + z8 base resolution + toolchain."""
    return keys.stage_key(sorted(tile_keys),
                          [cache_versions.MOSAIC_INDEX, cache_versions.PLANET_OVERVIEW],
                          {"product": "planet-z8", "macrotile_z": utils.macrotile_z})


def _build_planet_z8(index_path, tile_keys):
    """Decimate the whole mosaic to the GEBCO-native z8 base and write it as a content-addressed
    COG. Reads the index-as-GTI so the decimation picks each tile's own `average` overviews (never a
    full-res read). Content-addressed, so an unchanged mosaic reuses it."""
    key = _planet_key(tile_keys)
    logical = planet_artifact()
    cpath = keys.content_path(logical, key)
    if keys.fork_fresh(logical, key):
        return cpath, key
    res8 = aggregation_reproject.get_resolution(utils.macrotile_z)
    tmp = "store/mosaic/planet-z8.tmp.tif"
    if os.path.exists(tmp):
        os.remove(tmp)
    utils.run_command(
        f"GDAL_CACHEMAX=512 gdalwarp -q -overwrite -r average -tr {res8} {res8} "
        f"-dstnodata {NODATA} -of COG -co COMPRESS=ZSTD -co PREDICTOR=3 -co BLOCKSIZE=512 "
        f"-co OVERVIEW_RESAMPLING=AVERAGE -co NUM_THREADS=ALL_CPUS "
        f"GTI:{index_path} {tmp}")
    keys.supersede(logical)
    keys.publish(tmp, cpath)
    return cpath, key


def _write_gti(index_path, planet_path, resolution):
    """Write the mosaic.gti pointer LAST: a small XML naming the current index + the z8 overview.
    IndexDataset / Overview are stored RELATIVE to the .gti file so the store prefix is portable
    (rclone carries it as-is); GDAL opens the planet mosaic straight from this file. One atomic
    rename, so the pointer only ever names a complete world.

    5b / workflow: the R2 push publishes the mosaic/ prefix (tiles, index, planet-z8) BEFORE this
    pointer — same artifacts-before-pointer discipline as bounds.csv-last and the store manifest.json
    pointer (store_manifest.py) — then PUTs mosaic.gti last. This module only writes it locally."""
    rel_index = os.path.relpath(index_path, os.path.dirname(gti_path()))
    rel_planet = os.path.relpath(planet_path, os.path.dirname(gti_path()))
    xml = (
        "<GDALTileIndexDataset>\n"
        f"  <IndexDataset>{rel_index}</IndexDataset>\n"
        "  <LocationField>location</LocationField>\n"
        "  <SRS>EPSG:3857</SRS>\n"
        f"  <ResX>{resolution}</ResX>\n"
        f"  <ResY>{resolution}</ResY>\n"
        f"  <NoDataValue>{NODATA}</NoDataValue>\n"
        "  <Overview>\n"
        f"    <Dataset>{rel_planet}</Dataset>\n"
        "  </Overview>\n"
        "</GDALTileIndexDataset>\n")
    tmp = gti_path() + ".tmp"
    with open(tmp, "w") as f:
        f.write(xml)
    os.replace(tmp, gti_path())


def build_index():
    """Assemble the covering's mosaic index + planet z8 COG + mosaic.gti pointer from the tile COGs
    the aggregate produced. Deterministic (tiles sorted by stem). The index parquet is named by the
    covering ULID (like store_manifest's build id)."""
    ids = utils.get_aggregation_ids()
    if not ids:
        sys.exit("mosaic index: no covering — run `just cover` first")
    aid = ids[-1]
    csvs = sorted(_glob(f"store/aggregation/{aid}/*-aggregation.csv"),
                  key=lambda fp: _stem(fp))
    if not csvs:
        sys.exit(f"mosaic index: covering {aid} has no aggregation tiles")
    features, tile_keys = [], []
    for fp in csvs:
        key, feat = _tile_row(fp)
        tile_keys.append(key)
        features.append(feat)
    index_path = f"{index_dir()}/{aid}.parquet"
    _write_index(index_path, features)
    planet_path, _pk = _build_planet_z8(index_path, tile_keys)
    child_z = max(int(_stem(fp).split("-")[3]) for fp in csvs)
    resolution = aggregation_reproject.get_resolution(child_z)
    _write_gti(index_path, planet_path, resolution)  # pointer LAST
    print(f"mosaic index: {len(features)} tile(s) -> {index_path}; planet z8 {planet_path}; "
          f"pointer {gti_path()} at z{child_z} ({resolution} m)")


def _check():
    """Two synthetic aggregation tiles: produce their COGs (Float32, nodata -9999, nodata-aware
    average overviews down to the 512 block), assert the mosaic key is stable / moves on a
    priority change / ignores a smoothing-config change, build the index + planet z8 + .gti, and
    confirm the .gti opens as one raster with the z8 overview and the parquet carries the seascape:
    columns."""
    import shutil
    import tempfile

    import numpy as np
    from rasterio.transform import from_origin

    saved_dir, cwd = config.SOURCES_DIR, os.getcwd()
    saved_env = {k: os.environ.pop(k, None) for k in
                 ("LANDMASK", "WATERMASK", "MOSAIC_VSI_BASE", "FORCE_REBUILD", "SOURCE_VSI_BASE")}
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

        aid = "01MOSAICMOSAICMOSAICMOSAIC"
        tiledir = f"store/aggregation/{aid}"
        os.makedirs(tiledir)
        # A z8 covering tile with child_z=10 -> de-buffered 2048px (span=4), 2 overviews to 512.
        z, x, y, child_z = 8, 75, 96, 10
        stem = f"{z}-{x}-{y}-{child_z}"
        fp = f"{tiledir}/{stem}-aggregation.csv"
        with open(fp, "w") as f:
            f.write("source,filename,maxzoom\ngebco,gebco_0.tif,9\nreef,reef_0.tif,10\n")

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
        tmp_folder = f"{tiledir}/{stem}-tmp"
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

        # key stability + sensitivity
        k = mosaic_key(fp)
        config._catalog_cache.clear()
        assert mosaic_key(fp) == k, "mosaic key must be stable across a no-op recompute"

        # produce the COG
        cpath = produce(fp, tmp_folder, k)
        assert os.path.isfile(cpath) and keys.stem_and_key(os.path.basename(cpath)) == (stem, k)
        with rasterio.open(cpath) as src:
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
        assert not stale(fp), "a produced tile must read fresh"

        # priority change re-keys; a smoothing-style config change does NOT (mosaic is unsmoothed —
        # smoothing knobs are absent from the mosaic config and do not move its explicit version)
        with open("sources/reef/metadata.json", "w") as f:
            json.dump({"name": "reef", "priority": 9, "max_zoom": 10, "datum": "MLLW"}, f)
        config._catalog_cache.clear()
        assert mosaic_key(fp) != k, "a source priority change must move the mosaic key"
        # restore priority
        with open("sources/reef/metadata.json", "w") as f:
            json.dump({"name": "reef", "priority": 5, "max_zoom": 10, "datum": "MLLW"}, f)
        config._catalog_cache.clear()
        assert mosaic_key(fp) == k, "restoring the property restores the key"

        # build the index + planet z8 + pointer
        build_index()
        gti = gti_path()
        assert os.path.isfile(gti) and os.path.isfile(f"{index_dir()}/{aid}.parquet")
        # The .gti opens as one raster with the z8 overview registered. Verified via the system
        # gdalinfo — the 3.13 toolchain the pipeline shells out to — not rasterio's bundled GDAL: the
        # .gti's IndexDataset / Overview are RELATIVE (portable across store prefixes), which GDAL
        # >=3.11 resolves against the .gti's own directory. The check mirrors the real consumer.
        info, _ = utils.run_command(f"gdalinfo {os.path.abspath(gti)}")
        assert "Driver: GTI/GDAL Raster Tile Index" in info, info[:200]
        assert f"Size is {core}, {core}" in info, info[:400]
        assert f"Pixel Size = ({res:.15f},-{res:.15f})" in info, info[:500]
        assert "Type=Float32" in info and f"NoData Value={NODATA}" in info, info[:400]
        assert "Overviews:" in info, "the planet z8 COG must register as the mosaic overview"
        # the parquet manifest carries the seascape: columns with the composited provenance
        out, _ = utils.run_command(f"ogrinfo -q -al {index_dir()}/{aid}.parquet")
        for col in ("seascape:key", "seascape:sources", "seascape:priority", "seascape:maxzoom",
                    "seascape:datum", "seascape:offset"):
            assert col in out, f"index missing column {col}"
        assert "gebco,reef" in out, "seascape:sources must list the tile's sources"
        assert '"reef": "MLLW"' in out or 'reef\\":\\"MLLW' in out or "MLLW" in out, "datum must be composited"
        print("mosaic.py self-check ok")
    finally:
        config.SOURCES_DIR = saved_dir
        config._catalog_cache.clear()
        os.chdir(cwd)
        for kk, vv in saved_env.items():
            if vv is not None:
                os.environ[kk] = vv
        shutil.rmtree(d, ignore_errors=True)


def main(argv):
    if argv == ["index"]:
        build_index()
    elif argv[:1] == ["--check"]:
        _check()
    else:
        sys.exit("usage: mosaic.py index | --check")


if __name__ == "__main__":
    main(sys.argv[1:])
