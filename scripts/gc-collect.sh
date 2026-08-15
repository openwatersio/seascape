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
#     tile COG the index's `location` column names.
#   - each candidate a live build/<sha>/manifest.json names (.mosaic_gti), rooted the same way: a
#     published-but-unreleased build must stay promotable, and the build/ lifecycle rule (7 days)
#     bounds how long that hold lasts.
# Everything else under mosaic/ (superseded tiles/overviews, old candidate GTIs + indexes) is the
# delete set. Published pointers carry absolute /vsicurl public URLs; refs are stripped to
# store-relative paths before the set arithmetic.
#
# Failure semantics — absence is proof, errors are not: a candidate GTI or index ABSENT from the
# mosaic listing is an incomplete, unpromotable publish (publish uploads tiles → planet → index →
# GTI last), so it's skipped and its debris collected. Every other failure — a listing error, a
# listed object that won't read, an index that won't parse, a location the parser doesn't
# recognize — REFUSES the whole run: treating a transient backend error as absence would silently
# drop a live root and delete objects the next release needs.
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
    # Error-swallowing listing — ONLY for prefixes where under-listing is fail-safe (it can only
    # shrink the delete set). Never use it to enumerate roots.
    bk_files() { rclone lsf -R --files-only "$ROOT/$1" 2>/dev/null || true; }
    # Root enumeration: exit 3 ("directory not found") is a legitimately absent prefix; any other
    # nonzero is a backend error the caller must refuse on, because a silently-empty listing here
    # would hide a live root.
    bk_files_strict() {
      local out rc=0
      out=$(rclone lsf -R --files-only "$ROOT/$1" 2>/dev/null) || rc=$?
      [ "$rc" -eq 0 ] || [ "$rc" -eq 3 ] || return 1
      [ -z "$out" ] || printf '%s\n' "$out"
    }
    ;;
  local)
    bk_cat()   { cat "$ROOT/$1" 2>/dev/null; }
    bk_files() { (cd "$ROOT/$1" 2>/dev/null && find . -type f | sed 's#^\./##' | sort) || true; }
    # Local dirs can't fail transiently — absent = empty, same as rclone's exit 3.
    bk_files_strict() { bk_files "$1"; }
    ;;
  *) echo "::error::unknown backend '$BACKEND' (rclone|local)" >&2; exit 2 ;;
esac

refuse() { echo "::error::$1" >&2; exit 1; }

python3 -c 'import pyarrow.parquet' 2>/dev/null \
  || refuse "python3 + pyarrow required — the mosaic index is GeoParquet"

# Published pointers carry absolute /vsicurl public URLs (candidates must resolve off the bucket);
# strip a ref down to its mosaic/-relative path so the set arithmetic is form-independent.
strip_ref() { sed -E 's#^/vsicurl/##; s#^https?://[^[:space:]]*/mosaic/##'; }

# root_gti <gti-name> <hard|soft>: parse mosaic/<gti-name>, fetch the index it names, and append
# the GTI + index + planet overview + every tile location to gc-referenced.txt. soft (a
# build-manifest candidate) skips when the GTI or index is ABSENT from the mosaic listing — proof
# of an incomplete, unpromotable publish. Everything else refuses in BOTH modes: a listed object
# that won't read or parse is a backend error or a parser blind spot, and either one could be
# hiding a live root.
root_gti() {
  local gti=$1 mode=$2 xml="$GC_OUT/gc-gti.xml" idx="$GC_OUT/gc-index.parquet" midx planet
  if [ "$mode" = soft ] && ! grep -qxF "mosaic/$gti" "$GC_OUT/gc-all.txt"; then
    echo "skipping $gti — never published (absent from the mosaic listing)"; return 0
  fi
  if ! bk_cat "mosaic/$gti" > "$xml" || [ ! -s "$xml" ]; then
    refuse "mosaic/$gti unreadable — refusing to GC"
  fi
  midx=$(grep -o '<IndexDataset>[^<]*</IndexDataset>' "$xml" | sed -E 's#</?IndexDataset>##g' | strip_ref || true)
  planet=$(grep -o '<Dataset>[^<]*</Dataset>' "$xml" | sed -E 's#</?Dataset>##g' | strip_ref || true)
  case "$midx" in
    index/*.parquet) ;;
    *) refuse "mosaic/$gti has no parsable <IndexDataset> — corrupt mosaic pointer, refusing to GC" ;;
  esac
  case "$planet" in
    planet-z8-*.tif) ;;
    *) refuse "mosaic/$gti has no parsable overview <Dataset> — corrupt mosaic pointer, refusing to GC" ;;
  esac
  if [ "$mode" = soft ] && ! grep -qxF "mosaic/$midx" "$GC_OUT/gc-all.txt"; then
    echo "skipping $gti — its index never landed (incomplete publish)"; return 0
  fi
  if ! bk_cat "mosaic/$midx" > "$idx" || [ ! -s "$idx" ]; then
    refuse "mosaic/$midx (named by mosaic/$gti) failed to fetch — pointer/index mismatch, refusing to GC"
  fi
  if ! python3 -c 'import sys, pyarrow.parquet as pq
for loc in pq.read_table(sys.argv[1], columns=["location"]).column("location").to_pylist():
    print(loc)' "$idx" > "$GC_OUT/gc-locs-raw.txt" 2>/dev/null; then
    refuse "mosaic/$midx is not readable Parquet — refusing to GC"
  fi
  strip_ref < "$GC_OUT/gc-locs-raw.txt" > "$GC_OUT/gc-locs.txt"
  [ -s "$GC_OUT/gc-locs.txt" ] || refuse "mosaic/$midx names no tiles — empty index, refusing to GC"
  if grep -qvE '^tiles/[^/]+\.tif$' "$GC_OUT/gc-locs.txt"; then
    refuse "mosaic/$midx contains an unparsable tile location — refusing to GC"
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

# 2) The full mosaic/ listing — BEFORE rooting, because the roots' absence checks read it. The
#    pointer just read, so an empty listing is a backend/path problem, not an empty store.
bk_files mosaic | sed 's#^#mosaic/#' | sort -u > "$GC_OUT/gc-all.txt"
[ -s "$GC_OUT/gc-all.txt" ] || refuse "mosaic listing is empty but the pointer read — listing mismatch, refusing to GC"

root_gti mosaic.gti hard

# 3) Candidates still promotable: named by a live build/<sha>/manifest.json. The listing is
#    strict (a listing error would hide a live build and delete its candidate); a manifest that
#    LISTS but won't read is the same hazard, so it refuses. Only a manifest that reads as
#    invalid JSON is skipped — release.yml jq-parses it the same way, so it can't promote either.
bk_files_strict build > "$GC_OUT/gc-build-files.txt" \
  || refuse "build/ listing failed — a hidden live build would lose its candidate, refusing to GC"
grep '/manifest\.json$' "$GC_OUT/gc-build-files.txt" | sort > "$GC_OUT/gc-build-manifests.txt" || true
: > "$GC_OUT/gc-candidates.txt"
while IFS= read -r m; do
  [ -n "$m" ] || continue
  bk_cat "build/$m" > "$GC_OUT/gc-bm.json" \
    || refuse "build/$m is listed but unreadable — refusing to GC"
  if ! gti=$(jq -r '.mosaic_gti // empty' "$GC_OUT/gc-bm.json" 2>/dev/null); then
    echo "skipping build/$m — not valid JSON (torn upload; release.yml can't promote it either)"
    continue
  fi
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

# 4) Every referenced object must be PRESENT: a rooted set is complete by construction (publish
#    uploads tiles → planet → index → GTI, promotion copies a complete candidate), so a gap means
#    a stale or mismatched listing that would otherwise mark live objects unreferenced — refuse.
missing=$(comm -13 "$GC_OUT/gc-all.txt" "$GC_OUT/gc-referenced.txt" | head -3 | paste -sd' ' -)
[ -z "$missing" ] || refuse "referenced objects missing from the store listing ($missing …) — refusing to GC"

# 5) Unreferenced mosaic objects → the delete set.
comm -23 "$GC_OUT/gc-all.txt" "$GC_OUT/gc-referenced.txt" > "$GC_OUT/gc-delete.txt"

# 6) Retired source/<id>/bounds.csv registrations.
bk_files source | grep '/bounds\.csv$' | sed 's#^#source/#' >> "$GC_OUT/gc-delete.txt" || true
sort -u -o "$GC_OUT/gc-delete.txt" "$GC_OUT/gc-delete.txt"
del=$(wc -l < "$GC_OUT/gc-delete.txt")

# 7) The retired store-hydrate prefixes, purged wholesale wherever still non-empty. Plain
#    bk_files is fine here: a listing error only defers the purge to the next run.
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
