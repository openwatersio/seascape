"""Stage 2 + vector forks: reproject -> merge -> persist mosaic + contour/soundings/depare.

Vendored from mapterhorn (BSD-3). Picks the latest aggregation id and, for every tile in the
covering, produces the stage-2 MOSAIC tile (mosaic.produce — the persisted unsmoothed truth) and
computes a content-hash key per VECTOR FORK — contours / soundings / depare — from the merged DEM's
determinants plus each fork's own modules and config. Each fork's key rides IN its artifact filename
(``store/contour/<stem>-<key>.fgb``, keys.content_path), so a tile re-runs iff ANY fork's (or the
mosaic's) content-named file (or its empty marker) is absent; within the run, forks already present
are skipped. The merge (reproject + merge + smooth) is shared, so it re-runs whenever the mosaic or
any vector fork is stale. The TERRAIN raster is no longer produced here — terrain.py renders it
per-zoom from the persisted mosaic (stage 3), so a smoothing/quantization change re-renders terrain
against a fully cached mosaic with no re-merge. Tiles parallelize across a process pool.

CLI:
  aggregation_run.py                    process every tile with a stale fork (one machine)
  aggregation_run.py sources-manifest   write store/source-manifest.txt: the source files
                                        the stale tiles reference (see sources_manifest)
"""

import hashlib
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from glob import glob
from multiprocessing import Pool

import aggregation_merge
import aggregation_reproject
import config
import contour_run
import depare_run
import keys
import landmask
import mosaic
import scheduler
import smooth
import soundings_run
import utils

# The forks share the merged DEM; set these to 1 for raster-only / no-smooth runs.
SKIP_CONTOURS = os.environ.get("SKIP_CONTOURS", "")
SKIP_SOUNDINGS = os.environ.get("SKIP_SOUNDINGS", "")
SKIP_DEPARE = os.environ.get("SKIP_DEPARE", "")
SKIP_SMOOTH = os.environ.get("SKIP_SMOOTH", "")

# Code dependencies as a static per-fork module list (coarse per-module hashing; over-invalidation
# on a comment edit is accepted, under-invalidation is not). config.py is deliberately NOT listed:
# it mixes pure config constants (CONTOUR_LEVELS) with path plumbing, and a fork's config VALUES
# enter its key resolved (below), so a CONTOUR_LEVELS edit moves only the contour/depare keys, not
# terrain's. landmask stands in for the land+water masks: the masks' recipe-hash marker IS
# hashFiles(landmask.py) (sources.yml gates their mirror on it), so hashing the module is hashing
# the mask identity.
MERGE_MODULES = ["aggregation_reproject", "aggregation_merge", "smooth", "landmask", "utils"]
CONTOUR_MODULES = MERGE_MODULES + ["contour_run"]
SOUNDINGS_MODULES = MERGE_MODULES + ["soundings_run"]
DEPARE_MODULES = MERGE_MODULES + ["depare_run"]

# The terrain raster is no longer a fork here: it's stage 3's per-zoom render from the persisted
# mosaic (terrain.py), keyed off the mosaic tile hashes, not the transient merge. The aggregate is
# the stage-2 mosaic PRODUCER plus the vector forks (which still read the shared smoothed merge —
# their outputs stay identical, and their keys already re-merge only their own tiles).
FORKS = ("contour", "soundings", "depare")


def _merge_inputs_config(filepath):
    """The inputs + config shared by every fork of a tile: all three read the SAME merged, smoothed
    DEM, so its determinants key them all — the covering row (a re-tile / source-file change flips
    it), each intersecting source's recipe hash + every resolved build prop the reproject/merge
    reads (priority/maxzoom/offset/land_clamp/negate/band/mixed_crs), the smoothing + resample
    knobs, and any LOCAL mask file's content (a /vsicurl mask carries its identity via the
    landmask module hash instead)."""
    with open(filepath) as f:
        covering_row = f.read()
    sources = sorted({line.split(",")[0] for line in covering_row.splitlines()[1:] if line.strip()})
    inputs = [covering_row]
    props = {}
    for s in sources:
        inputs.append(config.source_recipe_hash(s))
        props[s] = {k: config.source_property(s, k) for k in
                    ("priority", "max_zoom", "land_clamp", "offset", "negate", "band", "mixed_crs")}
    for mask in (landmask.path(), landmask.water_path()):
        h = keys.file_hash(mask)
        if h is not None:
            inputs.append(h)
    cfg = {
        "sources": props,
        "resample": aggregation_reproject.RESAMPLE,
        "macrotile_z": utils.macrotile_z,
        "num_overviews": utils.num_overviews,
        "smooth": {} if SKIP_SMOOTH else {
            "sigma": smooth.DEM_SIGMA, "sigma_deep": smooth.DEM_SIGMA_DEEP,
            "mask_sigma": smooth.MASK_SIGMA, "slope_low": smooth.SLOPE_LOW,
            "slope_high": smooth.SLOPE_HIGH, "depth_full": smooth.DEPTH_FULL,
            "depth_smooth": smooth.DEPTH_SMOOTH, "block": smooth.BLOCK},
    }
    return inputs, cfg


def contour_key(filepath):
    inputs, cfg = _merge_inputs_config(filepath)
    cfg = {**cfg, "fork": "contour", "contour_levels": config.CONTOUR_LEVELS,
           "contour_levels_ft": config.CONTOUR_LEVELS_FT,
           "nav_smooth_max": contour_run.NAV_SMOOTH_MAX_M,
           "deep_cutoff_m": contour_run.DEEP_CUTOFF_M,
           "min_ring_area_m2": contour_run.MIN_RING_AREA_M2}
    return keys.stage_key(inputs, CONTOUR_MODULES, cfg)


def soundings_key(filepath):
    inputs, cfg = _merge_inputs_config(filepath)
    cfg = {**cfg, "fork": "soundings", "sound_cell_px": soundings_run.SOUND_CELL_PX,
           "sound_min_depth_m": soundings_run.SOUND_MIN_DEPTH_M}
    return keys.stage_key(inputs, SOUNDINGS_MODULES, cfg)


def depare_key(filepath):
    inputs, cfg = _merge_inputs_config(filepath)
    cfg = {**cfg, "fork": "depare", "depare_levels": config.DEPARE_LEVELS,
           "depare_levels_ft": config.DEPARE_LEVELS_FT, "drying_cap": config.DRYING_CAP,
           "sliver_min_px": depare_run.SLIVER_MIN_PX}
    return keys.stage_key(inputs, DEPARE_MODULES, cfg)


_KEYFN = {"contour": contour_key, "soundings": soundings_key, "depare": depare_key}


# store dir + extension per vector fork.
_VECTOR_ARTIFACT = {"contour": ("contour", "fgb"),
                    "soundings": ("soundings", "geojson"),
                    "depare": ("depare", "fgb")}


def _artifact(fork, stem):
    folder, ext = _VECTOR_ARTIFACT[fork]
    return f"store/{folder}/{stem}.{ext}"


def plan_forks(filepath):
    """Per fork: its LOGICAL artifact base, computed key, and whether to (re)run it — stale (or
    FORCEd), and not hard-skipped by a SKIP_* run mode. Freshness is content-addressed and uniform:
    the content-named file OR the empty marker exists under the key (keys.fork_fresh) — self-heal
    (a dropped artifact rebuilds) and the legitimately-empty vector fork both fall out of one
    existence check, so the phase-3 require_artifact split is gone."""
    stem = filepath.split("/")[-1].replace("-aggregation.csv", "")
    skips = {"contour": SKIP_CONTOURS, "soundings": SKIP_SOUNDINGS, "depare": SKIP_DEPARE}
    plan = {}
    for fork in FORKS:
        if skips.get(fork):
            plan[fork] = {"do": False}
            continue
        art = _artifact(fork, stem)
        key = _KEYFN[fork](filepath)
        plan[fork] = {"art": art, "key": key, "do": not keys.fork_fresh(art, key)}
    return plan


def run(filepath):
    stem = filepath.split("/")[-1].replace("-aggregation.csv", "")
    plan = plan_forks(filepath)
    # The mosaic is an ADDITIVE stage-2 product persisted ALONGSIDE the forks (the merged Float32
    # truth layer), keyed independently (mosaic.mosaic_key — no smoothing in its key). It shares the
    # same reproject+merge, so a stale mosaic (even with all forks fresh) must run the tile too.
    mosaic_key = mosaic.mosaic_key(filepath)
    do_mosaic = not keys.fork_fresh(mosaic.tile_artifact(stem), mosaic_key)
    if not any(p["do"] for p in plan.values()) and not do_mosaic:
        print(f"{stem} fresh — skip", flush=True)
        return
    # flush: a pool worker's stdout is block-buffered, so without it start/end reach the log
    # together at process exit and per-tile timings are unrecoverable from a run's log.
    print(f"{stem} start", flush=True)
    tmp_folder = filepath.replace("-aggregation.csv", "-tmp")
    # This tile's memory weight was reserved in the PARENT before dispatch (scheduler.map_budgeted
    # in run_all) and is released when this task completes — the whole body runs under it. The
    # feather-merge is the RAM peak (the merged array + reprojected sources + masks — S-102 alone
    # is ~52 overlapping products); the vector forks below run off the SAME already-merged DEM.
    aggregation_reproject.reproject(filepath)
    aggregation_merge.merge(filepath)
    if do_mosaic:
        mosaic.produce(filepath, tmp_folder, mosaic_key)  # persist BEFORE smooth (unsmoothed truth)
    if not SKIP_SMOOTH:
        smooth.smooth_merged(tmp_folder)         # slope-selective blur, shared by every vector fork
    # Terrain is no longer produced here — terrain.py renders it per-zoom from the mosaic (stage
    # 3), keyed off the mosaic tile hashes. The vector forks still read the smoothed merge below.
    # The three forks are independent readers of the same on-disk smoothed DEM (distinct tmp +
    # artifact files), so run them CONCURRENTLY: their heavy parts are subprocesses
    # (gdal_contour) and GIL-releasing GEOS/rasterio calls, and a dense z14 tile spent most of
    # its budget slot walking them serially — the forks' overlapped peak is covered by this
    # tile's single reservation (the factor is re-fit against parallel-fork log_peak data).
    def _fork(fork, mod):
        e = plan[fork]
        cpath = keys.content_path(e["art"], e["key"])
        keys.supersede(e["art"])                 # clear last build's key BEFORE generating (crash -> stale)
        mod.generate(filepath, cpath)            # vector fork off the same merged DEM; writes cpath atomically, or nothing
        if not os.path.isfile(cpath):
            keys.write_empty(e["art"], e["key"])  # legitimately empty -> mark it done

    forks = [(f, m) for f, m in (("contour", contour_run), ("soundings", soundings_run),
                                 ("depare", depare_run)) if plan[f]["do"]]
    with ThreadPoolExecutor(max_workers=3) as ex:
        for fut in [ex.submit(_fork, f, m) for f, m in forks]:
            fut.result()                         # propagate the first fork failure
    if not os.environ.get("KEEP_TMP"):           # KEEP_TMP=1 preserves the merged DEM for re-running a fork
        shutil.rmtree(tmp_folder)
    with open(filepath) as f:                    # covering rows = overlapping source files: the density
        n_src = sum(1 for _ in f) - 1            # signal behind the ~11× peak spread at one child_z
    scheduler.log_peak(stem, sources=n_src)       # whole-run peak RSS (covers the forks) vs weight + density — tuning data
    print(f"{stem} end", flush=True)


def covering_sorted():
    """Every aggregation CSV in the current covering, in a deterministic order (stable hash of
    the stem). The order is identity only: DISPATCH priority is the scheduler's job —
    scheduler.map_budgeted weighs and sorts the work list heaviest-first itself, in the parent,
    so no queue order here can starve the pool (both prior attempts did: heaviest-first parked
    every worker on a blocked heavy, and the md5 shuffle spread the same collapse thinly across
    the whole run). Order can't affect output: tiles are independent and content-addressed."""
    aggregation_id = utils.get_aggregation_ids()[-1]
    csvs = glob(f"store/aggregation/{aggregation_id}/*-aggregation.csv")
    return sorted(csvs, key=lambda fp: hashlib.md5(fp.rsplit("/", 1)[-1].encode()).hexdigest())


def dirty_filepaths():
    """Every tile with a stale fork — the work list for a single-machine `just planet` (dispatch
    priority is scheduler.map_budgeted's job, not this list's order). Spatial change detection falls out of the keys: only a tile whose intersecting source
    hashes / covering row / smooth config changed gets new fork keys, so a no-change rerun returns
    []. FORCE_REBUILD makes every tile stale (keys.is_fresh ignores the match). A tile whose ONLY
    stale product is the mosaic (a merge/precedence change that leaves the smoothed forks untouched)
    counts as dirty too — mosaic.stale reuses the same content-addressed freshness."""
    return [fp for fp in covering_sorted()
            if any(p["do"] for p in plan_forks(fp).values()) or mosaic.stale(fp)]


def sources_manifest():
    """Write store/source-manifest.txt: the unique ``<source>/<filename>`` rows the STALE
    tiles' covering CSVs reference — exactly the source files this run's aggregate will
    read, derived with the same key-based dirty_filepaths() the run uses (FORCE_REBUILD,
    self-heal, and code/config staleness all behave identically; under FORCE this is the
    covering's full source set). The caller must present the same key inputs the aggregate
    run will see — notably the LOCAL masks, whose content hashes enter every fork key — or
    the two compute different dirty sets. Rows are written verbatim: a filename that is
    already an absolute /vsi path passes through untouched, like source_path treats it —
    this module only walks the local store and never decides what a caller does with the
    list."""
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
    # Pool size = cores (AGG_PROCESSES, unset/0 = all cores), so cheap ocean tiles use every core.
    # Peak RAM is bounded SEPARATELY by a shared GB budget (AGG_MEM_BUDGET_GB), enforced in THIS
    # process: scheduler.map_budgeted dispatches a tile into the pool only once its weight fits
    # (heaviest admissible first, light tiles backfilling via the cheap lane), so the N densest
    # coastal macrotiles can't all peak at once — the exit-137 a fixed workers×DEM pool hit —
    # and a tile that doesn't fit waits here, never inside a worker. Budget unset/0 = no
    # admission control (local / small runs): a plain core-bound pool.
    procs = int(os.environ.get("AGG_PROCESSES", "0")) or os.cpu_count()
    budget = int(os.environ.get("AGG_MEM_BUDGET_GB", "0"))
    with Pool(procs, **scheduler.pool_kwargs()) as pool:
        scheduler.map_budgeted(pool, run, filepaths, budget, procs,
                               stem_of=lambda fp: fp.rsplit("/", 1)[-1].replace("-aggregation.csv", ""))


def main(argv):
    if not argv:
        run_all(dirty_filepaths())
    elif argv == ["sources-manifest"]:
        sources_manifest()
    else:
        sys.exit("usage: aggregation_run.py [sources-manifest]")


if __name__ == "__main__":
    main(sys.argv[1:])
