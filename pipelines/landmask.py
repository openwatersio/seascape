"""Land mask — stop coarse sources from painting water over land.

Coarse global sources (GEBCO, EMODnet) carry no land/water distinction: a ~460 m
GEBCO cell straddling the shoreline averages in water and reads *negative on land*,
which downstream renders as a false "water" rim over the coast (and false contours /
soundings). This module builds an OSM land-polygon mask and clamps a flagged coarse
source's warped raster: where the mask says land AND the pixel is negative -> 0
(chart datum at the shoreline). The clamp runs at warp time, on each source in turn,
while every pixel still carries source identity — so only flagged sources are ever
touched and S-102 / CUDEM / lake / river bathymetry cannot be damaged by a land-mask
error. That immunity is now structural, not a promise: the merge seam feather guards
its own sign flips (aggregation_merge), so no second, source-blind clamp runs after
the merge to re-flatten trusted data by geography.

Inland water: the mask subtracts an OSM-derived inland-water polygon layer (rivers,
lakes, canals, reservoirs — not the ocean, which the land polygons already bound) so a
flagged source keeps its genuine negative depths inside mapped water (EMODnet's Elbe
fairway, GEBCO's Amazon channel) instead of flattening them to 0. Worst case inside a
real water polygon is a coarse depth where water truly exists; the old worst case was
land where water exists. Wetlands stay land: they are above-datum terrain, and a
flagged negative there is exactly the shoreline-straddle junk the clamp exists for.

Ceilings. These unclamped depths ship provisional and biased deep — GEBCO is ~MSL,
which sits ~1-3 m above chart datum in the macrotidal estuaries this reopens. Deep
cryptodepression lakes (surface above MSL, bed below — Baikal, Tanganyika, Malawi,
Ladoga, Onega, Great Slave, Great Bear, Vänern) now expose coarse GEBCO "depths"
referenced to nothing a mariner can use; shallow lakes stay sign-inert (beds above MSL
render as land regardless). Real below-sea-level land (Netherlands polders, Death
Valley), inland seas mapped as coastline (Caspian), and negative-surface seas with no
water polygon (Dead Sea, Salton) still flatten to 0 — accepted ceiling, dedicated
lake/river sources are the path if any is ever wanted.

Local layout (store/landmask/): land.fgb — the prepared EPSG:3857 land mask; water.fgb
— the inland-water polygons subtracted from it (optional: absent -> land-only mask,
i.e. today's behavior, no crash). Both are spatial-indexed and HTTP-range friendly.
LANDMASK / WATERMASK override the paths (defaults store/landmask/{land,water}.fgb).

  python landmask.py prep        download -> unzip -> convert the land mask (run once)
  python landmask.py prep-water  download -> convert the inland-water mask (run once)
  python landmask.py --check     self-check
"""

import os
import sys
import zipfile

import numpy as np
import rasterio

import utils

NODATA = -9999

# OSM land polygons (osmdata.openstreetmap.de), ODbL — attribute alongside the other
# sources. The "split" variant is pre-tiled into manageable polygons (better than the
# single giant one) and reprojects/streams cleanly.
LAND_POLYGONS_URL = "https://osmdata.openstreetmap.de/download/land-polygons-split-4326.zip"

# Overture Maps water data (OSM-derived, ODbL like the land polygons) as GeoParquet on
# the public S3 mirror — one dataset read beats an osmium filter over a planet PBF. Water
# lives under theme=base (there is no theme=water). The release string is a dated
# snapshot; bump it to the current Overture release when re-preparing (listing:
# https://docs.overturemaps.org/release/). GDAL opens the partition directory as one
# dataset (~65M features); the bucket is anonymous, so no credentials in the read path.
OVERTURE_RELEASE = "2026-06-17.0"
WATER_PARQUET_URL = (
    f"/vsis3/overturemaps-us-west-2/release/{OVERTURE_RELEASE}/theme=base/type=water/")

# Web-mercator latitude limit: clip the polygons to +/-85.06 deg (also drops the
# Antarctica polygon's polar excess). The warp grids never reach beyond this.
MERC_LAT = 85.06

DEFAULT_LANDMASK = "store/landmask/land.fgb"
DEFAULT_WATERMASK = "store/landmask/water.fgb"


def path():
    """The land-mask path, read from LANDMASK at *call time* (default store/landmask/land.fgb).
    Not an import-time constant, so CI/tests/callers that set LANDMASK after this module is
    imported still take effect — mirrors config.source_path reading SOURCE_VSI_BASE per call."""
    return os.environ.get("LANDMASK", DEFAULT_LANDMASK)


def water_path():
    """The inland-water-mask path, read from WATERMASK at *call time* (default
    store/landmask/water.fgb). Optional: when it points nowhere the mask degrades to land-only.
    Read per call for the same reason path() is — a caller setting WATERMASK after import wins."""
    return os.environ.get("WATERMASK", DEFAULT_WATERMASK)


def require():
    """Preflight: fail early + actionably if a flagged source needs the mask but it's a local
    path that doesn't exist, instead of every aggregation worker dying deep in a run with an
    opaque ogr2ogr error. A /vsi path is assumed reachable (CI publishes it; a genuinely bad
    URL still fails per-tile). Cheap — reads only metadata flags, not the covering."""
    import config
    flagged = [s for s in config.sources() if config.load_metadata(s).get("land_clamp")]
    p = path()
    if flagged and not p.startswith("/vsi") and not os.path.isfile(p):
        raise SystemExit(
            f"land mask {p} not found, but {', '.join(flagged)} need it — "
            "run `just landmask` first (or set LANDMASK to an existing copy)")


def prep():
    """Download the OSM land polygons and convert them once into a single EPSG:3857
    FlatGeobuf at path() (spatial index; streams well over HTTP range requests). Each
    step is guarded so a re-run (a retried preview, a manual re-invoke) is a near no-op
    instead of re-paying a ~1 GB extraction + a whole-planet reprojection."""
    out = path()
    if os.path.isfile(out):
        print(f"land mask already present: {out}")
        return
    folder = "store/landmask"
    utils.create_folder(folder)
    zip_path = f"{folder}/land-polygons-split-4326.zip"
    if not os.path.isfile(zip_path):
        print(f"downloading {LAND_POLYGONS_URL} ...")
        utils.http_download(LAND_POLYGONS_URL, zip_path)
    shp = f"{folder}/land-polygons-split-4326/land_polygons.shp"
    if not os.path.isfile(shp):
        with zipfile.ZipFile(zip_path) as z:  # stdlib — the image has no unzip binary
            z.extractall(folder)
    utils.run_command(
        f"ogr2ogr -f FlatGeobuf -t_srs EPSG:3857 -overwrite "
        f"-clipsrc -180 -{MERC_LAT} 180 {MERC_LAT} {out} {shp}",
        silent=False)
    print(f"land mask ready: {out}")


def prep_water():
    """Build the inland-water mask once: read the Overture water parquet, keep only polygonal
    non-ocean features (rivers/lakes/canals/reservoirs — the ocean is already bounded by the
    land polygons), reproject to EPSG:3857, and write one spatial-indexed FlatGeobuf at
    water_path(). The land clamp's rasterize subtracts it per tile, so a flagged coarse source
    keeps its genuine depths inside mapped water. Guarded so a re-run is a no-op.

    Two passes. The remote pass pulls non-ocean rows with the one filter the parquet reader can
    push down (`subtype <> 'ocean'`); geometry-type predicates (OGR_GEOMETRY / ST_GeometryType)
    silently match nothing through that read path, so dropping the linear river/stream
    centerlines Overture also carries (a burned line would punch a spurious 1-px water gap
    across land) happens in a local pass — SQLite dialect over the temp copy, polygons only.
    AWS_DEFAULT_REGION is the region key GDAL honors, and it must be pinned: a dev/CI profile
    aimed at another S3-compatible store (region "auto") otherwise poisons the bucket hostname.
    The read is anonymous (AWS_NO_SIGN_REQUEST) — the Overture bucket needs no credentials.

    Coverage ceiling: this only reopens water OSM maps as *polygons*. Narrow tidal channels
    mapped as a bare waterway centerline (the ICW's Pablo Creek reach, for one) stay "land" in
    the mask — harmless for the clamp (trusted sources are never clamped) but it gates drying
    there until OSM grows an area or another feed does."""
    out = water_path()
    if os.path.isfile(out):
        print(f"inland-water mask already present: {out}")
        return
    utils.create_folder(os.path.dirname(out) or ".")
    raw = out + ".raw.gpkg"
    try:
        utils.run_command(
            "AWS_NO_SIGN_REQUEST=YES AWS_DEFAULT_REGION=us-west-2 "
            f"ogr2ogr -f GPKG -overwrite -nln water_raw -lco SPATIAL_INDEX=NO "
            "-where \"subtype <> 'ocean'\" "
            f"{raw} {WATER_PARQUET_URL}",
            silent=False)
        utils.run_command(
            f"ogr2ogr -f FlatGeobuf -t_srs EPSG:3857 -overwrite -nln water -dialect SQLITE "
            "-sql \"SELECT geometry FROM water_raw "
            "WHERE GeometryType(geometry) LIKE '%POLYGON%'\" "  # column name from the parquet
            f"-clipsrc -180 -{MERC_LAT} 180 {MERC_LAT} {out} {raw}",
            silent=False)
    finally:
        if os.path.isfile(raw):
            os.remove(raw)
    print(f"inland-water mask ready: {out}")


def _present(p):
    """A mask source is usable if it's a local file that exists or a /vsi path (assumed
    reachable, like require() does — a genuinely bad URL still fails loudly at read time)."""
    return p.startswith("/vsi") or os.path.isfile(p)


def rasterize(bounds_3857, res, out_tif, src=None, water_src=None):
    """Burn the land mask onto a Byte raster (1=land, 0=water) on the given 3857 grid, then
    subtract inland water (burn 0 over land) where a water mask is present.

    Pass the SAME -te/-tr gdalwarp uses for the tile, so the mask aligns pixel-for-pixel
    with the warped raster and neighbouring tiles rasterize their shared halo identically
    (the "buffer the input, restrict the output" seam contract — a bbox-selected polygon
    is burned on the same world grid regardless of which tile's origin). A -spat pre-clip
    into a temp FGB keeps each tile reading only the polygons it touches (FGB spatial index
    -> HTTP range reads) instead of scanning the whole planet mask. Deterministic for a
    given extent/res: -spat selects by bbox intersection, and any polygon covering an
    output pixel has a bbox intersecting the extent, so the burn is complete and stable.
    The clip lands in GPKG (not FlatGeobuf): an open-ocean tile selects zero features, and
    writing an *empty* indexed FGB errors — most tiles are ocean, so that would sink them.

    The water subtraction is a second burn (0) over the finished land raster on the same
    grid: land=1 is already down, and mapped inland water wins where they overlap, so a
    flagged coarse source keeps its genuine negative depths inside rivers/lakes instead of
    clamping them to 0. It uses the same -spat GPKG clip (the empty-selection case — a tile
    with no inland water — leaves an empty layer that gdal_rasterize burns as a clean no-op,
    so the mask stays land-only). When no water mask is present the whole step is skipped and
    the mask degrades to land-only (today's behavior), with no crash and no per-tile warning.
    Known ceiling: deep cryptodepression lakes (surface above MSL, bed below — Baikal,
    Tanganyika, ...) are unclamped here too and expose coarse GEBCO depths referenced to
    nothing a mariner can use; shallow lakes stay sign-inert (beds above MSL render as land).

    The rasterize writes to a temp then atomically renames, so a crash mid-burn never leaves
    a truncated mask at out_tif that a resume would trust (the reproject cache is trusted if
    present). DEFLATE+TILED+SPARSE_OK keeps the mask tiny — a binary mask compresses ~1000:1
    and all-water blocks (most of the planet) become sparse holes.
    """
    src = src or path()
    water = water_src if water_src is not None else water_path()
    xmin, ymin, xmax, ymax = bounds_3857
    clip = out_tif + ".clip.gpkg"
    water_clip = out_tif + ".water.gpkg"
    tmp_out = out_tif + ".tmp.tif"
    try:
        utils.run_command(
            f"ogr2ogr -f GPKG -overwrite -spat {xmin} {ymin} {xmax} {ymax} {clip} {src}")
        utils.run_command(
            f"gdal_rasterize -burn 1 -ot Byte -init 0 -te {xmin} {ymin} {xmax} {ymax} "
            f"-tr {res} {res} -co COMPRESS=DEFLATE -co TILED=YES -co SPARSE_OK=YES "
            f"{clip} {tmp_out}")
        if _present(water):
            utils.run_command(
                f"ogr2ogr -f GPKG -overwrite -spat {xmin} {ymin} {xmax} {ymax} {water_clip} {water}")
            utils.run_command(f"gdal_rasterize -burn 0 {water_clip} {tmp_out}")
        os.replace(tmp_out, out_tif)  # atomic: out_tif only ever exists complete
    finally:
        for f in (clip, water_clip, tmp_out):
            if os.path.isfile(f):
                os.remove(f)


def _clamp_negative_land(dem_path, mask_tif, valid):
    """Shared windowed clamp: where land (mask==1) AND valid(ds, win, a) AND value<0, set 0.
    Mask-first — read the cheap Byte mask window and skip landless blocks before decoding the
    DEM/alpha, so a mostly-water tile is a mask-only scan. GDAL_CACHEMAX-bounded like
    contains_nodata_pixels (the block cache these windowed reads accumulate, per worker)."""
    with rasterio.open(mask_tif) as m:
        mask_shape = (m.height, m.width)
    block = 2048
    with rasterio.env.Env(GDAL_CACHEMAX=64):
        with rasterio.open(dem_path, "r+", IGNORE_COG_LAYOUT_BREAK="YES") as ds, \
                rasterio.open(mask_tif) as m:
            if (ds.height, ds.width) != mask_shape:
                raise ValueError(
                    f"land mask {mask_shape} != raster {(ds.height, ds.width)} for {dem_path} "
                    "(rasterize -te/-tr must match the warp)")
            for row in range(0, ds.height, block):
                for col in range(0, ds.width, block):
                    win = rasterio.windows.Window(
                        col, row, min(block, ds.width - col), min(block, ds.height - row))
                    land = m.read(1, window=win) == 1
                    if not land.any():
                        continue
                    a = ds.read(1, window=win)
                    hit = land & valid(ds, win, a) & (a < 0)
                    if hit.any():
                        a[hit] = 0
                        ds.write(a, 1, window=win)


def clamp(cog_path, mask_tif):
    """In-place band-1 clamp on a flagged source's warped COG: valid ^ land ^ value<0 -> 0.

    Validity is the ADD_ALPHA mask (same COG-write pattern as negate_band1 —
    IGNORE_COG_LAYOUT_BREAK, read_masks so alpha/nodata pixels stay untouched; don't compare
    to a NODATA value, ADD_ALPHA moves the mask off the data band). Clamp to 0.0, not nodata:
    Terrarium has no transparency, 0 = chart datum at the shoreline, and smooth/contour/soundings
    all gate on < 0, so a clamped land pixel drops out of every downstream negative-only path."""
    _clamp_negative_land(cog_path, mask_tif,
                         lambda ds, win, a: ds.read_masks(1, window=win) != 0)


def _check():
    """Clamp semantics on the REAL pipeline file (a COG with an ADD_ALPHA mask band, like
    negate_band1's check), the inland-water subtraction (water burns land back to water, an
    absent water file degrades to land-only), and rasterize seam determinism."""
    import json
    import tempfile

    import aggregation_reproject  # translate() -> the real COG profile
    from rasterio.transform import from_origin

    d = tempfile.mkdtemp()

    # A land box in 4326 (lon/lat), reprojected to 3857 like prep does.
    gj = f"{d}/land.geojson"
    with open(gj, "w") as f:
        json.dump({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {},
                   "geometry": {"type": "Polygon", "coordinates":
                                [[[1.0, 1.0], [2.0, 1.0], [2.0, 2.0], [1.0, 2.0], [1.0, 1.0]]]}}]}, f)
    land_fgb = f"{d}/land.fgb"
    utils.run_command(f"ogr2ogr -f FlatGeobuf -t_srs EPSG:3857 -overwrite {land_fgb} {gj}")

    # An inland-water box wholly inside the land box (a lake/river inside the coastline).
    wgj = f"{d}/water.geojson"
    with open(wgj, "w") as f:
        json.dump({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {},
                   "geometry": {"type": "Polygon", "coordinates":
                                [[[1.4, 1.4], [1.6, 1.4], [1.6, 1.6], [1.4, 1.6], [1.4, 1.4]]]}}]}, f)
    water_fgb = f"{d}/water.fgb"
    utils.run_command(f"ogr2ogr -f FlatGeobuf -t_srs EPSG:3857 -overwrite {water_fgb} {wgj}")
    absent = f"{d}/no-such-water.fgb"  # never created — the degrade-to-land-only baseline

    # A grid spanning the box + margin (lon/lat 0.5..2.5), ~1 km pixels.
    from rasterio.warp import transform_bounds
    xmin, ymin, xmax, ymax = transform_bounds("EPSG:4326", "EPSG:3857", 0.5, 0.5, 2.5, 2.5)
    res = (xmax - xmin) / 240
    te = (xmin, ymin, xmax, ymax)

    # Land-only baseline (absent water -> no subtraction). Pass an explicit non-existent water
    # path so the baseline is hermetic even if a real water.fgb sits in the dev store.
    mask_tif = f"{d}/mask.tif"
    rasterize(te, res, mask_tif, src=land_fgb, water_src=absent)
    with rasterio.open(mask_tif) as m:
        land, tr, h, w = m.read(1), m.transform, m.height, m.width
    assert land.any() and (land == 0).any(), "mask must have both land and water pixels"

    # A DEM on the mask grid: -5 everywhere, one positive patch + one nodata cell inside land.
    ys, xs = np.where(land == 1)
    wy, wx = np.where(land == 0)
    dem = np.full((h, w), -5.0, dtype="float32")
    dem[ys[0], xs[0]] = 3.0            # positive inside land -> must survive
    dem[ys[1], xs[1]] = NODATA         # nodata inside land   -> must stay masked
    src_tif = f"{d}/dem.tif"
    with rasterio.open(src_tif, "w", driver="GTiff", height=h, width=w, count=1, dtype="float32",
                       nodata=NODATA, crs="EPSG:3857", transform=tr) as dst:
        dst.write(dem, 1)
    cog = f"{d}/dem_cog.tif"
    aggregation_reproject.translate(src_tif, cog)  # -of COG -co ADD_ALPHA, exactly as reproject()
    clamp(cog, mask_tif)

    with rasterio.open(cog) as r:
        out, valid = r.read(1), (r.read_masks(1) != 0)
    assert out[ys[2], xs[2]] == 0.0, "negative under land must clamp to 0"
    assert out[ys[0], xs[0]] == 3.0, "positive under land must survive"
    assert not valid[ys[1], xs[1]], "nodata under land must stay masked (untouched)"
    assert out[wy[0], wx[0]] == -5.0, "negative over water must survive"

    # Inland-water subtraction: the water box (inside the land box) burns land back to water,
    # so a flagged source keeps its depths there. The second burn only ever turns land->water
    # (0 wins over 1 where they overlap), never the reverse, and only inside the land box.
    mask_water = f"{d}/mask_water.tif"
    rasterize(te, res, mask_water, src=land_fgb, water_src=water_fgb)
    with rasterio.open(mask_water) as m:
        lw = m.read(1)
    subtracted = (land == 1) & (lw == 0)
    assert subtracted.any(), "an inland-water polygon must burn land pixels back to water"
    changed = lw != land
    assert (land[changed] == 1).all() and (lw[changed] == 0).all(), \
        "the water burn must only turn land->water, never water->land"
    assert not ((land == 0) & (lw == 1)).any(), "the water burn must not create land over water"

    # Seam determinism (with the water burn active): the same world cells rasterize identically
    # from a shifted (still grid-aligned) extent — the overlap is byte-identical, so tile halos
    # clamp the same whether or not the water subtraction moved a pixel.
    shift = 10
    te_b = (xmin + shift * res, ymin, xmax + shift * res, ymax)
    mask_b = f"{d}/mask_b.tif"
    rasterize(te_b, res, mask_b, src=land_fgb, water_src=water_fgb)
    with rasterio.open(mask_b) as m:
        lw_b = m.read(1)
    assert np.array_equal(lw[:, shift:], lw_b[:, :w - shift]), "seam not deterministic across extents"

    # Open-ocean tile: -spat selects zero land AND zero water polygons → an all-water mask, no
    # error (an empty FlatGeobuf clip would raise, sinking every ocean tile — hence the GPKG
    # clip; and gdal_rasterize over the empty water clip is a clean no-op).
    ocean = f"{d}/ocean.tif"
    rasterize((xmax + 1e6, ymin, xmax + 1e6 + 200 * res, ymax), res, ocean,
              src=land_fgb, water_src=water_fgb)
    with rasterio.open(ocean) as m:
        assert m.read(1).sum() == 0, "ocean extent must rasterize to all-water"

    print(f"landmask.py self-check ok (mask {h}x{w}, land {int(land.sum())}, "
          f"water-subtracted {int(subtracted.sum())})")


if __name__ == "__main__":
    if sys.argv[1:2] == ["prep"]:
        prep()
    elif sys.argv[1:2] == ["prep-water"]:
        prep_water()
    elif sys.argv[1:2] == ["--check"]:
        _check()
    else:
        sys.exit("usage: landmask.py prep | prep-water | --check")
