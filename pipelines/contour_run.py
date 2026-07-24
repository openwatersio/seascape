"""Contours as a fork off each aggregation tile's merged DEM.

Reads a window of the persisted mosaic (smoothed at read by smooth.py), so contours
come from one continuous surface. GDAL runs as a subprocess; geopandas/shapely do the
Chaikin smoothing.

Each aggregation tile contours its full-res merged DEM, so every source shows at
all zooms (coarse GEBCO is overzoomed by the renderer above its native z9; CUDEM
adds detail where present). Within a tile the merge is feathered, so the
CUDEM->GEBCO footprint transition is continuous; the residual coarse/fine
difference at the tile edge is fundamental (you can't draw GEBCO at CUDEM's
resolution) and is softened by the smoothing.

Per tile: gdal_contour (3857) -> Chaikin smooth + enrich (depth_abs_m)
-> clip to the unbuffered tile bbox -> reproject to EPSG:4326.

(A disjoint base/regional zoom-band split was tried to make lines join at z9, but
it hid GEBCO above z9 — not worth losing GEBCO at high zoom. Smoothing is the
real seam mitigation.)
"""

import json
import os
import shutil
import tempfile
import subprocess
import sys
from glob import glob

import mercantile

import aggregation_covering
import config
import landmask
import utils
from aggregation_reproject import get_resolution

CHAIKIN_ITERATIONS = 5
# Skip Chaikin in the navigable band: corner-cutting bows a contour toward the deep
# side at shallow-convex bends, which shrinks a shoal (unsafe). Shallow lines keep
# their raw gdal_contour geometry (sub-pixel simplify only); deeper contours smooth
# for looks. Mirrors smooth.DEPTH_FULL / the ECDIS safety-contour depth.
NAV_SMOOTH_MAX_M = int(os.environ.get("CONTOUR_NAV_SMOOTH_MAX", "30"))


def _chaikin_iters(depth_abs_m):
    return 0 if depth_abs_m <= NAV_SMOOTH_MAX_M else CHAIKIN_ITERATIONS


# Drop spurious tiny closed contours (abyssal stipple — micro-loops around bumps
# near a deep contour value) at deep levels only. Shallow rings are navigable
# shoals and are kept untouched (IHO safe-side: never drop a charted shoal). Areas
# in m² (geometry is EPSG:3857 here).
DEEP_CUTOFF_M = int(os.environ.get("CONTOUR_RING_DEEP_CUTOFF", "1000"))
MIN_RING_AREA_M2 = float(os.environ.get("CONTOUR_RING_MIN_AREA", "16e6"))  # ~16 km²

# The style hand-mirrors the DEFAULT metre ladder (style/index.ts DEPARE_LADDER_M/FT), so an
# env-overridden CONTOUR_LEVELS silently diverges the tiles from the style. Warn loudly, once
# per process; upgrade path is generating the style constants from config instead of mirroring.
if config.CONTOUR_LEVELS != config.CONTOUR_LEVELS_DEFAULT:
    print("WARNING: CONTOUR_LEVELS overridden — isobaths/depth-areas will diverge from the "
          "style's hand-mirrored DEPARE_LADDER_M/FT (style/index.ts); update it to match.",
          file=sys.stderr)


def _run(cmd, what):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if p.returncode != 0:
        raise Exception(f"{what} failed (exit {p.returncode}):\n{p.stdout}\n{p.stderr}")


def feature_count(fgb):
    out = subprocess.run(["ogrinfo", "-so", "-al", fgb], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "Feature Count" in line:
            return int(line.split(":")[1].strip())
    return 0


# ── Chaikin smoothing (ported from scripts/smooth-contours, via shapely) ─────

def _chaikin(coords, iterations):
    import numpy as np
    pts = np.asarray(coords, float)
    for _ in range(iterations):
        if len(pts) < 3:
            break
        q = 0.75 * pts[:-1] + 0.25 * pts[1:]
        r = 0.25 * pts[:-1] + 0.75 * pts[1:]
        out = np.empty((2 * len(q), 2))
        out[0::2], out[1::2] = q, r
        out[0], out[-1] = pts[0], pts[-1]  # preserve endpoints (lines must not drift)
        pts = out
    return pts


def _smooth_geom(geom, tol, iterations):
    from shapely.geometry import LineString, MultiLineString
    if geom.geom_type == "LineString":
        g = geom.simplify(tol) if tol > 0 else geom
        coords = list(g.coords)
        return LineString(_chaikin(coords, iterations)) if len(coords) >= 3 else geom
    if geom.geom_type == "MultiLineString":
        return MultiLineString([_smooth_geom(p, tol, iterations) for p in geom.geoms])
    return geom


def _drop_small_rings(geom, min_area):
    """Drop closed-ring parts enclosing < min_area m²; keep open lines. None if empty."""
    from shapely.geometry import MultiLineString, Polygon
    parts = [geom] if geom.geom_type == "LineString" else list(geom.geoms)
    kept = [p for p in parts
            if not (len(p.coords) > 3 and p.coords[0] == p.coords[-1]
                    and Polygon(p.coords).area < min_area)]
    if not kept:
        return None
    return kept[0] if len(kept) == 1 else MultiLineString(kept)


def smooth_and_enrich(sources, out_fgb, tol):
    """Concatenate the metre + feet contour sets (each `(fgb, sys)`), tag `sys`, Chaikin-smooth
    (in 3857, nav-band skip so smoothing never understates a shoal), drop deep micro-loop stipple,
    and add depth_abs_m / depth_ft / depth_fm. Feet features sit on whole-fathom depths so their
    depth_ft/depth_fm round clean; the viewer labels metre features in metres, feet features in
    feet or fathoms."""
    import geopandas as gpd
    import pandas as pd
    parts = []
    for fgb, sys in sources:
        g = gpd.read_file(fgb)
        g["sys"] = sys
        parts.append(g)
    gdf = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=parts[0].crs)
    gdf["depth_abs_m"] = (-gdf["depth_m"]).round().astype(int)
    gdf["depth_ft"] = (-gdf["depth_m"] / 0.3048).round().astype(int)
    gdf["depth_fm"] = (-gdf["depth_m"] / 1.8288).round().astype(int)
    gdf["geometry"] = [_smooth_geom(g, tol, _chaikin_iters(d))
                       for g, d in zip(gdf.geometry, gdf["depth_abs_m"])]
    deep = gdf["depth_abs_m"] >= DEEP_CUTOFF_M
    if deep.any():
        gdf.loc[deep, "geometry"] = [_drop_small_rings(g, MIN_RING_AREA_M2)
                                     for g in gdf.loc[deep, "geometry"]]
        gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    gdf.to_file(out_fgb, driver="FlatGeobuf")


def tile(stem):
    """The per-stem Snakemake job: contour one stem from a BUFFERED mosaic window, smoothed at read
    with the one shared f(depth, zoom), output at store/contour/<stem>.fgb. A featureless tile
    writes a 0-byte sentinel so the engine sees a complete output; bundling filters empties by
    size."""
    import mosaic
    import smooth
    z, x, y, child_z = (int(a) for a in stem.split("-"))
    out = f"store/contour/{stem}.fgb"
    tmp = tempfile.mkdtemp(prefix=f"contour-{stem}-")  # local scratch; publish crosses to the store
    dem = mosaic.window_dem(stem, f"{tmp}/dem.tiff")
    if not os.environ.get("SKIP_SMOOTH"):
        smooth.smooth_tiff(dem)
    # After smoothing: it smears sea negatives back across clamp-flattened islands, and
    # the warp-time clamp's centre-sampled mask misses narrow rims — either puts
    # isobaths on land.
    landmask.clamp_dem_to_land(dem)
    final = _contour_dem(dem, mercantile.Tile(x=x, y=y, z=z), child_z, tmp, stem)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    if final:
        utils.publish(final, out)  # scratch and store are separate filesystems
        print(f"contour tile {stem}: {feature_count(out)} features")
    else:
        open(out, "w").close()
        print(f"contour tile {stem}: empty")
    shutil.rmtree(tmp)


def _contour_dem(dem, tile_obj, child_z, tmp, label):
    """gdal_contour -> smooth/enrich -> clip -> reproject, off any DEM covering the tile's
    buffered extent. Returns the final FGB path inside ``tmp``, or None when featureless."""
    levels = " ".join(str(l) for l in config.CONTOUR_LEVELS)
    raw = f"{tmp}/contour-raw.fgb"
    _run(f"gdal_contour -q -fl {levels} -a depth_m -f FlatGeobuf {dem} {raw}", "gdal_contour m")
    sources = [(raw, "m")]

    # A second set at the fathom curves for feet/fathom charts (same DEM, tagged sys=ft).
    levels_ft = " ".join(str(l) for l in config.CONTOUR_LEVELS_FT)
    raw_ft = f"{tmp}/contour-raw-ft.fgb"
    _run(f"gdal_contour -q -fl {levels_ft} -a depth_m -f FlatGeobuf {dem} {raw_ft}", "gdal_contour ft")
    if feature_count(raw_ft) > 0:
        sources.append((raw_ft, "ft"))

    if sum(feature_count(f) for f, _ in sources) == 0:
        print(f"contour: no ocean features for {label}")
        return None

    smoothed = f"{tmp}/contour-smooth.fgb"
    smooth_and_enrich(sources, smoothed, tol=get_resolution(child_z))

    b = mercantile.xy_bounds(tile_obj)  # unbuffered, tile-aligned (EPSG:3857)
    clipped = f"{tmp}/contour-clip.fgb"
    _run(f"ogr2ogr -f FlatGeobuf -overwrite -nlt PROMOTE_TO_MULTI "
         f"-clipsrc {b.left} {b.bottom} {b.right} {b.top} {clipped} {smoothed}", "ogr2ogr clip")
    if feature_count(clipped) == 0:
        print(f"contour: no features in tile bbox for {label}")
        return None

    # Reproject inside tmp; the caller owns the atomic move into its lane's name.
    final = f"{tmp}/contour-final.fgb"
    _run(f"ogr2ogr -f FlatGeobuf -overwrite -nlt PROMOTE_TO_MULTI -t_srs EPSG:4326 {final} {clipped}",
         "ogr2ogr reproject")
    return final


# ── bundle ───────────────────────────────────────────────────────────────────

# Scale-dependent contour interval: coarse isobaths zoomed out, finer zoomed in
# (charts thin the deep, not the shelf — abyssal contours stipple into noise at
# small scale). (zoom_ceiling, depths_m shown below it); at/above the last ceiling
# every level shows. Each list must be a subset of config.CONTOUR_LEVELS.
CONTOUR_TIERS = [
    (5, [-200, -1000, -2000, -4000]),
    (7, [-200, -500, -1000, -2000, -3000, -4000]),
    (9, [-50, -100, -200, -300, -500, -1000, -2000, -3000, -4000, -5000, -6000, -8000, -10000]),
    (11, [-10, -20, -30, -50, -100, -200, -300, -500, -1000, -2000, -3000, -4000, -5000, -6000, -8000, -10000]),
]


def _per_zoom_filter():
    """tippecanoe's -j filter (exclusive $zoom bands). Metre isobaths (sys=m) keep the hand-picked
    CONTOUR_TIERS per zoom. Feet/fathom isobaths (sys=ft) mirror that thinning by depth — at each
    zoom they show only curves at least as deep as the shallowest metre curve shown, so deep
    fathom curves drop out at low zoom like the metre deep ones. Native+ zoom shows every curve."""
    bands, lo = [], 0
    for hi, depths in CONTOUR_TIERS:
        min_abs = min(-d for d in depths)  # shallowest metre curve shown in this band
        # Both bands test depth_m (gdal_contour's Real attribute): -j comparisons are
        # type-strict, and the enriched int columns (depth_abs_m et al.) reach the filter
        # as strings via the FlatGeobuf Integer64 path — a numeric test on them matches
        # nothing, which silently dropped every ft curve below native zoom.
        bands.append(["all", [">=", "$zoom", lo], ["<", "$zoom", hi], ["==", "sys", "m"], ["in", "depth_m", *depths]])
        bands.append(["all", [">=", "$zoom", lo], ["<", "$zoom", hi], ["==", "sys", "ft"], ["<=", "depth_m", -min_abs]])
        lo = hi
    bands.append(["all", [">=", "$zoom", lo]])  # native+ zoom: every curve, both systems
    return json.dumps({"*": ["any", *bands]})


PER_ZOOM_FILTER = _per_zoom_filter()


def _tippecanoe(fgbs, minz, maxz, out):
    with utils.log_group(f"contour tippecanoe ({len(fgbs)} inputs, z{minz}-{maxz})"):
        utils.run_monitored(
            ["tippecanoe", "-o", out, "-f", "-l", "contours",
             "-n", "Bathymetric contours", "-A", utils.ATTRIBUTION,
             "-Z", str(minz), "-z", str(maxz), "-P", "-q", "--drop-densest-as-needed",
             # Aggressive low-zoom vertex thinning. -S alone ALSO applies at maxzoom
             # (~76 m tolerance at z10), which cut isobaths across islands the DEM-level
             # land clamp had already routed around — pin maxzoom near-lossless so the
             # deepest tier keeps the clamped shoreline. Env-tunable to dial on a re-bundle.
             "--simplification", os.environ.get("CONTOUR_SIMPLIFICATION", "8"),
             "--simplification-at-maximum-zoom",
             os.environ.get("CONTOUR_SIMPLIFICATION_MAXZOOM", "1"),
             "-y", "depth_m", "-y", "depth_abs_m", "-y", "sys", "-y", "depth_ft", "-y", "depth_fm",
             # FlatGeobuf Integer64 attributes otherwise land in the MVT as strings.
             "-T", "depth_abs_m:int", "-T", "depth_ft:int", "-T", "depth_fm:int",
             "-j", PER_ZOOM_FILTER, *fgbs],
            "contour tippecanoe", out)


def _stems_maxz(stems):
    """Max child_z across covering stems ({z}-{x}-{y}-{child_z})."""
    return max(int(s.rsplit("-", 1)[1]) for s in stems)


def bundle_maxz_stable(own_max):
    """The shared tileset maxzoom every vector layer bundles to (contours/soundings/depare
    tile-join into one vector.pmtiles): the covering's max child_z (so they tile to the same depth and
    tile-join cleanly), else the caller's own files' max. The covering is always present
    (Snakemake refuses to run without it), so there is no None fallback."""
    import mosaic
    return max(own_max, _stems_maxz(mosaic.covering_stems()))


# ── source coverage (provenance) layer ───────────────────────────────────────
# Tile each source's union footprint (store/polygon/<id>.gpkg, from the source stage)
# into its own store/bundle/coverage.pmtiles, so the viewer can show which source
# covers a clicked point. Its own tileset, NOT a layer in vector.pmtiles: a joined
# archive shares one zoom range, and sea-sized fill polygons tiled to the contours'
# maxzoom mint millions of deep-ocean tiles, while tiled lower the layer vanishes
# above its maxzoom (MapLibre overzooms a missing *tile*, never a missing *layer*).
# Standalone, the renderer overzooms it past COVERAGE_MAX_ZOOM on its own. GEBCO
# (the global base) declares no max_zoom, so it's skipped; only regional footprints
# get a polygon.

BASE_SOURCE = "gebco"  # the global fallback; its footprint is the whole planet, never an overlay

# z8's 4096-cell MVT grid resolves ~37 m and the footprints are already simplified
# to 0.001° (~100 m) — fidelity is simplify-bound, not zoom-bound — while the z8
# tile address space caps the archive at 65k tiles.
COVERAGE_MAX_ZOOM = int(os.environ.get("COVERAGE_MAX_ZOOM", "8"))


def _source_maxzooms():
    """{source: resolved native/capped maxzoom}, independent of a planet covering."""
    return aggregation_covering.source_maxzooms()


def _coverage_geojson():
    """Combine every regional source's simplified footprint into one GeoJSON FC with
    {source_id, source_name, source_maxzoom}; write it and return the path. None if no
    polygons are present locally (coverage_bundle turns that into a hard failure).

    BBOX (W,S,E,N lon/lat, the same regional-build env the covering uses) pushes a -spat
    filter onto each footprint read, so a preview builds only the region's coverage instead of
    tippecanoeing every footprint whole (the large intertidal-survey unions — uk_surfzone,
    infomar — dominate that cost). Footprints are EPSG:4326, so BBOX maps straight to -spat.
    Unset (a planet/CI build) → the whole world."""
    valid = set(config.sources())
    zmax = _source_maxzooms()
    bbox = os.environ.get("BBOX", "").strip()
    spat = ["-spat", *(c.strip() for c in bbox.split(","))] if bbox else []
    feats = []
    for gpkg in sorted(glob("store/polygon/*.gpkg")):
        sid = gpkg.split("/")[-1].replace(".gpkg", "")
        if sid not in valid or sid == BASE_SOURCE:  # orphan polygon, or the global base — skip
            continue
        meta = config.load_metadata(sid)
        out = subprocess.run(
            ["ogr2ogr", "-f", "GeoJSON", "/vsistdout/", gpkg, *spat,
             "-simplify", "0.001", "-lco", "COORDINATE_PRECISION=5"],
            capture_output=True, text=True, check=True).stdout
        for f in json.loads(out).get("features", []):
            # source_maxzoom drives the viewer's deepest-wins pick on overlap (priority isn't
            # folded in — a higher-priority shallower source would mis-attribute; rare today).
            f["properties"] = {"source_id": sid, "source_name": meta.get("name", sid),
                               "source_maxzoom": zmax.get(sid) or meta.get("max_zoom") or 0}
            feats.append(f)
    if not feats:
        return None
    path = "store/contour/coverage.geojson"
    utils.create_folder("store/contour")
    with open(path, "w") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f)
    return path


def coverage_bundle():
    """tippecanoe the source footprints into store/bundle/coverage.pmtiles (layer
    `coverage`, z0..COVERAGE_MAX_ZOOM, footprints kept whole). Fails when no
    footprints are local: a planet build without them ships a dead provenance
    layer — the silent drop is exactly how the layer went missing from every
    published build."""
    src = _coverage_geojson()
    if not src:
        raise SystemExit("coverage: no footprints in store/polygon/ — pull "
                         "bathymetry/polygon/*.gpkg or prepare a source first")
    utils.create_folder("store/bundle")
    out = "store/bundle/coverage.pmtiles"
    subprocess.run(
        ["tippecanoe", "-o", out, "-f", "-l", "coverage", "-n", "Source coverage",
         "-A", utils.ATTRIBUTION, "-Z", "0", "-z", str(COVERAGE_MAX_ZOOM), "-P", "-q",
         "--no-tile-size-limit", src], check=True)
    print(f"coverage bundle: {out} (z0-{COVERAGE_MAX_ZOOM})")


def _finalize_contours(archives, out):
    """tile-join the contour lines pmtiles + the soundings/depare pmtiles (bundled first)
    into store/bundle/vector.pmtiles. ONE join: tile-join rewrites every tile of the whole
    archive, so folding each sparse layer in afterwards re-paid the planet-wide join
    per layer (~90 min each). -pk keeps every feature of every layer. Layers join by
    CONFIGURATION, never by disk state: soundings always, depare only when SKIP_DEPARE is
    unset — a stale depare.pmtiles on the store can't leak into the product, and an enabled
    layer whose bundle is missing fails loudly. Coverage is its own tileset (coverage_bundle),
    not a layer here. Drying is not its own layer; it folds into `depare` (a DEPARE band with
    negative drval1)."""
    layers = ["store/bundle/soundings.pmtiles"]
    if not os.environ.get("SKIP_DEPARE"):
        layers.append("store/bundle/depare.pmtiles")
    missing = [p for p in layers if not os.path.isfile(p)]
    if missing:
        raise SystemExit(f"vector join: required layer bundle(s) missing: {', '.join(missing)}")
    with utils.log_group(f"vector tile-join ({len(archives) + len(layers)} archives)"):
        utils.run_monitored(["tile-join", "-o", out, "-f", "-pk",
                             *archives, *layers], "vector tile-join",
                            out)


def require_stable_complete(layer, stems, files):
    """The no-holes gate: every covering stem must have its per-stem file on disk — a 0-byte file
    is a legitimately empty tile, a MISSING one is an incomplete build. The only bundle-time gate;
    Snakemake owns freshness."""
    missing = [s for s, f in zip(stems, files) if not os.path.exists(f)]
    if missing:
        raise SystemExit(
            f"{layer} incomplete: {len(missing)} of {len(stems)} covering tiles have no per-tile "
            f"file (an interrupted build) — e.g. {', '.join(missing[:5])}"
            f"{' …' if len(missing) > 5 else ''}")


def bundle_stable():
    """Vector bundle: tippecanoe the per-stem contour FGBs for the covering into contour lines,
    then tile-join the already-bundled soundings/depare pmtiles → vector.pmtiles. A 0-byte per-tile
    file is a legitimately empty tile (filtered by size); a MISSING one is an incomplete build
    (require_stable_complete). Snakemake decides when to invoke, so this always rebuilds. Depare
    joins only when SKIP_DEPARE is unset (_finalize_contours), regardless of disk state."""
    import mosaic
    stems = mosaic.covering_stems()
    files = [f"store/contour/{s}.fgb" for s in stems]
    require_stable_complete("contour", stems, files)
    fgbs = [f for f in files if os.path.getsize(f) > 0]
    vec = "store/bundle/vector.pmtiles"
    utils.create_folder("store/bundle")
    own_max = max((int(os.path.basename(f).split(".")[0].rsplit("-", 1)[1]) for f in fgbs), default=0)
    maxz = bundle_maxz_stable(own_max)
    lines = utils.vector_scratch("contours-lines.pmtiles")
    if os.path.exists(lines):
        os.remove(lines)
    _tippecanoe(fgbs, 0, maxz, lines)
    _finalize_contours([lines], vec)  # writes vector.pmtiles directly; Snakemake cleans a torn output
    os.remove(lines)
    print(f"contour bundle (stable): {vec} (z0-{maxz}, {len(fgbs)} FGBs)")


def _check():
    """Deep micro-loops dropped; big deep ring and open lines kept."""
    import numpy as np
    from shapely.geometry import LineString
    def ring(r, n=48):
        t = np.linspace(0, 2 * np.pi, n, endpoint=False)
        pts = [(r * np.cos(a), r * np.sin(a)) for a in t]
        return LineString(pts + [pts[0]])  # explicitly closed
    assert _drop_small_rings(ring(100), MIN_RING_AREA_M2) is None        # ~0.03 km² → drop
    assert _drop_small_rings(ring(3000), MIN_RING_AREA_M2) is not None   # ~28 km²  → keep
    assert _drop_small_rings(LineString([(0, 0), (5e3, 0), (1e4, 5e3)]), MIN_RING_AREA_M2) is not None  # open line kept
    # the shared bundle maxzoom reads child_z off covering stems
    assert _stems_maxz({"4-5-6-8", "11-300-400-13"}) == 13
    # navigable-band contours skip Chaikin (never bow a shoal deeper); deeper ones smooth
    assert _chaikin_iters(10) == 0 and _chaikin_iters(NAV_SMOOTH_MAX_M) == 0
    assert _chaikin_iters(NAV_SMOOTH_MAX_M + 1) == CHAIKIN_ITERATIONS
    # metre and feet/fathom isobaths each get their own per-zoom bands in the tippecanoe filter
    assert '["==", "sys", "m"]' in PER_ZOOM_FILTER and '["==", "sys", "ft"]' in PER_ZOOM_FILTER
    # the filter must only compare depth_m — the int columns are string-typed at -j time
    assert "depth_abs_m" not in PER_ZOOM_FILTER
    # every tier level must exist in the generated contour set (else it filters to nothing)
    assert all(d in config.CONTOUR_LEVELS for _, depths in CONTOUR_TIERS for d in depths)
    print("contour_run ring-drop self-check ok")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["tile"] and len(a) == 2:
        tile(a[1])
    elif a == ["bundle", "--stable"]:
        bundle_stable()
    elif a[:1] == ["coverage"]:
        coverage_bundle()
    elif a[:1] == ["check"]:
        _check()
    else:
        sys.exit("usage: contour_run.py tile <stem> | bundle --stable | coverage | check")
