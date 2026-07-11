"""Run the aggregation: reproject -> merge -> tile, per dirty aggregation tile.

Vendored from mapterhorn (BSD-3). Picks the latest aggregation id, processes only
tiles whose source set changed since the previous run (or all on the first run),
skips those already marked done, and parallelizes across tiles with a process pool.

CLI:
  aggregation_run.py                    process every dirty tile (one machine)
  aggregation_run.py sources-manifest   write store/source-manifest.txt: the source files
                                        the dirty tiles reference (see sources_manifest)
"""

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
    """Every aggregation CSV in the current covering, heaviest-first. child_z is a strong cost
    proxy (each level quadruples output tiles + contour features), so processing heaviest-first
    keeps the process pool balanced (the longest tiles start first, no straggler tail)."""
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


def sources_manifest():
    """Write store/source-manifest.txt: the unique ``<source>/<filename>`` rows the DIRTY
    tiles' covering CSVs reference — exactly the source files this run's aggregate will
    read, derived with the same dirty_filepaths() the run uses (FORCE_REBUILD, self-heal
    and the covering diff all behave identically). Rows are written verbatim: a filename
    that is already an absolute /vsi path passes through untouched, like source_path
    treats it — this module only walks the local store and never decides what a caller
    does with the list."""
    dirty = dirty_filepaths()
    files = set()
    for fp in dirty:
        with open(fp) as f:
            for line in f.readlines()[1:]:  # skip header
                line = line.strip()
                if not line:
                    continue
                source, filename, _maxzoom = line.split(",")
                files.add(f"{source}/{filename}")
    with open("store/source-manifest.txt", "w") as f:
        f.write("".join(k + "\n" for k in sorted(files)))
    print(f"source manifest: {len(files)} file(s) across {len(dirty)} dirty tile(s)")


def run_all(filepaths):
    if not filepaths:
        print("nothing to do.")
        return
    landmask.require()  # fail fast + actionably if a flagged source needs the mask and it's
                        # missing, not per-tile deep in the pool with an opaque ogr2ogr error
    print(f"start aggregating {len(filepaths)} items...")
    # Each tile holds a multi-GB merged DEM (a max 32768px tile ≈ 4 GB float32, more
    # with the halo + smooth) so peak RAM ≈ workers × DEM. Cap workers via AGG_PROCESSES to
    # stay under the machine's RAM (the build box sizes it from RAM); unset/0 = all cores.
    procs = int(os.environ.get("AGG_PROCESSES", "0")) or None
    with Pool(procs) as pool:
        pool.starmap(run, [(fp,) for fp in filepaths], chunksize=1)


def main(argv):
    if not argv:
        run_all(dirty_filepaths())
    elif argv == ["sources-manifest"]:
        sources_manifest()
    else:
        sys.exit("usage: aggregation_run.py [sources-manifest]")


if __name__ == "__main__":
    main(sys.argv[1:])
