# Per-source R2 publish rules. Ordering is the atomicity mechanism: artifacts, then
# bounds.csv, then catalog.json LAST (the currency marker lands after what it vouches for).

DATA_PREFIX = config.get("data_prefix", "bathymetry")
DEST = f"r2:$DATA_BUCKET/{DATA_PREFIX}"  # $DATA_BUCKET stays a shell env var (no braces)

# Fail fast without rclone/R2 env; {{ }} is Snakemake format-escaping, bash sees { }.
PUBLISH_GUARD = (
    'command -v rclone >/dev/null || {{ echo "rclone not found — publish runs on the box only" >&2; exit 1; }}; '
    ': "${{DATA_BUCKET:?DATA_BUCKET unset — publish runs on the box only}}"; '
    ': "${{RCLONE_CONFIG_R2_ACCESS_KEY_ID:?RCLONE_CONFIG_R2_* env unset — publish runs on the box only}}"; '
)


def publish_inputs(wc):
    """bounds + catalog always; polygon (prepped) or the objects stamp (volatile)."""
    ins = {"bounds": f"store/source/{wc.source}/bounds.csv",
           "catalog": f"store/source/{wc.source}/catalog.json"}
    if wc.source in MIRRORED:
        ins["objects"] = f"store/meta/publish/{wc.source}.objects"
    else:
        ins["polygon"] = f"store/polygon/{wc.source}.gpkg"
    return ins


# Top up objects/ from upstream, then push to R2 — steady-state moves only the week's
# churn; neither leg ever deletes.
rule mirror_objects:
    input:
        mirror="store/source/{source}/mirror.txt",
        bucket="store/source/{source}/mirror-bucket.txt",
    output:
        touch("store/meta/publish/{source}.objects")
    wildcard_constraints:
        source=pat(MIRRORED)
    priority: 5000  # ~190 GB when it runs; start first
    shell:
        PUBLISH_GUARD +
        'printf "[upstream]\\ntype = s3\\nprovider = AWS\\nregion = us-east-1\\n" > /tmp/upstream-{wildcards.source}.conf; '
        'bucket=$(cat {input.bucket}); '
        'rclone --config /tmp/upstream-{wildcards.source}.conf copy "upstream:$bucket" '
        '"store/source/{wildcards.source}/objects" --files-from {input.mirror} '
        '--transfers 16 --checkers 32 --retries 5 --stats 60s --stats-one-line; '
        'rclone copy "store/source/{wildcards.source}/objects" '
        '"{DEST}/source/{wildcards.source}/objects" --transfers 16 --checkers 32 --retries 5 '
        '--stats 60s --stats-one-line'


# Push one source, catalog.json last. Prepped: sync + footprint. Volatile: copy, never
# sync (objects/ under the prefix must never be swept).
rule publish_source:
    input:
        unpack(publish_inputs)
    output:
        touch("store/meta/publish/{source}")
    params:
        volatile=lambda wc: "true" if wc.source in MIRRORED else "false",
    wildcard_constraints:
        source=pat(PREPPED + MIRRORED)
    shell:
        PUBLISH_GUARD +
        'src="store/source/{wildcards.source}"; dest="{DEST}/source/{wildcards.source}"; '
        'if [ "{params.volatile}" = "true" ]; then '
        '  rclone copy "$src" "$dest" --exclude "bounds.csv" --exclude "catalog.json" --exclude "objects/**" --exclude "raw/**" --retries 5; '
        'else '
        '  rclone sync "$src" "$dest" --exclude "bounds.csv" --exclude "catalog.json" --exclude "raw/**" --retries 5; '
        '  if [ -f "store/polygon/{wildcards.source}.gpkg" ]; then '
        '    rclone copyto "store/polygon/{wildcards.source}.gpkg" "{DEST}/polygon/{wildcards.source}.gpkg" --retries 5; '
        '  fi; '
        'fi; '
        'rclone copyto "$src/bounds.csv" "$dest/bounds.csv" --retries 5; '
        'rclone copyto "$src/catalog.json" "$dest/catalog.json" --retries 5'


rule publish_coverage:
    input:
        "store/bundle/coverage.pmtiles"
    output:
        touch("store/meta/publish/coverage")
    shell:
        PUBLISH_GUARD +
        'rclone copyto "store/bundle/coverage.pmtiles" '
        '"{DEST}/coverage/coverage.pmtiles" --retries 5 --stats 30s --stats-one-line'


# One stamp for both masks: one module builds both, so they can't publish different snapshots.
rule publish_masks:
    input:
        land="store/landmask/land.fgb",
        water="store/landmask/water.fgb",
    output:
        touch("store/meta/publish/landmask")
    shell:
        PUBLISH_GUARD +
        'rclone copyto "{input.land}" "{DEST}/landmask/land.fgb" --retries 5; '
        'rclone copyto "{input.water}" "{DEST}/landmask/water.fgb" --retries 5'


# A single-source dispatch publishes only that source; full runs add coverage + masks.
rule publish:
    input:
        expand("store/meta/publish/{source}", source=TARGETS),
        [] if ONLY else ["store/meta/publish/coverage", "store/meta/publish/landmask"],
