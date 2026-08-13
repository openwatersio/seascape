"""GLDB v2 (`gldbv2.tar.gz`) → one GeoTIFF per documented-bathymetry lake.

The Global Lake Database (Kourzeneva & Choulga) ships its depth grid as a headerless
binary alongside a plain-text inventory:

    GlobalLakeDepth.dat              43200 x 21600 little-endian Int16, decimetres,
                                     row-major, row 0 at +90 lat, column 0 at -180 lon,
                                     1/120 deg (30 arcsec) cells, geographic; 0 = dry
    LargeLakesWithBathymetry_v2.txt  Name Country Area Lat Lon Mean Max Reference
                                     (the header's LON/LAT labels are swapped against
                                     the values), two sections then a reference list

Most of the grid holds a per-lake mean depth, a modeled estimate or the 10 m default —
only the inventory's lakes carry real bathymetry. Each of its 36 entries seeds an
8-connected flood over the positive cells; the floods resolve to 33 components (Huron
and Michigan, Bratsk's two entries, and Chudskoe and Pskovskoe each share one), and the
components a dedicated source already covers are dropped, leaving 25.

Even inside those 25, GLDB fills the parts of a lake mask it never digitised with the
lake's measured mean, under the same status code as the digitised cells — so the fill is
only visible as geometry: a large constant-value plate. Each is removed and the component
re-flooded from its seed, dropping whatever the plate was bridging (see `_plate_cells`).

What survives is written cropped to its own bounding box, positive-down Float32 metres
with everything outside the component nodata: decimetre precision does not survive Int16
metres, and gdalwarp ignores a band scale factor.

The archive digest, its members, the grid dimensions and every count are asserted — an
upstream change must fail the prep, not silently paint flat mean-depth plates over lakes
worldwide. No CRS is assigned here (source_normalize assigns EPSG:4326 from metadata.json),
so this stays a pure format converter.

Self-check:  uv run python convert_gldb.py --check [gldbv2.tar.gz]
"""

import hashlib
import re
import sys
import tarfile
from collections import deque

import numpy as np
import rasterio
from rasterio.transform import from_origin

ARCHIVE_SHA256 = "283e7b9ceaf7bec522a80ed80a55010bb03d361574e74b02e2ac8ecf9e318ef0"
DEPTH_MEMBER = "GlobalLakeDepth.dat"
INVENTORY_MEMBER = "LargeLakesWithBathymetry_v2.txt"
EXPECTED_MEMBERS = (DEPTH_MEMBER, "GlobalLakeDepth.txt", "GlobalLakeStatus.dat", INVENTORY_MEMBER)

GRID_W, GRID_H = 43200, 21600
CELLS_PER_DEG = 120
CELL_DEG = 1.0 / CELLS_PER_DEG
NODATA = -9999.0

SEED_COUNT = 36
COMPONENT_COUNT = 33
# The inventory entries that share a component with the entry before them.
MERGED_COMPONENTS = ("bratskoe1-bratskoe2", "chudskoe-pskovskoe", "huron-michigan")
# Components carried at finer resolution by great_lakes and african_great_lakes; GLDB's
# `priority` override would otherwise beat those sources.
SUPERSEDED = {"superior", "huron", "michigan", "erie", "ontario", "saint_clair",
              "victoria", "albert", "edward"}
SUPERSEDED_COMPONENTS = 8
OUTPUT_COUNT = 25
# An inventory centroid can land on a dry cell (Ladoga's does) — search out to ~3 km.
SEED_RADIUS = 6
# A constant-value blob this large (absolute, or as a share of its component) is a
# mean-depth fill, not bathymetry — measured: the fills run 537–11,039 cells, the largest
# surviving blob is 330 (Vänern at 50.0 m). Depths of 0.1 m are exempt: those blobs are the
# digitised shore rim and reach 2,148 cells.
PLATE_MIN_CELLS = 400
PLATE_MIN_FRACTION = 0.10
PLATE_MIN_DM = 2

_REFERENCE_ITEM = re.compile(r"^\d+\.\s")


class ArchiveChanged(ValueError):
    """The archive's bytes are truncated or otherwise unreadable. A ValueError so staging maps
    it onto its corrupt-raw self-heal, which refetches. An upstream republish exits instead —
    deleting readable bytes would destroy the only copy of what has to be re-verified."""


def parse_inventory(text):
    """The inventory's seed rows as (name, lat, lon, mean_m, max_m).

    Bratskoe2 and Pskovskoe continue the row above with `|` marks on the shared
    area/depth fields, leaving them Name Country Lat Lon Reference and no depths.
    """
    seeds = []
    for line in text.splitlines():
        row = line.strip()
        if _REFERENCE_ITEM.match(row):  # the numbered reference list ends the table
            break
        if not row or set(row) <= {"-"} or not row[0].isalpha():
            continue
        fields = row.replace("|", " ").split()[2:]  # past Name + Country
        if not fields or not fields[0][0].isdigit():  # prose and column headers
            continue
        if len(fields) == 6:
            lat, lon, mean, deepest = fields[1], fields[2], float(fields[3]), float(fields[4])
        elif len(fields) == 3:
            lat, lon, mean, deepest = fields[0], fields[1], None, None
        else:
            sys.exit(f"gldb inventory: unparseable row {row!r}")
        seeds.append((row.split()[0], float(lat), float(lon), mean, deepest))
    return seeds


def rowcol(lat, lon):
    """Grid indices of a geographic position."""
    return int((90.0 - lat) * CELLS_PER_DEG), int((lon + 180.0) * CELLS_PER_DEG)


def _resolve_seed(grid, row, col, name):
    """The nearest positive cell to an inventory centroid."""
    top, left = max(row - SEED_RADIUS, 0), max(col - SEED_RADIUS, 0)
    hits = np.argwhere(grid[top:row + SEED_RADIUS + 1, left:col + SEED_RADIUS + 1] > 0)
    if not len(hits):
        sys.exit(f"gldb: no lake cell within {SEED_RADIUS} cells of the {name} centroid "
                 f"(row {row}, col {col})")
    nearest = hits[((hits + [top - row, left - col]) ** 2).sum(axis=1).argmin()]
    return top + int(nearest[0]), left + int(nearest[1])


def _component(grid, row, col, within=None):
    """The 8-connected component of positive cells containing (row, col), optionally
    confined to the cells of `within`."""
    height, width = grid.shape
    cells = {(row, col)}
    queue = deque([(row, col)])
    while queue:
        r, c = queue.popleft()
        for rr in range(max(r - 1, 0), min(r + 2, height)):
            for cc in range(max(c - 1, 0), min(c + 2, width)):
                if (rr, cc) in cells or grid[rr, cc] <= 0:
                    continue
                if within is not None and (rr, cc) not in within:
                    continue
                cells.add((rr, cc))
                queue.append((rr, cc))
    return cells


def resolve_components(grid, seeds):
    """Flood each (name, row, col) seed and merge the seeds that land in one component.
    Returns {name: (seed, cells)}, a merged component named after all its seeds."""
    found = {}
    for name, row, col in seeds:
        seed = _resolve_seed(grid, row, col, name)
        cells = _component(grid, *seed)
        key = min(cells)  # a canonical cell identifies the component across its seeds
        found.setdefault(key, ([], seed, cells))[0].append(name.lower())
    return {"-".join(sorted(names)): (seed, cells) for names, seed, cells in found.values()}


def retained(components):
    """Components with no dedicated source of their own."""
    return {name: value for name, value in components.items()
            if not set(name.split("-")) & SUPERSEDED}


def _plate_cells(grid, cells):
    """The cells of every constant-value blob too large to be bathymetry. GLDB fills the
    undigitised part of a lake's mask with the lake's measured mean at the same status code
    as real soundings, so only the geometry gives it away."""
    limit = min(PLATE_MIN_CELLS, PLATE_MIN_FRACTION * len(cells))
    unvisited = set(cells)
    plates = set()
    while unvisited:
        start = unvisited.pop()
        value = grid[start]
        blob, queue = {start}, deque([start])
        while queue:
            r, c = queue.popleft()
            for rr in range(r - 1, r + 2):
                for cc in range(c - 1, c + 2):
                    if (rr, cc) in unvisited and grid[rr, cc] == value:
                        unvisited.discard((rr, cc))
                        blob.add((rr, cc))
                        queue.append((rr, cc))
        if value >= PLATE_MIN_DM and len(blob) >= limit:
            plates |= blob
    return plates


def drop_plates(grid, seed, cells, name):
    """Cut the mean-depth plates out of a component and re-flood from its seed, so the
    fragments a plate was bridging (whole neighbouring lakes, in Winnipeg's case) go with it."""
    keep = cells - _plate_cells(grid, cells)
    if not keep:
        sys.exit(f"gldb: {name} is entirely mean-depth plate — it carries no bathymetry")
    if seed not in keep:  # the centroid can sit inside the plate (Beloe's does)
        survivors = np.array(list(keep))
        seed = tuple(int(v) for v in survivors[((survivors - seed) ** 2).sum(axis=1).argmin()])
    return _component(grid, *seed, within=keep)


def _write_component(rc, depths, path):
    """Write one component's cells as a Float32 GeoTIFF of metres below the lake surface,
    cropped to its bounding box, everything else nodata."""
    (row0, col0), (row1, col1) = rc.min(axis=0), rc.max(axis=0)
    out = np.full((row1 - row0 + 1, col1 - col0 + 1), NODATA, dtype="float32")
    out[rc[:, 0] - row0, rc[:, 1] - col0] = depths
    transform = from_origin(-180.0 + col0 * CELL_DEG, 90.0 - row0 * CELL_DEG, CELL_DEG, CELL_DEG)
    with rasterio.open(path, "w", driver="GTiff", height=out.shape[0], width=out.shape[1],
                       count=1, dtype="float32", nodata=NODATA, transform=transform,
                       tiled=True, blockxsize=512, blockysize=512, compress="deflate") as dst:
        dst.write(out, 1)


def read_archive(archive_path):
    """The pinned archive's depth grid and inventory text, in one pass over the members
    (seeking inside a .tar.gz re-decompresses it from the start). extractfile().read() buffers
    the whole 1.87 GB grid — the conversion peaks at ~2.4 GB RSS."""
    with open(archive_path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    if digest != ARCHIVE_SHA256:
        # Readable bytes are a republish, not a bad fetch: exit so the raw survives for the
        # grid layout and counts to be re-verified against it before repinning. Only unreadable
        # bytes take the refetch path, which deletes them.
        try:
            with tarfile.open(archive_path, "r|gz") as probe:
                next(iter(probe))
        except (tarfile.TarError, OSError, EOFError, StopIteration) as e:
            raise ArchiveChanged(f"archive sha256 {digest} is not the pinned "
                                 f"{ARCHIVE_SHA256} and the bytes do not read ({e})") from e
        sys.exit(f"gldb: archive sha256 {digest} is not the pinned {ARCHIVE_SHA256}, but the "
                 f"bytes are a readable archive — upstream republished. Re-verify the grid "
                 f"layout and the asserted counts against {archive_path}, then repin.")
    depth, inventory, names = None, None, []
    with tarfile.open(archive_path, "r|gz") as tar:
        for member in tar:
            names.append(member.name)
            if member.name == INVENTORY_MEMBER:
                inventory = tar.extractfile(member).read().decode("latin-1")
            elif member.name == DEPTH_MEMBER:
                if member.size != GRID_W * GRID_H * 2:
                    sys.exit(f"gldb: {DEPTH_MEMBER} is {member.size} bytes, not the "
                             f"{GRID_W * GRID_H * 2} of a {GRID_W}x{GRID_H} Int16 grid")
                depth = np.frombuffer(tar.extractfile(member).read(),
                                      dtype="<i2").reshape(GRID_H, GRID_W)
    missing = [m for m in EXPECTED_MEMBERS if m not in names]
    if missing:
        sys.exit(f"gldb: archive is missing member(s) {missing}")
    return depth, inventory


def gldb_to_tifs(archive_path, out_dir, prefix="gldb", claim=None):
    """Convert the GLDB archive to one <prefix>_<lake>.tif per retained component, calling
    claim(basename) before each write so a caller can reserve the name. Returns the written
    paths, sorted."""
    grid, inventory = read_archive(archive_path)
    seeds = parse_inventory(inventory)
    if len(seeds) != SEED_COUNT:
        sys.exit(f"gldb: {INVENTORY_MEMBER} lists {len(seeds)} lakes, expected {SEED_COUNT}")
    stated = {name.lower(): (mean, deepest) for name, _, _, mean, deepest in seeds}
    components = resolve_components(grid, [(n, *rowcol(lat, lon)) for n, lat, lon, _, _ in seeds])
    if len(components) != COMPONENT_COUNT:
        sys.exit(f"gldb: {len(seeds)} seeds resolved to {len(components)} components, "
                 f"expected {COMPONENT_COUNT}")
    unmerged = [name for name in MERGED_COMPONENTS if name not in components]
    if unmerged:
        sys.exit(f"gldb: {unmerged} no longer share one component — the inventory or the grid "
                 "changed, and the component arithmetic no longer holds")
    superseded = [name for name in components if set(name.split("-")) & SUPERSEDED]
    if len(superseded) != SUPERSEDED_COMPONENTS:
        sys.exit(f"gldb: the {len(SUPERSEDED)} superseded seeds resolved to {len(superseded)} "
                 f"components, expected {SUPERSEDED_COMPONENTS}: {sorted(superseded)}")
    keep = retained(components)
    if len(keep) != OUTPUT_COUNT:
        sys.exit(f"gldb: {len(components)} components minus {len(superseded)} superseded left "
                 f"{len(keep)}, expected {OUTPUT_COUNT}")
    paths = []
    for name, (seed, cells) in sorted(keep.items()):
        cells = drop_plates(grid, seed, cells, name)
        left = _plate_cells(grid, cells)
        if left:
            sys.exit(f"gldb: {name} still holds a {len(left)}-cell mean-depth plate after the "
                     "cut — inspect the component before shipping it")
        rc = np.array(list(cells))
        depths = grid[rc[:, 0], rc[:, 1]] / 10.0
        distinct = len(set(depths.tolist()))
        if distinct < 2:
            sys.exit(f"gldb: {name} carries one depth ({depths[0]:.1f} m) — a mean-depth, "
                     "modeled or default-fill lake must never reach the output")
        path = f"{out_dir}/{prefix}_{name}.tif"
        if claim:
            claim(f"{prefix}_{name}.tif")
        _write_component(rc, depths, path)
        paths.append(path)
        mean, deepest = next(((m, d) for m, d in (stated[n] for n in name.split("-"))
                              if m is not None), (None, None))
        print(f"  {path}: {len(cells)} cell(s), {distinct} distinct depth(s), "
              f"mean {depths.mean():.1f} m (inventory {mean}), "
              f"max {depths.max():.1f} m (inventory {deepest})")
    return paths


# Per-lake cell counts after the plate cut — a regression pin on the real archive, not a
# property of the format.
LAKE_CELLS = {
    "athabasca": 17574, "balkhash": 29613, "beloe": 2063, "biwa": 940,
    "bratskoe1-bratskoe2": 9333, "champlain": 1822, "chudskoe-pskovskoe": 7917,
    "great_bear": 87539, "great_slave": 49525, "hjalmaren": 1175, "kujbishevskoe": 10926,
    "ladoga": 42785, "lough_neagh": 774, "malaren": 2517, "onego": 25384, "pyramid": 687,
    "rybinskoe": 8477, "sevan": 1732, "skadar": 550, "st_jean": 1888, "taymyr": 21081,
    "ustilimskoe": 3379, "vanern": 12776, "vattern": 4334, "winnipeg": 49458,
}


def _check(archive_path=None):
    """A synthetic grid exercises the mechanics — a `|` continuation row parses, a centroid
    on a dry cell resolves to the lake, two seeds in one blob merge into one named component,
    a superseded lake is dropped, and a mean-depth plate takes the fragment it bridges with it.
    Given the real archive (an explicit path — never picked up implicitly, so the offline test
    target stays offline), it converts for real and pins every lake's cell count."""
    import shutil
    import tempfile

    text = """Name         Country            Area         LON        LAT     Mean depth   Max. depth Reference
                               km**2
                                                                    m           m

Ladoga        Russia         17700.0       61.383     30.950       51.0        230.0        4
Chudskoe      Russia         |3512.0       58.683     27.483      | 7.1      |  16.6        5
Pskovskoe     Russia         |             58.065     27.905      |          |              5

1.  Amante, C. and B.W. Eakins, 2009: not a lake row 1.0 2.0 3.0 4.0 5.0 6
"""
    assert parse_inventory(text) == [("Ladoga", 61.383, 30.950, 51.0, 230.0),
                                     ("Chudskoe", 58.683, 27.483, 7.1, 16.6),
                                     ("Pskovskoe", 58.065, 27.905, None, None)], parse_inventory(text)
    assert rowcol(61.383, 30.950) == (3434, 25314), rowcol(61.383, 30.950)

    #   col 1 2 3      6 7 8
    # row 1  a a       b
    #     2  a a       b b
    #     5        c c
    grid = np.zeros((8, 10), dtype="<i2")
    grid[1:3, 1:3] = [[12, 34], [56, 78]]
    grid[1:3, 6:9] = [[10, 0, 0], [20, 30, 0]]
    grid[5, 4:6] = [40, 50]
    seeds = [("Victoria", 1, 1),     # superseded: dropped
             ("Ladoga", 1, 6), ("Onego", 2, 7),  # one blob, two seeds -> one merged component
             ("Skadar", 6, 6)]       # centroid on a dry cell, the lake is 1 row up and 1 col over
    components = resolve_components(grid, seeds)
    assert sorted(components) == ["ladoga-onego", "skadar", "victoria"], sorted(components)
    assert components["ladoga-onego"][1] == {(1, 6), (2, 6), (2, 7)}, components["ladoga-onego"]
    assert components["skadar"] == ((5, 5), {(5, 4), (5, 5)}), components["skadar"]
    assert sorted(retained(components)) == ["ladoga-onego", "skadar"], sorted(retained(components))

    # A plate: 500 cells of one value bridging the seeded lake to a second lake, which must
    # leave with it. The 0.1 m rim beside the seed is exempt from the cut at any size.
    plated = np.zeros((30, 60), dtype="<i2")
    plated[1, 1:20] = 1                  # a 19-cell rim at 0.1 m: kept
    plated[2, 1:20] = range(30, 49)      # the digitised lake
    plated[3:13, 1:51] = 60              # a 500-cell plate at 6.0 m: cut
    plated[13:15, 1:20] = range(70, 89)  # a second lake, reachable only across the plate
    seed, cells = resolve_components(plated, [("Winnipeg", 2, 2)])["winnipeg"]
    assert len(cells) == 500 + 19 + 19 + 38, len(cells)
    kept = drop_plates(plated, seed, cells, "winnipeg")
    assert kept == {(1, c) for c in range(1, 20)} | {(2, c) for c in range(1, 20)}, sorted(kept)
    assert not _plate_cells(plated, kept), "the cut must leave no removable blob"

    d = tempfile.mkdtemp()
    try:
        rc = np.array(sorted(components["skadar"][1]))
        _write_component(rc, np.array([4.0, 5.0]), f"{d}/synthetic.tif")
        with rasterio.open(f"{d}/synthetic.tif") as src:
            assert src.shape == (1, 2) and src.nodata == NODATA, (src.shape, src.nodata)
            assert list(src.read(1)[0]) == [4.0, 5.0], src.read(1)
            assert src.transform.c == -180.0 + 4 * CELL_DEG, src.transform
            assert src.transform.f == 90.0 - 5 * CELL_DEG, src.transform
        # Both digest-mismatch paths: unreadable bytes refetch, a readable archive exits so the
        # raw survives for repinning.
        with open(f"{d}/truncated.tar.gz", "wb") as f:
            f.write(b"\x1f\x8b\x08\x00 not a tar")
        try:
            read_archive(f"{d}/truncated.tar.gz")
        except ArchiveChanged:
            pass
        else:
            raise AssertionError("unreadable bytes must raise ArchiveChanged")
        with tarfile.open(f"{d}/wrong.tar.gz", "w:gz") as tar:
            tar.add(f"{d}/synthetic.tif", arcname=DEPTH_MEMBER)
        try:
            read_archive(f"{d}/wrong.tar.gz")
        except SystemExit:
            pass
        else:
            raise AssertionError("a readable archive off the pin must exit, not refetch")
        print("convert_gldb.py self-check ok (synthetic)")

        if not archive_path:
            print("convert_gldb.py real-archive check skipped (pass gldbv2.tar.gz to run it)")
            return
        paths = gldb_to_tifs(archive_path, d)
        assert len(paths) == OUTPUT_COUNT, len(paths)
        counts = {}
        for path in paths:
            with rasterio.open(path) as src:
                band = src.read(1)
            counts[path.rsplit("/", 1)[-1][len("gldb_"):-len(".tif")]] = int((band != NODATA).sum())
        assert counts == LAKE_CELLS, {k: v for k, v in counts.items() if LAKE_CELLS.get(k) != v}
        with rasterio.open(f"{d}/gldb_ladoga.tif") as src:
            ladoga = src.read(1)
        valid = ladoga[ladoga != NODATA]  # untouched by the plate cut: its largest blob is 15 cells
        assert len(valid) == 42785, len(valid)
        assert len(set(valid.tolist())) == 1844, len(set(valid.tolist()))
        assert abs(valid.min() - 0.1) < 1e-6 and abs(valid.max() - 205.5) < 1e-4, \
            (valid.min(), valid.max())
        print(f"convert_gldb.py self-check ok ({len(paths)} lakes; Ladoga {len(valid)} cells, "
              f"{len(set(valid.tolist()))} distinct, {valid.min():.1f}..{valid.max():.1f} m)")
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        _check(sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        sys.exit("usage: convert_gldb.py --check [gldbv2.tar.gz]")
