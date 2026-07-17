"""Download ONE file_list.txt entry to store/source/<id>/raw/<index>.

One URL, one
job, one retry unit. The raw name is the bare list index — extensionless on
purpose, so the output path is known before the bytes are (a URL that lies
about its extension can't rename the artifact; source_prep's stage() sniffs
the content).
Writes via a dot-prefixed temp name + rename (removed on failure) so an
interrupted download never leaves a truncated file at the declared path — and,
because raw/* globs skip dotfiles, never leaves anything staging could misread.

Run from pipelines/:  uv run python source_fetch.py <source-id> <index>
"""

import os
import sys

import config
import utils


def fetch(source, index):
    urls = config.file_list(source)
    if not 0 <= index < len(urls):
        sys.exit(f"{source}: index {index} out of range (file_list has {len(urls)} entries)")
    dest = f"store/source/{source}/raw/{index}"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    # Dot-prefixed temp: raw/* globs skip dotfiles, so a crashed download can never be
    # mistaken for a raw asset by staging; removed on failure so nothing lingers at all.
    tmp = f"store/source/{source}/raw/.{index}.tmp"
    print(f"{source}[{index}]: {urls[index]} -> {dest}")
    try:
        utils.http_download(urls[index], tmp)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    os.replace(tmp, dest)


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: source_fetch.py <source-id> <index>")
    fetch(sys.argv[1], int(sys.argv[2]))


def _check():
    """Offline: index bounds are enforced, and a failed download removes its temp file
    (a lingering raw/<i>.tmp used to crash the next prep's index parse)."""
    import shutil
    import tempfile
    from glob import glob

    saved = config.SOURCES_DIR
    try:
        config.SOURCES_DIR = "/nonexistent"
        try:
            fetch("no_such_source", 0)
            assert False, "expected an out-of-range index to exit"
        except SystemExit as e:
            assert "out of range" in str(e), e
    finally:
        config.SOURCES_DIR = saved

    d = tempfile.mkdtemp()
    cwd, saved = os.getcwd(), config.SOURCES_DIR
    saved_dl = utils.http_download
    try:
        os.chdir(d)
        config.SOURCES_DIR = "sources"
        sid = "_fetch_selfcheck"
        os.makedirs(f"sources/{sid}")
        with open(f"sources/{sid}/file_list.txt", "w") as f:
            f.write("https://x/a.tif\n")

        def failing_download(url, dest):
            with open(dest, "w") as f:
                f.write("partial")
            raise RuntimeError("simulated network failure")

        utils.http_download = failing_download
        try:
            fetch(sid, 0)
            assert False, "expected the simulated failure to raise"
        except RuntimeError:
            pass
        leftovers = glob(f"store/source/{sid}/raw/*") + glob(f"store/source/{sid}/raw/.*.tmp")
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
