#!/usr/bin/env bash
# Test of scripts/gc-collect.sh — the GC's guarded referenced-set arithmetic — against a synthetic
# bathymetry-shaped tree, invoking the SAME script gc.yml runs (local backend), so the workflow's
# Collect step and its test cannot drift. Covers the happy path (the exact delete/keep/purge sets,
# a pending candidate rooted via its build manifest, and an incomplete candidate that soft-skips)
# AND the failure semantics: absence skips (absent pointer no-op, unpublished candidate, an index
# that never landed, a torn build manifest), errors refuse (corrupt pointer, a missing/unreadable/
# empty index, an unparsable tile location, a referenced object missing from the listing, a listed
# build manifest that won't read). The delete phase stays in gc.yml; its only arithmetic (the
# bounded batch split) is checked here too.
# Run: `just test-gc` (bash + jq + python3/pyarrow; ci.yml runs it on every push).
set -euo pipefail

COLLECT="$(cd "$(dirname "$0")/.." && pwd)/scripts/gc-collect.sh"
root=$(mktemp -d)
trap 'rm -rf "$root"' EXIT
fail=0

python3 -c 'import pyarrow' 2>/dev/null || { echo "FAIL: pyarrow missing (pip install pyarrow)"; exit 1; }

# ── Synthetic bathymetry-shaped tree (the R2 layout gc.yml's rclone backend sees) ──
tree="$root/bathymetry"
mkdir -p "$tree/mosaic/tiles" "$tree/mosaic/index" \
         "$tree/build/aaa111" "$tree/build/bbb222" "$tree/build/ccc333" \
         "$tree/source/gebco" "$tree/landmask" \
         "$tree/pmtiles/7-1-1" "$tree/contour" "$tree/soundings" "$tree/depare" \
         "$tree/store/manifests" "$tree/aggregation/01OLDCOVERING0000000000000"

# Published pointers carry ABSOLUTE /vsicurl public URLs (they resolve off the bucket) — the
# fixture uses the same form so the test exercises the ref stripping.
PUB=/vsicurl/https://data.example.test/bathymetry/mosaic

index() { # <out> <tile-basename...> — a minimal GeoParquet-shaped index: just the location column
  local out=$1; shift
  python3 - "$out" "$@" <<'PY'
import sys, pyarrow as pa, pyarrow.parquet as pq
base = "/vsicurl/https://data.example.test/bathymetry/mosaic/tiles/"
pq.write_table(pa.table({"location": [base + t for t in sys.argv[2:]]}), sys.argv[1])
PY
}

gti() { # <out> <idxhash> <planet-name>
  printf '<GDALTileIndexDataset>\n  <IndexDataset>%s/index/%s.parquet</IndexDataset>\n  <LocationField>location</LocationField>\n  <Overview>\n    <Dataset>%s/%s</Dataset>\n  </Overview>\n</GDALTileIndexDataset>\n' \
    "$PUB" "$2" "$PUB" "$3" > "$1"
}

# The SERVING set (idxhash c1c1…): mosaic.gti is the promoted copy of that candidate.
index "$tree/mosaic/index/c1c1c1c1c1c1.parquet" 8-1-1-14-aaaaaaaaaaaa.tif 6-0-0-14-dddddddddddd.tif
gti "$tree/mosaic/mosaic.gti" c1c1c1c1c1c1 planet-z8-pppppppppppp.tif
# A PENDING candidate (idxhash c2c2…): published, not yet released — rooted via build/aaa111.
index "$tree/mosaic/index/c2c2c2c2c2c2.parquet" 8-1-1-14-aaaaaaaaaaaa.tif 8-2-2-14-bbbbbbbbbbbb.tif
gti "$tree/mosaic/mosaic-candidate-c2c2c2c2c2c2.gti" c2c2c2c2c2c2 planet-z8-qqqqqqqqqqqq.tif

mosaic_keep=(
  mosaic/mosaic.gti
  mosaic/index/c1c1c1c1c1c1.parquet
  mosaic/tiles/8-1-1-14-aaaaaaaaaaaa.tif              # shared by serving + pending
  mosaic/tiles/6-0-0-14-dddddddddddd.tif
  mosaic/planet-z8-pppppppppppp.tif
  mosaic/mosaic-candidate-c2c2c2c2c2c2.gti            # pending candidate, held by build/aaa111
  mosaic/index/c2c2c2c2c2c2.parquet
  mosaic/tiles/8-2-2-14-bbbbbbbbbbbb.tif
  mosaic/planet-z8-qqqqqqqqqqqq.tif
)
mosaic_del=(
  mosaic/mosaic-candidate-a1a1a1a1a1a1.gti            # an old build's candidate (manifest expired)
  mosaic/index/a1a1a1a1a1a1.parquet
  mosaic/tiles/8-1-1-14-999999999999.tif              # a superseded tile (old key)
  mosaic/planet-z8-888888888888.tif                   # a superseded planet z8
  mosaic/mosaic-candidate-c1c1c1c1c1c1.gti            # the PROMOTED candidate's own copy: release
                                                      # skips re-promotion of a published sha, so
                                                      # nothing ever reads it again
)
# touch, not truncate: the keep list includes the real GTIs/parquets written above
for f in "${mosaic_keep[@]}" "${mosaic_del[@]}"; do touch "$tree/$f"; done

# Build manifests: aaa111 holds the pending candidate; bbb222 predates mosaic_gti (skipped);
# ccc333 names a candidate whose GTI/index never landed (incomplete publish → soft skip).
printf '{"mosaic_gti":"mosaic-candidate-c2c2c2c2c2c2.gti"}\n' > "$tree/build/aaa111/manifest.json"
printf '{}\n' > "$tree/build/bbb222/manifest.json"
printf '{"mosaic_gti":"mosaic-candidate-deadbeefdead.gti"}\n' > "$tree/build/ccc333/manifest.json"

# Sources: the retired bounds.csv is swept (catalog.json carries seascape:files now);
# the catalog itself is never touched.
: > "$tree/source/gebco/bounds.csv"
: > "$tree/source/gebco/catalog.json"
: > "$tree/landmask/land.fgb"

# The retired store-hydrate prefixes: still non-empty → purged wholesale.
: > "$tree/pmtiles/7-1-1/8-1-1-12-aaaaaaaaaaaa.pmtiles"
: > "$tree/contour/8-1-1-12-eeeeeeeeeeee.fgb"
: > "$tree/soundings/8-1-1-12-222222222222.geojsons"
: > "$tree/depare/8-1-1-12-111111111111.fgb"
: > "$tree/aggregation/01OLDCOVERING0000000000000/cover.fgb"
: > "$tree/store/manifests/01AAAAAAAAAAAAAAAAAAAAAAAA.json"
printf '{"manifest":"manifests/01AAAAAAAAAAAAAAAAAAAAAAAA.json"}\n' > "$tree/store/manifest.json"

# ── Happy path: run the real Collect, assert the exact delete/keep/purge sets ──
out=$(mktemp -d)
GC_OUT="$out" bash "$COLLECT" local "$tree" > "$out/log" 2>&1 \
  || { echo "FAIL: happy-path collect refused:"; cat "$out/log"; exit 1; }

assert_in()  { grep -qxF "$1" "$2" || { echo "FAIL: expected '$1' in $2"; fail=1; }; }
assert_out() { grep -qxF "$1" "$2" && { echo "FAIL: '$1' must NOT be in $2"; fail=1; } || true; }

for f in "${mosaic_del[@]}";  do assert_in  "$f" "$out/gc-delete.txt"; done
for f in "${mosaic_keep[@]}"; do assert_out "$f" "$out/gc-delete.txt"; done
# the retired bounds.csv is swept; the catalog item is never touched
assert_in  "source/gebco/bounds.csv"   "$out/gc-delete.txt"
assert_out "source/gebco/catalog.json" "$out/gc-delete.txt"
# every retired prefix flagged for wholesale purge — and ONLY those
for p in pmtiles contour soundings depare aggregation store; do
  assert_in "$p" "$out/gc-purge-dirs.txt"
done
[ "$(grep -c . "$out/gc-purge-dirs.txt")" -eq 6 ] || { echo "FAIL: expected exactly 6 retired prefixes"; fail=1; }
# exact count: 5 superseded mosaic objects + 1 retired bounds.csv
[ "$(wc -l < "$out/gc-delete.txt")" -eq 6 ] \
  || { echo "FAIL: delete set size $(wc -l < "$out/gc-delete.txt") != 6"; cat "$out/gc-delete.txt"; fail=1; }
# the incomplete candidate soft-skipped with a note; the full inventory printed
grep -q "skipping mosaic-candidate-deadbeefdead.gti" "$out/log" \
  || { echo "FAIL: incomplete candidate must soft-skip with a note"; fail=1; }
grep -q "── inventory ──" "$out/log" || { echo "FAIL: no inventory in the collect log"; fail=1; }

# ── Refusal guards: each mutation must make the collect REFUSE (nonzero + its message) ──
mutate() { rm -rf "$root/mut"; cp -R "$tree" "$root/mut"; }
expect_refuse() { # <desc> <message substring>
  local rc=0 log
  log=$(GC_OUT="$(mktemp -d)" bash "$COLLECT" local "$root/mut" 2>&1) || rc=$?
  if [ "$rc" -eq 0 ]; then echo "FAIL: $1 must refuse (exited 0):"; echo "$log"; fail=1; return; fi
  grep -qF "$2" <<<"$log" || { echo "FAIL: $1 refused with the wrong message:"; echo "$log"; fail=1; }
}

# a) a present-but-corrupt mosaic.gti (no <IndexDataset>) → refuse (an unparseable serving pointer
#    would collect the live mosaic)
mutate; printf '<GDALTileIndexDataset></GDALTileIndexDataset>\n' > "$root/mut/mosaic/mosaic.gti"
expect_refuse "corrupt mosaic.gti" "corrupt mosaic pointer"

# b) the serving index missing → refuse (pointer/index mismatch)
mutate; rm "$root/mut/mosaic/index/c1c1c1c1c1c1.parquet"
expect_refuse "missing serving index" "pointer/index mismatch"

# c) the serving index unreadable as Parquet → refuse
mutate; echo "not parquet" > "$root/mut/mosaic/index/c1c1c1c1c1c1.parquet"
expect_refuse "unreadable serving index" "not readable Parquet"

# d) the serving index names no tiles → refuse
mutate; index "$root/mut/mosaic/index/c1c1c1c1c1c1.parquet"
expect_refuse "empty serving index" "empty index"

# e) a location that doesn't strip to tiles/<name>.tif (a location-format change) → refuse rather
#    than mark every real tile unreferenced
mutate
python3 - "$root/mut/mosaic/index/c1c1c1c1c1c1.parquet" <<'PY'
import sys, pyarrow as pa, pyarrow.parquet as pq
pq.write_table(pa.table({"location": ["https://elsewhere.test/foo.tif"]}), sys.argv[1])
PY
expect_refuse "unparsable tile location" "unparsable tile location"

# f) a referenced tile absent from the listing (stale/mismatched listing) → refuse
mutate; rm "$root/mut/mosaic/tiles/6-0-0-14-dddddddddddd.tif"
expect_refuse "referenced object missing" "missing from the store listing"

# h) a LISTED build manifest that won't read → refuse (an error is not absence: treating it as
#    "no candidate" would delete a live build's candidate)
mutate; chmod 000 "$root/mut/build/aaa111/manifest.json"
expect_refuse "unreadable build manifest" "listed but unreadable"

# i) a torn (invalid-JSON) build manifest is NOT a refusal — release.yml jq-parses it the same
#    way, so it can't promote either; its candidate loses the hold and falls to collection
mutate; echo '{ torn' > "$root/mut/build/aaa111/manifest.json"
tornout=$(mktemp -d)
GC_OUT="$tornout" bash "$COLLECT" local "$root/mut" > "$tornout/log" 2>&1 \
  || { echo "FAIL: torn build manifest must not refuse:"; cat "$tornout/log"; fail=1; }
grep -q "skipping build/aaa111/manifest.json" "$tornout/log" \
  || { echo "FAIL: torn build manifest must log a skip"; fail=1; }
assert_in "mosaic/mosaic-candidate-c2c2c2c2c2c2.gti" "$tornout/gc-delete.txt"

# j) a pending candidate's LISTED index that won't parse → refuse (soft mode only forgives absence)
mutate; echo "not parquet" > "$root/mut/mosaic/index/c2c2c2c2c2c2.parquet"
expect_refuse "unreadable pending index" "not readable Parquet"

# k) a pending candidate whose index never landed → skip + collect (absence IS proof: publish
#    order means no index = the candidate can never be promoted)
mutate; rm "$root/mut/mosaic/index/c2c2c2c2c2c2.parquet"
skipout=$(mktemp -d)
GC_OUT="$skipout" bash "$COLLECT" local "$root/mut" > "$skipout/log" 2>&1 \
  || { echo "FAIL: absent pending index must not refuse:"; cat "$skipout/log"; fail=1; }
grep -q "its index never landed" "$skipout/log" \
  || { echo "FAIL: expected the index-never-landed skip"; fail=1; }
assert_in "mosaic/mosaic-candidate-c2c2c2c2c2c2.gti" "$skipout/gc-delete.txt"

# g) ABSENT pointer is not a refusal: pre-mosaic store → exit 0 with empty outputs
mutate; rm "$root/mut/mosaic/mosaic.gti"
noptr=$(mktemp -d)
GC_OUT="$noptr" bash "$COLLECT" local "$root/mut" > /dev/null 2>&1 \
  || { echo "FAIL: absent pointer must exit 0 (pre-mosaic no-op)"; fail=1; }
[ ! -s "$noptr/gc-delete.txt" ] && [ ! -s "$noptr/gc-purge-dirs.txt" ] \
  || { echo "FAIL: absent pointer must flag nothing"; fail=1; }

# ── The delete phase's only arithmetic (gc.yml): a 2500-line list splits into 3 batches <= 1000 ──
seq 2500 | sed 's#^#mosaic/tiles/x-#; s#$#.tif#' > "$root/big.txt"
tmpb=$(mktemp -d)
split -l 1000 -d "$root/big.txt" "$tmpb/batch-"
n=$(find "$tmpb" -name 'batch-*' | wc -l)
[ "$n" -eq 3 ] || { echo "FAIL: 2500 objects must split into 3 batches, got $n"; fail=1; }
for b in "$tmpb"/batch-*; do
  [ "$(wc -l < "$b")" -le 1000 ] || { echo "FAIL: a batch exceeds 1000"; fail=1; }
done
rm -rf "$tmpb"

[ "$fail" -eq 0 ] || exit 1
echo "gc-sim ok — ${#mosaic_del[@]} mosaic garbage + 1 bounds.csv flagged, ${#mosaic_keep[@]} referenced kept (serving + pending candidate), 6 retired prefixes purged, 8 guards refuse, 4 absence cases skip/no-op, batches bounded"
