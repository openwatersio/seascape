"""Run the aggregation: reproject -> merge -> tile, per aggregation tile, keyed per fork.

Vendored from mapterhorn (BSD-3). Picks the latest aggregation id and, for every tile in the
covering, computes a content-hash key per FORK — terrain / contours / soundings / depare — from
the merged DEM's determinants plus each fork's own modules and config. A tile re-runs iff ANY of
its fork keys is stale (a missing artifact or a mismatched key sidecar); within the run, forks
whose key is already fresh are skipped. The merge (reproject + merge + smooth) is shared by every
fork, so it re-runs whenever any fork is stale — what a fresh fork saves is its own regenerate +
rewrite, not the merge. Consequence worth stating: a contour-config change re-merges the affected
tiles but leaves the terrain pmtiles' keys untouched, so downsample + terrain-bundle skip
entirely (only vector.pmtiles re-runs). Tiles parallelize across a process pool.

CLI:
  aggregation_run.py                    process every tile with a stale fork (one machine)
  aggregation_run.py sources-manifest   write store/source-manifest.txt: the source files
                                        the stale tiles reference (see sources_manifest)
"""

import os
import shutil
import sys
from glob import glob
from multiprocessing import Pool

import aggregation_merge
import aggregation_reproject
import aggregation_tile
import config
import contour_run
import depare_run
import keys
import landmask
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
TERRAIN_MODULES = MERGE_MODULES + ["aggregation_tile", "encode"]
CONTOUR_MODULES = MERGE_MODULES + ["contour_run"]
SOUNDINGS_MODULES = MERGE_MODULES + ["soundings_run"]
DEPARE_MODULES = MERGE_MODULES + ["depare_run"]

FORKS = ("terrain", "contour", "soundings", "depare")


def _merge_inputs_config(filepath):
    """The inputs + config shared by every fork of a tile: all four read the SAME merged, smoothed
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


def terrain_key(filepath):
    inputs, cfg = _merge_inputs_config(filepath)
    return keys.stage_key(inputs, TERRAIN_MODULES, {**cfg, "fork": "terrain"})


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


_KEYFN = {"terrain": terrain_key, "contour": contour_key,
          "soundings": soundings_key, "depare": depare_key}


# store dir + extension per vector fork (terrain is the z7-sharded pmtiles store).
_VECTOR_ARTIFACT = {"contour": ("contour", "fgb"),
                    "soundings": ("soundings", "geojson"),
                    "depare": ("depare", "fgb")}


def _artifact(fork, stem):
    if fork == "terrain":
        z, x, y, _child_z = (int(a) for a in stem.split("-"))
        return f"{utils.get_pmtiles_folder(x, y, z)}/{stem}.pmtiles"
    folder, ext = _VECTOR_ARTIFACT[fork]
    return f"store/{folder}/{stem}.{ext}"


def plan_forks(filepath):
    """Per fork: its artifact, computed key, and whether to (re)run it — stale (or FORCEd), and not
    hard-skipped by a SKIP_* run mode. Only terrain requires its artifact to exist to be fresh
    (self-heal, as the old covering diff did); a vector fork may legitimately produce nothing, so
    its key sidecar alone marks it done."""
    stem = filepath.split("/")[-1].replace("-aggregation.csv", "")
    skips = {"contour": SKIP_CONTOURS, "soundings": SKIP_SOUNDINGS, "depare": SKIP_DEPARE}
    plan = {}
    for fork in FORKS:
        if skips.get(fork):
            plan[fork] = {"do": False}
            continue
        art = _artifact(fork, stem)
        key = _KEYFN[fork](filepath)
        fresh = keys.is_fresh(art, key, require_artifact=(fork == "terrain"))
        plan[fork] = {"art": art, "key": key, "do": not fresh}
    return plan


def _record(entry):
    """Write a fork's key sidecar next to its artifact, creating the store dir first (a fork that
    produced nothing still records its key so it stops re-running)."""
    utils.create_folder(os.path.dirname(entry["art"]))
    keys.write_key(entry["art"], entry["key"])


def run(filepath):
    stem = filepath.split("/")[-1].replace("-aggregation.csv", "")
    plan = plan_forks(filepath)
    if not any(p["do"] for p in plan.values()):
        print(f"{stem} fresh — skip")
        return
    print(f"{stem} start")
    aggregation_reproject.reproject(filepath)
    aggregation_merge.merge(filepath)
    tmp_folder = filepath.replace("-aggregation.csv", "-tmp")
    if not SKIP_SMOOTH:
        smooth.smooth_merged(tmp_folder)         # slope-selective blur, shared by every fork
    if plan["terrain"]["do"]:
        # Invalidate before writing (the crash rule every fork follows): a crash mid-archive
        # must read stale on the next run, never fresh-with-a-torn-artifact — under FORCE the
        # key is unchanged, so a surviving sidecar would vouch for whatever half-write remains.
        sc = keys.sidecar(plan["terrain"]["art"])
        if os.path.isfile(sc):
            os.remove(sc)
        aggregation_tile.main(filepath)          # raster Terrain-RGB tiles
        _record(plan["terrain"])
    for fork, mod in (("contour", contour_run), ("soundings", soundings_run), ("depare", depare_run)):
        if plan[fork]["do"]:
            mod.generate(filepath)               # vector fork off the same merged DEM
            _record(plan[fork])
    if not os.environ.get("KEEP_TMP"):           # KEEP_TMP=1 preserves the merged DEM for re-running a fork
        shutil.rmtree(tmp_folder)
    print(f"{stem} end")


def covering_sorted():
    """Every aggregation CSV in the current covering, heaviest-first. child_z is a strong cost
    proxy (each level quadruples output tiles + contour features), so processing heaviest-first
    keeps the process pool balanced (the longest tiles start first, no straggler tail)."""
    aggregation_id = utils.get_aggregation_ids()[-1]
    csvs = glob(f"store/aggregation/{aggregation_id}/*-aggregation.csv")

    def child_z(fp):
        return int(fp.split("/")[-1].replace("-aggregation.csv", "").split("-")[3])
    return sorted(csvs, key=lambda fp: (-child_z(fp), fp))


def dirty_filepaths():
    """Every tile with a stale fork, heaviest-first — the work list for a single-machine `just
    planet`. Spatial change detection falls out of the keys: only a tile whose intersecting source
    hashes / covering row / smooth config changed gets new fork keys, so a no-change rerun returns
    []. FORCE_REBUILD makes every tile stale (keys.is_fresh ignores the match)."""
    return [fp for fp in covering_sorted() if any(p["do"] for p in plan_forks(fp).values())]


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
