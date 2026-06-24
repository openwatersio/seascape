/**
 * Unified bathymetry tile endpoint.
 *
 *   GET /bathymetry/{z}/{x}/{y}.webp  (or .png)  → Terrarium WebP (raster terrain)
 *   GET /bathymetry/{z}/{x}/{y}.pbf   (or .mvt)  → MVT (vector — contours, more layers later)
 *
 * Extension picks the representation: webp/png → raster, pbf/mvt → vector.
 *
 * Reads the bundles published to R2 (planet.pmtiles + per-source <id>.pmtiles +
 * contours.pmtiles + manifest.json) and resolves per tile:
 *   - z ≤ planet.max_zoom        → planet tile
 *   - z > planet.max_zoom, a source overlay covers it → that overlay's tile
 *   - otherwise                  → OVERZOOM the planet's deepest ancestor tile
 *
 * Overzoom of Terrarium is bilinear ON DECODED HEIGHTS, not on the packed bytes:
 * decode each source pixel to a float elevation, interpolate the elevations, re-encode.
 * Averaging the raw RGB would corrupt the decode (G wraps at 256); nearest-neighbour
 * leaves flat plateaus + cliff edges that the hillshade renders as stair-steps. Contours
 * need no overzoom — tippecanoe bakes base lines to the deepest zoom, so it's passthrough.
 */

import { PMTiles, Source, RangeResponse } from "pmtiles";
import { unpackTerrarium, packTerrariumInto } from "./terrarium";
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
  RELEASE_PREFIX?: string; // R2 key prefix selecting which release to serve, e.g. "bathymetry/<sha>/"; default ""
  BASE_PATH?: string; // URL mount path = the Cloudflare route prefix; default "/bathymetry"
}

interface BundleMeta {
  file: string;
  min_zoom: number;
  max_zoom: number;
  bbox: [number, number, number, number]; // w, s, e, n
}
interface Manifest {
  planet: BundleMeta;
  sources: (BundleMeta & { id: string })[]; // pre-sorted deepest-first
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
  let p = pmCache.get(key);
  if (!p) {
    p = new PMTiles(new R2Source(env.TILES, key));
    pmCache.set(key, p);
  }
  return p;
}

let manifestCache: Manifest | undefined;
async function manifest(env: Env): Promise<Manifest> {
  if (!manifestCache) {
    const obj = await env.TILES.get(
      (env.RELEASE_PREFIX ?? "") + "manifest.json",
    );
    if (!obj) throw new Error("manifest.json missing");
    manifestCache = JSON.parse(await obj.text());
  }
  return manifestCache!;
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

// ── tile geometry ─────────────────────────────────────────────────────────
function tileBounds(
  z: number,
  x: number,
  y: number,
): [number, number, number, number] {
  const n = 2 ** z;
  const lon = (i: number) => (i / n) * 360 - 180;
  const lat = (j: number) =>
    (Math.atan(Math.sinh(Math.PI * (1 - (2 * j) / n))) * 180) / Math.PI;
  return [lon(x), lat(y + 1), lon(x + 1), lat(y)]; // w, s, e, n
}
function intersects(a: number[], b: number[]): boolean {
  return a[0] < b[2] && a[2] > b[0] && a[1] < b[3] && a[3] > b[1];
}

// ── Terrarium overzoom: bilinear on decoded heights ─────────────────────────
// Decode the ancestor tile to elevations, bilinearly resample the elevations into the
// output sub-tile, re-encode. Smooths the DEM the way MapLibre would for a client-side
// overzoom, so the hillshade no longer steps. Sampling clamps to the ancestor's edge:
// seams at ancestor-tile boundaries flatten slightly — far below the old stair-stepping.
async function overzoom(
  env: Env,
  srcFile: string,
  srcMax: number,
  z: number,
  x: number,
  y: number,
): Promise<ArrayBuffer | null> {
  const levels = z - srcMax;
  const span = 1 << levels; // sub-tiles per axis within the ancestor
  const px = x >> levels,
    py = y >> levels; // ancestor tile at srcMax
  const subX = x - (px << levels),
    subY = y - (py << levels);

  const parent = await tile(env, srcFile, srcMax, px, py);
  if (!parent) return null; // ancestor missing in this source; caller tries the next

  await ensureCodec();
  const img = await decodeWebp(parent); // {data: Uint8ClampedArray RGBA, width, height}
  const src = img.data;
  const W = img.width,
    H = img.height;
  const out = new Uint8ClampedArray(TILE * TILE * 4);
  const srcSize = TILE / span; // pixels of the ancestor this sub-tile spans
  const ox = subX * srcSize,
    oy = subY * srcSize;
  const h = (sx: number, sy: number) => {
    const i = (sy * W + sx) * 4;
    return unpackTerrarium(src[i], src[i + 1], src[i + 2]);
  };
  for (let j = 0; j < TILE; j++) {
    // output-pixel centre → fractional ancestor row, clamped inside the tile
    let sy = oy + (j + 0.5) / span - 0.5;
    sy = sy < 0 ? 0 : sy > H - 1 ? H - 1 : sy;
    const y0 = sy | 0,
      y1 = y0 + 1 < H ? y0 + 1 : y0,
      wy = sy - y0;
    for (let i = 0; i < TILE; i++) {
      let sx = ox + (i + 0.5) / span - 0.5;
      sx = sx < 0 ? 0 : sx > W - 1 ? W - 1 : sx;
      const x0 = sx | 0,
        x1 = x0 + 1 < W ? x0 + 1 : x0,
        wx = sx - x0;
      const height =
        h(x0, y0) * (1 - wx) * (1 - wy) +
        h(x1, y0) * wx * (1 - wy) +
        h(x0, y1) * (1 - wx) * wy +
        h(x1, y1) * wx * wy;
      packTerrariumInto(out, (j * TILE + i) * 4, height);
    }
  }
  return encodeWebp({ data: out, width: TILE, height: TILE } as ImageData, {
    lossless: 1,
  });
}

let transparentCache: ArrayBuffer | undefined;
async function _makeTransparent(): Promise<ArrayBuffer> {
  // Terrarium sea level (0 m) so the depth ramp renders it transparent.
  await ensureCodec();
  const out = new Uint8ClampedArray(TILE * TILE * 4);
  for (let k = 0; k < TILE * TILE; k++) {
    out[k * 4] = 128; // 32768 → R=128, G=0, B=0  (height 0)
    out[k * 4 + 3] = 255;
  }
  return encodeWebp({ data: out, width: TILE, height: TILE } as ImageData, {
    lossless: 1,
  });
}
async function transparentBytes(): Promise<ArrayBuffer> {
  if (!transparentCache) transparentCache = await _makeTransparent();
  return transparentCache;
}

const CORS = { "access-control-allow-origin": "*" };
// Stable tile URLs: a deploy never orphans cached tiles, and stale-while-revalidate/-if-error keep
// serving them (refreshing in the background) so a nav app shows stale bathymetry over a blank tile.
const TILE_CACHE =
  "public, max-age=3600, stale-while-revalidate=31536000, stale-if-error=31536000";
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
  async fetch(req: Request, env: Env): Promise<Response> {
    const noTile = () => new Response(null, { status: 204, headers: CORS });
    const url = new URL(req.url);
    const path = url.pathname;
    // ETag = release id (from RELEASE_PREFIX): every resource the worker serves is deterministic
    // within a release, so one id validates all of them. A revalidation that still matches gets a
    // bodyless 304 — cheap refresh for stale-while-revalidate/-if-error. (ETag is per release, so a
    // deploy re-downloads even unchanged tiles; a per-tile content hash would avoid that, at the
    // cost of hashing every response.)
    const etag = `"${(env.RELEASE_PREFIX ?? "").split("/").filter(Boolean).pop() ?? "dev"}"`;
    const fresh = req.headers.get("If-None-Match") === etag;
    const send = (
      body: BodyInit | null,
      headers: Record<string, string>,
    ): Response =>
      fresh
        ? new Response(null, {
            status: 304,
            headers: {
              etag,
              "cache-control": headers["cache-control"],
              ...CORS,
            },
          })
        : new Response(body, { headers: { ...headers, etag, ...CORS } });
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
    const base = (env.BASE_PATH ?? "/bathymetry").replace(/\/+$/, "");
    const mounted =
      base !== "" && (path === base || path.startsWith(base + "/"));
    const rel = mounted ? path.slice(base.length) : path;
    const mount = mounted ? base : "";

    if (rel === "/manifest.json") {
      return json(await manifest(env));
    }
    // TileJSON per representation — point MapLibre/Mapbox at these directly.
    // A TileJSON is single-format, so raster and vector get separate docs.
    if (rel === "/raster.json") {
      const mf = await manifest(env);
      return json({
        tilejson: "3.0.0",
        name: "Open Waters Bathymetry (raster)",
        tiles: [`${url.origin}${mount}/{z}/{x}/{y}.webp`],
        minzoom: mf.planet.min_zoom,
        // Worker overzooms past native data, so the served ceiling is the deepest source.
        maxzoom: Math.max(
          mf.planet.max_zoom,
          ...mf.sources.map((s) => s.max_zoom),
        ),
        bounds: mf.planet.bbox,
        encoding: "terrarium",
        attribution: mf.attribution ?? "",
      });
    }
    if (rel === "/vector.json") {
      const mf = await manifest(env);
      const h = await pm(env, "contours.pmtiles").getHeader();
      return json({
        tilejson: "3.0.0",
        name: "Open Waters Bathymetry",
        tiles: [`${url.origin}${mount}/{z}/{x}/{y}.pbf`],
        minzoom: h.minZoom,
        maxzoom: h.maxZoom,
        bounds: [h.minLon, h.minLat, h.maxLon, h.maxLat],
        vector_layers: [
          {
            id: "contours",
            fields: { depth_m: "Number", depth_abs_m: "Number" },
          },
        ],
        attribution: mf.attribution ?? "",
      });
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

    // Tile revalidation that still matches the deployed release → 304, skip the R2 read entirely.
    if (fresh) return send(null, WEBP);

    const isVector = ext === "pbf" || ext === "mvt";

    // Out-of-range x/y (the pmtiles coord check throws on these) → blank tile, not a 500.
    const n = 2 ** z;
    if (x >= n || y >= n)
      return isVector ? noTile() : send(await transparentBytes(), WEBP);

    if (isVector) {
      const t = await tile(env, "contours.pmtiles", z, x, y);
      return t ? send(t, MVT) : noTile();
    }

    // Terrain always returns a valid 512px tile (transparent sea-level on a miss)
    // so raster-dem never sees a 0-dim neighbour during border backfill.
    const mf = await manifest(env);
    if (z <= mf.planet.max_zoom) {
      const t = await tile(env, "planet.pmtiles", z, x, y);
      return t ? send(t, WEBP) : send(await transparentBytes(), WEBP);
    }
    // Deepest-first: serve (or overzoom) the highest-res source covering this tile,
    // so above an overlay's native zoom we upscale THAT overlay's regional detail
    // instead of the coarse planet.
    const tb = tileBounds(z, x, y);
    for (const s of mf.sources) {
      if (!intersects(tb, s.bbox)) continue;
      // A miss (covering claims this bbox but lacks the tile) OR an error (R2 miss, bad
      // range, decode failure) must not dead-end the tile — fall through to the next
      // covering source, then the planet overzoom below.
      try {
        const t =
          z <= s.max_zoom
            ? await tile(env, s.file, z, x, y)
            : await overzoom(env, s.file, s.max_zoom, z, x, y);
        if (t) return send(t, WEBP);
      } catch (e) {
        console.log(
          `overlay ${s.file} failed at ${z}/${x}/${y}, trying next: ${e}`,
        );
      }
    }
    // No overlay covers it: overzoom the planet, or transparent if even that's absent.
    const planetOz = await overzoom(
      env,
      "planet.pmtiles",
      mf.planet.max_zoom,
      z,
      x,
      y,
    );
    return send(planetOz ?? (await transparentBytes()), WEBP);
  },
};
