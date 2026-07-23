/**
 * Vector-tile overzoom synthesis — the sibling of the raster B-spline path in
 * index.ts. On a vector miss (a child tile beyond the baked variable-depth
 * leaves) the Worker walks to the deepest baked ancestor and synthesizes the
 * child here, and every baked leaf served at its own zoom passes through the
 * same clean/simplify pass (variable-depth leaves are written unsimplified, so
 * the serving layer owns cleaning — tippecanoe's README makes this explicit).
 *
 * This is a faithful port of `tippecanoe-overzoom`'s SEMANTICS (clip.cpp
 * `overzoom()` + the `simple_clip_poly` / `clip_lines` / `clip_point` /
 * `to_tile_scale` / `remove_noop` helpers), NOT a naive scale+clip:
 *
 *   1. Each source feature's geometry → 32-bit "world" coordinates, then offset
 *      to the output tile's origin while KEEPING world scale (this is what makes
 *      zoom-oriented clipping and the buffer arithmetic exact).
 *   2. Clip to the child extent + buffer (Cohen–Sutherland for lines, Sutherland
 *      –Hodgman for polygons, box test for points).
 *   3. `to_tile_scale` — divide back to the output extent (1<<detail), rounding.
 *      This quantization to the child's grid, plus `remove_noop`, IS the
 *      "simplify to the child's effective grid" step (default line-simplification
 *      is 0 in tippecanoe-overzoom).
 *   4. Repair polygons after clipping (degenerate-ring drop) so the depare fill
 *      partition contract — pairwise-disjoint bands meeting exactly at seams —
 *      survives. Sutherland–Hodgman clips both neighbours of a seam identically,
 *      so disjointness and seam-exactness are preserved; a ring collapsed to a
 *      sliver by the scale-down is dropped.
 *
 * It operates on tippecanoe-style op-lists ({op, x, y}, op ∈ moveto/lineto), so
 * the port reads line-for-line against clip.cpp. Property value TYPES (numeric
 * stays numeric), feature ids, layer names, and `rank` (a plain property) all
 * ride through @mapbox/vector-tile → this pipeline → vt-pbf untouched.
 *
 * The multi-parent form (tippecanoe's several-`-t`-sources overzoom, where a
 * child's buffer is fed from a neighbour parent that leafed at a different
 * depth) is a first-class capability of synthesizeLayers: it merges features
 * from every source tile into the output layers by name. The Worker feeds a
 * single ancestor because the buffer-arithmetic invariant (a child's buffer need
 * in parent units is b/2^Δz, which fits inside the parent's own baked buffer b)
 * guarantees one ancestor covers the child's whole buffered extent — the tests
 * exercise the multi-parent function directly and assert that invariant.
 *
 * Pure JS (@mapbox/vector-tile + pbf + vt-pbf) — Workers-compatible, no WASM.
 * synthesizeLayers is a pure function (decoded tiles in → decoded layers out) so
 * an oracle harness can drive the TS port and the C++ binary off identical
 * inputs and diff per feature; encodeTile / decodeTile bracket it for the wire.
 */
import { VectorTile } from "@mapbox/vector-tile";
import Pbf from "pbf";
import vtpbf from "vt-pbf";

// Feature geometry types (MVT geomtype == tippecanoe VT_*): 1 point, 2 line, 3 polygon.
export const POINT = 1,
  LINE = 2,
  POLYGON = 3;
// Op codes on a drawvec point (tippecanoe VT_MOVETO / VT_LINETO). We never
// materialize CLOSEPATH: decoded polygon rings arrive explicitly closed (last ==
// first) and stay that way, which is exactly what vt-pbf's encoder wants.
const MOVETO = 1,
  LINETO = 2;

// tippecanoe-overzoom CLI defaults (overzoom.cpp): extent == 1<<detail, buffer
// in 1/256ths of the extent. The bundle emits 4096-extent tiles, so detail 12.
const DETAIL = 12;
const BUFFER = 5;

type Op = { op: number; x: number; y: number };
export interface DecodedFeature {
  id?: number;
  type: number; // POINT | LINE | POLYGON
  properties: Record<string, unknown>;
  ops: Op[]; // moveto/lineto op-list (polygon rings explicitly closed)
}
export interface DecodedLayer {
  name: string;
  version: number;
  extent: number;
  features: DecodedFeature[];
}
export interface SourceTile {
  z: number;
  x: number;
  y: number;
  layers: DecodedLayer[];
}
export interface SynthOpts {
  detail?: number;
  buffer?: number;
  // Clean/simplify pass (remove_noop + polygon repair). tippecanoe skips it when
  // the output tile IS the input tile (its `sametile` shortcut); we default it ON
  // even then, because variable-depth leaves are written uncleaned and the
  // serving layer must clean them (plan item 3). Off only for tests that want the
  // raw scale+clip.
  clean?: boolean;
}

// C++ std::round rounds half AWAY from zero; JS Math.round rounds half toward
// +∞. They disagree on negative .5 (buffer-fringe coords go negative), so match
// C++ to keep the port faithful (and bit-comparable to the oracle later).
const cround = (v: number): number => (v < 0 ? -Math.round(-v) : Math.round(v));
// C++ integer division truncates toward zero.
const idiv = (a: number, b: number): number => Math.trunc(a / b);

// ── decode / encode (wire ⇄ decoded layers) ─────────────────────────────────
export function decodeTile(bytes: ArrayBuffer | Uint8Array): DecodedLayer[] {
  const u8 = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  const vt = new VectorTile(new Pbf(u8));
  const out: DecodedLayer[] = [];
  for (const name of Object.keys(vt.layers)) {
    const layer = vt.layers[name];
    const features: DecodedFeature[] = [];
    for (let i = 0; i < layer.length; i++) {
      const f = layer.feature(i);
      const ops: Op[] = [];
      // loadGeometry returns rings/lines as point arrays; polygon rings come
      // back closed (the closepath is expanded to a repeat of the first point),
      // and a multipoint's points each arrive as their own single-point ring —
      // both exactly the drawvec shape tippecanoe builds.
      for (const ring of f.loadGeometry()) {
        for (let k = 0; k < ring.length; k++)
          ops.push({ op: k === 0 ? MOVETO : LINETO, x: ring[k].x, y: ring[k].y });
      }
      const feat: DecodedFeature = {
        type: f.type,
        properties: { ...f.properties },
        ops,
      };
      if (f.id !== undefined) feat.id = f.id as number;
      features.push(feat);
    }
    out.push({ name, version: layer.version ?? 1, extent: layer.extent, features });
  }
  return out;
}

// One vt-pbf feature view over a decoded feature: rings reconstructed from the
// op-list (each moveto starts a ring). Polygons handed back closed, which is
// what vt-pbf.writeGeometry expects (it drops the closing repeat and re-emits a
// closepath command itself).
function featureView(f: DecodedFeature, extent: number) {
  const rings: { x: number; y: number }[][] = [];
  let cur: { x: number; y: number }[] | null = null;
  for (const p of f.ops) {
    if (p.op === MOVETO) {
      cur = [];
      rings.push(cur);
    }
    cur!.push({ x: p.x, y: p.y });
  }
  if (f.type === POLYGON)
    for (const r of rings) {
      const a = r[0],
        b = r[r.length - 1];
      if (r.length > 0 && (a.x !== b.x || a.y !== b.y)) r.push({ x: a.x, y: a.y });
    }
  const view: {
    id?: number;
    type: number;
    extent: number;
    properties: Record<string, unknown>;
    loadGeometry: () => { x: number; y: number }[][];
  } = {
    type: f.type,
    extent,
    properties: f.properties,
    loadGeometry: () => rings,
  };
  if (f.id !== undefined) view.id = f.id;
  return view;
}

export function encodeTile(layers: DecodedLayer[]): Uint8Array {
  const tileLayers: Record<string, unknown> = {};
  for (const l of layers) {
    const extent = l.extent || 1 << DETAIL;
    tileLayers[l.name] = {
      version: l.version || 2,
      name: l.name,
      extent,
      length: l.features.length,
      feature: (i: number) => featureView(l.features[i], extent),
    };
  }
  const buf = vtpbf.fromVectorTileJs({ layers: tileLayers });
  return buf instanceof Uint8Array ? buf : new Uint8Array(buf);
}

export function featureCount(layers: DecodedLayer[]): number {
  return layers.reduce((n, l) => n + l.features.length, 0);
}

// Authoritative vector serving maxzoom: the manifest's covering-derived value
// wins, the archive header (deepest baked leaf) is the fallback. Kept as a named
// function so the "manifest wins, header is fallback" contract is explicit and
// tested — getting it backwards would cap serving at the leaf depth and push
// clients onto their own overzoom for the levels the Worker means to synthesize.
export function serveMaxZoom(manifestMax: number | undefined, headerMax: number): number {
  return manifestMax ?? headerMax;
}

// ── the clip helpers (direct ports of clip.cpp) ──────────────────────────────
// Sutherland–Hodgman `inside` per edge (clip.cpp): 0 top, 1 right, 2 bottom, 3
// left. Tile coords run y-down, so miny is the top. Strict — a point exactly on
// the edge is outside, matching tippecanoe.
function inside(p: Op, edge: number, minx: number, miny: number, maxx: number, maxy: number): boolean {
  switch (edge) {
    case 0:
      return p.y > miny;
    case 1:
      return p.x < maxx;
    case 2:
      return p.y < maxy;
    default:
      return p.x > minx;
  }
}
function intersect(a: Op, b: Op, edge: number, minx: number, miny: number, maxx: number, maxy: number): Op {
  switch (edge) {
    case 0:
      return { op: LINETO, x: a.x + ((b.x - a.x) * (miny - a.y)) / (b.y - a.y), y: miny };
    case 1:
      return { op: LINETO, x: maxx, y: a.y + ((b.y - a.y) * (maxx - a.x)) / (b.x - a.x) };
    case 2:
      return { op: LINETO, x: a.x + ((b.x - a.x) * (maxy - a.y)) / (b.y - a.y), y: maxy };
    default:
      return { op: LINETO, x: minx, y: a.y + ((b.y - a.y) * (minx - a.x)) / (b.x - a.x) };
  }
}

// clip_poly1 with prevent_simplify_shared_nodes = false (the overzoom call):
// plain Sutherland–Hodgman of one closed ring against the buffer rectangle. The
// input ring includes its closing repeat, as in tippecanoe.
function clipPolyRing(ring: Op[], minx: number, miny: number, maxx: number, maxy: number): Op[] {
  let out = ring.slice();
  for (let edge = 0; edge < 4 && out.length > 0; edge++) {
    const input = out;
    out = [];
    let S = input[input.length - 1];
    for (const E of input) {
      if (inside(E, edge, minx, miny, maxx, maxy)) {
        if (!inside(S, edge, minx, miny, maxx, maxy)) out.push(intersect(S, E, edge, minx, miny, maxx, maxy));
        out.push(E);
      } else if (inside(S, edge, minx, miny, maxx, maxy)) {
        out.push(intersect(S, E, edge, minx, miny, maxx, maxy));
      }
      S = E;
    }
  }
  if (out.length > 0) {
    const a = out[0],
      b = out[out.length - 1];
    if (a.x !== b.x || a.y !== b.y) out.push({ op: a.op, x: a.x, y: a.y });
    if (out.length < 3) return [];
  }
  // Round to integers, as clip_poly1 does (std::round) on push.
  return out.map((p, i) => ({ op: i === 0 ? MOVETO : LINETO, x: cround(p.x), y: cround(p.y) }));
}

// Cohen–Sutherland segment clip (clip.cpp `clip`). Integer truncating division,
// as in tippecanoe. Returns changed+1 (>1 clipped, 1 unchanged) or 0 rejected.
const INSIDE = 0,
  LEFT = 1,
  RIGHT = 2,
  BOTTOM = 4,
  TOP = 8;
function outcode(x: number, y: number, xmin: number, ymin: number, xmax: number, ymax: number): number {
  let c = INSIDE;
  if (x < xmin) c |= LEFT;
  else if (x > xmax) c |= RIGHT;
  if (y < ymin) c |= BOTTOM;
  else if (y > ymax) c |= TOP;
  return c;
}
function clipSeg(
  s: { x0: number; y0: number; x1: number; y1: number },
  xmin: number,
  ymin: number,
  xmax: number,
  ymax: number,
): number {
  let oc0 = outcode(s.x0, s.y0, xmin, ymin, xmax, ymax);
  let oc1 = outcode(s.x1, s.y1, xmin, ymin, xmax, ymax);
  let accept = 0,
    changed = 0;
  for (;;) {
    if (!(oc0 | oc1)) {
      accept = 1;
      break;
    } else if (oc0 & oc1) {
      break;
    }
    let x = s.x0,
      y = s.y0;
    const ocOut = oc0 ? oc0 : oc1;
    if (ocOut & TOP) {
      x = s.x0 + idiv((s.x1 - s.x0) * (ymax - s.y0), s.y1 - s.y0);
      y = ymax;
    } else if (ocOut & BOTTOM) {
      x = s.x0 + idiv((s.x1 - s.x0) * (ymin - s.y0), s.y1 - s.y0);
      y = ymin;
    } else if (ocOut & RIGHT) {
      y = s.y0 + idiv((s.y1 - s.y0) * (xmax - s.x0), s.x1 - s.x0);
      x = xmax;
    } else if (ocOut & LEFT) {
      y = s.y0 + idiv((s.y1 - s.y0) * (xmin - s.x0), s.x1 - s.x0);
      x = xmin;
    }
    if (ocOut === oc0) {
      s.x0 = x;
      s.y0 = y;
      oc0 = outcode(s.x0, s.y0, xmin, ymin, xmax, ymax);
      changed = 1;
    } else {
      s.x1 = x;
      s.y1 = y;
      oc1 = outcode(s.x1, s.y1, xmin, ymin, xmax, ymax);
      changed = 1;
    }
  }
  return accept === 0 ? 0 : changed + 1;
}

// clip_lines (clip.cpp): clip each consecutive segment; a clipped segment becomes
// its own subpath and the pen resets to the ORIGINAL vertex so the next segment
// continues from there. remove_noop drops the resulting stub movetos afterward.
function clipLineOps(ops: Op[], minx: number, miny: number, maxx: number, maxy: number): Op[] {
  const out: Op[] = [];
  for (let i = 0; i < ops.length; i++) {
    const prev = ops[i - 1];
    if (i > 0 && (prev.op === MOVETO || prev.op === LINETO) && ops[i].op === LINETO) {
      const seg = { x0: prev.x, y0: prev.y, x1: ops[i].x, y1: ops[i].y };
      const c = clipSeg(seg, minx, miny, maxx, maxy);
      if (c > 1) {
        out.push({ op: MOVETO, x: seg.x0, y: seg.y0 });
        out.push({ op: LINETO, x: seg.x1, y: seg.y1 });
        out.push({ op: MOVETO, x: ops[i].x, y: ops[i].y });
      } else if (c === 1) {
        out.push(ops[i]);
      } else {
        out.push({ op: MOVETO, x: ops[i].x, y: ops[i].y });
      }
    } else {
      out.push(ops[i]);
    }
  }
  return out;
}

// clip_point (clip.cpp): keep points inside the buffered box (inclusive).
function clipPointOps(ops: Op[], minx: number, miny: number, maxx: number, maxy: number): Op[] {
  return ops.filter((p) => p.x >= minx && p.y >= miny && p.x <= maxx && p.y <= maxy);
}

// simple_clip_poly (clip.cpp): split into rings at each moveto, clip each ring.
function clipPolyOps(ops: Op[], minx: number, miny: number, maxx: number, maxy: number): Op[] {
  const out: Op[] = [];
  for (const ring of ringsOf(ops)) {
    const clipped = clipPolyRing(ring, minx, miny, maxx, maxy);
    for (const p of clipped) out.push(p);
  }
  return out;
}

function ringsOf(ops: Op[]): Op[][] {
  const rings: Op[][] = [];
  let cur: Op[] | null = null;
  for (const p of ops) {
    if (p.op === MOVETO) {
      cur = [];
      rings.push(cur);
    }
    if (cur) cur.push(p);
  }
  return rings;
}

// to_tile_scale (clip.cpp): world coords → output extent (1<<detail), rounding.
function toTileScale(ops: Op[], nz: number, detail: number): void {
  const shift = 32 - detail - nz;
  if (shift < 0) {
    const m = 2 ** -shift;
    for (const p of ops) {
      p.x = cround(p.x * m);
      p.y = cround(p.y * m);
    }
  } else {
    const d = 2 ** shift;
    for (const p of ops) {
      p.x = cround(p.x / d);
      p.y = cround(p.y / d);
    }
  }
}

// remove_noop (clip.cpp), shift 0: drop empty linetos (pass 1), unused movetos
// (pass 2, non-point), and stub movetos after a coincident lineto (pass 3, line).
function removeNoop(ops: Op[], type: number): Op[] {
  let out: Op[] = [];
  let ox = 0,
    oy = 0,
    have = false;
  for (const g of ops) {
    if (g.op === LINETO && have && g.x === ox && g.y === oy) continue;
    out.push(g);
    ox = g.x;
    oy = g.y;
    have = true;
  }
  if (type !== POINT) {
    const geom = out;
    out = [];
    for (let i = 0; i < geom.length; i++) {
      if (geom[i].op === MOVETO) {
        if (i + 1 >= geom.length) continue; // moveto at end of geometry
        if (geom[i + 1].op === MOVETO) continue; // moveto followed by moveto
      }
      out.push(geom[i]);
    }
  }
  if (type === LINE) {
    const geom = out;
    out = [];
    for (let i = 0; i < geom.length; i++) {
      if (i > 1 && geom[i].op === MOVETO) {
        const p = geom[i - 1];
        if (p.op === LINETO && p.x === geom[i].x && p.y === geom[i].y) continue;
      }
      out.push(geom[i]);
    }
  }
  return out;
}

// Shoelace area of one ring's points (drop the closing repeat if present).
function ringArea(ring: Op[]): number {
  const n = ring.length >= 2 && ring[0].x === ring[ring.length - 1].x && ring[0].y === ring[ring.length - 1].y
    ? ring.length - 1
    : ring.length;
  let area = 0;
  const bx = ring[0].x,
    by = ring[0].y;
  for (let k = 0; k < n; k++) {
    const a = ring[k],
      b = ring[(k + 1) % n];
    area += (a.x - bx) * (b.y - by) - (a.y - by) * (b.x - bx);
  }
  return area / 2;
}

// Polygon repair after clipping/scaling (the essence of clean_or_clip_poly with
// clip=false for our already-valid tippecanoe inputs): drop rings the scale-down
// collapsed to a sliver — fewer than 3 distinct vertices or zero signed area.
// The full wagyu self-intersection/winding resolution is deliberately NOT ported
// (see the note in the plan report): rectangle clipping preserves the validity
// and orientation of a valid simple ring, and it clips a shared seam identically
// for both neighbours, so the depare partition stays disjoint and seam-exact —
// which the tests assert directly.
function cleanPolygon(ops: Op[]): Op[] {
  const out: Op[] = [];
  for (const ring of ringsOf(ops)) {
    if (ring.length < 4) continue; // moveto + <3 linetos can't bound area
    if (ringArea(ring) === 0) continue;
    for (const p of ring) out.push(p);
  }
  return out;
}

// Ensure each ring ends on its start (vt-pbf wants closed rings).
function closeRings(ops: Op[]): Op[] {
  const out: Op[] = [];
  for (const ring of ringsOf(ops)) {
    if (ring.length === 0) continue;
    for (const p of ring) out.push(p);
    const a = ring[0],
      b = ring[ring.length - 1];
    if (a.x !== b.x || a.y !== b.y) out.push({ op: LINETO, x: a.x, y: a.y });
  }
  return out;
}

// ── the synthesis core (pure: decoded sources → decoded output layers) ───────
export function synthesizeLayers(
  sources: SourceTile[],
  nz: number,
  nx: number,
  ny: number,
  opts: SynthOpts = {},
): DecodedLayer[] {
  const detail = opts.detail ?? DETAIL;
  const buffer = opts.buffer ?? BUFFER;
  const clean = opts.clean ?? true;
  const outExtent = 1 << detail;
  const outtilesize = 2 ** (32 - nz); // output tile size in world coords
  const clipBuffer = idiv(buffer * outtilesize, 256);
  const minB = -clipBuffer,
    maxB = outtilesize + clipBuffer;

  const outLayers = new Map<string, DecodedLayer>();
  const layerOrder: string[] = [];

  for (const src of sources) {
    const tilesize = 2 ** (32 - src.z); // source tile size in world coords
    for (const layer of src.layers) {
      let out = outLayers.get(layer.name);
      if (!out) {
        out = { name: layer.name, version: layer.version, extent: outExtent, features: [] };
        outLayers.set(layer.name, out);
        layerOrder.push(layer.name);
      }
      for (const feat of layer.features) {
        const t = feat.type;
        // 1+2: to world coords, offset to output tile origin, keep world scale.
        let geom: Op[] = feat.ops.map((g) => ({
          op: g.op,
          x: idiv(g.x * tilesize, layer.extent) + src.x * tilesize - nx * outtilesize,
          y: idiv(g.y * tilesize, layer.extent) + src.y * tilesize - ny * outtilesize,
        }));

        // 3: quick bounding-box exclusion (clip.cpp).
        let xmin = Infinity,
          ymin = Infinity,
          xmax = -Infinity,
          ymax = -Infinity;
        for (const g of geom) {
          if (g.x < xmin) xmin = g.x;
          if (g.y < ymin) ymin = g.y;
          if (g.x > xmax) xmax = g.x;
          if (g.y > ymax) ymax = g.y;
        }
        if (xmax < minB || ymax < minB || xmin > maxB || ymin > maxB) continue;

        // 4: clip to output extent + buffer.
        if (t === LINE) geom = clipLineOps(geom, minB, minB, maxB, maxB);
        else if (t === POLYGON) geom = clipPolyOps(geom, minB, minB, maxB, maxB);
        else geom = clipPointOps(geom, minB, minB, maxB, maxB);
        if (geom.length === 0) continue;

        // 6: scale world coords down to the output tile extent.
        toTileScale(geom, nz, detail);

        // 7: clean geometries (tippecanoe skips this only for its sametile
        // shortcut; we clean always — see SynthOpts.clean).
        if (clean) {
          geom = removeNoop(geom, t);
          if (t === POLYGON) geom = cleanPolygon(geom);
        }
        if (t === POLYGON) geom = closeRings(geom);
        if (geom.length === 0) continue;

        const of: DecodedFeature = { type: t, properties: feat.properties, ops: geom };
        if (feat.id !== undefined) of.id = feat.id;
        out.features.push(of);
      }
    }
  }
  return layerOrder.map((n) => outLayers.get(n)!);
}
