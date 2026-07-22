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

import collections
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


def contour_minzoom(sys_tag, depth_m):
    """Per-curve tippecanoe.minzoom: the first CONTOUR_TIERS band that shows the curve (native+ if
    none). The leaf-safe replacement for the old -j $zoom PER_ZOOM_FILTER — a variable-depth leaf
    freezes at its own zoom, so zoom gating must ride each feature, not the $zoom expression. Metre
    isobaths match a tier's hand-picked levels; feet/fathom curves mirror by depth (shown once at
    least as deep as the shallowest metre curve the band shows)."""
    lo = 0
    for hi, depths in CONTOUR_TIERS:
        if sys_tag == "m":
            shown = round(depth_m) in depths
        else:  # ft: at least as deep as the shallowest metre curve shown in this band
            shown = depth_m <= -min(-d for d in depths)
        if shown:
            return lo
        lo = hi
    return lo  # native+ band ([lo, inf)); lo is the last tier ceiling


def _stems_maxz(stems):
    """Max child_z across covering stems ({z}-{x}-{y}-{child_z})."""
    return max(int(s.rsplit("-", 1)[1]) for s in stems)


def _vector_maxz(own_max):
    """The single run's -z: the covering's max child_z (native depth), so a variable-depth leaf can
    reach it, else the caller's own inputs' max. The covering is always present (Snakemake refuses
    to run without it). Replaces the old shared bundle_maxz — one joint run, not a shared tile-join
    maxzoom across three separately-tiled layers."""
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


# ── one variable-depth run: contours + soundings + depare → sparse vector.pmtiles ─────────────
# ONE tippecanoe --generate-variable-depth-tile-pyramid over all three layers, leafing per-tile
# across them jointly (separately-tiled layers leaf at different depths, and a tile-join then mints
# deep tiles missing the shallower-leafing layer — the vanishing bug the old shared bundle_maxz
# existed to prevent). All zoom gating rides as per-feature tippecanoe.minzoom: contour tiers via
# contour_minzoom, depare's z6 floor as a uniform minzoom (the run-level -Z6 dies in a -Z0 joint
# run whose soundings need z0), soundings' pyramid levels unchanged. The archive is SPARSE — the
# Worker overzooms ancestors (Part 1); manifest.vector.max_zoom is what turns that on.

# Union of the three layers' MVT attributes; the FlatGeobuf/GeoJSON Integer64 columns need :int so
# they don't land as strings. depth_m stays untyped (contours carry an int level, soundings a float
# depth — tippecanoe detects per layer).
VECTOR_ATTRS = ["depth_m", "depth_abs_m", "sys", "depth_ft", "depth_fm",
                "drval1", "drval2", "kind", "rank"]
VECTOR_TYPES = ["depth_abs_m:int", "depth_ft:int", "depth_fm:int", "rank:int"]
DEPARE_MINZOOM = 6  # depare's zoom floor, now per-feature (was depare_run's run-level -Z)


def _json_scalar(v):
    """A GeoJSON-safe scalar: unwrap numpy/pandas scalars; None for missing (None/NaN) so the
    property stays ABSENT in the MVT — depare nodata truly carries no drval1, the fill's switch."""
    if v is None:
        return None
    if hasattr(v, "item"):
        try:
            v = v.item()
        except (ValueError, AttributeError):
            pass
    if isinstance(v, float) and v != v:  # NaN
        return None
    return v


def _fgb_to_seq(fgbs, cols, minzoom_fn, out_path):
    """Stream per-tile FGBs → one GeoJSONSeq (newline-delimited features) carrying a per-feature
    tippecanoe.minzoom — FGB can't hold the tippecanoe extension, so the joint run reads GeoJSON
    like soundings already do. One tile in memory at a time (macrotile-sized). Returns the count."""
    import geopandas as gpd
    from shapely.geometry import mapping
    n = 0
    with open(out_path, "w") as fh:
        for fgb in fgbs:
            g = gpd.read_file(fgb)
            for r in g.itertuples():
                props = {}
                for c in cols:
                    v = _json_scalar(getattr(r, c, None))
                    if v is not None:
                        props[c] = v
                feat = {"type": "Feature", "tippecanoe": {"minzoom": minzoom_fn(props)},
                        "properties": props, "geometry": mapping(r.geometry)}
                fh.write(json.dumps(feat))
                fh.write("\n")
                n += 1
    return n


def _soundings_to_seq(gjs, out_path):
    """Concatenate the per-tile sounding FeatureCollections into one GeoJSONSeq, features passed
    through untouched — each already carries its own tippecanoe.minzoom (pyramid level)."""
    n = 0
    with open(out_path, "w") as fh:
        for gj in gjs:
            for ft in json.load(open(gj)).get("features", []):
                fh.write(json.dumps(ft))
                fh.write("\n")
                n += 1
    return n


def _tippecanoe_joint(layers, maxz, out):
    """ONE --generate-variable-depth-tile-pyramid run over the named layer seqs → out (sparse
    pmtiles). Global --coalesce-smallest-as-needed (merges, never drops: a dropped depare polygon
    is a partition hole; contours coalesce cleanly, proven by the STEP-0 gate) since every
    as-needed strategy is invocation-wide. --detect-shared-borders keeps partition seams crack-free."""
    cmd = ["tippecanoe", "-o", out, "-f", "--generate-variable-depth-tile-pyramid",
           "-n", "Open Waters Bathymetry", "-A", utils.ATTRIBUTION,
           "-Z", "0", "-z", str(maxz), "-P", "-q",
           "--coalesce-smallest-as-needed", "--detect-shared-borders",
           "--simplification", os.environ.get("VECTOR_SIMPLIFICATION", "8")]
    for a in VECTOR_ATTRS:
        cmd += ["-y", a]
    for t in VECTOR_TYPES:
        cmd += ["-T", t]
    for name, seq in layers:
        cmd += ["-L", f"{name}:{seq}"]
    with utils.log_group(f"vector tippecanoe ({len(layers)} layers, z0-{maxz})"):
        utils.run_monitored(cmd, "vector tippecanoe", out)


def bundle_stable():
    """Vector bundle: ONE variable-depth tippecanoe run over all three per-stem layer sets
    (contours + soundings + depare) → store/bundle/vector.pmtiles, a SPARSE pyramid the Worker
    overzooms (Part 1). Replaces the old contour tippecanoe + tile-join fold of soundings/depare. A
    0-byte per-tile file is a legitimately empty tile (filtered by size); a MISSING one is an
    incomplete build (require_stable_complete, per layer, before the run). Snakemake decides when to
    invoke, so this always rebuilds. Depare rides only when SKIP_DEPARE is unset (by CONFIGURATION,
    never disk state), gated at z6 by a per-feature tippecanoe.minzoom (DEPARE_MINZOOM)."""
    import mosaic
    stems = mosaic.covering_stems()
    cfiles = [f"store/contour/{s}.fgb" for s in stems]
    sfiles = [f"store/soundings/{s}.geojson" for s in stems]
    require_stable_complete("contour", stems, cfiles)
    require_stable_complete("soundings", stems, sfiles)
    depare_on = not os.environ.get("SKIP_DEPARE")
    dfiles = [f"store/depare/{s}.fgb" for s in stems] if depare_on else []
    if depare_on:
        require_stable_complete("depare", stems, dfiles)
    cfgbs = [f for f in cfiles if os.path.getsize(f) > 0]
    sgjs = [f for f in sfiles if os.path.getsize(f) > 0]
    dfgbs = [f for f in dfiles if os.path.getsize(f) > 0]

    vec = "store/bundle/vector.pmtiles"
    utils.create_folder("store/bundle")
    all_inputs = cfgbs + sgjs + dfgbs
    own_max = max((int(os.path.basename(f).split(".")[0].rsplit("-", 1)[1]) for f in all_inputs),
                  default=0)
    maxz = _vector_maxz(own_max)

    cseq = utils.vector_scratch("contours.geojsons")
    sseq = utils.vector_scratch("soundings.geojsons")
    dseq = utils.vector_scratch("depare.geojsons")
    try:
        nc = _fgb_to_seq(cfgbs, ("depth_m", "depth_abs_m", "sys", "depth_ft", "depth_fm"),
                         lambda p: contour_minzoom(p["sys"], float(p["depth_m"])), cseq)
        ns = _soundings_to_seq(sgjs, sseq)
        nd = _fgb_to_seq(dfgbs, ("drval1", "drval2", "sys", "kind", "rank"),
                         lambda p: DEPARE_MINZOOM, dseq)
        layers = []
        if nc:
            layers.append(("contours", cseq))
        if ns:
            layers.append(("soundings", sseq))
        if nd:
            layers.append(("depare", dseq))
        if not layers:
            raise SystemExit("vector bundle: no features in any layer for the covering")
        _tippecanoe_joint(layers, maxz, vec)  # writes vector.pmtiles; Snakemake cleans a torn output
    finally:
        for seq in (cseq, sseq, dseq):
            if os.path.exists(seq):
                os.remove(seq)
    _vector_selfcheck(vec, maxz)
    print(f"vector bundle (stable): {vec} (z0-{maxz}; contours={nc} soundings={ns} depare={nd})")


def _decode_tile(vec, z, x, y):
    """{layer: [feature, ...]} for one tile of a pmtiles archive, via tippecanoe-decode (XYZ)."""
    out = subprocess.run(["tippecanoe-decode", "-c", vec, str(z), str(x), str(y)],
                         capture_output=True, text=True).stdout
    layers = collections.defaultdict(list)
    for line in out.splitlines():
        line = line.strip().rstrip(",")
        if not line.startswith("{"):
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if o.get("type") == "Feature":
            layers[o.get("tippecanoe", {}).get("layer", "?")].append(o)
    return layers


def _vector_selfcheck(vec, maxz, per_zoom=6):
    """Cheap continuous guard on the sparse archive (the STEP-0 gate proved semantic equivalence to
    the old 3-run + tile-join oracle; this samples a few tiles per zoom to catch a regression in the
    minzoom wiring or the global coalesce). Asserts: no depare below its z6 floor, no contour below
    its tier minzoom, depare m-bands pairwise disjoint (coalesce displaced nothing), soundings present."""
    from pmtiles.reader import Reader, MmapSource, all_tiles
    from shapely.geometry import shape
    from shapely.ops import unary_union
    byz = collections.defaultdict(list)
    with open(vec, "r+b") as f:
        reader = Reader(MmapSource(f))
        for tile_tuple, _ in all_tiles(reader.get_bytes):
            z, x, y = tile_tuple
            byz[z].append((x, y))
    problems, n_soundings = [], 0
    for z in sorted(byz):
        ts = byz[z]
        for x, y in ts[:: max(1, len(ts) // per_zoom)]:
            L = _decode_tile(vec, z, x, y)
            n_soundings += len(L.get("soundings", []))
            if z < DEPARE_MINZOOM and L.get("depare"):
                problems.append(f"z{z} {x}/{y}: {len(L['depare'])} depare below z{DEPARE_MINZOOM}")
            for feat in L.get("contours", []):
                p = feat["properties"]
                d, s = p.get("depth_m"), p.get("sys")
                if d is not None and s and contour_minzoom(s, float(d)) > z:
                    problems.append(f"z{z} {x}/{y}: contour depth_m={d} sys={s} below minzoom "
                                    f"{contour_minzoom(s, float(d))}")
                    break
            mbands = [feat for feat in L.get("depare", [])
                      if feat["properties"].get("sys") == "m"
                      and (feat["properties"].get("drval1") or -1) >= 0]
            if mbands:
                geoms = [shape(feat["geometry"]).buffer(0) for feat in mbands]
                asum, uni = sum(g.area for g in geoms), unary_union(geoms).area
                if uni and (asum - uni) / uni >= 0.02:
                    problems.append(f"z{z} {x}/{y}: depare m-bands overlap {(asum - uni) / uni:.2%} "
                                    f"(coalesce displaced a partition)")
    if n_soundings == 0:
        problems.append("no soundings in any sampled tile (the layer vanished)")
    if problems:
        raise SystemExit("vector self-check FAILED:\n  " + "\n  ".join(problems[:20]))
    print(f"vector self-check ok ({sum(len(v) for v in byz.values())} tiles, "
          f"z{min(byz)}-{max(byz)}, {n_soundings} sampled soundings)")


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
    # the run's -z reads child_z off covering stems
    assert _stems_maxz({"4-5-6-8", "11-300-400-13"}) == 13
    # navigable-band contours skip Chaikin (never bow a shoal deeper); deeper ones smooth
    assert _chaikin_iters(10) == 0 and _chaikin_iters(NAV_SMOOTH_MAX_M) == 0
    assert _chaikin_iters(NAV_SMOOTH_MAX_M + 1) == CHAIKIN_ITERATIONS
    # per-feature minzoom reproduces the CONTOUR_TIERS thinning (the leaf-safe replacement for -j).
    # A shallow metre curve only shows at native+ (last ceiling); a deep one from the first tier
    # shows at z0; a fathom curve is gated by depth like the deep metre ones.
    ceilings = [hi for hi, _ in CONTOUR_TIERS]
    assert contour_minzoom("m", -2) == ceilings[-1]              # -2 in no tier -> native+
    assert contour_minzoom("m", -4000) == 0                      # -4000 in the first tier -> z0
    assert contour_minzoom("m", -50) == 7 and contour_minzoom("m", -10) == 9
    assert contour_minzoom("ft", -0.5) == ceilings[-1]          # shallower than any band's floor
    assert contour_minzoom("ft", -4000) == 0                     # deeper than the first band's floor
    # every tier level must exist in the generated contour set (else it gates nothing)
    assert all(d in config.CONTOUR_LEVELS for _, depths in CONTOUR_TIERS for d in depths)
    print("contour_run ring-drop + minzoom self-check ok")


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
