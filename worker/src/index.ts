/**
 * Unified bathymetry tile endpoint.
 *
 *   GET /bathymetry/terrain/{z}/{x}/{y}    → Terrarium WebP (terrain)
 *   GET /bathymetry/contours/{z}/{x}/{y}   → MVT (vector contours)
 *
 * Reads the bundles published to R2 (planet.pmtiles + per-source <id>.pmtiles +
 * contours.pmtiles + manifest.json) and resolves per tile:
 *   - z ≤ planet.max_zoom        → planet tile
 *   - z > planet.max_zoom, a source overlay covers it → that overlay's tile
 *   - otherwise                  → OVERZOOM the planet's deepest ancestor tile
 *
 * Overzoom of Terrarium MUST be nearest-neighbour: every output pixel is an exact
 * source pixel = a real elevation. Bilinear would interpolate the packed RGB and
 * corrupt the decode. Contours need no overzoom — tippecanoe already bakes the
 * base lines to the deepest zoom, so that layer is a straight passthrough.
 */

import { PMTiles, Source, RangeResponse } from "pmtiles";
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
  TILES_PREFIX?: string; // e.g. "bathymetry/"; default ""
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
  const key = (env.TILES_PREFIX ?? "") + file;
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
    const obj = await env.TILES.get((env.TILES_PREFIX ?? "") + "manifest.json");
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

// ── Terrarium nearest-neighbour overzoom ────────────────────────────────────
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
  const out = new Uint8ClampedArray(TILE * TILE * 4);
  const srcSize = TILE / span; // pixels of the ancestor this sub-tile spans
  const ox = subX * srcSize,
    oy = subY * srcSize;
  for (let j = 0; j < TILE; j++) {
    const sy = oy + Math.floor(j / span);
    for (let i = 0; i < TILE; i++) {
      const sx = ox + Math.floor(i / span);
      const si = (sy * img.width + sx) * 4;
      const di = (j * TILE + i) * 4;
      out[di] = src[si];
      out[di + 1] = src[si + 1];
      out[di + 2] = src[si + 2];
      out[di + 3] = src[si + 3];
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
function transparentTile(): ArrayBuffer {
  return transparentCache ?? new ArrayBuffer(0);
}
async function transparentResponse(): Promise<Response> {
  if (!transparentCache) transparentCache = await _makeTransparent();
  return new Response(transparentCache, { headers: WEBP });
}

const CORS = { "access-control-allow-origin": "*" };
const WEBP = {
  "content-type": "image/webp",
  "cache-control": "public, max-age=86400",
  ...CORS,
};
const MVT = {
  "content-type": "application/x-protobuf",
  "cache-control": "public, max-age=86400",
  ...CORS,
};

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const noTile = () => new Response(null, { status: 204, headers: CORS });
    const path = new URL(req.url).pathname;
    if (path === "/bathymetry/manifest.json") {
      return new Response(JSON.stringify(await manifest(env)), {
        headers: { "content-type": "application/json", ...CORS },
      });
    }
    const m = path.match(/^\/bathymetry\/(terrain|contours)\/(\d+)\/(\d+)\/(\d+)/);
    if (!m)
      return new Response("usage: /bathymetry/{terrain,contours}/{z}/{x}/{y}", {
        status: 404,
        headers: CORS,
      });
    const layer = m[1];
    const z = +m[2],
      x = +m[3],
      y = +m[4];

    // Out-of-range x/y (the pmtiles coord check throws on these) → blank tile, not a 500.
    const n = 2 ** z;
    if (x >= n || y >= n)
      return layer === "contours" ? noTile() : transparentResponse();

    if (layer === "contours") {
      const t = await tile(env, "contours.pmtiles", z, x, y);
      return t ? new Response(t, { headers: MVT }) : noTile();
    }

    // Terrain always returns a valid 512px tile (transparent sea-level on a miss)
    // so raster-dem never sees a 0-dim neighbour during border backfill.
    const mf = await manifest(env);
    if (z <= mf.planet.max_zoom) {
      const t = await tile(env, "planet.pmtiles", z, x, y);
      return t ? new Response(t, { headers: WEBP }) : transparentResponse();
    }
    // Deepest-first: serve (or overzoom) the highest-res source covering this tile,
    // so above an overlay's native zoom we upscale THAT overlay's regional detail
    // instead of the coarse planet.
    const tb = tileBounds(z, x, y);
    for (const s of mf.sources) {
      if (!intersects(tb, s.bbox)) continue;
      const t =
        z <= s.max_zoom
          ? await tile(env, s.file, z, x, y)
          : await overzoom(env, s.file, s.max_zoom, z, x, y);
      if (t) return new Response(t, { headers: WEBP });
    }
    // No overlay covers it: overzoom the planet, or transparent if even that's absent.
    if (!transparentCache) transparentCache = await _makeTransparent();
    const planetOz = await overzoom(env, "planet.pmtiles", mf.planet.max_zoom, z, x, y);
    return new Response(planetOz ?? transparentCache, { headers: WEBP });
  },
};
