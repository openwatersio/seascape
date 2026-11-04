"""Download ONE enumerated item to store/source/<id>/raw/<hash>.

The item is identified by its URL hash (config.item_hash — first 16 hex of sha256(url)),
so the output path is known before the bytes are: a URL that lies about its extension can't
rename the artifact, and inserting an item into items.txt can't re-key the others (each raw
name follows its own URL, not a list position), so only genuinely new URLs ever refetch.

Self-migration, zero refetch: the legacy layout named raw files by their list INDEX
(raw/<i>). When raw/<hash> is missing but the item's legacy raw/<index> still exists, it is
renamed into place instead of re-downloading — a store full of index-named tiles re-keys to
hashes on first touch without re-pulling a byte.

Writes via a dot-prefixed temp name + rename (removed on failure) so an interrupted download
never leaves a truncated file at the declared path — and, because raw/* globs skip dotfiles,
never leaves anything staging could misread.

Run from pipelines/:  uv run python source_fetch.py <source-id> <hash>
"""

import os
import sys

import config
import utils


TIFF = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")  # classic and BigTIFF headers


def chs(source):
    """NONNA's CHS GeoServer sheds load for minutes, and under load can answer 200 with an XML
    error report instead of a GeoTIFF."""
    return source.startswith("nonna_")


def busy_wait(source):
    """First retry wait after a 429, a 503 or a rejected body."""
    return 30 if chs(source) else 1


def fetch(source, item_hash):
    items = config.items(source)
    by_hash = {config.item_hash(u): (i, u) for i, u in enumerate(items)}
    if item_hash not in by_hash:
        sys.exit(f"{source}: no enumerated item hashes to {item_hash} "
                 f"(items.txt has {len(items)} entries) — re-run the enumerate checkpoint")
    index, url = by_hash[item_hash]
    root = f"store/source/{source}/raw"
    dest = f"{root}/{item_hash}"
    os.makedirs(root, exist_ok=True)

    legacy = f"{root}/{index}"  # the pre-hash layout named this item raw/<index>
    if os.path.exists(legacy):
        print(f"{source}[{item_hash}]: migrating legacy raw/{index} -> raw/{item_hash} (no refetch)")
        os.replace(legacy, dest)
        return

    tmp = f"{root}/.{item_hash}.tmp"  # dotfile: invisible to raw/* globs
    print(f"{source}[{item_hash}]: {url} -> {dest}")
    try:
        utils.http_download(url, tmp, busy_wait=busy_wait(source), magic=TIFF if chs(source) else None)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    os.replace(tmp, dest)


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: source_fetch.py <source-id> <hash>")
    fetch(sys.argv[1], sys.argv[2])


def _check_backoff():
    """NONNA waits minutes after a 429, a 503 or a body that isn't a TIFF; everything else keeps
    the short backoff and takes the body as sent."""
    import time
    from types import SimpleNamespace
    from unittest import mock

    import requests

    def waits(status, source):
        response = SimpleNamespace(status_code=status) if status else None
        error = requests.exceptions.RequestException(response=response)
        sleeps = []
        with mock.patch.object(requests, "get", side_effect=error), \
                mock.patch.object(time, "sleep", sleeps.append):
            try:
                utils.http_download("https://x/a.tif", os.devnull, busy_wait=busy_wait(source))
                assert False, "expected the last attempt to raise"
            except requests.exceptions.RequestException:
                pass
        return sleeps

    assert waits(503, "nonna_10") == [30, 60, 120, 240], "a busy CHS waits minutes"
    assert waits(429, "nonna_100") == [30, 60, 120, 240], "a rate limit waits like a busy server"
    assert waits(500, "nonna_10") == waits(None, "nonna_10") == [1, 2, 4, 8], "other failures stay short"
    assert waits(503, "cudem") == [1, 2, 4, 8], "other sources stay short"

    class Ok:  # a 200 whose body is whatever the server sent
        def __init__(self, body):
            self.body, self.headers = body, {}
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def raise_for_status(self):
            pass
        def iter_content(self, _):
            yield self.body

    import shutil
    import struct
    import tempfile
    d = tempfile.mkdtemp()
    out = os.path.join(d, "raw")

    def outcome(body, source):
        """(sleeps, error) for a 200 carrying body."""
        sleeps = []
        with mock.patch.object(requests, "get", return_value=Ok(body)), \
                mock.patch.object(time, "sleep", sleeps.append):
            try:
                utils.http_download("https://x/a.tif", out, busy_wait=busy_wait(source),
                                    magic=TIFF if chs(source) else None)
                return sleeps, None
            except requests.exceptions.RequestException as e:
                return sleeps, str(e)

    try:
        bad = [b'<?xml version="1.0"?><ows:ExceptionReport/>', b'\xef\xbb\xbf<?xml version="1.0"?>',
               "<?xml?>".encode("utf-16"), b'{"error": "busy"}', b""]
        for body in bad:
            sleeps, err = outcome(body, "nonna_10")
            assert err and "unexpected body" in err and sleeps == [30, 60, 120, 240], (body, sleeps, err)
            assert outcome(body, "cudem") == ([], None), "other sources take the body as sent"
        # byte-order mark then version: 42 is classic TIFF, 43 is BigTIFF
        for order, fmt in ((b"II", "<H"), (b"MM", ">H")):
            for version in (42, 43):
                header = order + struct.pack(fmt, version)
                assert outcome(header + b"tiff", "nonna_10") == ([], None), header
        sleeps = []
        with mock.patch.object(requests, "get", side_effect=[Ok(bad[0]), Ok(b"II*\x00tiff")]), \
                mock.patch.object(time, "sleep", sleeps.append):
            utils.http_download("https://x/a.tif", out, busy_wait=30, magic=TIFF)
        assert sleeps == [30] and open(out, "rb").read() == b"II*\x00tiff", "a retry replaces the report"
    finally:
        shutil.rmtree(d)


def _check():
    """Offline: an unknown hash exits, a failed download removes its temp file, and the
    legacy raw/<index> layout self-migrates to raw/<hash> without a download."""
    import shutil
    import tempfile
    from glob import glob

    _check_backoff()

    d = tempfile.mkdtemp()
    cwd, saved = os.getcwd(), config.SOURCES_DIR
    saved_dl = utils.http_download
    try:
        os.chdir(d)
        config.SOURCES_DIR = "sources"
        sid = "_fetch_selfcheck"
        os.makedirs(f"sources/{sid}")
        with open(f"sources/{sid}/file_list.txt", "w") as f:
            f.write("https://x/a.tif\nhttps://x/b.tif\n")
        os.makedirs(f"store/source/{sid}")
        with open(f"store/source/{sid}/items.txt", "w") as f:
            f.write("https://x/a.tif\nhttps://x/b.tif\n")
        h0 = config.item_hash("https://x/a.tif")
        h1 = config.item_hash("https://x/b.tif")

        try:
            fetch(sid, "deadbeefdeadbeef")
            assert False, "expected an unknown hash to exit"
        except SystemExit as e:
            assert "no enumerated item" in str(e), e

        # self-migration: a legacy raw/<index> for item 1 renames to raw/<h1>, no download.
        def forbidden_download(url, dest, **_):
            raise AssertionError(f"must not download during self-migration: {url}")

        utils.http_download = forbidden_download
        os.makedirs(f"store/source/{sid}/raw")
        with open(f"store/source/{sid}/raw/1", "wb") as f:
            f.write(b"legacy-bytes")
        fetch(sid, h1)
        assert not os.path.exists(f"store/source/{sid}/raw/1"), "legacy index file must be renamed away"
        assert open(f"store/source/{sid}/raw/{h1}", "rb").read() == b"legacy-bytes", "renamed in place"

        # a genuinely new item (no legacy file) downloads; a failure leaves nothing behind.
        def failing_download(url, dest, **_):
            with open(dest, "w") as f:
                f.write("partial")
            raise RuntimeError("simulated network failure")

        utils.http_download = failing_download
        try:
            fetch(sid, h0)
            assert False, "expected the simulated failure to raise"
        except RuntimeError:
            pass
        leftovers = glob(f"store/source/{sid}/raw/.*") + [
            p for p in glob(f"store/source/{sid}/raw/*") if os.path.basename(p) != h1]
        assert not leftovers, f"failed fetch must leave nothing behind: {leftovers}"
    finally:
        utils.http_download = saved_dl
        os.chdir(cwd)
        config.SOURCES_DIR = saved
        shutil.rmtree(d, ignore_errors=True)
    print("source_fetch.py self-check ok")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        _check()
    else:
        main()
