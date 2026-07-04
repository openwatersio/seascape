"""Land mask — stop coarse sources from painting water over land.

Coarse global sources (GEBCO, EMODnet) carry no land/water distinction: a ~460 m
GEBCO cell straddling the shoreline averages in water and reads *negative on land*,
which downstream renders as a false "water" rim over the coast (and false contours /
soundings). This module builds an OSM land-polygon mask and clamps a flagged coarse
source's warped raster: where the mask says land AND the pixel is negative -> 0
(chart datum at the shoreline). Only flagged sources are touched, so S-102 / CUDEM /
lake / river bathymetry can never be damaged by a land-mask error.

Two clamp points: `clamp` on each flagged source's warped raster (before the merge),
and `clamp_merged` on the final merged+smoothed DEM (after them). The second is needed
because the merge's Gaussian seam feather blends the clamped 0-land with adjacent
negative water across the shoreline, re-bleeding a soft negative band back onto land;
the final pass restores a crisp land=0 shoreline for every consumer.

Ceiling: OSM land polygons treat inland lakes/tidal rivers as "land", so a coarse
source's own values inside them flatten to 0 (acceptable — the project's lake/river
sources outrank GEBCO). Real below-sea-level land (Netherlands polders, Death Valley)
and inland seas with genuine negative surfaces (Dead Sea, Salton) also flatten to 0.
Upgrade path: subtract an inland-water polygon layer from the mask.

Local layout (store/landmask/): land.fgb — the prepared EPSG:3857 land mask (spatial
index, HTTP-range friendly). LANDMASK overrides the path (default store/landmask/land.fgb).

  python landmask.py prep      download -> unzip -> convert (run once)
  python landmask.py --check   self-check
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

# Web-mercator latitude limit: clip the polygons to +/-85.06 deg (also drops the
# Antarctica polygon's polar excess). The warp grids never reach beyond this.
MERC_LAT = 85.06

DEFAULT_LANDMASK = "store/landmask/land.fgb"


def path():
    """The land-mask path, read from LANDMASK at *call time* (default store/landmask/land.fgb).
    Not an import-time constant, so CI/tests/callers that set LANDMASK after this module is
    imported still take effect — mirrors config.source_path reading SOURCE_VSI_BASE per call."""
    return os.environ.get("LANDMASK", DEFAULT_LANDMASK)


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


def rasterize(bounds_3857, res, out_tif, src=None):
    """Burn the land mask onto a Byte raster (1=land, 0=water) on the given 3857 grid.

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

    The rasterize writes to a temp then atomically renames, so a crash mid-burn never leaves
    a truncated mask at out_tif that a resume would trust (the reproject cache is trusted if
    present). DEFLATE+TILED+SPARSE_OK keeps the mask tiny — a binary mask compresses ~1000:1
    and all-water blocks (most of the planet) become sparse holes.
    """
    src = src or path()
    xmin, ymin, xmax, ymax = bounds_3857
    clip = out_tif + ".clip.gpkg"
    tmp_out = out_tif + ".tmp.tif"
    try:
        utils.run_command(
            f"ogr2ogr -f GPKG -overwrite -spat {xmin} {ymin} {xmax} {ymax} {clip} {src}")
        utils.run_command(
            f"gdal_rasterize -burn 1 -ot Byte -init 0 -te {xmin} {ymin} {xmax} {ymax} "
            f"-tr {res} {res} -co COMPRESS=DEFLATE -co TILED=YES -co SPARSE_OK=YES "
            f"{clip} {tmp_out}")
        os.replace(tmp_out, out_tif)  # atomic: out_tif only ever exists complete
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


def clamp_merged(dem_path, mask_tif):
    """Re-clamp the final merged+smoothed DEM against the land mask, to undo the merge seam
    feather's re-bleed of negative water onto the per-source-clamped land (and any widening the
    smooth adds), so every consumer sees a crisp land=0 shoreline. The merged DEM is a single
    band with a nodata sentinel (not the per-source alpha COG), so validity is `!= nodata`, like
    smooth_array. Only negative-under-land changes -> positive land (S-102/CUDEM topo) untouched."""
    def valid(ds, win, a):
        nodata = ds.nodata if ds.nodata is not None else NODATA
        return a != nodata
    _clamp_negative_land(dem_path, mask_tif, valid)


def _check():
    """Clamp semantics on the REAL pipeline file (a COG with an ADD_ALPHA mask band, like
    negate_band1's check) + the single-band merged variant + rasterize seam determinism."""
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

    # A grid spanning the box + margin (lon/lat 0.5..2.5), ~1 km pixels.
    from rasterio.warp import transform_bounds
    xmin, ymin, xmax, ymax = transform_bounds("EPSG:4326", "EPSG:3857", 0.5, 0.5, 2.5, 2.5)
    res = (xmax - xmin) / 240
    te = (xmin, ymin, xmax, ymax)

    mask_tif = f"{d}/mask.tif"
    rasterize(te, res, mask_tif, src=land_fgb)
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

    # clamp_merged: a single-band DEM with a nodata sentinel (the merged/smoothed surface, no
    # alpha band) — negative-under-land clamps; nodata, positive land, and water are untouched.
    merged = f"{d}/merged.tif"
    mdem = np.full((h, w), -5.0, dtype="float32")
    mdem[ys[0], xs[0]] = 3.0        # positive land -> survive
    mdem[ys[1], xs[1]] = NODATA     # nodata land   -> untouched
    with rasterio.open(merged, "w", driver="GTiff", height=h, width=w, count=1, dtype="float32",
                       nodata=NODATA, crs="EPSG:3857", transform=tr) as dst:
        dst.write(mdem, 1)
    clamp_merged(merged, mask_tif)
    with rasterio.open(merged) as r:
        mo = r.read(1)
    assert mo[ys[2], xs[2]] == 0.0, "merged: negative under land must clamp to 0"
    assert mo[ys[0], xs[0]] == 3.0, "merged: positive land must survive"
    assert mo[ys[1], xs[1]] == NODATA, "merged: nodata must stay"
    assert mo[wy[0], wx[0]] == -5.0, "merged: water must survive"

    # Seam determinism: the same world cells rasterize identically from a shifted (still
    # grid-aligned) extent — the overlap is byte-identical, so tile halos clamp the same.
    shift = 10
    te_b = (xmin + shift * res, ymin, xmax + shift * res, ymax)
    mask_b = f"{d}/mask_b.tif"
    rasterize(te_b, res, mask_b, src=land_fgb)
    with rasterio.open(mask_b) as m:
        land_b = m.read(1)
    assert np.array_equal(land[:, shift:], land_b[:, :w - shift]), "seam not deterministic across extents"

    # Open-ocean tile: -spat selects zero polygons → an all-water mask, no error (an empty
    # FlatGeobuf clip would raise, sinking every ocean tile — hence the GPKG clip).
    ocean = f"{d}/ocean.tif"
    rasterize((xmax + 1e6, ymin, xmax + 1e6 + 200 * res, ymax), res, ocean, src=land_fgb)
    with rasterio.open(ocean) as m:
        assert m.read(1).sum() == 0, "ocean extent must rasterize to all-water"

    print(f"landmask.py self-check ok (mask {h}x{w}, land pixels {int(land.sum())})")


if __name__ == "__main__":
    if sys.argv[1:2] == ["prep"]:
        prep()
    elif sys.argv[1:2] == ["--check"]:
        _check()
    else:
        sys.exit("usage: landmask.py prep | --check")
