# Health Check

A weekly runbook for spotting production regressions. Each section is self-contained: read it, run the queries, judge the numbers against the "healthy" note. Nothing here mutates anything — all reads.

Credentials: the read-only Cloudflare API token and account ID live in the repo's gitignored `.envrc` (the "Read-only" comment block). Never commit them; never paste them into a doc or a PR.

```sh
CF_TOKEN=…   # cfat_… from .envrc
ACCT=7822da9c68cfce969e63d07534969359
```

## Worker

The `seascape-tiles` Worker serves the raster/vector tiles. Its overzoom path decodes a parent WebP, resamples, and re-encodes — the memory-heavy step that has failed under load before ([#67](https://github.com/openwatersio/seascape/pull/67)). Observability logging is on (`observability.logs.enabled`), so `console.log` lines and uncaught exceptions are queryable for ~the last few days.

### 1. Which version is live

An error's `scriptVersion.id` only means something once you know what's deployed. Confirm the live version (and the release prefix it serves) before trusting any log:

```sh
curl -s "https://api.cloudflare.com/client/v4/accounts/$ACCT/workers/scripts/seascape-tiles/deployments" \
  -H "Authorization: Bearer $CF_TOKEN" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); dep=d['result']['deployments'][0]; print(dep['created_on'], [v['version_id'] for v in dep['versions']])"

# Release prefix (which R2 snapshot it reads):
curl -s "https://api.cloudflare.com/client/v4/accounts/$ACCT/workers/services/seascape-tiles/environments/production/settings" \
  -H "Authorization: Bearer $CF_TOKEN" \
  | python3 -c "import sys,json; b=json.load(sys.stdin)['bindings']; print({x['name']:x.get('text') for x in b if x['type']=='plain_text'})"
```

### 2. Query the logs

Workers observability (the same store the dashboard's "Logs" tab reads) is queried via the telemetry endpoint. The body needs a `parameters` wrapper; `filters[].key` uses the dotted event field (`$metadata.message`, `$workers.outcome`, …). This pulls the last 24h of decode errors:

```sh
NOW=$(python3 -c "import time; print(int(time.time()*1000))")
FROM=$(python3 -c "import time; print(int((time.time()-24*3600)*1000))")

curl -s "https://api.cloudflare.com/client/v4/accounts/$ACCT/workers/observability/telemetry/query" \
  -H "Authorization: Bearer $CF_TOKEN" -H "Content-Type: application/json" \
  -d "{\"queryId\":\"q1\",\"view\":\"events\",\"limit\":500,\"dry\":false,
       \"timeframe\":{\"from\":$FROM,\"to\":$NOW},
       \"parameters\":{\"datasets\":[\"cloudflare-workers\"],
         \"filters\":[{\"id\":\"f1\",\"key\":\"\$metadata.message\",\"type\":\"string\",
                       \"operation\":\"includes\",\"value\":\"Decoding error\"}]}}" \
  > /tmp/events.json
```

For a broad sweep (not just decode errors), swap the filter to catch every failed request — `{"key":"$workers.outcome","operation":"eq","value":"exception"}` — or drop `filters` entirely to see all events. `operation` is one of `includes` / `eq` / `neq` / `exists`. `limit` caps at a few hundred per call; widen `timeframe` or narrow the filter rather than paging.

### 3. Aggregate

Raw events lie about scale — one bad tile requested a thousand times looks like a thousand problems. Collapse to distinct causes. For the decode errors, the useful key is the *parent* tile actually being decoded (the message reports the overzoom target, not the parent):

```python
import json, re, collections
evs = json.load(open('/tmp/events.json'))['result']['events']['events']
out, parents = collections.Counter(), collections.Counter()
for e in evs:
    out[e['$workers']['outcome']] += 1                     # ok / exception / canceled
    m = re.search(r'overlay (\S+) overzoom z(\d+) failed at (\d+)/(\d+)/(\d+)', e['source']['message'])
    if m:
        f, sz, z, x, y = m[1], int(m[2]), int(m[3]), int(m[4]), int(m[5])
        lv = z - sz
        parents[(f, sz, x >> lv, y >> lv)] += 1             # the tile that failed to decode
print('outcomes:', dict(out))
print('distinct failing parent tiles:', len(parents))
for k, v in parents.most_common(15): print(f'  {k[0]} z{k[1]}/{k[2]}/{k[3]}  ×{v}')
```

Also bucket `e['timestamp']` by minute — a tight burst points at load/memory pressure; an even spread points at a persistently broken tile or a code bug.

### 4. Is a suspect tile actually corrupt?

A "Decoding error" is almost never bad bytes in R2 — confirm before chasing the pipeline. Fetch the failing parent straight from the Worker and decode it locally; if `dwebp` (libwebp CLI) is happy, the data is fine and the fault is runtime memory pressure, not the tile:

```sh
curl -s "https://tiles.openwaters.io/seascape/9/282/149.webp" -o /tmp/t.webp
dwebp /tmp/t.webp -o /dev/null   # "Decoded … 512 x 512" = valid; the R2 tile is not the problem
```

### Healthy

Near-zero `outcome: exception` over a week. The overzoom path is the usual offender; a burst of `Decoding error` with `exception` outcomes clustered in minutes is the [#67](https://github.com/openwatersio/seascape/pull/67) failure mode (isolate OOM under concurrent overzoom) — if it recurs, the `limiter(4)` cap in `worker/src/index.ts` is the knob to raise. A steady trickle on one fixed tile instead means that tile genuinely won't decode; fetch it (step 4) and, if `dwebp` also fails, rebuild it in the pipeline.
