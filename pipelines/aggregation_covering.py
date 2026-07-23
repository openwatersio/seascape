"""Plan the aggregation: slice the planet into source-aware work tiles.

Vendored from mapterhorn (BSD-3) with two bathymetry adaptations:
  - **Per-source maxzoom: inferred, optionally capped, floored.**
    maxzoom = max(min(native_overzoom, cap), macrotile_z), where native_overzoom is
    derived from pixel size and cap is the OPTIONAL metadata.json ``max_zoom`` (omit
    it to use native). The floor to macrotile_z keeps the covering invariant (a tile's
    zoom never exceeds its content zoom). So CUDEM (native ~z18) is capped to z13;
    GEBCO (native ~z8, no cap) is floored up to z9.
  - **Optional BBOX filter.** A regional build sets BBOX=W,S,E,N (lon/lat) to only
    enumerate macrotiles within the window — the per-phase test loop, and what
    keeps the covering tractable before GEBCO is sliced into smaller tiles (the
    global-scale concern, deferred to the CI/scaling phase).

Emits one ``{z}-{x}-{y}-{child_z}-aggregation.csv`` per aggregation tile, listing
exactly which source files + maxzooms feed it.
"""

import os
from glob import glob

import mercantile
from ulid import ULID

import config
import utils


def get_mercator_resolutions(minzoom, maxzoom):
    resolutions = []
    for z in range(minzoom, maxzoom + 1):
        bounds = mercantile.xy_bounds(mercantile.Tile(x=0, y=0, z=z))
        resolutions.append((bounds.right - bounds.left) / 512)
    return resolutions


def bounds_intersect_no_antimeridian_crossing(a, b):
    la, ba, ra, ta = a
    lb, bb, rb, tb = b
    return not (ra <= lb or rb <= la or ta <= bb or tb <= ba)


def split_at_antimeridian(bbox):
    left, bottom, right, top = bbox
    if left < right:
        return [bbox]
    return [(left, bottom, utils.X_MAX_3857, top), (utils.X_MIN_3857, bottom, right, top)]


def bounds_intersect(a, b):
    for aa in split_at_antimeridian(a):
        for bb in split_at_antimeridian(b):
            if bounds_intersect_no_antimeridian_crossing(aa, bb):
                return True
    return False


def get_intersecting_tiles_dfs(bounds, tile, zoom):
    if not bounds_intersect(bounds, mercantile.xy_bounds(tile)):
        return []
    if tile.z == zoom:
        return [tile]
    result = []
    for child in mercantile.children(tile, zoom=tile.z + 1):
        result += get_intersecting_tiles_dfs(bounds, child, zoom)
    return result


def get_smallest_overzoom(left, bottom, right, top, width, height, mercator_resolutions):
    hres = (right - left) / width if left < right else (left - right) / width
    vres = (top - bottom) / height
    for z in range(len(mercator_resolutions)):
        if mercator_resolutions[z] < hres and mercator_resolutions[z] < vres:
            return z
    raise ValueError(f"no overzoom for {(left, bottom, right, top, width, height)}")


def _registered_sources():
    """Sources with a registration on disk (catalog.json), skipping orphan store dirs whose
    sources/<id>/ recipe is gone."""
    result = []
    for filepath in sorted(glob("store/source/*/catalog.json")):
        source = filepath.split("/")[-2]
        if not os.path.isfile(f"{config.SOURCES_DIR}/{source}/metadata.json"):
            print(f"skipping orphan store/source/{source} (no sources/{source}/)")
            continue
        result.append(source)
    return result


def source_maxzooms():
    """Resolved native/capped maxzoom per registered source, from its catalog seascape:files."""
    resolutions = get_mercator_resolutions(0, 32)
    result = {}
    for source in _registered_sources():
        cap = config.source_property(source, "max_zoom")
        for _filename, left, bottom, right, top, width, height in config.source_files(source):
            zoom = get_smallest_overzoom(left, bottom, right, top, width, height, resolutions)
            if cap is not None:
                zoom = min(zoom, cap)
            result[source] = max(result.get(source, 0), zoom, utils.macrotile_z)
    return result


def bbox_3857():
    """The BBOX env (W,S,E,N lon/lat) as 3857 bounds, or None."""
    bbox = os.environ.get("BBOX", "").strip()
    if not bbox:
        return None
    w, s, e, n = (float(x) for x in bbox.split(","))
    left, bottom = mercantile.xy(w, s)
    right, top = mercantile.xy(e, n)
    return (left, bottom, right, top)


def get_macrotile_map():
    macrotile_map = {}
    mercator_resolutions = get_mercator_resolutions(0, 32)
    clip = bbox_3857()
    for source in _registered_sources():
        cap = config.source_property(source, "max_zoom")
        rows = config.source_files(source)
        print(f"reading {source} ({len(rows)} file(s), max_zoom cap={cap})...")
        for filename, left, bottom, right, top, width, height in rows:
            buffer = 2 * utils.macrotile_buffer_3857
            bounds = (left - buffer, bottom - buffer, right + buffer, top + buffer)
            if clip is not None:
                if not bounds_intersect(bounds, clip):
                    continue
                bounds = (max(bounds[0], clip[0]), max(bounds[1], clip[1]),
                          min(bounds[2], clip[2]), min(bounds[3], clip[3]))

            maxzoom = get_smallest_overzoom(left, bottom, right, top, width, height, mercator_resolutions)
            if cap is not None:
                maxzoom = min(maxzoom, cap)
            # Floor to macrotile_z so aggregation-tile zoom never exceeds a
            # tile's content zoom (the cap can't go below this universal floor).
            maxzoom = max(maxzoom, utils.macrotile_z)

            for tile in get_intersecting_tiles_dfs(bounds, mercantile.Tile(x=0, y=0, z=0), utils.macrotile_z):
                cell = macrotile_map.setdefault((tile.x, tile.y), {"sources": {}})
                cell["sources"].setdefault(source, []).append({"filename": filename, "maxzoom": maxzoom})
    return macrotile_map


def add_group_ids(macrotile_map):
    for cell in macrotile_map.values():
        parts = set()
        for source, items in cell["sources"].items():
            for item in items:
                parts.add((source, item["maxzoom"]))
        cell["group_id"] = tuple(sorted(parts))


def get_aggregation_tiles_dfs(candidate, macrotile_map):
    if candidate.z == utils.macrotile_z:
        return [candidate]
    group_ids = set()
    for macrotile in mercantile.children(candidate, zoom=utils.macrotile_z):
        cell = macrotile_map.get((macrotile.x, macrotile.y))
        if cell is not None:
            group_ids.add(cell["group_id"])
    if len(group_ids) == 0:
        return []
    if len(group_ids) == 1:
        maxzoom = max(part[1] for part in next(iter(group_ids)))
        if candidate.z >= maxzoom - utils.num_overviews:
            return [candidate]
    result = []
    for child in mercantile.children(candidate, zoom=candidate.z + 1):
        result += get_aggregation_tiles_dfs(child, macrotile_map)
    return result


def get_aggregation_tiles(macrotile_map):
    seed_z = max(utils.macrotile_z - utils.num_overviews, 0)
    candidates = {
        mercantile.parent(mercantile.Tile(x=x, y=y, z=utils.macrotile_z), zoom=seed_z)
        for (x, y) in macrotile_map
    }
    tiles = []
    for candidate in candidates:
        tiles += get_aggregation_tiles_dfs(candidate, macrotile_map)
    return tiles


def aggregation_items(macrotile_map, aggregation_tiles):
    """{csv filename: csv content} for the covering — the write-mode-independent core."""
    items = {}
    for aggregation_tile in aggregation_tiles:
        rows = set()
        child_z = 0
        for macrotile in mercantile.children(aggregation_tile, zoom=utils.macrotile_z):
            cell = macrotile_map.get((macrotile.x, macrotile.y))
            if cell is None:
                continue
            for source, source_items in cell["sources"].items():
                for item in source_items:
                    rows.add((source, item["filename"], str(item["maxzoom"])))
                    child_z = max(child_z, item["maxzoom"])
        if not rows:
            continue
        lines = ["source,filename,maxzoom\n"] + [",".join(r) + "\n" for r in sorted(rows)]
        name = f"{aggregation_tile.z}-{aggregation_tile.x}-{aggregation_tile.y}-{child_z}-aggregation.csv"
        items[name] = "".join(lines)
    return items


def write_aggregation_items(macrotile_map, aggregation_tiles, aggregation_id):
    folder = f"store/aggregation/{aggregation_id}"
    utils.create_folder(folder)
    for name, content in aggregation_items(macrotile_map, aggregation_tiles).items():
        with open(f"{folder}/{name}", "w") as f:
            f.write(content)


# Shared write-if-changed (utils): mtimes don't churn on a no-op re-cover, so engine
# provenance sees an unchanged covering as unchanged.
write_if_changed = utils.write_if_changed


def write_stable(macrotile_map, aggregation_tiles):
    """The Snakemake-lane covering: per-tile CSVs at stable paths directly under
    store/aggregation/ (no ULID run directories), write-if-changed, stale tiles pruned,
    plus store/aggregation/covering.txt — the stem list the planet/preview invocation
    parses its DAG from. Under BBOX only intersecting tiles are computed, so pruning
    keeps out-of-window tiles: a bbox covering refreshes its window without erasing the
    planet's. Legacy ULID directories (subdirectories) are untouched."""
    folder = "store/aggregation"
    utils.create_folder(folder)
    items = aggregation_items(macrotile_map, aggregation_tiles)
    written = sum(write_if_changed(f"{folder}/{name}", content)
                  for name, content in items.items())
    clip = bbox_3857()
    pruned = 0
    for path in glob(f"{folder}/*-aggregation.csv"):
        name = path.rsplit("/", 1)[-1]
        if name in items:
            continue
        z, x, y, _ = (int(a) for a in name.removesuffix("-aggregation.csv").split("-"))
        tile_bounds = mercantile.xy_bounds(mercantile.Tile(x=x, y=y, z=z))
        if clip is not None and not bounds_intersect(tuple(tile_bounds), clip):
            continue  # outside this bbox run's window — not ours to judge
        os.remove(path)
        pruned += 1
    stems = sorted(p.rsplit("/", 1)[-1].removesuffix("-aggregation.csv")
                   for p in glob(f"{folder}/*-aggregation.csv"))
    write_if_changed(f"{folder}/covering.txt", "".join(s + "\n" for s in stems))
    print(f"stable covering: {len(items)} tile(s), {written} changed, {pruned} pruned, "
          f"{len(stems)} total in covering.txt")


def main(stable=False):
    print("get_macrotile_map...")
    macrotile_map = get_macrotile_map()
    print("add group ids...")
    add_group_ids(macrotile_map)
    print("get aggregation tiles...")
    aggregation_tiles = get_aggregation_tiles(macrotile_map)
    if stable:
        write_stable(macrotile_map, aggregation_tiles)
        return
    aggregation_id = str(ULID())
    utils.create_folder(f"store/aggregation/{aggregation_id}")
    print(f"write {len(aggregation_tiles)} aggregation items to {aggregation_id}...")
    write_aggregation_items(macrotile_map, aggregation_tiles, aggregation_id)


def _check():
    """Offline: write_if_changed leaves mtimes alone on identical content, and the
    stable covering prunes a stale in-window tile while keeping an out-of-window one."""
    import shutil
    import tempfile
    import time

    d = tempfile.mkdtemp()
    cwd = os.getcwd()
    try:
        os.chdir(d)
        utils.create_folder("store/aggregation")
        path = "store/aggregation/8-1-1-9-aggregation.csv"
        assert write_if_changed(path, "a,b\n1,2\n") is True
        before = os.stat(path).st_mtime_ns
        time.sleep(0.01)
        assert write_if_changed(path, "a,b\n1,2\n") is False
        assert os.stat(path).st_mtime_ns == before, "unchanged content must not touch mtime"
        assert write_if_changed(path, "a,b\n1,3\n") is True
        assert os.stat(path).st_mtime_ns != before

        # stale prune honors the bbox window: with BBOX over NY harbor, a stale csv
        # on the covering tile there is pruned, one on the other side of the planet
        # is kept, and covering.txt lists exactly what remains on disk.
        t = mercantile.tile(-74.0, 40.6, utils.macrotile_z)
        inside = f"{t.z}-{t.x}-{t.y}-13-aggregation.csv"
        for name in (inside, "8-200-100-9-aggregation.csv"):
            with open(f"store/aggregation/{name}", "w") as f:
                f.write("source,filename,maxzoom\nx,y.tif,9\n")
        os.environ["BBOX"] = "-74.30,40.40,-73.75,40.80"
        try:
            write_stable({}, [])  # empty covering: everything in-window is stale
        finally:
            del os.environ["BBOX"]
        assert not os.path.exists(f"store/aggregation/{inside}"), \
            "stale in-window tile must be pruned"
        assert os.path.exists("store/aggregation/8-200-100-9-aggregation.csv"), \
            "out-of-window tile must survive a bbox run"
        with open("store/aggregation/covering.txt") as f:
            stems = f.read().split()
        assert stems == ["8-1-1-9", "8-200-100-9"], stems
        print("aggregation_covering.py self-check ok")
    finally:
        os.chdir(cwd)
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["--check"]:
        _check()
    else:
        main(stable="--stable" in sys.argv[1:])
