"""Offline end-to-end self-check for the source stage.

Pre-places a synthetic positive-depth raster in store/source (the fetch steps are
network-bound and validated against live data), then runs the transform chain
(datum -> normalize -> bounds -> polygonize -> tarball) in an isolated tmp dir and
asserts the artifacts + the bathymetry value transform (negate + offset on valid
pixels, nodata preserved).

Run from pipelines/:  uv run python test_source_stage.py
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
    env = {**os.environ, "SOURCES_DIR": "sources", "PYTHONPATH": PIPE}
    subprocess.run([sys.executable, os.path.join(PIPE, args[0]), *args[1:]],
                   cwd=tmp, env=env, check=True)


def check_remote_parsers():
    """Streaming-source URL/name parsing — the bits that bit us (mixed n39x00/n25X75 case,
    .shx sidecars in the manifest, virtual-host vs s3:// URLs, newest-gpkg resolution)."""
    import source_remote as sr
    import source_register_remote_urllist as ru
    import source_register_remote_geopkg as rg
    assert sr.to_vsicurl("https://noaa-nos-coastal-lidar-pds.s3.amazonaws.com/dem/X/t.tif") \
        == "/vsicurl/https://noaa-nos-coastal-lidar-pds.s3.amazonaws.com/dem/X/t.tif"
    assert sr.to_vsicurl("s3://b/k/t.tif") == "/vsicurl/https://b.s3.amazonaws.com/k/t.tif"
    assert ru.tile_lonlat("ncei19_n39x00_w075x25_2014v1.tif") == (-75.25, 39.0)
    assert ru.tile_lonlat("ncei19_n25X75_w080X25_2018v1.tif") == (-80.25, 25.75)  # uppercase X
    assert ru.tile_lonlat("southeast_topobathy_19.shx") is None  # sidecar, unparseable
    assert rg._newest_key(
        "<r><Contents><Key>a/x_20240101.gpkg</Key></Contents>"
        "<Contents><Key>a/x_20260616.gpkg</Key></Contents></r>").endswith("20260616.gpkg")
    print("remote parsers ok")


def check_http_download():
    """utils.http_download resume semantics against a local Range-aware server:
    fresh fetch, resume of a seeded .part (206 append), a server that ignores
    Range (200 rewrite), a mid-stream cut (short read → retried with resume),
    and a stale .part at EOF (416 → clean restart)."""
    import http.server
    import threading
    import utils

    payload = bytes(range(256)) * 200  # 51_200 bytes, position-dependent content
    state = {"cut_next": False, "ignore_range": False}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            start = 0
            rng = self.headers.get("Range")
            if rng and not state["ignore_range"]:
                start = int(rng.split("=")[1].rstrip("-"))
                if start >= len(payload):
                    self.send_response(416)
                    self.end_headers()
                    return
                self.send_response(206)
                self.send_header("Content-Range",
                                 f"bytes {start}-{len(payload) - 1}/{len(payload)}")
            else:
                self.send_response(200)
            body = payload[start:]
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if state["cut_next"]:
                state["cut_next"] = False
                self.wfile.write(body[:1000])  # lie, then hang up mid-stream
                self.connection.close()
                return
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/f"
    tmp = tempfile.mkdtemp()
    try:
        def fetch(name, seed=None):
            dest = f"{tmp}/{name}"
            if seed is not None:
                with open(dest + ".part", "wb") as f:
                    f.write(seed)
            utils.http_download(url, dest, chunk=4096, retries=3)
            with open(dest, "rb") as f:
                assert f.read() == payload, name
            assert not os.path.exists(dest + ".part"), name

        fetch("fresh")
        fetch("resumed", seed=payload[:10_000])       # 206 appends the tail
        state["ignore_range"] = True
        fetch("range_ignored", seed=payload[:10_000])  # 200 rewrites from scratch
        state["ignore_range"] = False
        state["cut_next"] = True
        fetch("cut_midstream")                         # short read → retry resumes
        fetch("stale_part", seed=payload)              # 416 → clean restart
        print("http_download resume ok")
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    check_remote_parsers()
    check_http_download()
    tmp = tempfile.mkdtemp()
    try:
        sid = "_synth"
        os.makedirs(f"{tmp}/sources/{sid}")
        os.makedirs(f"{tmp}/store/source/{sid}")
        nodata = -9999.0
        arr = np.array([[5, 10, nodata], [0, 2.5, 100]], dtype="float32")  # +depth
        # Pre-place the "downloaded" raster directly in store/source.
        with rasterio.open(f"{tmp}/store/source/{sid}/{sid}_0.tif", "w", driver="GTiff",
                           height=2, width=3, count=1, dtype="float32", nodata=nodata,
                           crs="EPSG:4326", transform=from_origin(-74.0, 40.7, 0.001, 0.001)) as d:
            d.write(arr, 1)

        with open(f"{tmp}/sources/{sid}/metadata.json", "w") as f:
            json.dump({"name": "synth", "max_zoom": 13}, f)

        for step in (["source_datum.py", sid, "--negate", "--offset", "-1"],
                     ["source_normalize.py", sid, "--crs", "EPSG:4326", "--nodata", "-9999"],
                     ["source_bounds.py", sid], ["source_polygonize.py", sid, "2"],
                     ["source_create_tarball.py", sid]):
            run(tmp, *step)

        norm = f"{tmp}/store/source/{sid}/{sid}_0.tif"
        assert os.path.exists(norm), norm
        with rasterio.open(norm) as s:
            out = s.read(1)
            valid = s.read_masks(1) != 0
        assert out[0, 0] == -6.0 and out[1, 2] == -101.0, out  # negate then -1 m
        assert not valid[0, 2], "nodata pixel should remain nodata"
        for artifact in (f"{tmp}/store/source/{sid}/bounds.csv",
                         f"{tmp}/store/polygon/{sid}.gpkg",
                         f"{tmp}/store/tar/{sid}.tar"):
            assert os.path.exists(artifact), artifact
        print("source stage e2e ok")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
