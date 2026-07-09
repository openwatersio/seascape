"""Run the aggregation: reproject -> merge -> tile, per dirty aggregation tile.

Vendored from mapterhorn (BSD-3). Picks the latest aggregation id, processes only
tiles whose source set changed since the previous run (or all on the first run),
skips those already marked done, and parallelizes across tiles with a process pool.

CLI:
  aggregation_run.py                 process every dirty tile (local `just planet`)
  aggregation_run.py freeze          write the dirty list into the covering (plan, once)
  aggregation_run.py shard <i> <n>   process the frozen dirty[i::n] (matrix shard i of n)
  aggregation_run.py matrix <max>    print the shard matrix JSON (sized to the dirt)

`shard` takes a strided slice of the single dirty list `freeze` wrote, so every shard
partitions the identical list — no overlap, no coordination, nothing recomputed per shard.
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
import depare_run
import landmask
import smooth
import soundings_run
import utils

# The forks share the merged DEM; set these to 1 for raster-only / no-smooth runs.
SKIP_CONTOURS = os.environ.get("SKIP_CONTOURS", "")
SKIP_SOUNDINGS = os.environ.get("SKIP_SOUNDINGS", "")
SKIP_DEPARE = os.environ.get("SKIP_DEPARE", "")
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
    if not SKIP_SOUNDINGS:
        soundings_run.generate(filepath)     # vector soundings off the same merged DEM
    if not SKIP_DEPARE:
        depare_run.generate(filepath)        # depth areas (ENC DEPARE): bands + drying + nodata
    if not os.environ.get("KEEP_TMP"):       # KEEP_TMP=1 preserves the merged DEM for re-running a fork
        shutil.rmtree(tmp_folder)
    utils.run_command(f'touch {filepath.replace("-aggregation.csv", "-aggregation.done")}')
    print(f"{item} end")


def covering_sorted():
    """Every aggregation CSV in the current covering, heaviest-first. Depends ONLY on the
    immutable covering, never on which tiles are already built — so every shard derives the
    identical tile order, and thus identical ownership, regardless of when it runs. child_z
    is a strong cost proxy (each level quadruples output tiles + contour features), so the
    heaviest-first stride hands each shard a balanced mix."""
    aggregation_id = utils.get_aggregation_ids()[-1]
    csvs = glob(f"store/aggregation/{aggregation_id}/*-aggregation.csv")

    def child_z(fp):
        return int(fp.split("/")[-1].replace("-aggregation.csv", "").split("-")[3])
    return sorted(csvs, key=lambda fp: (-child_z(fp), fp))


def dirty_predicate():
    """is_dirty(csv) → needs (re)build: covering changed since the previous run (all on the
    first run) OR its pmtiles is missing, and not already marked .done. The missing-pmtiles
    check is load-bearing self-heal: a covering is recorded before its tile is built, so a
    prior failed build can leave a covering with no tile — without this the diff would call
    it clean forever."""
    aggregation_ids = utils.get_aggregation_ids()
    aggregation_id = aggregation_ids[-1]
    if len(aggregation_ids) < 2:
        changed = None  # first run → everything is dirty
    else:
        names = utils.get_dirty_aggregation_filenames(aggregation_id, aggregation_ids[-2])
        changed = {f"store/aggregation/{aggregation_id}/{name}" for name in names}
    have = utils.existing_pmtiles()

    def is_dirty(csv):
        if os.path.isfile(csv.replace("-aggregation.csv", "-aggregation.done")):
            return False
        if changed is None or csv in changed:
            return True
        pmtiles = csv.split("/")[-1].replace("-aggregation.csv", "") + ".pmtiles"
        return pmtiles not in have
    return is_dirty


def dirty_filepaths():
    """All dirty tiles, heaviest-first — the work list for a single-machine `just planet`."""
    is_dirty = dirty_predicate()
    return [fp for fp in covering_sorted() if is_dirty(fp)]


FROZEN = "_dirty-aggregate.txt"


def freeze():
    """Write the dirty work list into the covering dir, computed ONCE, so it travels with the
    covering to every shard and they all partition the identical list."""
    aggregation_id = utils.get_aggregation_ids()[-1]
    with open(f"store/aggregation/{aggregation_id}/{FROZEN}", "w") as f:
        f.write("".join(fp + "\n" for fp in dirty_filepaths()))


def work_list():
    """The frozen dirty list if present (every shard reads the SAME one, so the [i::n] stride
    partitions identically across shards), else compute it live (local single-machine runs).
    Freezing is load-bearing for sharding: when each shard recomputed the dirty set itself it
    saw the store at a different moment — as sibling shards filled it in, the missing set (and
    so the list order) shifted, which moved the stride and left some self-heal tile owned by no
    shard. It silently never got built, and downsample then aborted 'pyramid incomplete'."""
    aggregation_id = utils.get_aggregation_ids()[-1]
    path = f"store/aggregation/{aggregation_id}/{FROZEN}"
    if os.path.isfile(path):
        with open(path) as f:
            return [line.strip() for line in f if line.strip()]
    return dirty_filepaths()


def run_all(filepaths):
    if not filepaths:
        print("nothing to do.")
        return
    landmask.require()  # fail fast + actionably if a flagged source needs the mask and it's
                        # missing, not per-tile deep in the pool with an opaque ogr2ogr error
    print(f"start aggregating {len(filepaths)} items...")
    # Each tile holds a multi-GB merged DEM (a max 32768px tile ≈ 4 GB float32, more
    # with the halo + smooth) so peak RAM ≈ workers × DEM. Cap workers in CI via
    # AGG_PROCESSES to stay under runner RAM; unset/0 = all cores (local builds).
    procs = int(os.environ.get("AGG_PROCESSES", "0")) or None
    with Pool(procs) as pool:
        pool.starmap(run, [(fp,) for fp in filepaths], chunksize=1)


def main(argv):
    if argv == ["freeze"]:
        freeze()
    elif argv[:1] == ["matrix"]:
        # Size the CI matrix to the dirt: <= max shards, >= 1 (a clean run still
        # spins one no-op shard, keeping the bundle's `needs` graph simple).
        n = min(int(argv[1]), max(len(work_list()), 1))
        print(json.dumps([{"i": i, "n": n} for i in range(n)]))
    elif argv[:1] == ["shard"]:
        i, n = int(argv[1]), int(argv[2])
        run_all(work_list()[i::n])
    elif not argv:
        run_all(dirty_filepaths())
    else:
        sys.exit("usage: aggregation_run.py [freeze | shard <i> <n> | matrix <max>]")


if __name__ == "__main__":
    main(sys.argv[1:])
