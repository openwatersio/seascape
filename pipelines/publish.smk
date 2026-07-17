# Per-source publish choreography, ported from .github/workflows/sources.yml (the "Mirror
# upstream objects", "Push source to R2", and "Publish bounds.csv + catalog marker" steps).
# Publishing is a rule: it pushes a source's artifacts, then bounds.csv, then catalog.json
# LAST — that ordering is the atomic-pointer mechanism (bounds.csv drives the covering; the
# catalog's recipe hash is the staleness marker, so whatever vouches "current" lands after
# everything it vouches for). These rules only run on the box that owns rclone + the R2
# credentials; a dry run is pure — the destination and ordering are decided here at parse,
# the network happens in the shell.
#
# Destination: r2:$DATA_BUCKET/<data_prefix>/…, defaulting to the production prefix — the
# lanes share one store and one mirror (the source contract is identical); data_prefix
# remains a config knob only for ad-hoc experiments against a scratch prefix.

DATA_PREFIX = config.get("data_prefix", "bathymetry")
DEST = f"r2:$DATA_BUCKET/{DATA_PREFIX}"  # $DATA_BUCKET stays a shell env var (no braces)

# Fail fast in every publish shell when the box isn't set up for R2 — rclone missing, or the
# ambient RCLONE_CONFIG_R2_* env / DATA_BUCKET unset. Never trips a dry run (shells don't run).
PUBLISH_GUARD = (
    'command -v rclone >/dev/null || {{ echo "rclone not found — publish runs on the box only" >&2; exit 1; }}; '
    ': "${{DATA_BUCKET:?DATA_BUCKET unset — publish runs on the box only}}"; '
    ': "${{RCLONE_CONFIG_R2_ACCESS_KEY_ID:?RCLONE_CONFIG_R2_* env unset — publish runs on the box only}}"; '
)


def publish_inputs(wc):
    """publish_source's inputs: bounds.csv + catalog.json always, plus the polygon for a
    prepped source (its footprint publishes alongside) or the objects-mirror stamp for a
    volatile source (every object a bounds.csv row references must be verified-present in
    the mirror before the pointer lands)."""
    ins = {"bounds": f"store/source/{wc.source}/bounds.csv",
           "catalog": f"store/source/{wc.source}/catalog.json"}
    if wc.source in MIRRORED:
        ins["objects"] = f"store/meta/publish/{wc.source}.objects"
    else:
        ins["polygon"] = f"store/polygon/{wc.source}.gpkg"
    return ins


# Volatile only: top up the store's objects/ from upstream (newly-listed keys; existing
# ones skip by size+mtime), then push the same objects to R2. With the single shared store
# both copies already exist from the legacy lane, so steady-state this moves only the
# week's churn. Neither leg ever deletes.
rule mirror_objects:
    input:
        mirror="store/source/{source}/mirror.txt",
        bucket="store/source/{source}/mirror-bucket.txt",
    output:
        touch("store/meta/publish/{source}.objects")
    wildcard_constraints:
        source=pat(MIRRORED)
    priority: 5000  # CUDEM's object mirror moves ~190 GB — the longest data leg when it runs
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


# Push one source to R2, catalog.json last. Prepped: sync (excluded files are invisible to
# --delete, so bounds/catalog are never swept) + the footprint. Volatile: copy (never sync —
# objects/ under the same prefix must never be deleted), objects already mirrored above.
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


# The source-coverage tileset (store/bundle/coverage.pmtiles) is a stage-1 product too —
# publish it to <prefix>/coverage/coverage.pmtiles, same env gating + prefix guard.
rule publish_coverage:
    input:
        "store/bundle/coverage.pmtiles"
    output:
        touch("store/meta/publish/coverage")
    shell:
        PUBLISH_GUARD +
        'rclone copyto "store/bundle/coverage.pmtiles" '
        '"{DEST}/coverage/coverage.pmtiles" --retries 5 --stats 30s --stats-one-line'


# Both masks → <prefix>/landmask/ — same guard/env pattern. One stamp for both: one
# module (landmask.py) builds both, so they can never publish different snapshots.
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


# The publish aggregate target — every converted source's publish stamp, plus the coverage
# tileset and the masks on a full run (a single-source dispatch, --config source=<id>,
# publishes only that source: coverage is planet-wide, so building it would pull every
# other source's footprint, and the masks are source-independent).
rule publish:
    input:
        expand("store/meta/publish/{source}", source=TARGETS),
        [] if ONLY else ["store/meta/publish/coverage", "store/meta/publish/landmask"],
