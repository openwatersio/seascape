#!/usr/bin/env bash
# Seed the local R2 simulation from pipelines/store/bundle so the dev server
# (`just dev`) serves the freshly-built bundles. Run after `snakemake preview` /
# `snakemake planet`. The Vite plugin reads the same state dir (vite.config.js
# points persistState here), and dev serving is uncached, so a reseed is live on
# plain refresh.
set -euo pipefail
cd "$(dirname "$0")"
# Ensure wrangler is installed (fresh checkout, or the container's empty node_modules
# volume) — otherwise npx would fetch an arbitrary latest wrangler from the registry.
[ -x ../node_modules/.bin/wrangler ] || (cd .. && npm ci)
B=../pipelines/store/bundle
shopt -s nullglob
for f in "$B"/*.pmtiles "$B"/manifest.json; do
  echo "seeding $(basename "$f")..."
  npx wrangler r2 object put "tiles/$(basename "$f")" --file "$f" --local >/dev/null
done
echo "seeded $(ls "$B"/*.pmtiles "$B"/manifest.json 2>/dev/null | wc -l | tr -d ' ') objects into local R2"
