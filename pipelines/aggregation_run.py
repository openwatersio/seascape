"""Run the aggregation: reproject -> merge -> tile, per dirty aggregation tile.

Vendored from mapterhorn (BSD-3). Picks the latest aggregation id, processes only
tiles whose source set changed since the previous run (or all on the first run),
skips those already marked done, and parallelizes across tiles with a process pool.

CLI:
  aggregation_run.py                 process every dirty tile (local `just planet`)
  aggregation_run.py shard <i> <n>   process dirty[i::n] (CI matrix shard i of n)
  aggregation_run.py matrix <max>    print the CI shard matrix JSON (sized to dirt)

`shard` re-derives the same sorted dirty list each runner pulls from R2 and takes a
strided slice, so shards partition the work with no overlap and no coordination.
"""

import json
import os
import shutil
import sys
from glob import glob
from multiprocessing import Pool

import aggregation_merge
import aggregation_reproject
import aggregation_tile
import contour_run
import smooth
import utils

# Both forks share the merged DEM; set these to 1 for raster-only / no-smooth runs.
SKIP_CONTOURS = os.environ.get("SKIP_CONTOURS", "")
SKIP_SMOOTH = os.environ.get("SKIP_SMOOTH", "")


def run(filepath):
    item = filepath.split("/")[-1].replace("-aggregation.csv", "")
    print(f"{item} start")
    aggregation_reproject.reproject(filepath)
    aggregation_merge.merge(filepath)
    tmp_folder = filepath.replace("-aggregation.csv", "-tmp")
    if not SKIP_SMOOTH:
        smooth.smooth_merged(tmp_folder)     # slope-selective blur, shared by both
    aggregation_tile.main(filepath)          # raster Terrain-RGB tiles
    if not SKIP_CONTOURS:
        contour_run.generate(filepath)       # vector contours off the merged DEM
    shutil.rmtree(tmp_folder)
    utils.run_command(f'touch {filepath.replace("-aggregation.csv", "-aggregation.done")}')
    print(f"{item} end")


def existing_pmtiles():
    """Basenames of pmtiles already in the store. From a CI-provided listing
    (store/pmtiles-keys.txt = the R2 keys) when present, else a local scan."""
    keyfile = "store/pmtiles-keys.txt"
    if os.path.isfile(keyfile):
        with open(keyfile) as f:
            return {line.strip().split("/")[-1] for line in f if line.strip()}
    return {p.split("/")[-1] for p in glob("store/pmtiles/**/*.pmtiles", recursive=True)}


def dirty_filepaths():
    """Sorted aggregation CSVs to (re)build: tiles whose covering changed since the
    previous run (all on the first run) PLUS any whose pmtiles is missing; minus any
    already marked .done. The missing-pmtiles check is load-bearing: `plan` pushes
    coverings to R2 before aggregate builds them, so a prior failed build can leave a
    covering with no tile — without this the diff would call it clean forever."""
    aggregation_ids = utils.get_aggregation_ids()
    aggregation_id = aggregation_ids[-1]
    all_csvs = sorted(glob(f"store/aggregation/{aggregation_id}/*-aggregation.csv"))
    if len(aggregation_ids) < 2:
        changed = set(all_csvs)
    else:
        names = utils.get_dirty_aggregation_filenames(aggregation_id, aggregation_ids[-2])
        changed = {f"store/aggregation/{aggregation_id}/{name}" for name in names}

    have = existing_pmtiles()

    def needs_build(csv):
        if csv in changed:
            return True
        pmtiles = csv.split("/")[-1].replace("-aggregation.csv", "") + ".pmtiles"
        return pmtiles not in have

    dirty = [fp for fp in all_csvs if needs_build(fp)]
    dirty = [fp for fp in dirty if not os.path.isfile(fp.replace("-aggregation.csv", "-aggregation.done"))]
    # Order heaviest-first so the strided shard slices (dirty[i::n]) each draw a
    # balanced mix instead of one shard piling up the dense high-zoom tiles (the
    # straggler). child_z is a strong cost proxy: each level quadruples the output
    # tiles and multiplies the contour feature count. Deterministic, so every runner
    # derives the identical order and the shards still partition with no overlap.
    def child_z(fp):
        return int(fp.split("/")[-1].replace("-aggregation.csv", "").split("-")[3])
    return sorted(dirty, key=lambda fp: (-child_z(fp), fp))


def run_all(filepaths):
    if not filepaths:
        print("nothing to do.")
        return
    print(f"start aggregating {len(filepaths)} items...")
    # Each tile holds a multi-GB merged DEM (a max 32768px tile ≈ 4 GB float32, more
    # with the halo + smooth) so peak RAM ≈ workers × DEM. Cap workers in CI via
    # AGG_PROCESSES to stay under runner RAM; unset/0 = all cores (local builds).
    procs = int(os.environ.get("AGG_PROCESSES", "0")) or None
    with Pool(procs) as pool:
        pool.starmap(run, [(fp,) for fp in filepaths], chunksize=1)


def main(argv):
    if argv[:1] == ["matrix"]:
        # Size the CI matrix to the dirt: <= max shards, >= 1 (a clean run still
        # spins one no-op shard, keeping the bundle's `needs` graph simple).
        n = min(int(argv[1]), max(len(dirty_filepaths()), 1))
        print(json.dumps([{"i": i, "n": n} for i in range(n)]))
    elif argv[:1] == ["shard"]:
        i, n = int(argv[1]), int(argv[2])
        run_all(dirty_filepaths()[i::n])
    elif not argv:
        run_all(dirty_filepaths())
    else:
        sys.exit("usage: aggregation_run.py [shard <i> <n> | matrix <max>]")


if __name__ == "__main__":
    main(sys.argv[1:])
