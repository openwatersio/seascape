/**
 * Unified bathymetry tile endpoint.
 *
 *   GET /seascape/{z}/{x}/{y}.webp  (or .png)  → Terrarium WebP (raster terrain)
 *   GET /seascape/{z}/{x}/{y}.pbf   (or .mvt)  → MVT (vector — contours, more layers later)
 *
 * Extension picks the representation: webp/png → raster, pbf/mvt → vector.
 *
 * Reads the bundles published to R2 (planet.pmtiles + one overlay-{cell}.pmtiles
 * per populated grid cell + contours.pmtiles + manifest.json) and resolves per tile:
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
import { unpackTerrarium, packTerrariumInto, cubicBSpline } from "./terrarium";
import { OverlayIndex, overlayFor } from "./overlay";
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
    // Tolerate a pre-grid manifest (old release / local seed): planet-only, no 500s.
    manifestCache!.overlay ??= { split_z: 0, cells: {} };
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

// ── Terrarium overzoom: cubic B-spline on decoded heights ───────────────────
// Decode the ancestor tile to elevations, resample into the output sub-tile with a cubic
// B-spline (GDAL's `cubicspline`), re-encode. Its basis is non-negative and C2, so it can't
// overshoot a sharp shelf edge into a halo and leaves no stairstep — the trade is that it
// smooths rather than interpolating. Sampling clamps to the ancestor's edge: seams at
// ancestor-tile boundaries flatten slightly — invisible.
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
    const base = (env.BASE_PATH ?? "/seascape").replace(/\/+$/, "");
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
        // Worker overzooms past native data, so the served ceiling is the deepest cell.
        maxzoom: Math.max(
          mf.planet.max_zoom,
          ...Object.values(mf.overlay.cells),
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
          const t = await tile(env, ov.file, z, x, y);
          if (t) return send(t, WEBP);
        } catch (e) {
          console.log(`overlay ${ov.file} failed at ${z}/${x}/${y}: ${e}`);
        }
      }
      for (let sm = Math.min(ov.maxZoom, z - 1); sm > mf.planet.max_zoom; sm--) {
        try {
          const t = await overzoom(env, ov.file, sm, z, x, y);
          if (t) return send(t, WEBP);
        } catch (e) {
          console.log(
            `overlay ${ov.file} overzoom z${sm} failed at ${z}/${x}/${y}: ${e}`,
          );
        }
      }
    }
    // Nothing in the cell: overzoom the planet, or transparent if even that's absent.
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
