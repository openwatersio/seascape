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
        utils.http_download(url, tmp)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    os.replace(tmp, dest)


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: source_fetch.py <source-id> <hash>")
    fetch(sys.argv[1], sys.argv[2])


def _check():
    """Offline: an unknown hash exits, a failed download removes its temp file, and the
    legacy raw/<index> layout self-migrates to raw/<hash> without a download."""
    import shutil
    import tempfile
    from glob import glob

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
        def forbidden_download(url, dest):
            raise AssertionError(f"must not download during self-migration: {url}")

        utils.http_download = forbidden_download
        os.makedirs(f"store/source/{sid}/raw")
        with open(f"store/source/{sid}/raw/1", "wb") as f:
            f.write(b"legacy-bytes")
        fetch(sid, h1)
        assert not os.path.exists(f"store/source/{sid}/raw/1"), "legacy index file must be renamed away"
        assert open(f"store/source/{sid}/raw/{h1}", "rb").read() == b"legacy-bytes", "renamed in place"

        # a genuinely new item (no legacy file) downloads; a failure leaves nothing behind.
        def failing_download(url, dest):
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
