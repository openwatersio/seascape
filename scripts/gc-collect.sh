#!/usr/bin/env bash
# The GC "Collect" step — the guarded referenced-set arithmetic that decides what the store GC may
# delete. ONE implementation, two backends: .github/workflows/gc.yml runs it with the rclone
# backend against R2, and pipelines/test_gc.sh runs it with the local backend against a synthetic
# store tree (happy path + every refusal guard), so the workflow's arithmetic and its test can't
# drift. Deletion itself stays in gc.yml (the dry-run gate + bounded batches) — this script only
# computes, inventories, and refuses.
#
# The referenced set is rooted on the published mosaic:
#   - mosaic/mosaic.gti (the serving pointer): its index parquet, planet-z8 overview, and every
#     tile COG the index's `location` column names. Any failure here REFUSES — a broken serving
#     set is a human's problem, never a delete list.
#   - each candidate a live build/<sha>/manifest.json names (.mosaic_gti), rooted the same way: a
#     published-but-unreleased build must stay promotable, and the build/ lifecycle rule (7 days)
#     bounds how long that hold lasts. A candidate whose GTI or index never landed can never be
#     promoted — skipped with a note, so its debris falls to collection.
# Everything else under mosaic/ (superseded tiles/overviews, old candidate GTIs + indexes) is the
# delete set. Published pointers carry absolute /vsicurl public URLs; refs are stripped to
# store-relative paths before the set arithmetic.
#
# Also flags:
#   - the retired store-hydrate prefixes WHOLESALE (gc-purge-dirs.txt): pmtiles/ contour/
#     soundings/ depare/ aggregation/ store/ — dead since the store moved to the persistent
#     build volume; nothing reads or writes them.
#   - retired source/<id>/bounds.csv registrations (catalog.json carries the per-file rows as
#     seascape:files).
# NEVER touches: build/<sha>/ (read-only roots here; an R2 lifecycle rule collects the prefix),
# source COGs / catalog.json / polygon/ / landmask/ (sources.yml owns them).
#
# usage: gc-collect.sh <rclone|local> <root>
#   <root>: the bathymetry prefix — an rclone remote path (R2:data/bathymetry) or a local dir
# Needs python3 + pyarrow (the mosaic index is GeoParquet).
#
# outputs under $GC_OUT (default /tmp):
#   gc-delete.txt      unreferenced objects to delete, paths relative to <root>
#   gc-purge-dirs.txt  retired prefixes to purge wholesale, relative to <root>
# exit 0: collected (or nothing to GC yet — empty outputs); nonzero: a guard refused.
set -euo pipefail

BACKEND=${1:?usage: gc-collect.sh <rclone|local> <root>}
ROOT=${2:?usage: gc-collect.sh <rclone|local> <root>}
GC_OUT=${GC_OUT:-/tmp}

case "$BACKEND" in
  rclone)
    bk_cat()   { rclone cat "$ROOT/$1" 2>/dev/null; }
    bk_files() { rclone lsf -R --files-only "$ROOT/$1" 2>/dev/null || true; }
    ;;
  local)
    bk_cat()   { cat "$ROOT/$1" 2>/dev/null; }
    bk_files() { (cd "$ROOT/$1" 2>/dev/null && find . -type f | sed 's#^\./##' | sort) || true; }
    ;;
  *) echo "::error::unknown backend '$BACKEND' (rclone|local)" >&2; exit 2 ;;
esac

refuse() { echo "::error::$1" >&2; exit 1; }

python3 -c 'import pyarrow.parquet' 2>/dev/null \
  || refuse "python3 + pyarrow required — the mosaic index is GeoParquet"

# Strip a pointer ref down to its mosaic/-relative path (published candidates and the promoted
# serving pointer carry absolute /vsicurl URLs so they resolve off the bucket).
strip_ref() { sed -E 's#^/vsicurl/##; s#^https?://[^[:space:]]*/mosaic/##'; }

# root_gti <gti-name> <hard|soft>: parse mosaic/<gti-name>, fetch the index it names, and append
# the GTI + index + planet overview + every tile location to gc-referenced.txt. hard (the serving
# pointer) refuses on any failure; soft (a build-manifest candidate) logs and adds nothing — an
# incompletely-published candidate can never be promoted, so its debris is collectable.
root_gti() {
  local gti=$1 mode=$2 xml="$GC_OUT/gc-gti.xml" idx="$GC_OUT/gc-index.parquet" midx planet
  if ! bk_cat "mosaic/$gti" > "$xml" || [ ! -s "$xml" ]; then
    if [ "$mode" = hard ]; then refuse "mosaic/$gti unreadable — refusing to GC"; fi
    echo "skipping $gti — its GTI never landed (incomplete publish)"; return 0
  fi
  midx=$(grep -o '<IndexDataset>[^<]*</IndexDataset>' "$xml" | sed -E 's#</?IndexDataset>##g' | strip_ref || true)
  planet=$(grep -o '<Dataset>[^<]*</Dataset>' "$xml" | sed -E 's#</?Dataset>##g' | strip_ref || true)
  case "$midx" in
    index/*.parquet) ;;
    *) if [ "$mode" = hard ]; then refuse "mosaic/$gti has no parsable <IndexDataset> — corrupt mosaic pointer, refusing to GC"; fi
       echo "skipping $gti — unparsable index ref"; return 0 ;;
  esac
  case "$planet" in
    planet-z8-*.tif) ;;
    *) if [ "$mode" = hard ]; then refuse "mosaic/$gti has no parsable overview <Dataset> — corrupt mosaic pointer, refusing to GC"; fi
       echo "skipping $gti — unparsable planet ref"; return 0 ;;
  esac
  if ! bk_cat "mosaic/$midx" > "$idx" || [ ! -s "$idx" ]; then
    if [ "$mode" = hard ]; then refuse "mosaic/$midx (named by mosaic/$gti) failed to fetch — pointer/index mismatch, refusing to GC"; fi
    echo "skipping $gti — its index never landed (incomplete publish)"; return 0
  fi
  if ! python3 -c 'import sys, pyarrow.parquet as pq
for loc in pq.read_table(sys.argv[1], columns=["location"]).column("location").to_pylist():
    print(loc)' "$idx" > "$GC_OUT/gc-locs-raw.txt" 2>/dev/null; then
    if [ "$mode" = hard ]; then refuse "mosaic/$midx is not readable Parquet — refusing to GC"; fi
    echo "skipping $gti — its index is not readable Parquet"; return 0
  fi
  strip_ref < "$GC_OUT/gc-locs-raw.txt" > "$GC_OUT/gc-locs.txt"
  if [ ! -s "$GC_OUT/gc-locs.txt" ]; then
    if [ "$mode" = hard ]; then refuse "mosaic/$midx names no tiles — empty index, refusing to GC"; fi
    echo "skipping $gti — empty index"; return 0
  fi
  if grep -qvE '^tiles/[^/]+\.tif$' "$GC_OUT/gc-locs.txt"; then
    if [ "$mode" = hard ]; then refuse "mosaic/$midx contains an unparsable tile location — refusing to GC"; fi
    echo "skipping $gti — unparsable tile location in its index"; return 0
  fi
  { printf 'mosaic/%s\n' "$gti" "$midx" "$planet"; sed 's#^#mosaic/#' "$GC_OUT/gc-locs.txt"; } >> "$GC_OUT/gc-referenced.txt"
  echo "rooted $gti → $(wc -l < "$GC_OUT/gc-locs.txt") tile(s) + index + planet"
}

# 1) The serving pointer. Genuinely ABSENT = pre-mosaic store, nothing to GC yet — empty outputs,
#    exit 0. Present means the whole serving set must resolve, or the run refuses.
: > "$GC_OUT/gc-referenced.txt"
if ! bk_cat mosaic/mosaic.gti > "$GC_OUT/gc-probe.gti" || [ ! -s "$GC_OUT/gc-probe.gti" ]; then
  echo "no mosaic pointer — nothing to GC yet (pre-mosaic store)"
  : > "$GC_OUT/gc-delete.txt"
  : > "$GC_OUT/gc-purge-dirs.txt"
  exit 0
fi
root_gti mosaic.gti hard

# 2) Candidates still promotable: named by a live build/<sha>/manifest.json. A torn or invalid
#    manifest can't be released either (release.yml jq-parses it the same way) — skipped.
bk_files build | grep '/manifest\.json$' | sort > "$GC_OUT/gc-build-manifests.txt" || true
: > "$GC_OUT/gc-candidates.txt"
while IFS= read -r m; do
  [ -n "$m" ] || continue
  gti=$(bk_cat "build/$m" | jq -r '.mosaic_gti // empty' 2>/dev/null) || gti=""
  case "$gti" in
    mosaic-candidate-*.gti) echo "$gti" >> "$GC_OUT/gc-candidates.txt" ;;
    "") ;;
    *) echo "skipping build/$m — unrecognized mosaic_gti '$gti'" ;;
  esac
done < "$GC_OUT/gc-build-manifests.txt"
sort -u -o "$GC_OUT/gc-candidates.txt" "$GC_OUT/gc-candidates.txt"
while IFS= read -r c; do
  [ -n "$c" ] || continue
  root_gti "$c" soft
done < "$GC_OUT/gc-candidates.txt"
sort -u -o "$GC_OUT/gc-referenced.txt" "$GC_OUT/gc-referenced.txt"
echo "referenced mosaic objects: $(wc -l < "$GC_OUT/gc-referenced.txt")"

# 3) Everything actually under mosaic/. Every referenced object must be PRESENT: a rooted set is
#    complete by construction (publish uploads tiles → planet → index → GTI, promotion copies a
#    complete candidate), so a gap means a stale or mismatched listing that would otherwise mark
#    live objects unreferenced — refuse.
bk_files mosaic | sed 's#^#mosaic/#' | sort -u > "$GC_OUT/gc-all.txt"
[ -s "$GC_OUT/gc-all.txt" ] || refuse "mosaic listing is empty but the pointer read — listing mismatch, refusing to GC"
missing=$(comm -13 "$GC_OUT/gc-all.txt" "$GC_OUT/gc-referenced.txt" | head -3 | paste -sd' ' -)
[ -z "$missing" ] || refuse "referenced objects missing from the store listing ($missing …) — refusing to GC"

# 4) Unreferenced mosaic objects → the delete set.
comm -23 "$GC_OUT/gc-all.txt" "$GC_OUT/gc-referenced.txt" > "$GC_OUT/gc-delete.txt"

# 5) Retired source/<id>/bounds.csv registrations.
bk_files source | grep '/bounds\.csv$' | sed 's#^#source/#' >> "$GC_OUT/gc-delete.txt" || true
sort -u -o "$GC_OUT/gc-delete.txt" "$GC_OUT/gc-delete.txt"
del=$(wc -l < "$GC_OUT/gc-delete.txt")

# 6) The retired store-hydrate prefixes, purged wholesale wherever still non-empty.
: > "$GC_OUT/gc-purge-dirs.txt"
for p in pmtiles contour soundings depare aggregation store; do
  [ -n "$(bk_files "$p" | head -1)" ] && echo "$p" >> "$GC_OUT/gc-purge-dirs.txt"
done
dirs=$(grep -c . "$GC_OUT/gc-purge-dirs.txt" || true)

# ── Full inventory BEFORE anything deletes ──
echo "── inventory ──"
mt=$(grep -c '^mosaic/' "$GC_OUT/gc-all.txt" || true)
md=$(grep -c '^mosaic/' "$GC_OUT/gc-delete.txt" || true)
echo "  mosaic: $mt objects, $((mt - md)) kept, $md to delete"
bc=$(grep -c '^source/.*/bounds\.csv$' "$GC_OUT/gc-delete.txt" || true)
echo "  source/*/bounds.csv: $bc to delete"
echo "  retired prefixes to purge: $(paste -sd' ' "$GC_OUT/gc-purge-dirs.txt")"
echo "totals: $del objects + $dirs retired prefix(es)"
echo "── first 20 objects flagged for deletion ──"
head -20 "$GC_OUT/gc-delete.txt" || true
