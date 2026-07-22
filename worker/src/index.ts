/**
 * Unified bathymetry tile endpoint.
 *
 *   GET /seascape/{z}/{x}/{y}.webp  (or .png)  → Terrarium WebP (raster terrain)
 *   GET /seascape/{z}/{x}/{y}.pbf   (or .mvt)  → MVT (vector — contours, soundings, depare)
 *   GET /seascape/coverage/{z}/{x}/{y}.pbf     → MVT (source-provenance footprints)
 *
 * Extension picks the representation: webp/png → raster, pbf/mvt → vector.
 *
 * Reads the bundles published to R2 (planet.pmtiles + one overlay-{cell}.pmtiles
 * per populated grid cell + vector.pmtiles + coverage.pmtiles + manifest.json)
 * and resolves per tile:
 *   - z ≤ planet.max_zoom        → planet tile
 *   - z > planet.max_zoom, the tile's grid cell is populated → that cell's tile
 *     (or the overzoomed deepest ancestor present in the cell)
 *   - otherwise                  → OVERZOOM the planet's deepest ancestor tile
 * The owning cell is computed from the tile address (overlay.ts) — no bbox search.
 *
 * Overzoom of Terrarium is a cubic B-spline ON DECODED HEIGHTS, not on the packed bytes:
 * decode each source pixel to a float elevation, resample the elevations, re-encode.
 * Averaging the raw RGB would corrupt the decode (G wraps at 256). Nearest leaves flat
 * plateaus + cliffs; bilinear is only C0 so iso-depth band edges still step; cubic
 * convolution / Catmull-Rom is C1 but its negative lobes ring past a sharp shelf edge into
 * a halo. The B-spline (GDAL's `cubicspline`) has a non-negative C2 basis: no ring, no
 * step, smoothest surface — it smooths (blurs) rather than interpolating, the accepted
 * trade. Contours need no overzoom — tippecanoe bakes base lines to the deepest zoom.
 */

import { PMTiles, Source, RangeResponse } from "pmtiles";
import { style as seascapeStyle } from "@openwaters/seascape";
import { CachedSource, contentEtag, OVERZOOM_TAG_VERSION } from "./cache";
import { coverageTileJSON } from "./coverage";
import { unpackTerrarium, packTerrariumInto, cubicBSpline } from "./terrarium";
import { OverlayIndex, overlayFor, previewRoute } from "./overlay";
import { limiter } from "./semaphore";
// jSquash on Workers: WASM must be imported as a module and passed to init()
// (no fetch-based instantiation in the Workers runtime).
import decodeWebp, { init as initWebpDecode } from "@jsquash/webp/decode";
import encodeWebp, { init as initWebpEncode } from "@jsquash/webp/encode";
import DEC_WASM from "@jsquash/webp/codec/dec/webp_dec.wasm";
import ENC_WASM from "@jsquash/webp/codec/enc/webp_enc.wasm";

let codecReady: Promise<void> | undefined;
function ensureCodec(): Promise<void> {
  if (!codecReady) {
    codecReady = Promise.all([
      initWebpDecode(DEC_WASM),
      initWebpEncode(ENC_WASM),
    ]).then(() => {});
  }
  return codecReady;
}

export interface Env {
  TILES: R2Bucket;
  RELEASE_PREFIX?: string; // R2 key prefix selecting which release to serve, e.g. "seascape/<sha>/"; default ""
  BASE_PATH?: string; // URL mount path = the Cloudflare route prefix; default "/seascape"
  // Preview Worker: when set, TILES is the public data bucket and the leading path
  // segment is a build's sha — serve that build straight from bathymetry/build/<sha>/.
  // Runs uncached (like local dev) so a re-pushed build shows up immediately and the
  // release-scoped colo cache (keyed on tiles.openwaters.io) is never touched.
  PREVIEW?: string;
}

interface BundleMeta {
  file: string;
  min_zoom: number;
  max_zoom: number;
  bbox: [number, number, number, number]; // w, s, e, n
}
interface Manifest {
  planet: BundleMeta;
  overlay: OverlayIndex; // {split_z, cells: {"z-x-y": max_zoom}}
  source_ids?: string[]; // every configured source (the viewer's provenance palette)
  attribution?: string; // combined HTML credit for every contributing dataset
}

const TILE = 512;

class R2Source implements Source {
  constructor(
    private bucket: R2Bucket,
    private key: string,
  ) {}
  getKey() {
    return this.key;
  }
  async getBytes(offset: number, length: number): Promise<RangeResponse> {
    const obj = await this.bucket.get(this.key, { range: { offset, length } });
    if (!obj) throw new Error(`R2 miss: ${this.key}`);
    return { data: await obj.arrayBuffer() };
  }
}

// One PMTiles instance per file, reused across requests within an isolate.
const pmCache = new Map<string, PMTiles>();
function pm(env: Env, file: string): PMTiles {
  const key = (env.RELEASE_PREFIX ?? "") + file;
  // Dev (no RELEASE_PREFIX) and preview (a re-pushed build): don't reuse the isolate
  // cache. A local reseed / rebuild replaces the pmtiles under the same key, and a cached
  // instance's stale header/directory would then read the new bytes at old offsets
  // (garbage / 500s) until wrangler restarts. A fresh instance re-reads them. Prod keys are
  // release-immutable, so the cache is safe there.
  if (!env.RELEASE_PREFIX || env.PREVIEW)
    return new PMTiles(new R2Source(env.TILES, key));
  let p = pmCache.get(key);
  if (!p) {
    // Range reads go through the colo cache (CachedSource) so directory walks
    // are shared across isolates. The release prefix in the key makes entries
    // self-invalidating across releases (superseded ones LRU out).
    p = new PMTiles(
      new CachedSource(
        new R2Source(env.TILES, key),
        `https://tiles.openwaters.io/__pmtiles/${key}`,
      ),
    );
    pmCache.set(key, p);
  }
  return p;
}

let manifestCache: Manifest | undefined;
async function manifest(env: Env): Promise<Manifest> {
  // Dev (no RELEASE_PREFIX) and preview: re-read every call — a local reseed / rebuild
  // replaces manifest.json under the running Worker, and the isolate cache would otherwise
  // pin the old one (e.g. an old contour max_zoom) until restart. Also, preview serves many
  // builds from one isolate, so a shared singleton would cross builds. Prod caches it
  // (immutable within a release).
  const cacheable = !!env.RELEASE_PREFIX && !env.PREVIEW;
  if (manifestCache && cacheable) return manifestCache;
  const obj = await env.TILES.get((env.RELEASE_PREFIX ?? "") + "manifest.json");
  if (!obj) throw new Error("manifest.json missing");
  const m: Manifest = JSON.parse(await obj.text());
  // Tolerate a pre-grid manifest (old release / local seed): planet-only, no 500s.
  m.overlay ??= { split_z: 0, cells: {} };
  if (cacheable) manifestCache = m;
  return m;
}

async function tile(
  env: Env,
  file: string,
  z: number,
  x: number,
  y: number,
): Promise<ArrayBuffer | undefined> {
  const r = await pm(env, file).getZxy(z, x, y);
  return r?.data;
}

// ── Terrarium overzoom: cubic B-spline on decoded heights ───────────────────
// Decode the ancestor tile to elevations, resample into the output sub-tile with a cubic
// B-spline (GDAL's `cubicspline`), re-encode. Its basis is non-negative and C2, so it can't
// overshoot a sharp shelf edge into a halo and leaves no stairstep — the trade is that it
// smooths rather than interpolating. Sampling clamps to the ancestor's edge: seams at
// ancestor-tile boundaries flatten slightly — invisible.
// The synthesis half of overzoom, split from the ancestor fetch so the
// handler can derive the tile's validator from the ancestor bytes (the output
// is a pure function of them) and skip this work entirely on a matching
// revalidation.
// Decode + B-spline + re-encode each hold several MB of libwebp working memory.
// A request burst over a detailed region ran enough concurrently to exhaust the
// isolate — malloc returned null and jsquash threw "Decoding error" on tiles that
// are perfectly valid (they decode fine one at a time). Cap the concurrency so the
// slow overzoom path queues instead of OOMing.
// ponytail: fixed cap; raise it if bursts still starve throughput.
const overzoomGate = limiter(4);

async function synthesize(
  parent: ArrayBuffer,
  srcMax: number,
  z: number,
  x: number,
  y: number,
): Promise<ArrayBuffer> {
  const levels = z - srcMax;
  const span = 1 << levels; // sub-tiles per axis within the ancestor
  const px = x >> levels,
    py = y >> levels; // ancestor tile at srcMax
  const subX = x - (px << levels),
    subY = y - (py << levels);

  await ensureCodec();
  const img = await decodeWebp(parent); // {data: Uint8ClampedArray RGBA, width, height}
  const src = img.data;
  const W = img.width,
    H = img.height;
  // Decode the whole ancestor to heights once — each output pixel reads 16 taps, so
  // re-unpacking per tap would decode every texel ~16×.
  const ha = new Float64Array(W * H);
  for (let p = 0, q = 0; p < ha.length; p++, q += 4)
    ha[p] = unpackTerrarium(src[q], src[q + 1], src[q + 2]);

  const out = new Uint8ClampedArray(TILE * TILE * 4);
  const srcSize = TILE / span; // pixels of the ancestor this sub-tile spans
  const ox = subX * srcSize,
    oy = subY * srcSize;
  const cl = (v: number, hi: number) => (v < 0 ? 0 : v > hi ? hi : v);

  for (let j = 0; j < TILE; j++) {
    // output-pixel centre → fractional ancestor row, clamped inside the tile
    const sy = cl(oy + (j + 0.5) / span - 0.5, H - 1);
    const iy = sy | 0,
      ty = sy - iy;
    const r0 = cl(iy - 1, H - 1) * W,
      r1 = cl(iy, H - 1) * W,
      r2 = cl(iy + 1, H - 1) * W,
      r3 = cl(iy + 2, H - 1) * W;
    for (let i = 0; i < TILE; i++) {
      const sx = cl(ox + (i + 0.5) / span - 0.5, W - 1);
      const ix = sx | 0,
        tx = sx - ix;
      const c0 = cl(ix - 1, W - 1),
        c1 = cl(ix, W - 1),
        c2 = cl(ix + 1, W - 1),
        c3 = cl(ix + 2, W - 1);
      // B-spline across each of the 4 rows, then down the 4 results. The kernel is non-
      // negative, so its tensor product is non-negative in 2D too: this separable pass can't
      // overshoot in either axis → no shelf-edge halo, and C2 everywhere → no stairstep.
      const h = cubicBSpline(
        cubicBSpline(ha[r0 + c0], ha[r0 + c1], ha[r0 + c2], ha[r0 + c3], tx),
        cubicBSpline(ha[r1 + c0], ha[r1 + c1], ha[r1 + c2], ha[r1 + c3], tx),
        cubicBSpline(ha[r2 + c0], ha[r2 + c1], ha[r2 + c2], ha[r2 + c3], tx),
        cubicBSpline(ha[r3 + c0], ha[r3 + c1], ha[r3 + c2], ha[r3 + c3], tx),
        ty,
      );
      packTerrariumInto(out, (j * TILE + i) * 4, h);
    }
  }
  return encodeWebp({ data: out, width: TILE, height: TILE } as ImageData, {
    lossless: 1,
  });
}

let landEtagCache: string | undefined;
async function landFallbackEtag(): Promise<string> {
  if (!landEtagCache) landEtagCache = await contentEtag(await landFallbackBytes());
  return landEtagCache;
}

// Missing-tile fill: the land code (published raster codes: 0 unknown-depth water,
// 1 drying, 2 land — pipelines/terrain.py). 0 would read as unknown-depth WATER
// under the published contract.
const LAND_SENTINEL = 2;

let landFallbackCache: ArrayBuffer | undefined;
async function _makeLandFallbackTile(): Promise<ArrayBuffer> {
  // Terrarium +2 m — the land code, which the depth ramp paints as the land
  // wash. A missing tile degrades to land rendering, never to phantom water
  // (Terrarium has no transparency to degrade to).
  await ensureCodec();
  const out = new Uint8ClampedArray(TILE * TILE * 4);
  const packed = LAND_SENTINEL + 32768; // 32770 → R=128, G=2, B=0
  for (let k = 0; k < TILE * TILE; k++) {
    out[k * 4] = packed >> 8;
    out[k * 4 + 1] = packed & 0xff;
    out[k * 4 + 3] = 255;
  }
  return encodeWebp({ data: out, width: TILE, height: TILE } as ImageData, {
    lossless: 1,
  });
}
async function landFallbackBytes(): Promise<ArrayBuffer> {
  if (!landFallbackCache) landFallbackCache = await _makeLandFallbackTile();
  return landFallbackCache;
}

const CORS = { "access-control-allow-origin": "*" };
// Stable tile URLs: a deploy never orphans cached tiles, and stale-while-revalidate/-if-error keep
// serving them (refreshing in the background) so a nav app shows stale bathymetry over a blank tile.
// s-maxage governs the colo cache: entries live long because cache keys are
// release-scoped — a deploy switches namespaces, so freshness comes from the
// key, not the TTL. Browsers ignore s-maxage and keep the 1 day max-age.
// max-age is 1 day, not 1 h: every browser-cache expiry fires a conditional
// request that still bills as a Worker invocation (cache hits are billed), so a
// longer window cuts revalidation traffic ~24×. The trade is a released
// bathymetry correction can take up to a day to reach a client that already
// cached the tile; stale-if-error still serves the old tile if a fetch fails.
const TILE_CACHE =
  "public, max-age=86400, s-maxage=2592000, stale-while-revalidate=31536000, stale-if-error=31536000";
const WEBP = {
  "content-type": "image/webp",
  "cache-control": TILE_CACHE,
  ...CORS,
};
const MVT = {
  "content-type": "application/x-protobuf",
  "cache-control": TILE_CACHE,
  ...CORS,
};

export default {
  async fetch(
    req: Request,
    env: Env,
    ctx: ExecutionContext,
  ): Promise<Response> {
    // An uncaught throw becomes the runtime's bare 500 with no CORS headers,
    // which browsers report as a CORS block (masking the real status) and
    // MapLibre then won't retry cleanly. Catch everything and answer with CORS.
    return this.handle(req, env, ctx).catch((e: unknown) => {
      console.log(`unhandled: ${e}`);
      return new Response("internal error", {
        status: 500,
        headers: { "cache-control": "no-store", ...CORS },
      });
    });
  },
  async handle(
    req: Request,
    env: Env,
    ctx: ExecutionContext,
  ): Promise<Response> {
    const noTile = () => new Response(null, { status: 204, headers: CORS });
    const url = new URL(req.url);
    const path = url.pathname;
    // Two validator schemes. JSON endpoints (manifest/TileJSON/style) use
    // ETag = release id: they derive from the manifest, so they SHOULD change
    // every release. Tiles use a content hash (see the tile section below):
    // most releases leave most tiles byte-identical, and a content validator
    // lets every unchanged tile revalidate to a bodyless 304 across releases
    // instead of re-downloading.
    // Dev (no RELEASE_PREFIX): serve uncacheable and drop the validator — a local reseed must
    // show up immediately, but the ETag would be a constant "dev" that 304s stale bodies across
    // reseeds (browser cache), and long stale-while-revalidate would keep serving them. Prod
    // keeps the per-release ETag + long cache (every resource is deterministic within a release).
    // Preview is uncached like dev: builds are re-pushed under the same sha and the colo
    // cache key is hardcoded to the tiles.openwaters.io zone, not this Worker's data host.
    const dev = !env.RELEASE_PREFIX || !!env.PREVIEW;
    // Colo cache: repeat tiles are served without touching R2 or the codecs.
    // Only tile responses are put (below) — the JSON endpoints stay
    // release-validated. The key embeds the release, so a deploy atomically
    // switches namespaces (no purge, nothing else on the zone touched, and
    // old isolates racing the rollout write only into the old namespace);
    // superseded entries LRU out. Same-zone host so a manual dashboard
    // purge-everything remains an escape hatch.
    const cacheKey = `https://tiles.openwaters.io/__cache/${env.RELEASE_PREFIX}${path}`;
    if (!dev && req.method === "GET") {
      const hit = await caches.default.match(cacheKey);
      if (hit) {
        const hitTag = hit.headers.get("etag");
        return hitTag && req.headers.get("If-None-Match") === hitTag
          ? new Response(null, {
              status: 304,
              headers: {
                etag: hitTag,
                "cache-control": hit.headers.get("cache-control") ?? TILE_CACHE,
                ...CORS,
              },
            })
          : hit;
      }
    }
    const etag = `"${(env.RELEASE_PREFIX ?? "").split("/").filter(Boolean).pop() ?? "dev"}"`;
    const fresh = !dev && req.headers.get("If-None-Match") === etag;
    const send = (
      body: BodyInit | null,
      headers: Record<string, string>,
    ): Response => {
      if (fresh)
        return new Response(null, {
          status: 304,
          headers: { etag, "cache-control": headers["cache-control"], ...CORS },
        });
      return new Response(body, {
        headers: {
          ...headers,
          ...(dev ? { "cache-control": "no-store" } : { etag }),
          ...CORS,
        },
      });
    };
    const json = (o: unknown) =>
      send(JSON.stringify(o), {
        "content-type": "application/json",
        "cache-control":
          "public, max-age=60, stale-while-revalidate=604800, stale-if-error=31536000",
      });
    // The mount prefix (the Cloudflare route) is present in prod and absent in
    // dev at root — tolerate both: strip it when present, else treat the path as
    // already relative. `mount` is echoed back into TileJSON tile URLs so they
    // stay correct either way.
    const base = (env.BASE_PATH ?? "/seascape").replace(/\/+$/, "");
    const mounted =
      base !== "" && (path === base || path.startsWith(base + "/"));
    let rel = mounted ? path.slice(base.length) : path;
    let mount = mounted ? base : "";
    // Preview Worker: peel the build sha off the path and read that build's bundle
    // straight from the data bucket. `renv` carries the per-request R2 prefix so the
    // shared pm()/manifest()/tile() helpers resolve bathymetry/build/<sha>/*; the base
    // Worker leaves it as `env`. See previewRoute (overlay.ts) for the sha guard.
    let renv = env;
    if (env.PREVIEW) {
      const p = previewRoute(rel, mount);
      if (!p)
        return new Response(`usage: ${base}/<sha>/{z}/{x}/{y}.{webp,pbf}`, {
          status: 404,
          headers: CORS,
        });
      renv = { ...env, RELEASE_PREFIX: p.prefix };
      rel = p.rel;
      mount = p.mount;
    }
    // Absolute endpoint base echoed into TileJSON/style URLs. `wrangler dev`
    // rewrites the request URL *and* Host header to the configured route host
    // (tiles.openwaters.io), leaving no truthful origin in a local request — so
    // LOCAL dev pins localhost at the port the dev script binds (worker/package.json);
    // preview and prod are deployed on a real host, so they trust the request origin.
    const tilesBase =
      !env.RELEASE_PREFIX && !env.PREVIEW
        ? `http://localhost:8787${mount}`
        : `${url.origin}${mount}`;

    if (rel === "/manifest.json") {
      return json(await manifest(renv));
    }
    // Drop-in MapLibre style for these tiles — the same style the viewer
    // renders (assembled by @openwaters/seascape); the endpoint base is derived
    // from the request. Point MapLibre's `style:` (or Maputnik) at this URL
    // directly. ?unit=m|ft|fm, ?safety=<metres>, and ?shading=relief|bands set
    // mariner defaults in the served style; on a live map the package's
    // applyState changes unit/safety in place.
    if (rel === "/style.json") {
      // Uncacheable plain-text 400s: an intermediary must never cache an error
      // for a URL that would succeed once the param is fixed.
      const bad = (msg: string) =>
        new Response(msg, {
          status: 400,
          headers: {
            "content-type": "text/plain; charset=utf-8",
            "cache-control": "no-store",
            ...CORS,
          },
        });
      const unitParam = url.searchParams.get("unit");
      const unit =
        unitParam === null
          ? undefined
          : (["m", "ft", "fm"] as const).find((u) => u === unitParam);
      if (unitParam !== null && unit === undefined)
        return bad("unit must be m, ft, or fm");
      const safetyParam = url.searchParams.get("safety");
      const safety = safetyParam === null ? undefined : Number(safetyParam);
      if (safety !== undefined && !(Number.isFinite(safety) && safety >= 0))
        return bad("safety must be a non-negative number (metres)");
      const shadingParam = url.searchParams.get("shading");
      const shading =
        shadingParam === null
          ? undefined
          : (["relief", "bands"] as const).find((s) => s === shadingParam);
      if (shadingParam !== null && shading === undefined)
        return bad("shading must be relief or bands");
      return json(
        seascapeStyle({
          tilesBase,
          ...(unit !== undefined ? { unit } : {}),
          ...(safety !== undefined ? { safety } : {}),
          ...(shading !== undefined ? { shading } : {}),
        }),
      );
    }
    // TileJSON per representation — point MapLibre/Mapbox at these directly.
    // A TileJSON is single-format, so raster and vector get separate docs.
    if (rel === "/raster.json") {
      const mf = await manifest(renv);
      return json({
        tilejson: "3.0.0",
        name: "Open Waters Bathymetry (raster)",
        tiles: [`${tilesBase}/{z}/{x}/{y}.webp`],
        minzoom: mf.planet.min_zoom,
        // Advertise a few levels past the deepest real data: the Worker
        // cubic-B-spline-overzooms the Terrarium raster PAST native tiles —
        // its whole purpose (smooth iso-depth band edges + shorelines at high
        // zoom). A renderer left to its own overzoom bilinearly stretches the
        // last native tile, and bilinear is only C0, so band edges facet into
        // steps; the Worker's B-spline is C2. Beyond a few levels it only
        // blurs, so the margin stays small.
        maxzoom:
          Math.max(mf.planet.max_zoom, ...Object.values(mf.overlay.cells)) + 3,
        bounds: mf.planet.bbox,
        encoding: "terrarium",
        attribution: mf.attribution ?? "",
      });
    }
    if (rel === "/vector.json") {
      const mf = await manifest(renv);
      const h = await pm(renv, "vector.pmtiles").getHeader();
      return json({
        tilejson: "3.0.0",
        name: "Open Waters Bathymetry",
        tiles: [`${tilesBase}/{z}/{x}/{y}.pbf`],
        minzoom: h.minZoom,
        maxzoom: h.maxZoom,
        bounds: [h.minLon, h.minLat, h.maxLon, h.maxLat],
        vector_layers: [
          {
            id: "contours",
            fields: {
              depth_m: "Number",
              depth_abs_m: "Number",
              sys: "String",
              depth_ft: "Number",
              depth_fm: "Number",
            },
          },
          {
            id: "soundings",
            fields: {
              depth_m: "Number",
              depth_ft: "Number",
              depth_fm: "Number",
            },
          },
          {
            // Depth-area partitions (ENC DEPARE): three feature kinds keyed by
            // attribute presence — depth bands (drval1/drval2 = shallow/deep
            // bound, positive-down m; sys tags the m/ft ladder), drying (negative
            // drval1, no sys), and unknown-depth water (no drval1, `kind` carries
            // the OSM water subtype). `rank` orders them within the fill.
            id: "depare",
            fields: {
              drval1: "Number",
              drval2: "Number",
              sys: "String",
              kind: "String",
              rank: "Number",
            },
          },
        ],
        attribution: mf.attribution ?? "",
      });
    }
    // Source-provenance footprints — their own tileset with a low maxzoom that
    // MapLibre overzooms independently of the vector source (in vector.pmtiles
    // the layer either minted millions of deep-ocean fill tiles or vanished
    // above its zoom — a joined archive shares one zoom range).
    if (rel === "/coverage.json") {
      const mf = await manifest(renv);
      // Absent coverage.pmtiles (a pre-coverage release) → empty TileJSON, not
      // a 500 — same tolerance manifest() extends to pre-grid releases.
      const h = await pm(renv, "coverage.pmtiles")
        .getHeader()
        .catch(() => null);
      return json(coverageTileJSON(h, tilesBase, mf.attribution ?? ""));
    }
    // Tiles validate by CONTENT, not release: ETag = hash of the tile bytes
    // (or of the native ancestor a synthesized tile is a pure function of).
    // An unchanged tile keeps its validator across releases, so clients
    // revalidate to bodyless 304s instead of re-downloading.
    const inm = req.headers.get("If-None-Match");
    const notModified = (tag: string) =>
      new Response(null, {
        status: 304,
        headers: { etag: tag, "cache-control": TILE_CACHE, ...CORS },
      });
    const sendTile = (
      bytes: ArrayBuffer,
      headers: Record<string, string>,
      tag: string,
      cache = true,
    ): Response => {
      const res = new Response(bytes, {
        headers: {
          ...headers,
          etag: tag,
          ...(dev ? { "cache-control": "no-store" } : {}),
          ...CORS,
        },
      });
      if (cache && !dev && req.method === "GET")
        ctx.waitUntil(caches.default.put(cacheKey, res.clone()));
      return inm === tag ? notModified(tag) : res;
    };

    // Coverage tiles, from their own archive. A within-range miss (open ocean),
    // out-of-range x/y, and an absent archive (pre-coverage release) all 204 —
    // the same noTile contract as vector, never a 500.
    const cov = rel.match(/^\/coverage\/(\d+)\/(\d+)\/(\d+)\.(pbf|mvt)$/);
    if (cov) {
      const cz = +cov[1],
        cx = +cov[2],
        cy = +cov[3];
      if (cx >= 2 ** cz || cy >= 2 ** cz) return noTile();
      const t = await tile(renv, "coverage.pmtiles", cz, cx, cy).catch(
        () => undefined,
      );
      return t ? sendTile(t, MVT, await contentEtag(t)) : noTile();
    }

    // Tiles: extension selects representation — webp/png → raster, pbf/mvt → vector.
    const m = rel.match(/^\/(\d+)\/(\d+)\/(\d+)\.(png|webp|pbf|mvt)$/);
    if (!m)
      return new Response(`usage: ${base}/{z}/{x}/{y}.{webp,pbf}`, {
        status: 404,
        headers: CORS,
      });
    const z = +m[1],
      x = +m[2],
      y = +m[3];
    const ext = m[4];
    // Overzoom: resolve the ancestor first — the validator derives from it, so
    // a matching revalidation skips the decode → B-spline → encode entirely.
    // (The 304 path doesn't populate the colo cache; the next full GET does.)
    const overzoomTile = async (
      srcFile: string,
      srcMax: number,
    ): Promise<Response | null> => {
      const levels = z - srcMax;
      const parent = await tile(renv, srcFile, srcMax, x >> levels, y >> levels);
      if (!parent) return null; // ancestor missing; caller tries the next source
      const tag = await contentEtag(parent, OVERZOOM_TAG_VERSION + "-");
      if (inm === tag) return notModified(tag);
      const body = await overzoomGate(() =>
        synthesize(parent, srcMax, z, x, y),
      );
      return sendTile(body, WEBP, tag);
    };

    const isVector = ext === "pbf" || ext === "mvt";

    // Out-of-range x/y (the pmtiles coord check throws on these) → blank tile,
    // not a 500. Uncached: the address space is unbounded junk.
    const n = 2 ** z;
    if (x >= n || y >= n)
      return isVector
        ? noTile()
        : sendTile(await landFallbackBytes(), WEBP, await landFallbackEtag(), false);

    if (isVector) {
      const t = await tile(renv, "vector.pmtiles", z, x, y);
      return t ? sendTile(t, MVT, await contentEtag(t)) : noTile();
    }

    // Terrain always returns a valid 512px tile (sea-level/land on a miss)
    // so raster-dem never sees a 0-dim neighbour during border backfill.
    const mf = await manifest(renv);
    if (z <= mf.planet.max_zoom) {
      const t = await tile(renv, "planet.pmtiles", z, x, y);
      return t
        ? sendTile(t, WEBP, await contentEtag(t))
        : sendTile(await landFallbackBytes(), WEBP, await landFallbackEtag());
    }
    // The tile's grid cell, if populated: serve it directly, else overzoom the
    // deepest ancestor present in the cell — the cell's max_zoom is only its
    // deepest spot, and a shallower source in the same cell stops lower, so walk
    // the ancestor zooms down until one is there (upscales the regional detail,
    // not the coarse planet). A miss OR an error (R2 miss, bad range, decode
    // failure) must not dead-end the tile — try the next level, then the planet.
    const ov = overlayFor(mf.overlay, z, x, y);
    if (ov) {
      if (z <= ov.maxZoom) {
        try {
          const t = await tile(renv, ov.file, z, x, y);
          if (t) return sendTile(t, WEBP, await contentEtag(t));
        } catch (e) {
          console.log(`overlay ${ov.file} failed at ${z}/${x}/${y}: ${e}`);
        }
      }
      for (
        let sm = Math.min(ov.maxZoom, z - 1);
        sm > mf.planet.max_zoom;
        sm--
      ) {
        try {
          const r = await overzoomTile(ov.file, sm);
          if (r) return r;
        } catch (e) {
          console.log(
            `overlay ${ov.file} overzoom z${sm} failed at ${z}/${x}/${y}: ${e}`,
          );
        }
      }
    }
    // Nothing in the cell: overzoom the planet, or sea-level if even that's absent.
    const planetOz = await overzoomTile("planet.pmtiles", mf.planet.max_zoom);
    return (
      planetOz ?? sendTile(await landFallbackBytes(), WEBP, await landFallbackEtag())
    );
  },
};
