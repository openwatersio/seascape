#!/usr/bin/env bash
# The GC "Collect" step — the guarded referenced-set arithmetic that decides what the store GC may
# delete. ONE implementation, two backends: .github/workflows/gc.yml runs it with the rclone
# backend against R2, and pipelines/test_gc.sh runs it with the local backend against a synthetic
# store tree (happy path + every refusal guard), so the workflow's arithmetic and its test can't
# drift. Deletion itself stays in gc.yml (the dry-run gate + bounded batches) — this script only
# computes, inventories, and refuses.
#
# usage: gc-collect.sh <rclone|local> <root> [keep_n]
#   <root>:  the bathymetry prefix — an rclone remote path (R2:data/bathymetry) or a local dir
#   keep_n:  union of the newest N store manifests = the referenced set (default 3)
#
# outputs under $GC_OUT (default /tmp):
#   gc-delete.txt      unreferenced objects to delete, paths relative to <root>
#   gc-purge-dirs.txt  retired diff-era aggregation/<ulid> covering dirs to purge
# exit 0: collected (or nothing to GC yet — empty outputs); nonzero: a guard refused.
set -euo pipefail

BACKEND=${1:?usage: gc-collect.sh <rclone|local> <root> [keep_n]}
ROOT=${2:?usage: gc-collect.sh <rclone|local> <root> [keep_n]}
KEEP=${3:-3}
GC_OUT=${GC_OUT:-/tmp}

case "$BACKEND" in
  rclone)
    bk_cat()   { rclone cat "$ROOT/$1" 2>/dev/null; }
    bk_files() { rclone lsf -R --files-only "$ROOT/$1" 2>/dev/null || true; }
    bk_dirs()  { rclone lsf "$ROOT/$1/" --dirs-only 2>/dev/null | sed 's#/$##' || true; }
    ;;
  local)
    bk_cat()   { cat "$ROOT/$1" 2>/dev/null; }
    bk_files() { (cd "$ROOT/$1" 2>/dev/null && find . -type f | sed 's#^\./##' | sort) || true; }
    bk_dirs()  { (cd "$ROOT/$1" 2>/dev/null && find . -mindepth 1 -maxdepth 1 -type d | sed 's#^\./##' | sort) || true; }
    ;;
  *) echo "::error::unknown backend '$BACKEND' (rclone|local)" >&2; exit 2 ;;
esac

refuse() { echo "::error::$1" >&2; exit 1; }

# 1) The pointer must fetch AND parse cleanly, else there's no referenced set to compute — refuse
#    on a corrupt one. A genuinely-ABSENT pointer is not an error: the store is still
#    pre-immutable, nothing to collect yet — empty outputs, exit 0.
if ! bk_cat store/manifest.json > "$GC_OUT/gc-pointer.json" || [ ! -s "$GC_OUT/gc-pointer.json" ]; then
  echo "no store pointer (or unreadable) — nothing to GC yet (pre-immutable store)"
  : > "$GC_OUT/gc-delete.txt"
  : > "$GC_OUT/gc-purge-dirs.txt"
  exit 0
fi
current=$(jq -re '.manifest' "$GC_OUT/gc-pointer.json" 2>/dev/null) \
  || refuse "store pointer is not valid JSON (or has no .manifest) — refusing to GC"
echo "pointer → $current"

# 2) The newest N manifests by ULID sort (chronological). The pointer's manifest is the newest (GC
#    never runs during a build — shared concurrency group) and MUST be among them — a stale or
#    corrupt listing otherwise refuses rather than under-referencing.
bk_files store/manifests | grep '\.json$' | sort > "$GC_OUT/gc-all-manifests.txt" || true
[ -s "$GC_OUT/gc-all-manifests.txt" ] || refuse "no store manifests listed — refusing to GC"
tail -n "$KEEP" "$GC_OUT/gc-all-manifests.txt" > "$GC_OUT/gc-keep-manifests.txt"
grep -qxF "${current#manifests/}" "$GC_OUT/gc-keep-manifests.txt" \
  || refuse "pointer manifest $current not among the newest $KEEP — stale/corrupt listing, refusing to GC"

# 3) Referenced set = union of those manifests' names. Each must fetch and be valid JSON with
#    .entries — one unreadable manifest refuses the whole run (an incomplete referenced set would
#    mark live artifacts as garbage).
: > "$GC_OUT/gc-referenced.txt"
while IFS= read -r m; do
  [ -n "$m" ] || continue
  bk_cat "store/manifests/$m" > "$GC_OUT/gc-m.json" || refuse "manifest $m unreadable — refusing to GC"
  jq -e '.entries' "$GC_OUT/gc-m.json" >/dev/null 2>&1 \
    || refuse "manifest $m is not valid JSON with .entries — refusing to GC"
  jq -r '.entries[].name' "$GC_OUT/gc-m.json" >> "$GC_OUT/gc-referenced.txt"
done < "$GC_OUT/gc-keep-manifests.txt"
sort -u -o "$GC_OUT/gc-referenced.txt" "$GC_OUT/gc-referenced.txt"
ref=$(wc -l < "$GC_OUT/gc-referenced.txt")
[ "$ref" -gt 0 ] || refuse "referenced set is empty — refusing to GC"
echo "referenced by the last $KEEP manifests: $ref artifacts"

# 4) Every store object in the content-addressed prefixes, path relative to <root> (matching the
#    manifest 'name' form).
: > "$GC_OUT/gc-all.txt"
for p in pmtiles contour soundings depare; do
  bk_files "$p" | sed "s#^#$p/#" >> "$GC_OUT/gc-all.txt"
done
sort -u -o "$GC_OUT/gc-all.txt" "$GC_OUT/gc-all.txt"
total=$(wc -l < "$GC_OUT/gc-all.txt")
echo "store objects in content prefixes: $total"

# Sanity: the referenced set must intersect what's actually in the store, or a path/listing
# mismatch would mark the whole store unreferenced and delete everything.
kept=$(comm -12 "$GC_OUT/gc-all.txt" "$GC_OUT/gc-referenced.txt" | wc -l)
[ "$kept" -gt 0 ] || refuse "0 referenced artifacts are present in the store — path mismatch, refusing to GC"

# 5) Unreferenced content objects (pre-phase-4 mutable names + .key sidecars fall out here too —
#    they sit in these prefixes and no manifest names them).
comm -23 "$GC_OUT/gc-all.txt" "$GC_OUT/gc-referenced.txt" > "$GC_OUT/gc-delete.txt"

# 6) Named legacy debris beyond the content prefixes: volatile sources' retired
#    source/<id>/.recipe-hash markers (NOT landmask/.recipe-hash — that one is still live).
bk_files source | grep '/\.recipe-hash$' | sed 's#^#source/#' >> "$GC_OUT/gc-delete.txt" || true
sort -u -o "$GC_OUT/gc-delete.txt" "$GC_OUT/gc-delete.txt"
del=$(wc -l < "$GC_OUT/gc-delete.txt")

# 7) Retired diff-era coverings — whole aggregation/<ulid>/ dirs (nothing reads a covering from
#    the store under phase 4; hydrate is manifest-driven).
bk_dirs aggregation > "$GC_OUT/gc-purge-dirs.txt"
dirs=$(grep -c . "$GC_OUT/gc-purge-dirs.txt" || true)

# ── Full inventory (per content prefix) BEFORE anything deletes ──
echo "── inventory ──"
for p in pmtiles contour soundings depare; do
  pt=$(grep -c "^$p/" "$GC_OUT/gc-all.txt" || true)
  pd=$(grep -c "^$p/" "$GC_OUT/gc-delete.txt" || true)
  echo "  $p: $pt objects, $((pt - pd)) kept, $pd to delete"
done
rh=$(grep -c '^source/.*/\.recipe-hash$' "$GC_OUT/gc-delete.txt" || true)
echo "  source/*/.recipe-hash: $rh to delete"
echo "  aggregation/ coverings: $dirs dir(s) to purge"
echo "totals: $del objects + $dirs covering dir(s) to delete; $kept of $ref referenced objects present"
echo "── first 20 objects flagged for deletion ──"
head -20 "$GC_OUT/gc-delete.txt" || true
