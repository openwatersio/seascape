"""Compose ``store/datum/ign69_zh.tif`` — French chart datum as a height in the legal
altimetric frame of its own zone.

A support artifact like the landmask, NOT a ``sources/`` entry (everything under ``sources/``
enters the merge). It is the reference a Litto3D source is corrected against on the subtract
convention shared with ``datum_grid.py``::

    reference = ZH_ell - N        ZH's height in the legal frame (Brest: 47.089 - 50.670)
    bed_ZH    = bed_IGN69 - reference

Shom delivers Litto3D as an ALTITUDE in the zone's legal system (``CHANTIER_SYSALTI IGN69``
in every dalle sidecar), not on a chart datum. ``reference`` is negative almost everywhere —
ZH sits below the geoid — so the corrected bed rises and depths get shallower, the
bias-shallow-safe direction. Its magnitude is the whole reason a scalar will not do: -0.19 m
at Cavalaire against -6.77 m at Cancale, and -2.63 to -6.77 m inside Bretagne alone.

Two inputs, both open and both fetched by URL pin:

- **BATHYELLI v2.1** (Shom, Licence Ouverte), the height of the zéro hydrographique above the
  RGF93 ellipsoid. Not a raster: two ``.glhi`` files of scattered ``lon lat height uncertainty``
  nodes, 178,944 over the Atlantic/Manche and 32,844 over the Mediterranean, median spacing
  0.7 km, median stated uncertainty 0.13 m.
- **RAF20 / RAC23** (IGN, via the PROJ CDN), the geoid separations that turn that ellipsoidal
  height into an altitude. Two grids because Corsica is a different legal datum, not a
  different region of one: mainland Litto3D is IGN69 and Litto3D Corse is IGN78.

Stages: parse both node sets -> triangulate each and interpolate onto one 0.005 deg grid ->
bound the result to where BATHYELLI actually sampled -> subtract RAF20, and RAC23 wherever
RAC23 reaches, each carrying its region's shift onto the legal chart zero -> one 4326 COG.
Coverage is marine: it runs from open water across the drying flats to roughly 2 km inland,
then stops. Beyond it the reference is nodata, which ``source_datum --offset-surface`` treats
as "leave the pixel alone" — so a source reaching further inland keeps raw IGN69 altitudes
there, and its ``corrected_fraction`` is what says so.

Publication is gated on the RAM benchmarks and on coverage, because either alone ships a
broken surface: 19 ports validate the composition, its sign, and its centring against Shom's
own published ties, and ``check_coverage`` validates that the grid is the size it was.

Accuracy is bounded by the inputs, not by this composition. Against all 167 RAM sites the
surface has a median residual of 0.000 m and 0.27 m of scatter (1 sd), worst 1.40 m — the
spread between a modelled tidal surface and a levelled port benchmark. That is the ceiling on
a correction whose job is to remove 1.3-6.8 m of datum error.

Run from pipelines/:
    uv run python datum_grid_fr.py              # fetch the inputs and compose
    uv run python datum_grid_fr.py --check
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request

import numpy as np
import rasterio
from rasterio.transform import Affine
from rasterio.windows import Window

from source_normalize import COG_OPTS

# Shom's INSPIRE pre-packaged download service — anonymous, and the version IS the pin.
SHOM_PACK = ("https://services.data.shom.fr/INSPIRE/telechargement/prepackageGroup/"
             "BATHYELLI_2_1_PACK_DL/prepackage/BATHYELLI_ZH_ell_V2_1/file/"
             "BATHYELLI_ZH_ell_V2_1.7z")
# Some services.data.shom.fr routes reject a request carrying no Referer.
SHOM_HEADERS = {"Referer": "https://diffusion.shom.fr/"}

# IGN's geoid grids as PROJ redistributes them, each with the shift onto France's LEGAL chart
# zero. RAC23 is listed second because it WINS the overlap: its whole extent is Corsica, whose
# Litto3D is IGN78, while RAF20's bbox merely spills 0.15 deg past the mainland's last land.
#
# BATHYELLI models LAT; a French port's ZH is fixed at or below it, so the model sits
# systematically ABOVE the legal zero — the direction that charts deeper than the chart does.
# The shift is the median of (composed - published) over Shom's own RAM tie table, measured per
# geoid region because Corsica's ports run twice the mainland's offset: 0.181 m over 156
# mainland sites (regionally flat: +0.14 Atlantic SW, +0.16 Bretagne, +0.16 Manche, +0.26 Med)
# and 0.346 m over 11 Corsican ones. It centres the surface on the legal datum instead of
# leaving a one-sided bias, and it moves depths shoal-ward, so its own error is the safe way up.
GEOIDS = [("https://cdn.proj.org/fr_ign_RAF20.tif", "fr_ign_RAF20.tif", 0.181),
          ("https://cdn.proj.org/fr_ign_RAC23.tif", "fr_ign_RAC23.tif", 0.346)]

STORE = "store/datum"
OUT = f"{STORE}/ign69_zh.tif"
NODATA = -9999.0

# 0.005 deg (~400 m) is ~2x finer than BATHYELLI's 0.7 km median node spacing, so the
# triangulation is resolved rather than resampled. Finer buys nothing: the field moves ~2 cm
# per km, so a pixel spans well under a centimetre of it.
RES = 0.005

# Delaunay interpolation fills the convex HULL, which bridges the Channel to England and
# reaches inland across France — values invented across gaps BATHYELLI never sampled. Beyond
# this distance from a real node the reference goes nodata instead. Sits above the largest
# node-to-node gap in either file (6.0 km) and far below the scale of a hull bridge.
MAX_NODE_KM = 8.0
KM_PER_DEG = 111.32

# Shom's Références Altimétriques Maritimes, the per-port tie between the hydrographic zero and
# the legal altimetric system (RAM.csv column ZH_Ref, published in the same prepackage service:
# RAM_PACK_DL/prepackage/RAM_PACK). Chosen to span the full -0.19…-6.77 m range, both geoids,
# and every Litto3D zone on the mainland — Cancale and Cavalaire alone are the 6.6 m spread
# that rules out any single scalar.
BENCHMARKS = [
    # (site, lon, lat, published ZH_Ref in metres, legal frame)
    ("Dunkerque", 2.366667, 51.050000, -2.693, "IGN69"),
    ("Calais", 1.867500, 50.969333, -3.459, "IGN69"),
    ("Cherbourg", -1.635500, 49.651167, -3.285, "IGN69"),
    ("Le Havre", 0.106167, 49.481667, -4.375, "IGN69"),
    ("Saint-Malo", -2.027500, 48.641167, -6.289, "IGN69"),
    ("Cancale", -1.826667, 48.680000, -6.774, "IGN69"),
    ("Roscoff", -3.965694, 48.718694, -4.764, "IGN69"),
    ("Brest", -4.494722, 48.382500, -3.635, "IGN69"),
    ("Concarneau", -3.906667, 47.873333, -2.534, "IGN69"),
    ("Lorient (Port de commerce)", -3.354500, 47.737500, -2.630, "IGN69"),
    ("Saint-Nazaire", -2.201667, 47.266944, -3.160, "IGN69"),
    ("La Rochelle - La Pallice", -1.221667, 46.160000, -3.503, "IGN69"),
    ("Arcachon", -1.163667, 44.664333, -1.980, "IGN69"),
    ("Toulon", 5.914669, 43.122678, -0.252, "IGN69"),
    ("Marseille (Corniche)", 5.353736, 43.278789, -0.329, "IGN69"),
    ("Cavalaire", 6.535500, 43.172500, -0.190, "IGN69"),
    ("Nice", 7.285267, 43.695500, -0.344, "IGN69"),
    ("Bastia", 9.455000, 42.700000, -0.485, "IGN78"),
    ("Ajaccio", 8.762839, 41.922703, -0.368, "IGN78"),
]
# Per-site agreement is capped by the inputs, not by this composition: a port's ZH is a levelled
# benchmark, BATHYELLI is a model interpolated toward it, and the two disagree by 0.27 m (1 sd)
# over the whole 167-site table. So the per-site bound only has to catch a gross error — a sign
# flip is 12 m out, the wrong geoid 45 m, a dropped legal-zero shift 0.18 m in ONE direction.
BENCHMARK_TOL = 0.45
# …and that last one is what the mean is for. A systematic shift hides inside a per-site bound
# this loose, so the set's mean residual is gated an order of magnitude tighter: it is the only
# check that fails when the surface is centred on modelled LAT instead of the legal zero.
BENCHMARK_BIAS_TOL = 0.10

# Valid pixels in the published surface. Composition is deterministic under the pinned inputs,
# so the band absorbs only resampler edge changes; anything that truncates the node set, drops
# a geoid, or loosens the distance bound moves it by far more.
EXPECTED_TOTAL_PX = 1_154_409
COVERAGE_TOL = 0.02


# ── inputs ───────────────────────────────────────────────────────────────────────────

def fetch(url, path, headers=None):
    """Download once, atomically. The URL is the version pin, so a present file is the pin."""
    if os.path.isfile(path):
        return path
    print(f"fetching {url}")
    request = urllib.request.Request(url, headers=headers or {})
    tmp = path + ".part"
    with urllib.request.urlopen(request, timeout=300) as response, open(tmp, "wb") as out:
        shutil.copyfileobj(response, out)
    os.replace(tmp, path)
    return path


def harvest(dest):
    """Fetch BATHYELLI + both geoid grids into ``dest``, returning (glhi paths, geoid paths)."""
    import py7zr

    os.makedirs(dest, exist_ok=True)
    archive = fetch(SHOM_PACK, os.path.join(dest, os.path.basename(SHOM_PACK)), SHOM_HEADERS)
    root = os.path.join(dest, "bathyelli")
    # A fresh tree per extraction: a new BATHYELLI edition unpacked over an old one would
    # leave both editions' nodes in the DATA directory and triangulate them together.
    shutil.rmtree(root, ignore_errors=True)
    with py7zr.SevenZipFile(archive) as z:
        members = [n for n in z.getnames() if n.lower().endswith(".glhi")]
        if not members:
            sys.exit(f"{archive}: no .glhi members — the product layout changed")
        z.extract(path=root, targets=members)
    glhi = sorted(os.path.join(base, name)
                  for base, _dirs, names in os.walk(root)
                  for name in names if name.lower().endswith(".glhi"))
    geoids = [fetch(url, os.path.join(dest, name)) for url, name, _bias in GEOIDS]
    return glhi, geoids


def read_glhi(path):
    """A BATHYELLI ``.glhi`` as (lon, lat, ZH ellipsoidal height, uncertainty) columns.

    v2.0 padded the file with 0.0-height rows and v2.1 ships only real nodes; drop them either
    way, because a 0 m ellipsoidal height would triangulate as a 45 m cliff."""
    nodes = np.loadtxt(path, dtype="float64")
    if nodes.ndim != 2 or nodes.shape[1] != 4:
        sys.exit(f"{path}: expected 4 columns, got shape {nodes.shape}")
    return nodes[nodes[:, 2] != 0.0]


# ── composition ──────────────────────────────────────────────────────────────────────

def grid_axes(nodes, res=RES):
    """Pixel-centre lon/lat axes and the transform of the grid covering every node, snapped
    outward onto the output lattice."""
    west = np.floor(nodes[:, 0].min() / res) * res
    east = np.ceil(nodes[:, 0].max() / res) * res
    south = np.floor(nodes[:, 1].min() / res) * res
    north = np.ceil(nodes[:, 1].max() / res) * res
    width = int(round((east - west) / res))
    height = int(round((north - south) / res))
    transform = Affine(res, 0.0, west, 0.0, -res, north)
    lons = west + (np.arange(width) + 0.5) * res
    lats = north - (np.arange(height) + 0.5) * res
    return lons, lats, transform, (height, width)


def interpolate(nodes, lons, lats, max_node_km=MAX_NODE_KM):
    """BATHYELLI's scattered ZH heights on a regular grid, NaN where no node is near.

    Linear over the Delaunay triangulation, not an inverse-distance or spline fit: the tidal
    surface is smooth and densely sampled, so a linear blend of the three surrounding nodes
    reproduces it without the overshoot a higher order invents between them — and it can never
    return a value outside the range of the nodes that produced it, which is what keeps a
    reference that gets subtracted from charted depths honest."""
    from scipy.interpolate import LinearNDInterpolator
    from scipy.spatial import cKDTree

    grid_lon, grid_lat = np.meshgrid(lons, lats)
    values = LinearNDInterpolator(nodes[:, :2], nodes[:, 2])(grid_lon, grid_lat)

    # Distances in km, not degrees: a degree of longitude is 2/3 of a degree of latitude here,
    # so a bound measured in degrees would reach half again as far east-west as intended.
    scale = np.cos(np.radians(np.mean(lats)))
    tree = cKDTree(np.c_[nodes[:, 0] * scale * KM_PER_DEG, nodes[:, 1] * KM_PER_DEG])
    distance, _ = tree.query(np.c_[grid_lon.ravel() * scale * KM_PER_DEG,
                                   grid_lat.ravel() * KM_PER_DEG])
    return np.where(distance.reshape(values.shape) <= max_node_km, values, np.nan)


def sample_geoid(path, lons, lats):
    """A PROJ geoid grid bilinearly on the output lattice, NaN outside its own extent.

    The grids are plain lon/lat (EPSG:9781, RGF93 v2 — a metre of the output's own 400 m pixel
    away from 4326), so this is interpolation, not reprojection."""
    from scipy.ndimage import map_coordinates

    with rasterio.open(path) as src:
        data = src.read(1).astype("float64")
        inverse = ~src.transform
        bounds = src.bounds
    grid_lon, grid_lat = np.meshgrid(lons, lats)
    cols, rows = inverse * (grid_lon, grid_lat)
    out = map_coordinates(data, np.stack([rows - 0.5, cols - 0.5]), order=1, mode="nearest")
    # mode="nearest" would otherwise extend the edge row/column across the whole output.
    inside = ((grid_lon >= bounds.left) & (grid_lon <= bounds.right) &
              (grid_lat >= bounds.bottom) & (grid_lat <= bounds.top))
    return np.where(inside, out, np.nan)


def compose(glhi_paths, geoid_paths, res=RES):
    """The reference grid, its transform, and a per-input node count.

    The Atlantic and Mediterranean node sets are triangulated SEPARATELY and unioned: one
    triangulation over both would span the 0.5 deg of Languedoc coast between them with
    triangles whose vertices sit 500 km apart."""
    sets = [(os.path.basename(p), read_glhi(p)) for p in glhi_paths]
    counts = {name: int(len(nodes)) for name, nodes in sets}
    lons, lats, transform, shape = grid_axes(np.vstack([n for _, n in sets]), res)

    zh = np.full(shape, np.nan)
    for name, nodes in sets:
        part = interpolate(nodes, lons, lats)
        # The two extents do not overlap (the Atlantic set ends at 2.54 E, the Mediterranean
        # one starts at 3.03 E), so a plain "fill what is still empty" union is unambiguous.
        zh = np.where(np.isnan(zh), part, zh)

    # The geoid and its region's legal-zero shift travel together — a pixel corrected with
    # RAC23 is a Corsican pixel, and must take Corsica's shift and no other.
    geoid = np.full(shape, np.nan)
    bias = np.full(shape, np.nan)
    for path, shift in zip(geoid_paths, [b for _url, _name, b in GEOIDS]):
        part = sample_geoid(path, lons, lats)
        # Later grid wins: RAC23 is Corsica's own datum region and must not be overwritten by
        # RAF20's bbox spilling across the Ligurian Sea.
        geoid = np.where(np.isnan(part), geoid, part)
        bias = np.where(np.isnan(part), bias, shift)

    reference = zh - geoid - bias
    valid = np.isfinite(reference)
    return np.where(valid, reference, NODATA).astype("float32"), transform, counts


def build(work_root=None, out=OUT):
    # Keyed to the output so a concurrent surface build cannot share — and race on — one
    # scratch tree.
    work = work_root or out + ".work"
    os.makedirs(work, exist_ok=True)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

    glhi, geoids = harvest(work)
    print(f"composing from {len(glhi)} node set(s) and {len(geoids)} geoid grid(s)")
    reference, transform, counts = compose(glhi, geoids)
    for name, n in sorted(counts.items()):
        print(f"  {name}: {n} nodes")

    height, width = reference.shape
    valid = int((reference != NODATA).sum())
    values = reference[reference != NODATA]
    report = {"nodes": counts, "total_px": valid, "res": RES, "max_node_km": MAX_NODE_KM,
              "size": [width, height],
              "bounds": [transform.c, transform.f + height * transform.e,
                         transform.c + width * transform.a, transform.f],
              "reference_min": float(values.min()), "reference_max": float(values.max()),
              "bathyelli": SHOM_PACK,
              "geoids": {name: bias for _url, name, bias in GEOIDS}}

    raw = os.path.join(work, "reference.tif")
    profile = dict(driver="GTiff", height=height, width=width, count=1, dtype="float32",
                   crs="EPSG:4326", nodata=NODATA, transform=transform,
                   tiled=True, blockxsize=512, blockysize=512, compress="deflate",
                   predictor=3)
    with rasterio.open(raw, "w", **profile) as dst:
        dst.write(reference, 1)

    tmp = out + ".tmp.tif"
    # OVERVIEWS=NONE: nothing reads a pyramid of the reference — reference_on and the checks
    # read it at full resolution.
    subprocess.run(["gdal_translate", "-of", "COG", *COG_OPTS, "-co", "OVERVIEWS=NONE",
                    "-co", "PREDICTOR=3", raw, tmp], check=True)
    # Gate publication on the ports AND on coverage: a composition regression must fail here,
    # never ship a surface that silently mis-corrects every Litto3D depth.
    check_benchmarks(tmp)
    check_coverage(report)
    os.replace(tmp, out)
    with open(report_path(out), "w") as f:
        json.dump(report, f, indent=2)
    shutil.rmtree(work, ignore_errors=True)
    print(f"{out}: {width}x{height}, {valid} valid px, "
          f"reference {values.min():.3f}…{values.max():.3f} m, "
          f"{os.path.getsize(out) / 1e6:.1f} MB")


# ── verification ─────────────────────────────────────────────────────────────────────

def report_path(out=OUT):
    """The coverage sidecar beside a composed surface."""
    return os.path.splitext(out)[0] + ".json"


def sample_bilinear(path, lon, lat):
    """One point out of a 4326 raster, bilinear on pixel centres. None if any contributing
    pixel is nodata."""
    with rasterio.open(path) as src:
        col, row = ~src.transform * (lon, lat)
        col, row = col - 0.5, row - 0.5
        col0, row0 = int(np.floor(col)), int(np.floor(row))
        fx, fy = col - col0, row - row0
        cell = src.read(1, window=Window(col0, row0, 2, 2), boundless=True,
                       fill_value=src.nodata).astype("float64")
        if np.any(cell == src.nodata):
            return None
    top = cell[0, 0] * (1 - fx) + cell[0, 1] * fx
    bottom = cell[1, 0] * (1 - fx) + cell[1, 1] * fx
    return top * (1 - fy) + bottom * fy


def check_benchmarks(path=OUT, tol=BENCHMARK_TOL, bias_tol=BENCHMARK_BIAS_TOL):
    """The composed reference against Shom's published per-port ZH_Ref. The sign is the point:
    the file holds ZH's height in the legal frame, which is what a port publishes directly, so
    a benchmark reads back unnegated — the opposite of ``datum_grid.py``'s -S convention."""
    residuals = []
    for site, lon, lat, published, frame in BENCHMARKS:
        value = sample_bilinear(path, lon, lat)
        if value is None:
            raise AssertionError(f"{site}: no reference coverage at {lon},{lat}")
        residual = value - published
        residuals.append(residual)
        print(f"  {site:26s} {frame}  ZH_Ref={value:+.4f}  published={published:+.3f}  "
              f"residual={residual:+.4f}")
        assert abs(residual) <= tol, f"{site}: |{residual:.4f}| > {tol} m"
    bias = float(np.mean(residuals))
    assert abs(bias) <= bias_tol, \
        (f"the surface sits {bias:+.4f} m off the legal zero across {len(residuals)} ports "
         f"(> {bias_tol} m) — the per-region shift is wrong or missing")
    print(f"RAM benchmark ports ok ({len(residuals)} ports, worst |residual| = "
          f"{max(abs(r) for r in residuals):.4f} m, mean {bias:+.4f} m)")


def check_total_px(total, tol=COVERAGE_TOL):
    """The surface is the size it was. A truncated node set, a dropped geoid, or a distance
    bound that stopped bounding all land here and nowhere else."""
    if not EXPECTED_TOTAL_PX * (1 - tol) <= total <= EXPECTED_TOTAL_PX * (1 + tol):
        raise AssertionError(f"surface holds {total} valid px, expected {EXPECTED_TOTAL_PX} "
                             f"+/-{tol:.0%} — coverage changed, not just its values")
    return total


def check_coverage(report, tol=COVERAGE_TOL):
    """Both node sets reached the composition, and the grid is the size it was.

    The ports sample 20 points along the coast: a node set that parsed but composed to a
    fraction of itself, or a distance bound that quietly stopped bounding, moves none of them,
    and the resulting hole is a coast charted on raw IGN69 altitudes — metres too deep."""
    empty = sorted(name for name, n in report["nodes"].items() if not n)
    if len(report["nodes"]) < 2:
        raise AssertionError(f"expected 2 BATHYELLI node sets, composed {len(report['nodes'])}"
                             f": {sorted(report['nodes'])}")
    if empty:
        raise AssertionError(f"node set(s) parsed to 0 nodes: {', '.join(empty)}")
    total = check_total_px(report["total_px"], tol)
    print(f"coverage ok ({len(report['nodes'])} node sets, {total} valid px, "
          f"{total / EXPECTED_TOTAL_PX - 1:+.2%} vs expected)")


def _check():
    """Offline tier: the node filter, the grid lattice, the triangulation's interior accuracy
    and its distance bound, the geoid priority, and the coverage gate — on synthetic nodes.
    The grid-dependent tier runs on top whenever a composed surface is present: its counted
    size, its sidecar if one sits beside it, and the RAM ports."""
    import tempfile

    # read_glhi drops the 0.0-height padding v2.0 ships, keeps every real node.
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.glhi")
        with open(path, "w") as f:
            f.write("1.0 44.0 45.5 0.1\n1.1 44.0 0.0 0.0\n1.2 44.0 45.7 0.2\n")
        nodes = read_glhi(path)
        assert nodes.shape == (2, 4) and nodes[1, 2] == 45.7, nodes

    # A plane through the nodes must come back exactly: linear interpolation of a linear field
    # is the identity, so any residual here is a lattice or transform error, not interpolation.
    rng = np.random.default_rng(0)
    pts = rng.uniform([1.0, 44.0], [2.0, 45.0], size=(400, 2))
    plane = 40.0 + 2.0 * pts[:, 0] + 3.0 * pts[:, 1]
    nodes = np.c_[pts, plane, np.full(len(pts), 0.1)]
    lons, lats, transform, shape = grid_axes(nodes, res=0.01)
    assert transform.a == 0.01 and shape == (len(lats), len(lons)), (transform, shape)
    got = interpolate(nodes, lons, lats)
    grid_lon, grid_lat = np.meshgrid(lons, lats)
    want = 40.0 + 2.0 * grid_lon + 3.0 * grid_lat
    interior = np.isfinite(got)
    assert interior.sum() > 0.5 * got.size, f"only {interior.sum()} of {got.size} interpolated"
    assert np.nanmax(np.abs(got[interior] - want[interior])) < 1e-9, \
        float(np.nanmax(np.abs(got[interior] - want[interior])))

    # The distance bound is what keeps the hull from inventing a reference across a gap: two
    # clusters 1 deg (~80 km) apart must not be bridged, however happily Delaunay spans them.
    split = np.vstack([nodes, np.c_[pts + [2.0, 0.0], plane, np.full(len(pts), 0.1)]])
    lons2, lats2, _t, _s = grid_axes(split, res=0.01)
    bridged = interpolate(split, lons2, lats2)
    gap = (lons2 > 2.2) & (lons2 < 2.8)
    assert not np.isfinite(bridged[:, gap]).any(), "the distance bound let the hull bridge a gap"

    # Geoid priority: the second grid wins where it has data, the first survives elsewhere.
    a = np.where(np.isnan(np.array([np.nan, 2.0, np.nan])), np.array([1.0, 1.0, np.nan]),
                 np.array([np.nan, 2.0, np.nan]))
    assert a[0] == 1.0 and a[1] == 2.0 and np.isnan(a[2]), a

    assert check_total_px(EXPECTED_TOTAL_PX) == EXPECTED_TOTAL_PX
    assert check_total_px(int(EXPECTED_TOTAL_PX * 1.01)) > 0
    for bad in (int(EXPECTED_TOTAL_PX * 0.9), int(EXPECTED_TOTAL_PX * 1.1)):
        try:
            check_total_px(bad)
        except AssertionError:
            pass
        else:
            raise AssertionError(f"coverage gate accepted {bad}")
    for bad_report in ({"nodes": {"a": 1}, "total_px": EXPECTED_TOTAL_PX},
                       {"nodes": {"a": 1, "b": 0}, "total_px": EXPECTED_TOTAL_PX}):
        try:
            check_coverage(bad_report)
        except AssertionError:
            pass
        else:
            raise AssertionError(f"coverage gate accepted {bad_report}")
    print("datum_grid_fr.py self-check ok (offline tier)")

    if os.path.isfile(OUT):
        check_total_px(valid_px(OUT))
        if os.path.isfile(report_path(OUT)):
            with open(report_path(OUT)) as f:
                check_coverage(json.load(f))
        check_benchmarks(OUT)
    else:
        print(f"{OUT} absent — skipping the composed-surface tier "
              f"(build it with: uv run python datum_grid_fr.py)")


def valid_px(path):
    """Valid pixels in a composed surface, counted block by block."""
    with rasterio.open(path) as src:
        return sum(int(np.count_nonzero(src.read(1, window=window) != src.nodata))
                   for _, window in src.block_windows(1))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="run the self-check and exit")
    parser.add_argument("--work", help="scratch directory holding the fetched inputs")
    parser.add_argument("--out", default=OUT)
    args = parser.parse_args()
    if args.check:
        _check()
    else:
        build(args.work, args.out)
