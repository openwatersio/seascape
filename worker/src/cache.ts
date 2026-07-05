/**
 * Colo-shared caching for the tile Worker. Two pieces:
 *
 *  - contentEtag: a validator derived from tile bytes (or from the native
 *    ancestor bytes a synthesized tile is a pure function of), so a release
 *    that leaves a tile unchanged leaves its validator unchanged — clients
 *    revalidate to bodyless 304s instead of re-downloading the planet. The
 *    release id never enters the tag.
 *
 *  - CachedSource: wraps a PMTiles Source so byte-range reads go through the
 *    zone cache (caches.default). The same root/leaf directories serve whole
 *    regions of tile requests, so this collapses the cold-isolate directory
 *    walk (2–4 sequential R2 round trips) into colo-local lookups shared by
 *    every isolate at the colo. Tile-data ranges land here too — harmless
 *    (the response cache upstream catches repeats; entries LRU out) and it
 *    keeps the wrapper free of PMTiles format knowledge.
 *
 * This module stays importable by plain Node (cache.test.mjs): erasable
 * TypeScript syntax only, and caches.default is only dereferenced at call
 * time so tests can stub `globalThis.caches`.
 */
import type { Source, RangeResponse } from "pmtiles";

// Bump when the overzoom output changes for the same input (resampler or
// encoder settings): synthesized tiles validate by their *input*, so a code
// change must rotate the tags or matching revalidations would 304 clients
// onto stale renders until their caches age out.
export const OVERZOOM_TAG_VERSION = "oz1";

// 64 bits of SHA-256 — plenty for cache validation (not a security boundary).
export async function contentEtag(
  bytes: BufferSource,
  prefix = "",
): Promise<string> {
  const d = await crypto.subtle.digest("SHA-256", bytes);
  const hex = Array.from(new Uint8Array(d, 0, 8), (b) =>
    b.toString(16).padStart(2, "0"),
  ).join("");
  return `"${prefix}${hex}"`;
}

export class CachedSource implements Source {
  inner: Source;
  // The release prefix inside keyBase makes every key immutable: a deploy
  // switches namespaces and superseded entries LRU out on their own. Keep the
  // host on the served zone (tiles.openwaters.io) so a manual dashboard
  // purge-everything remains an escape hatch.
  keyBase: string;
  constructor(inner: Source, keyBase: string) {
    this.inner = inner;
    this.keyBase = keyBase;
  }
  getKey() {
    return this.inner.getKey();
  }
  async getBytes(offset: number, length: number): Promise<RangeResponse> {
    const key = `${this.keyBase}/${offset}-${length}`;
    const hit = await caches.default.match(key);
    if (hit) return { data: await hit.arrayBuffer() };
    const r = await this.inner.getBytes(offset, length);
    // Await the put: it's colo-local (fast), and this call has no
    // ExecutionContext to waitUntil on — a dangling promise could be
    // cancelled when the request settles. slice(): don't share the buffer
    // between the stored body and the returned RangeResponse.
    await caches.default.put(
      key,
      new Response(r.data.slice(0), {
        headers: { "cache-control": "public, s-maxage=2592000" },
      }),
    );
    return r;
  }
}
