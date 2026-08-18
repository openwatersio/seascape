#!/usr/bin/env -S uv run --script
"""Local profiling fixtures: small real windows over the Gulf marsh, the Delmarva lagoons and the
Iberian abyssal plain, cut from the published mosaic COGs by range read.

A real z15 marsh stem is a z8 macrotile: 65666 px square, 17.2 GB as Float32. That does not run
on a 17 GB laptop, so the fixtures are synthetic macrotile-z stems over the same ground —
`2^(15-12)*512 = 4096` px core for a z12 stem, at the macrotile's own child_z resolution. Nothing
in the stage-3 path reads utils.macrotile_z, and geometry comes from the stem's mercantile bounds
plus the raster's own transform, so these exercise the real code at real detail.

Each crop is written as the stem's OWN mosaic tile at exactly the stem's buffered bounds, so
mosaic.intersecting_tiles(stem) == [stem] and window_dem's -te matches the crop — no nodata fill
at the halo, and `smooth.py prepare-window` runs unmodified.

Tile URLs are content-addressed and therefore immutable; they are pinned here rather than
resolved at run time because the published pointer index lags the newest publish.

    fixtures.py build [site ...]   cut the crops + covering.txt + mask links into the fixture root
    fixtures.py masks             size-check the masks against the published artifacts
    fixtures.py list              show the fixture stems and their windows
    fixtures.py --check           self-check (geometry only, no network)
"""
import os
import subprocess
import sys

import mercantile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import aggregation_reproject  # noqa: E402
import mosaic  # noqa: E402

PUB = "https://data.openwaters.io/bathymetry"
ROOT = "store/profile/root"  # the isolated fixture store; never the real covering
CHILD_Z = 15

# Crops are range reads, and the public data.openwaters.io host refuses them ("Range downloading
# not supported by this server"), so cut from the bucket over /vsis3 with the R2 profile. Whole-
# file mask downloads still use the public URL, which needs no credentials.
TILE_BASE = os.environ.get("PERF_TILE_BASE", "/vsis3/data/bathymetry/mosaic/tiles")
R2_ENV = {
    "AWS_S3_ENDPOINT": "7822da9c68cfce969e63d07534969359.r2.cloudflarestorage.com",
    "AWS_VIRTUAL_HOSTING": "FALSE", "AWS_HTTPS": "YES", "AWS_REGION": "auto",
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR", "VSI_CACHE": "TRUE",
    "GDAL_HTTP_MULTIPLEX": "YES",
}

# site -> (published tile basename, centre lon, lat, why this site)
SITES = {
    "terrebonne": ("8-63-106-15-96818ff65980.tif", -90.75, 29.25,
                   "marsh + bay, GIWW crosses it; 4483 parts / 574600 vtx at z12"),
    "birdfoot": ("8-64-106-15-cf1e964b1781.tif", -89.35, 29.25,
                 "Southwest Pass; most parts of any probed site"),
    "barataria": ("8-64-106-15-cf1e964b1781.tif", -89.65, 29.45,
                  "Barataria Waterway; highest 0 m crossings/row (67)"),
    "atchafalaya": ("8-63-105-15-c5e84cd361a5.tif", -91.25, 29.60,
                    "open bay with real -2/-5 m bathymetry; the level-set control"),
    "delmarva": ("8-74-99-14-3fb472fde1f8.tif", -75.9103, 37.2197,
                 "back-barrier lagoons; the drying fragmentation site, 713 parts at z12"),
    "iberian-abyssal": ("8-110-91-10-db6827ae146c.tif", -24.61, 45.58,
                        "deep flat GEBCO-only ocean; the control every other site lacks — every "
                        "shoal-water measurement needs ground where it should trivially pass"),
}

# A crop must carry its macrotile's OWN child_z or it is not the surface production contours:
# the Delmarva macrotile is cz14, and reading it at CHILD_Z would invent detail the mosaic
# does not hold.
SITE_CHILD_Z = {"delmarva": 14, "iberian-abyssal": 10}

# Sites whose macrotile is coarse enough that a z12 stem would be a few hundred pixels. Build
# these at a lower z so the crop is a comparable amount of ground: the core is 2^(cz-z)*512 px.
SITE_Z = {"iberian-abyssal": 8}

# Published mask sizes, so a stale mask can never silently change a measurement. A stale or
# absent water.fgb changes the nodata layer AND the drying cut, which is exactly the mechanism
# the shallow-coarsening plan's grey-ribbon failure mode runs through.
MASKS = {"land.fgb": 1358292496, "water.fgb": 17814296120}


def stem_for(site, z=12, child_z=None):
    _tile, lon, lat, _why = SITES[site]
    t = mercantile.tile(lon, lat, z)
    return f"{z}-{t.x}-{t.y}-{child_z or SITE_CHILD_Z.get(site, CHILD_Z)}"


def geometry(stem):
    """The stem's buffered bounds, resolution and pixel dims — from the pipeline's own
    functions, so a fixture is byte-compatible with what window_dem would have produced."""
    z, x, y, cz = (int(v) for v in stem.split("-"))
    buf = mosaic.window_buffer_3857(stem)
    l, b, r, t = aggregation_reproject.buffered_bounds(mercantile.Tile(x=x, y=y, z=z), buf)
    res = aggregation_reproject.get_resolution(cz)
    return (l, b, r, t), res, int(round((r - l) / res)), int(round((t - b) / res))


def _run(*argv):
    subprocess.run([str(a) for a in argv], check=True, env={**os.environ, **R2_ENV})


def build(sites=None, z=12):
    sites = sites or list(SITES)
    tiles = f"{ROOT}/store/mosaic/tiles"
    for d in ("aggregation", "mosaic/tiles", "landmask", "window", "depare", "contour",
              "soundings", "polygon"):
        os.makedirs(f"{ROOT}/store/{d}", exist_ok=True)
    stems = []
    for site in sites:
        tile, lon, lat, _why = SITES[site]
        stem = stem_for(site, SITE_Z.get(site, z))
        stems.append(stem)
        out = f"{tiles}/{stem}.tif"
        if os.path.exists(out):
            print(f"{site}: {stem} present")
            continue
        (l, b, r, t), res, w, h = geometry(stem)
        vrt = f"/tmp/perf-{stem}.vrt"
        src = f"{TILE_BASE}/{tile}"
        print(f"{site}: {stem} {w}x{h} @ {res:.7f} m/px  <- {tile}")
        # The same two commands mosaic.window_dem uses, against the remote COG.
        _run("gdalbuildvrt", "-q", "-overwrite", "-te", l, b, r, t,
             "-tr", res, res, "-r", "bilinear", vrt, src)
        _run("gdal_translate", "-q", "-ot", "Float32", "-a_nodata", mosaic.NODATA,
             "-co", "TILED=YES", "-co", "BLOCKSIZE=512", "-co", "COMPRESS=ZSTD",
             "-co", "PREDICTOR=3", "-co", "BIGTIFF=IF_SAFER", vrt, out)
        os.remove(vrt)
        print(f"  {out} ({os.path.getsize(out)/1e6:.1f} MB)")
    # covering.txt in the fixture root only — the real covering stays untouched.
    with open(f"{ROOT}/store/aggregation/covering.txt", "w") as f:
        f.write("\n".join(stems) + "\n")
    for m in MASKS:
        link, target = f"{ROOT}/store/landmask/{m}", os.path.abspath(f"store/landmask/{m}")
        if not os.path.exists(link) and os.path.exists(target):
            os.symlink(target, link)
    print(f"\nfixture root {ROOT} ready: {len(stems)} stems")
    print("run stages with cwd=" + ROOT)


def masks():
    """Refuse to vouch for a measurement taken against a stale mask."""
    ok = True
    for m, want in MASKS.items():
        p = f"store/landmask/{m}"
        got = os.path.getsize(p) if os.path.exists(p) else 0
        state = "OK" if got == want else ("MISSING" if not got else "STALE")
        if got != want:
            ok = False
        print(f"{m:12s} {state:8s} local {got:>13,} want {want:>13,}")
    if not ok:
        print(f"\nrefresh: curl -C - -o store/landmask/<name>.new {PUB}/landmask/<name>")
        sys.exit(1)


def _list():
    for site in SITES:
        base = SITE_Z.get(site, 12)
        for z in range(base, base - 4, -1):
            stem = stem_for(site, z)
            (_bounds, res, w, h) = geometry(stem)
            gb = w * h * 4 / 1e9
            tif = f"{ROOT}/store/mosaic/tiles/{stem}.tif"
            print(f"{site:12s} z{z} {stem:18s} {w:6d}px {gb:6.2f} GB float32 "
                  f"{'present' if os.path.exists(tif) else '-'}")


def _check():
    """A z12 stem is exactly a 4096 px core at z15 resolution, and the buffered window adds
    the halo symmetrically on the pixel grid."""
    res = aggregation_reproject.get_resolution(CHILD_Z)
    stem = stem_for("terrebonne", 12)
    (l, b, r, t), res2, w, h = geometry(stem)
    assert res == res2
    assert w == h, (w, h)
    halo_px = (w - 4096) / 2
    assert halo_px == int(halo_px) and halo_px > 0, f"halo not whole px: {halo_px}"
    z, x, y, _cz = (int(v) for v in stem.split("-"))
    core = mercantile.xy_bounds(mercantile.Tile(x=x, y=y, z=z))
    assert abs((core.right - core.left) / res - 4096) < 1e-6, "z12 tile is not 4096 px at z15 res"
    # The buffer must be symmetric, and the core must sit inside the buffered window.
    assert abs((core.left - l) - (r - core.right)) < 1e-9
    assert l < core.left and r > core.right and b < core.bottom and t > core.top
    for site in SITES:
        s = stem_for(site, 12)
        assert len(s.split("-")) == 4
        assert SITES[site][0].endswith(".tif")
        # A crop read at the wrong child_z is a different surface, so the stem's child_z must be
        # the one its published macrotile carries — which the tile's own basename states.
        assert s.split("-")[3] == SITES[site][0].split("-")[3], \
            f"{site}: stem {s} does not carry its macrotile's child_z ({SITES[site][0]})"
    print(f"fixtures self-check ok (z12 -> {w}px window, {int(halo_px)}px halo, {res:.7f} m/px)")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["--check"]:
        _check()
    elif a[:1] == ["build"]:
        build(a[1:] or None)
    elif a[:1] == ["masks"]:
        masks()
    elif a[:1] == ["list"]:
        _list()
    else:
        sys.exit(__doc__.strip().split("\n\n")[-1])
