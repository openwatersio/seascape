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

Inverse clamp (#24): the mirror bug is fabrication, not erasure — where a coarse source
holds no lake bathymetry it leaves stale land topo, so an unsurveyed lake reads positive
(+15..+135 m over Lake Huron) and the >= 0 ramp paints it tan. clamp_positive_water clears
that to nodata on a flagged source's warped COG wherever mapped inland water is positive,
so the merge fills it to 0 (flat, unshaded) and the vector nodata depth-area renders the
honest unknown-depth water. It keys on a WATER-ONLY raster (rasterize_water), never the
combined mask: ocean and lake are both 0 there, so a positive->nodata over the combined 0
would punch holes into the coastal ocean. Positive-only — a below-datum cryptodepression
bed (negative in water) is left for the depth path, not nodata'd.

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
— the inland-water polygons subtracted from it, each carrying its Overture `kind`
(river/lake/canal/reservoir) for the depare nodata layer (optional: absent -> land-only
mask, i.e. today's behavior, no crash). Both are spatial-indexed and HTTP-range friendly.
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

    Each feature keeps its Overture `subtype` as `kind` (river/lake/canal/reservoir): the
    rasterize burns geometry only (so Part 2's clamp is unaffected), but the depare nodata layer
    passes `kind` through to label unknown-depth water. Two passes. The remote pass pulls
    non-ocean rows with the one filter the parquet reader can push down (`subtype <> 'ocean'`);
    geometry-type predicates (OGR_GEOMETRY / ST_GeometryType) silently match nothing through that
    read path, so dropping the linear river/stream centerlines Overture also carries (a burned
    line would punch a spurious 1-px water gap across land) happens in a local pass — SQLite
    dialect over the temp copy, polygons only.
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
            "-sql \"SELECT geometry, subtype AS kind FROM water_raw "
            "WHERE GeometryType(geometry) LIKE '%POLYGON%'\" "  # geometry/subtype names from the parquet
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


def rasterize_water(bounds_3857, res, out_tif, water_src=None):
    """Burn ONLY the inland-water polygons onto a Byte raster (1=inland water, 0=elsewhere) on
    the given 3857 grid — the key for the #24 inverse clamp. This is deliberately NOT the combined
    land mask: there ocean and lake are both 0, so a positive->nodata clamp keyed on it would punch
    nodata holes into the coastal ocean wherever a shoreline-straddling coarse cell reads slightly
    positive. Keying on water==1 touches only mapped inland water, unambiguously the #24 case.

    Same -te/-tr seam contract and -spat GPKG clip as rasterize (pass the warp's exact grid; the
    GPKG clip's empty-selection case — an ocean tile with no inland water — burns a clean all-0
    no-op, where an empty FGB would raise). Atomic temp->rename, DEFLATE+TILED+SPARSE_OK. The caller
    gates on the water feed being present (landmask._present), so this always burns when invoked."""
    water = water_src if water_src is not None else water_path()
    xmin, ymin, xmax, ymax = bounds_3857
    clip = out_tif + ".clip.gpkg"
    tmp_out = out_tif + ".tmp.tif"
    try:
        utils.run_command(
            f"ogr2ogr -f GPKG -overwrite -spat {xmin} {ymin} {xmax} {ymax} {clip} {water}")
        utils.run_command(
            f"gdal_rasterize -burn 1 -ot Byte -init 0 -te {xmin} {ymin} {xmax} {ymax} "
            f"-tr {res} {res} -co COMPRESS=DEFLATE -co TILED=YES -co SPARSE_OK=YES "
            f"{clip} {tmp_out}")
        os.replace(tmp_out, out_tif)
    finally:
        for f in (clip, tmp_out):
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


def clamp_positive_water(cog_path, water_tif):
    """Inverse per-source clamp (#24), sibling of clamp/_clamp_negative_land mirrored to the
    positive side: on a flagged source's warped COG, where inland water (water==1) AND a valid
    pixel is > 0, set it to nodata. A coarse global source carries no lake bathymetry, so over an
    unsurveyed lake it leaves stale positive land topo the >= 0 ramp paints tan; nodata (NOT 0 —
    0 renders identically as shoreline land) removes the fabricated signal so the merge fills it
    to 0 and Part 3's nodata depth-area renders the honest unknown-depth water. Positive-only: a
    below-datum cryptodepression bed (negative in water) is untouched, kept on the depth path.

    Same windowed, mask-first, GDAL_CACHEMAX-bounded scan as _clamp_negative_land, but keyed on
    the water-only raster (rasterize_water), never the combined land mask. `nodata` here means
    both, so every downstream reader agrees: band 1 set to NODATA — the merge keys on the value
    (np.nan_to_num(read(1)) == NODATA) and fills it to 0, and contains_nodata_pixels sees it too;
    AND, where translate added an ADD_ALPHA band, the alpha cleared to 0 so read_masks agrees
    (this pipeline's COG carries a nodata value and derives read_masks from it, so setting the
    value already flips the mask; clearing alpha too is correct on either GDAL's layout, like
    negate_band1/clamp read the mask off whichever band GDAL chose)."""
    from rasterio.enums import ColorInterp
    with rasterio.open(water_tif) as m:
        water_shape = (m.height, m.width)
    block = 2048
    with rasterio.env.Env(GDAL_CACHEMAX=64):
        with rasterio.open(cog_path, "r+", IGNORE_COG_LAYOUT_BREAK="YES") as ds, \
                rasterio.open(water_tif) as m:
            if (ds.height, ds.width) != water_shape:
                raise ValueError(
                    f"water mask {water_shape} != raster {(ds.height, ds.width)} for {cog_path} "
                    "(rasterize_water -te/-tr must match the warp)")
            alpha = (ds.colorinterp.index(ColorInterp.alpha) + 1
                     if ColorInterp.alpha in ds.colorinterp else None)
            for row in range(0, ds.height, block):
                for col in range(0, ds.width, block):
                    win = rasterio.windows.Window(
                        col, row, min(block, ds.width - col), min(block, ds.height - row))
                    water = m.read(1, window=win) == 1
                    if not water.any():
                        continue
                    a = ds.read(1, window=win)
                    valid = ds.read_masks(1, window=win) != 0
                    hit = water & valid & (a > 0)
                    if hit.any():
                        a[hit] = NODATA
                        ds.write(a, 1, window=win)
                        if alpha is not None:
                            al = ds.read(alpha, window=win)
                            al[hit] = 0
                            ds.write(al, alpha, window=win)


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

    # An inland-water box wholly inside the land box (a lake/river inside the coastline). It
    # carries a `kind` like the real water.fgb (prep_water keeps the Overture subtype); the burn
    # is geometry-only, so this rides along untouched — Part 2's clamp is unaffected.
    wgj = f"{d}/water.geojson"
    with open(wgj, "w") as f:
        json.dump({"type": "FeatureCollection", "features": [{"type": "Feature",
                   "properties": {"kind": "lake"}, "geometry": {"type": "Polygon", "coordinates":
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

    # ── #24 inverse clamp: fabricated positive land over inland water -> nodata ──
    # rasterize_water burns ONLY the water box (1=water); everything else (land-not-water AND
    # ocean) stays 0, so the clamp fires only inside the water box.
    water_only = f"{d}/water_only.tif"
    rasterize_water(te, res, water_only, water_src=water_fgb)
    with rasterio.open(water_only) as m:
        wonly = m.read(1)
    assert (wonly == 1).any() and (wonly == 0).any(), "water-only mask needs both water and non-water"
    assert not ((wonly == 1) & (land == 0)).any(), "the water box sits inside the land box"
    ocean_only = f"{d}/ocean_only.tif"  # an offshore extent selects zero water -> all 0, no error
    rasterize_water((xmax + 1e6, ymin, xmax + 1e6 + 200 * res, ymax), res, ocean_only, water_src=water_fgb)
    with rasterio.open(ocean_only) as m:
        assert m.read(1).sum() == 0, "an offshore extent must rasterize_water to all-zero"

    wy2, wx2 = np.where(wonly == 1)   # inland water
    ny2, nx2 = np.where(wonly == 0)   # land-not-water or ocean — both must be untouched (water==0)
    dem2 = np.full((h, w), -5.0, dtype="float32")
    dem2[wy2[0], wx2[0]] = 42.0       # positive over inland water -> must clear to nodata
    # dem2[wy2[1], wx2[1]] stays -5.0: negative in water (cryptodepression) -> must survive
    dem2[ny2[0], nx2[0]] = 42.0       # positive over non-water (land/ocean) -> must survive
    src2 = f"{d}/dem2.tif"
    with rasterio.open(src2, "w", driver="GTiff", height=h, width=w, count=1, dtype="float32",
                       nodata=NODATA, crs="EPSG:3857", transform=tr) as dst:
        dst.write(dem2, 1)
    cog2 = f"{d}/dem2_cog.tif"
    aggregation_reproject.translate(src2, cog2)
    clamp_positive_water(cog2, water_only)
    with rasterio.open(cog2) as r:
        out2, valid2 = r.read(1), (r.read_masks(1) != 0)
    assert not valid2[wy2[0], wx2[0]], "positive over inland water must clear to nodata"
    assert valid2[wy2[1], wx2[1]] and out2[wy2[1], wx2[1]] == -5.0, \
        "negative in water (cryptodepression) must survive — positive-only"
    assert valid2[ny2[0], nx2[0]] and out2[ny2[0], nx2[0]] == 42.0, \
        "positive over non-water (land/ocean, water==0) must survive"

    print(f"landmask.py self-check ok (mask {h}x{w}, land {int(land.sum())}, "
          f"water-subtracted {int(subtracted.sum())}, water-only {int((wonly == 1).sum())})")


if __name__ == "__main__":
    if sys.argv[1:2] == ["prep"]:
        prep()
    elif sys.argv[1:2] == ["prep-water"]:
        prep_water()
    elif sys.argv[1:2] == ["--check"]:
        _check()
    else:
        sys.exit("usage: landmask.py prep | prep-water | --check")
