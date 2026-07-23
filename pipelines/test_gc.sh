#!/usr/bin/env bash
# Test of scripts/gc-collect.sh — the GC's guarded referenced-set arithmetic — against a synthetic
# bathymetry-shaped tree, invoking the SAME script gc.yml runs (local backend), so the workflow's
# Collect step and its test cannot drift. Covers the happy path (the exact delete/keep/purge sets)
# AND every refusal guard: corrupt pointer, absent pointer (clean no-op), corrupt kept manifest,
# pointer outside the kept window, empty referenced set, and the zero-overlap sanity check. The
# delete phase stays in gc.yml; its only arithmetic (the bounded batch split) is checked here too.
# Run: `just test-gc` (bash + jq only; ci.yml runs it on every push).
set -euo pipefail

COLLECT="$(cd "$(dirname "$0")/.." && pwd)/scripts/gc-collect.sh"
KEEP=3
root=$(mktemp -d)
trap 'rm -rf "$root"' EXIT
fail=0

# ── Synthetic bathymetry-shaped tree (the R2 layout gc.yml's rclone backend sees) ──
tree="$root/bathymetry"
mkdir -p "$tree/pmtiles/7-1-1" "$tree/contour" "$tree/depare" "$tree/soundings" \
         "$tree/mosaic/tiles" "$tree/mosaic/index" \
         "$tree/store/manifests" "$tree/source/gebco" "$tree/landmask" \
         "$tree/aggregation/01OLDCOVERING0000000000000" "$tree/aggregation/01NEWCOVERING0000000000000"

# Referenced by a manifest in the kept window: must be KEPT.
keep_objs=(
  pmtiles/7-1-1/8-1-1-12-aaaaaaaaaaaa.pmtiles   # current terrain
  pmtiles/7-1-1/8-1-1-12-cccccccccccc.pmtiles   # last build's terrain (still in the N window)
  pmtiles/7-1-1/6-0-0-8-dddddddddddd.pmtiles     # an overview
  contour/8-1-1-12-eeeeeeeeeeee.fgb
  contour/8-1-1-12-ffffffffffff.empty            # an empty-fork marker is a referenced name too
  depare/8-1-1-12-111111111111.fgb
  soundings/8-1-1-12-222222222222.geojson
)
# Garbage: unreferenced by ANY of the last N manifests → must be DELETED.
del_objs=(
  pmtiles/7-1-1/8-1-1-12-999999999999.pmtiles    # a superseded key from a build older than the window
  pmtiles/7-1-1/8-1-1-12.pmtiles                  # a pre-phase-4 mutable-named artifact
  pmtiles/7-1-1/8-1-1-12.pmtiles.key              # its retired .key sidecar
  contour/9-2-2-13-333333333333.fgb               # an orphan from a re-tiled covering
)
for f in "${keep_objs[@]}" "${del_objs[@]}"; do : > "$tree/$f"; done

# MOSAIC (stage-2 product): the tile COGs + planet-z8 overview are content-addressed and named by
# the manifests (kept via the union); the mosaic.gti pointer + the index it names are held by the
# pointer set, and every SUPERSEDED tile/overview/index must be collected (the bug-4 fix).
mosaic_keep=(
  mosaic/tiles/8-1-1-14-aaaaaaaaaaaa.tif             # current mosaic tile COG (in the manifests)
  mosaic/planet-z8-pppppppppppp.tif                  # current planet z8 overview (in the manifests)
  mosaic/index/01CCCCCCCCCCCCCCCCCCCCCCCC.parquet    # the index the live mosaic.gti names
  mosaic/mosaic.gti                                  # the mosaic pointer itself
)
mosaic_del=(
  mosaic/tiles/8-1-1-14-999999999999.tif             # a superseded mosaic tile (old key)
  mosaic/planet-z8-888888888888.tif                  # a superseded planet z8 (old key)
  mosaic/index/01AAAAAAAAAAAAAAAAAAAAAAAA.parquet    # an old build's index (the pointer moved on)
)
for f in "${mosaic_keep[@]}" "${mosaic_del[@]}"; do : > "$tree/$f"; done
# the live pointer names the current index (relative to the .gti dir); GDAL resolves it there.
printf '<GDALTileIndexDataset><IndexDataset>index/01CCCCCCCCCCCCCCCCCCCCCCCC.parquet</IndexDataset><LocationField>location</LocationField></GDALTileIndexDataset>\n' \
  > "$tree/mosaic/mosaic.gti"

# Sources: bounds/catalog are never swept (source/ is not a content prefix).
: > "$tree/source/gebco/bounds.csv"
: > "$tree/source/gebco/catalog.json"

# Three manifests (M1 oldest .. M3 newest = the pointer's). M3 references the current build; M1/M2
# keep the previous builds' artifacts referenced (so their keys survive one more window).
manifest() { # <dir> <id> <name...>
  local dir=$1 id=$2; shift 2
  printf '%s\n' "$@" | jq -R . | jq -s '{entries: [.[] | {name: .}]}' > "$dir/store/manifests/$id.json"
}
manifest "$tree" 01AAAAAAAAAAAAAAAAAAAAAAAA \
  pmtiles/7-1-1/8-1-1-12-cccccccccccc.pmtiles pmtiles/7-1-1/6-0-0-8-dddddddddddd.pmtiles \
  contour/8-1-1-12-eeeeeeeeeeee.fgb depare/8-1-1-12-111111111111.fgb soundings/8-1-1-12-222222222222.geojson
manifest "$tree" 01BBBBBBBBBBBBBBBBBBBBBBBB \
  pmtiles/7-1-1/8-1-1-12-cccccccccccc.pmtiles pmtiles/7-1-1/6-0-0-8-dddddddddddd.pmtiles \
  contour/8-1-1-12-eeeeeeeeeeee.fgb contour/8-1-1-12-ffffffffffff.empty \
  depare/8-1-1-12-111111111111.fgb soundings/8-1-1-12-222222222222.geojson
manifest "$tree" 01CCCCCCCCCCCCCCCCCCCCCCCC \
  pmtiles/7-1-1/8-1-1-12-aaaaaaaaaaaa.pmtiles pmtiles/7-1-1/6-0-0-8-dddddddddddd.pmtiles \
  contour/8-1-1-12-eeeeeeeeeeee.fgb contour/8-1-1-12-ffffffffffff.empty \
  depare/8-1-1-12-111111111111.fgb soundings/8-1-1-12-222222222222.geojson \
  mosaic/tiles/8-1-1-14-aaaaaaaaaaaa.tif mosaic/planet-z8-pppppppppppp.tif
printf '{"manifest":"manifests/01CCCCCCCCCCCCCCCCCCCCCCCC.json"}\n' > "$tree/store/manifest.json"

# ── Happy path: run the real Collect, assert the exact delete/keep/purge sets ──
out=$(mktemp -d)
GC_OUT="$out" bash "$COLLECT" local "$tree" "$KEEP" > "$out/log" 2>&1 \
  || { echo "FAIL: happy-path collect refused:"; cat "$out/log"; exit 1; }

assert_in()  { grep -qxF "$1" "$2" || { echo "FAIL: expected '$1' in $2"; fail=1; }; }
assert_out() { grep -qxF "$1" "$2" && { echo "FAIL: '$1' must NOT be in $2"; fail=1; } || true; }

for f in "${del_objs[@]}";  do assert_in  "$f" "$out/gc-delete.txt"; done
for f in "${keep_objs[@]}"; do assert_out "$f" "$out/gc-delete.txt"; done
# mosaic: superseded tiles/overview/index collected; current tiles/overview/index + the pointer kept
for f in "${mosaic_del[@]}";  do assert_in  "$f" "$out/gc-delete.txt"; done
for f in "${mosaic_keep[@]}"; do assert_out "$f" "$out/gc-delete.txt"; done
# source/ registrations are not a swept prefix, never listed
assert_out "source/gebco/bounds.csv"   "$out/gc-delete.txt"
assert_out "source/gebco/catalog.json" "$out/gc-delete.txt"
# both diff-era coverings are queued for purge
assert_in "01OLDCOVERING0000000000000" "$out/gc-purge-dirs.txt"
assert_in "01NEWCOVERING0000000000000" "$out/gc-purge-dirs.txt"
[ "$(grep -c . "$out/gc-purge-dirs.txt")" -eq 2 ] || { echo "FAIL: expected 2 covering dirs to purge"; fail=1; }
# exact count: 4 unreferenced content objects + 3 superseded mosaic objects
[ "$(wc -l < "$out/gc-delete.txt")" -eq 7 ] \
  || { echo "FAIL: delete set size $(wc -l < "$out/gc-delete.txt") != 7"; cat "$out/gc-delete.txt"; fail=1; }
# the full inventory printed before anything would delete
grep -q "── inventory ──" "$out/log" || { echo "FAIL: no inventory in the collect log"; fail=1; }

# ── Refusal guards: each mutation must make the collect REFUSE (nonzero + its message) ──
mutate() { rm -rf "$root/mut"; cp -R "$tree" "$root/mut"; }
expect_refuse() { # <desc> <keep_n> <message substring>
  local rc=0 log
  log=$(GC_OUT="$(mktemp -d)" bash "$COLLECT" local "$root/mut" "$2" 2>&1) || rc=$?
  if [ "$rc" -eq 0 ]; then echo "FAIL: $1 must refuse (exited 0):"; echo "$log"; fail=1; return; fi
  grep -qF "$3" <<<"$log" || { echo "FAIL: $1 refused with the wrong message:"; echo "$log"; fail=1; }
}

# a) corrupt pointer JSON → refuse (an unreadable pointer must never read as "no pointer")
mutate; echo "not json {" > "$root/mut/store/manifest.json"
expect_refuse "corrupt pointer" "$KEEP" "store pointer is not valid JSON"

# b) a corrupt manifest INSIDE the kept window → refuse (an incomplete referenced set would mark
#    live artifacts as garbage)
mutate; echo "{ broken" > "$root/mut/store/manifests/01BBBBBBBBBBBBBBBBBBBBBBBB.json"
expect_refuse "corrupt kept manifest" "$KEEP" "is not valid JSON with .entries"

# c) pointer naming a manifest OUTSIDE the kept window (a stale/corrupt listing) → refuse.
#    A newer 01DDD manifest + keep_n=1 pushes the pointer's 01CCC out of the window.
mutate
manifest "$root/mut" 01DDDDDDDDDDDDDDDDDDDDDDDD pmtiles/7-1-1/8-1-1-12-aaaaaaaaaaaa.pmtiles
expect_refuse "pointer outside kept window" 1 "not among the newest 1"

# d) empty referenced set (every kept manifest has entries: []) → refuse
mutate
for m in "$root/mut/store/manifests/"*.json; do echo '{"entries":[]}' > "$m"; done
expect_refuse "empty referenced set" "$KEEP" "referenced set is empty"

# e) zero-overlap sanity: the referenced names match NOTHING present (a path/listing mismatch) —
#    the guard that keeps a bad listing from marking the whole store unreferenced. Remove mosaic too,
#    else its still-present referenced tiles/overview keep the overlap nonzero.
mutate; rm -rf "$root/mut/pmtiles" "$root/mut/contour" "$root/mut/soundings" "$root/mut/depare" "$root/mut/mosaic"
expect_refuse "zero-overlap sanity" "$KEEP" "path mismatch, refusing to GC"

# g) a present-but-corrupt mosaic.gti (no <IndexDataset>) → refuse (an unparseable mosaic pointer
#    would drop the current index from the referenced set and collect a live mosaic).
mutate; printf '<GDALTileIndexDataset></GDALTileIndexDataset>\n' > "$root/mut/mosaic/mosaic.gti"
expect_refuse "corrupt mosaic.gti" "$KEEP" "corrupt mosaic pointer"

# f) ABSENT pointer is not a refusal: pre-immutable store → exit 0 with empty outputs
mutate; rm "$root/mut/store/manifest.json"
noptr=$(mktemp -d)
GC_OUT="$noptr" bash "$COLLECT" local "$root/mut" "$KEEP" > /dev/null 2>&1 \
  || { echo "FAIL: absent pointer must exit 0 (pre-immutable no-op)"; fail=1; }
[ ! -s "$noptr/gc-delete.txt" ] && [ ! -s "$noptr/gc-purge-dirs.txt" ] \
  || { echo "FAIL: absent pointer must flag nothing"; fail=1; }

# ── The delete phase's only arithmetic (gc.yml): a 2500-line list splits into 3 batches <= 1000 ──
seq 2500 | sed 's#^#pmtiles/x-#; s#$#.pmtiles#' > "$root/big.txt"
tmpb=$(mktemp -d)
split -l 1000 -d "$root/big.txt" "$tmpb/batch-"
n=$(find "$tmpb" -name 'batch-*' | wc -l)
[ "$n" -eq 3 ] || { echo "FAIL: 2500 objects must split into 3 batches, got $n"; fail=1; }
for b in "$tmpb"/batch-*; do
  [ "$(wc -l < "$b")" -le 1000 ] || { echo "FAIL: a batch exceeds 1000"; fail=1; }
done
rm -rf "$tmpb"

[ "$fail" -eq 0 ] || exit 1
echo "gc-sim ok — ${#del_objs[@]} content + ${#mosaic_del[@]} mosaic garbage flagged, ${#keep_objs[@]} content + ${#mosaic_keep[@]} mosaic referenced kept, 2 coverings purged, 6 guards refuse, absent-pointer no-op, batches bounded"
