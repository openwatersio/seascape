"""End-to-end self-check for the aggregation -> downsampling -> bundle engine.

Builds two synthetic sources in an isolated tmp dir — a coarse broad ``base``
(-100 m, native ~z10) and a fine small ``fine`` (-50 m, native ~z13 but capped to
z11 in metadata) inside it — then runs the whole engine and asserts:

  - the per-source max_zoom CAP binds (fine renders at z11, not its native z13);
  - PRIORITY: at the fine source's zoom the merged value is the fine value (-50),
    not the base (-100) — highest-maxzoom source wins in overlap;
  - the base shows through where fine is absent (-100 present);
  - the E1 split: planet.pmtiles spans z0..macrotile_z and the deeper fine tiles
    land in the fine.pmtiles overlay (so the engine covers z0..11 across bundles).

Run from pipelines/:  uv run python test_engine.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np
import rasterio
from rasterio.transform import from_origin

PIPE = os.path.dirname(os.path.abspath(__file__))


def run(tmp, *args):
    # Small macrotile_z / num_overviews keep the synthetic rasters tiny.
    # SKIP_CONTOURS/SKIP_SMOOTH: this is the raster priority test (both have/need none).
    env = {**os.environ, "SOURCES_DIR": "sources", "PYTHONPATH": PIPE,
           "MACROTILE_Z": "10", "NUM_OVERVIEWS": "2", "SKIP_CONTOURS": "1", "SKIP_SMOOTH": "1"}
    subprocess.run([sys.executable, os.path.join(PIPE, args[0]), *args[1:]],
                   cwd=tmp, env=env, check=True)


def make_source(tmp, sid, west, north, deg, px, value, max_zoom):
    os.makedirs(f"{tmp}/sources/{sid}", exist_ok=True)
    os.makedirs(f"{tmp}/store/source/{sid}", exist_ok=True)
    arr = np.full((px, px), value, dtype="float32")
    res = deg / px
    with rasterio.open(f"{tmp}/store/source/{sid}/{sid}_0.tif", "w", driver="GTiff",
                       height=px, width=px, count=1, dtype="float32", nodata=-9999,
                       crs="EPSG:4326", transform=from_origin(west, north, res, res)) as d:
        d.write(arr, 1)
    with open(f"{tmp}/sources/{sid}/metadata.json", "w") as f:
        json.dump({"name": sid, "max_zoom": max_zoom}, f)


def decode_bundles(tmp):
    """Return {zoom: [median elevation per tile]} across ALL bundle pmtiles —
    planet (z0..PLANET_MAX_ZOOM) plus the per-source overlays above it (E1
    routes child_z > PLANET_MAX_ZOOM into <source>.pmtiles, not planet)."""
    import glob
    import imagecodecs
    from pmtiles.reader import Reader, MmapSource, all_tiles
    import encode

    by_zoom = {}
    for path in sorted(glob.glob(f"{tmp}/store/bundle/*.pmtiles")):
        with open(path, "r+b") as f:
            reader = Reader(MmapSource(f))
            for (z, x, y), tile_bytes in all_tiles(reader.get_bytes):
                elev = encode.decode(imagecodecs.webp_decode(tile_bytes).astype("float32"))
                by_zoom.setdefault(z, []).append(float(np.median(elev)))
    return by_zoom


def main():
    tmp = tempfile.mkdtemp()
    try:
        # base: 1°x1° near the equator (-100, native ~z10). fine: 0.4°x0.4° inside
        # it (-50, native ~z14, well above the z11 cap so the cap binds on any GDAL
        # version). 0.4° spans more than a z11 tile so a z11 tile is fully fine.
        make_source(tmp, "base", west=-0.5, north=0.5, deg=1.0, px=1024, value=-100, max_zoom=10)
        make_source(tmp, "fine", west=-0.2, north=0.2, deg=0.4, px=4096, value=-50, max_zoom=11)

        run(tmp, "source_bounds.py", "base")
        run(tmp, "source_bounds.py", "fine")
        run(tmp, "aggregation_covering.py")
        run(tmp, "aggregation_run.py")
        run(tmp, "downsampling.py", "cover")
        run(tmp, "downsampling.py", "run")
        run(tmp, "bundle.py", "1")

        # covering wrote the cap into child_z: the deepest aggregation tile is z9, not z11.
        agg_dir = f"{tmp}/store/aggregation"
        agg_id = sorted(os.listdir(agg_dir))[-1]
        child_zs = [int(n.replace("-aggregation.csv", "").split("-")[3])
                    for n in os.listdir(f"{agg_dir}/{agg_id}") if n.endswith("-aggregation.csv")]
        assert max(child_zs) == 11, f"cap not applied (want 11): child_z={sorted(set(child_zs))}"

        # E1 split: planet caps at macrotile_z (10); the z11 fine tiles route to
        # the fine.pmtiles overlay. Both must exist.
        assert os.path.exists(f"{tmp}/store/bundle/planet.pmtiles"), "missing planet.pmtiles"
        assert os.path.exists(f"{tmp}/store/bundle/fine.pmtiles"), "missing fine overlay"
        by_zoom = decode_bundles(tmp)
        assert by_zoom, "no tiles in any bundle"
        max_z = max(by_zoom)
        assert max_z == 11, f"expected max zoom 11, got {max_z}"

        # PRIORITY: z11 only exists where fine is present; the fine-dominated z11
        # tile must read ~-50 (fine wins over base in overlap), not -100.
        z11_shallowest = max(by_zoom[11])  # -50 is shallower than base's -100
        assert z11_shallowest > -55, f"fine should win at z11 (shallowest z11 tile {z11_shallowest:.1f})"
        # base shows through somewhere: some tile reads ~-100.
        all_meds = [m for meds in by_zoom.values() for m in meds]
        assert min(all_meds) < -90, f"base (-100) should appear (min median {min(all_meds):.1f})"
        print(f"engine e2e ok — zooms {min(by_zoom)}..{max_z}, fine wins at z11 "
              f"({z11_shallowest:.1f}), base present (min {min(all_meds):.1f})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
