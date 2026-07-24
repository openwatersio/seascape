// Effective-land mask compositing for the raster overzoom path. Ships as a small
// vector tileset (land.pmtiles: layers `land` and `water`, either may be absent) and is
// rasterized onto the 512² synthesized tile so a coarse DEM's shoreline is re-cut to the
// OSM effective-land line. Mirrors landmask.rasterize: burn land = 1, then water = 0 (the
// water burn only ever re-opens water). See docs/plans/2026-07-23-worker-land-mask.md.
import { VectorTile } from "@mapbox/vector-tile";
import Protobuf from "pbf";

const TILE = 512;

// Published raster codes (pipelines/terrain.py): 0 unknown-depth water, 1 drying, 2 land.
const LAND = 2;
const WATER = 0;

interface Pt {
  x: number;
  y: number;
}
// The slice of @mapbox/vector-tile's VectorTileLayer this rasterizer touches — kept as an
// interface so the scanline core is unit-testable without encoding a real MVT.
interface MaskLayer {
  extent: number;
  length: number;
  feature(i: number): { loadGeometry(): Pt[][] };
}

// Composite the mask onto synthesized heights before packing. h ≥ 2 is the load-bearing
// threshold: in a mask-water cell it catches genuine land topography and the land code
// itself (the averaged-away bay → unknown-depth water, an honest wash, never an invented
// depth), while [0,2) passes through so drying (1) and shoreline spline blends survive
// (OSM land ≈ high-water, so the foreshore is mask-water). h < 0 is real interpolated depth.
export function composite(heights: Float64Array, mask: Uint8Array): void {
  for (let i = 0; i < heights.length; i++) {
    if (mask[i] === 1) heights[i] = LAND;
    else if (heights[i] >= LAND) heights[i] = WATER;
  }
}

// Absent mask tile within a present archive = open ocean: an all-water mask, a no-op on
// real ocean (h < 0) and a knock-down of any phantom DEM land there. Shared, read-only.
let allWater: Uint8Array | undefined;
export function waterMask(): Uint8Array {
  return (allWater ??= new Uint8Array(TILE * TILE));
}

export function rasterizeMask(
  bytes: ArrayBuffer | Uint8Array,
  z: number,
  x: number,
  y: number,
  maskMaxZoom: number,
): Uint8Array {
  const u8 = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  const vt = new VectorTile(new Protobuf(u8));
  return rasterizeLayers(
    vt.layers["land"] as MaskLayer | undefined,
    vt.layers["water"] as MaskLayer | undefined,
    z,
    x,
    y,
    maskMaxZoom,
  );
}

export function rasterizeLayers(
  land: MaskLayer | undefined,
  water: MaskLayer | undefined,
  z: number,
  x: number,
  y: number,
  maskMaxZoom: number,
): Uint8Array {
  const mask = new Uint8Array(TILE * TILE);
  // The mask tile may be an ancestor: map its extent coords into this target tile's 512px
  // frame with the shift/scale synthesize() uses for the DEM ancestor.
  const lz = Math.min(z, maskMaxZoom);
  const levels = z - lz;
  const span = 1 << levels;
  const subX = x - ((x >> levels) << levels);
  const subY = y - ((y >> levels) << levels);
  burnLayer(mask, land, 1, span, subX, subY);
  burnLayer(mask, water, 0, span, subX, subY);
  return mask;
}

function burnLayer(
  mask: Uint8Array,
  layer: MaskLayer | undefined,
  value: number,
  span: number,
  subX: number,
  subY: number,
): void {
  if (!layer) return;
  const scale = (TILE * span) / layer.extent; // extent coord → output px
  const offX = subX * TILE,
    offY = subY * TILE;
  for (let f = 0; f < layer.length; f++)
    fillRings(mask, layer.feature(f).loadGeometry(), value, scale, offX, offY);
}

// Even-odd scanline fill of one feature's rings. Even-odd parity handles holes and disjoint
// polygons (multipolygon) without ring-orientation classification. MVT buffer geometry can
// run past [0, extent]; row bounds and per-span x clamp to the 512px frame.
function fillRings(
  mask: Uint8Array,
  rings: Pt[][],
  value: number,
  scale: number,
  offX: number,
  offY: number,
): void {
  let minRow = TILE,
    maxRow = 0;
  const tRings: Float64Array[] = [];
  for (const ring of rings) {
    const t = new Float64Array(ring.length * 2);
    for (let k = 0; k < ring.length; k++) {
      const py = ring[k].y * scale - offY;
      t[k * 2] = ring[k].x * scale - offX;
      t[k * 2 + 1] = py;
      if (py < minRow) minRow = py;
      if (py > maxRow) maxRow = py;
    }
    tRings.push(t);
  }
  const r0 = Math.max(0, Math.ceil(minRow - 0.5));
  const r1 = Math.min(TILE - 1, Math.floor(maxRow - 0.5));
  const xs: number[] = [];
  for (let row = r0; row <= r1; row++) {
    const yc = row + 0.5; // sample pixel centres, matching synthesize()
    xs.length = 0;
    for (const t of tRings) {
      const n = t.length / 2;
      for (let k = 0; k < n; k++) {
        const ax = t[k * 2],
          ay = t[k * 2 + 1];
        const j = (k + 1) % n;
        const bx = t[j * 2],
          by = t[j * 2 + 1];
        if (ay <= yc === by <= yc) continue; // edge doesn't cross this scanline
        xs.push(ax + ((yc - ay) / (by - ay)) * (bx - ax));
      }
    }
    if (xs.length < 2) continue;
    xs.sort((a, b) => a - b);
    const base = row * TILE;
    for (let s = 0; s + 1 < xs.length; s += 2) {
      const xa = Math.max(0, Math.ceil(xs[s] - 0.5));
      const xb = Math.min(TILE - 1, Math.floor(xs[s + 1] - 0.5));
      for (let px = xa; px <= xb; px++) mask[base + px] = value;
    }
  }
}
