#!/usr/bin/env bash
# Seed the local wrangler R2 simulation from pipelines/store/bundle so `wrangler
# dev` serves the freshly-built bundles. Run after `just preview` / `just planet`.
set -euo pipefail
cd "$(dirname "$0")"
B=../pipelines/store/bundle
shopt -s nullglob
for f in "$B"/*.pmtiles "$B"/manifest.json; do
  echo "seeding $(basename "$f")..."
  npx wrangler r2 object put "tiles/$(basename "$f")" --file "$f" --local >/dev/null
done
echo "seeded $(ls "$B"/*.pmtiles "$B"/manifest.json 2>/dev/null | wc -l | tr -d ' ') objects into local R2"
