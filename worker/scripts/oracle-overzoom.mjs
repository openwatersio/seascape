// Semantic oracle for the tippecanoe-overzoom port in worker/src/vector.ts.
//
// Feeds IDENTICAL inputs through the real C++ `tippecanoe-overzoom` binary and
// the pure-JS `synthesizeLayers` port, then compares the two outputs PER FEATURE
// after decode — layer name, feature id, geometry, property values AND types,
// and `rank`. Encoder byte order differs between the two; SEMANTICS must not.
// This is Verification item 1 of docs/plans/2026-07-14-native-resolution.md.
//
// ── How to run ────────────────────────────────────────────────────────────────
// The binary is NOT in CI (it's a ~600 KB C++ build), so this harness is a
// developer tool, never wired into `just test`. Build the binary once:
//
//   git clone --depth 1 https://github.com/felt/tippecanoe.git
//   cd tippecanoe && make -j4 tippecanoe-overzoom
//
// then run (from worker/):
//
//   OVERZOOM_BIN=/path/to/tippecanoe/tippecanoe-overzoom node scripts/oracle-overzoom.mjs
//
// If OVERZOOM_BIN is unset the harness looks for `tippecanoe-overzoom` on PATH.
// Exit code 0 = every fixture × every (Δz, child) agreed semantically; nonzero =
// at least one discrepancy (printed with a per-field diff). Add real production
// tiles by pointing REAL_PMTILES at a local *.pmtiles archive (optional).
import { gunzipSync } from "node:zlib";
import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync, readFileSync, existsSync, openSync, readSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, isAbsolute, resolve } from "node:path";
import { execSync } from "node:child_process";
import { decodeTile, encodeTile, synthesizeLayers, POINT, LINE, POLYGON } from "../src/vector.ts";

// ── locate the binary ─────────────────────────────────────────────────────────
function findBinary() {
  const env = process.env.OVERZOOM_BIN;
  if (env) {
    if (!existsSync(env)) fail(`OVERZOOM_BIN=${env} does not exist`);
    return env;
  }
  try {
    return execSync("command -v tippecanoe-overzoom", { encoding: "utf8" }).trim();
  } catch {
    fail(
      "tippecanoe-overzoom not found. Build it:\n" +
        "  git clone --depth 1 https://github.com/felt/tippecanoe.git\n" +
        "  cd tippecanoe && make -j4 tippecanoe-overzoom\n" +
        "then set OVERZOOM_BIN=/abs/path/to/tippecanoe-overzoom",
    );
  }
}
function fail(msg) {
  console.error(msg);
  process.exit(2);
}
const BIN = findBinary();
const WORK = mkdtempSync(join(tmpdir(), "oracle-"));

// ── fixture builders (decoded representation; polygon rings closed) ────────────
const M = 1,
  L = 2;
function poly(id, rings, properties) {
  const ops = [];
  for (const ring of rings) {
    const closed =
      ring[0][0] === ring[ring.length - 1][0] && ring[0][1] === ring[ring.length - 1][1] ? ring : [...ring, ring[0]];
    for (let i = 0; i < closed.length; i++) ops.push({ op: i ? L : M, x: closed[i][0], y: closed[i][1] });
  }
  return { id, type: POLYGON, properties, ops };
}
function line(id, pts, properties) {
  return { id, type: LINE, properties, ops: pts.map((p, i) => ({ op: i ? L : M, x: p[0], y: p[1] })) };
}
function point(id, x, y, properties) {
  return { id, type: POINT, properties, ops: [{ op: M, x, y }] };
}
function multipoint(id, pts, properties) {
  return { id, type: POINT, properties, ops: pts.map(([x, y]) => ({ op: M, x, y })) };
}
const layer = (name, features, extent = 4096, version = 2) => ({ name, version, extent, features });
const src = (z, x, y, layers) => ({ z, x, y, layers });
const FULL = [
  [0, 0],
  [4096, 0],
  [4096, 4096],
  [0, 4096],
];

// ── run the binary: encode each source → temp pbf, overzoom, gunzip, decode ────
let runSeq = 0;
function runBinary(sources, nz, nx, ny) {
  const args = ["-o", join(WORK, `out-${runSeq}.mvt`), "-t", `${nz}/${nx}/${ny}`];
  sources.forEach((s, i) => {
    const file = join(WORK, `in-${runSeq}-${i}.mvt`);
    writeFileSync(file, encodeTile(s.layers));
    args.push(file, `${s.z}/${s.x}/${s.y}`);
  });
  runSeq++;
  const outfile = args[1];
  execFileSync(BIN, args, { stdio: ["ignore", "ignore", "pipe"] });
  const raw = readFileSync(outfile);
  if (raw.length === 0) return [];
  const bytes = raw[0] === 0x1f && raw[1] === 0x8b ? gunzipSync(raw) : raw;
  return decodeTile(bytes);
}
function runPort(sources, nz, nx, ny) {
  // Round-trip through the wire so the port is compared exactly as it serves.
  return decodeTile(encodeTile(synthesizeLayers(sources, nz, nx, ny)));
}

// ── canonicalization (ordering that is encoder-noise, not semantics) ───────────
const ptKey = (p) => `${p.x},${p.y}`;
function ringsOf(ops) {
  const rings = [];
  let cur = null;
  for (const p of ops) {
    if (p.op === M) {
      cur = [];
      rings.push(cur);
    }
    cur.push(p);
  }
  return rings;
}
// Rotation-invariant signature of a closed ring (drop closing repeat, then the
// lexicographically smallest rotation). Winding is NOT normalized away — a
// reversed outer ring is a real (hole-vs-fill) difference and must surface.
function ringSig(ring) {
  const pts = ring.slice();
  if (pts.length > 1 && ptKey(pts[0]) === ptKey(pts[pts.length - 1])) pts.pop();
  const keys = pts.map(ptKey);
  let best = null;
  for (let i = 0; i < keys.length; i++) {
    const rot = keys.slice(i).concat(keys.slice(0, i)).join(" ");
    if (best === null || rot < best) best = rot;
  }
  return best ?? "";
}
function geomSig(type, ops) {
  const rings = ringsOf(ops);
  if (type === POINT) return rings.map((r) => r.map(ptKey).join(",")).sort().join(" ; ");
  if (type === POLYGON) return rings.map(ringSig).sort().join(" ; ");
  // LINE: each subpath ordered, but its start/direction and subpath order are
  // encoder-free; canonicalize each to min(forward, reversed), then sort.
  return rings
    .map((r) => {
      const f = r.map(ptKey).join(">");
      const b = r.slice().reverse().map(ptKey).join(">");
      return f < b ? f : b;
    })
    .sort()
    .join(" ; ");
}
function propsSig(props) {
  return Object.keys(props)
    .sort()
    .map((k) => `${k}=${typeof props[k]}:${JSON.stringify(props[k])}`)
    .join("|");
}

// For POLYGONS the semantic invariant is the FILLED REGION, not the ring
// structure: the overzoom binary runs full wagyu (clean_or_clip_poly, positive
// fill), which merges a hole opened by the clip box into one ring and drops a
// fully-cancelled feature — structural normalizations the port deliberately does
// not carry (see the wagyu note in vector.ts). Both render identically, so we
// compare polygons by sampling MVT's nonzero-winding fill over the buffered
// extent. Lines/points are compared exactly by vertex set (that IS their
// semantics). Sampling bounds: [-buffer, extent+buffer] with a fine step.
const SAMPLE_LO = -80,
  SAMPLE_HI = 4176,
  SAMPLE_STEP = 16;
function fillCells(ops) {
  const rings = ringsOf(ops);
  const cells = new Set();
  for (let y = SAMPLE_LO; y <= SAMPLE_HI; y += SAMPLE_STEP) {
    for (let x = SAMPLE_LO; x <= SAMPLE_HI; x += SAMPLE_STEP) {
      let w = 0;
      for (const r of rings) {
        for (let i = 0; i < r.length - 1; i++) {
          const a = r[i],
            b = r[i + 1];
          if (a.y <= y !== b.y <= y) {
            const xc = a.x + ((y - a.y) / (b.y - a.y)) * (b.x - a.x);
            if (xc > x) w += b.y > a.y ? 1 : -1;
          }
        }
      }
      if (w !== 0) cells.add(`${x},${y}`);
    }
  }
  return cells;
}
function fillEqual(opsA, opsB) {
  const a = fillCells(opsA),
    b = fillCells(opsB);
  if (a.size !== b.size) return false;
  for (const c of a) if (!b.has(c)) return false;
  return true;
}
const isEmptyFill = (type, ops) => type === POLYGON && fillCells(ops).size === 0;

// A layer → array of per-feature canonical records. Empty layers are dropped
// (the binary omits them; the port keeps a shell — not a semantic difference).
function canonLayers(layers) {
  const map = new Map();
  for (const l of layers) {
    if (l.features.length === 0) continue;
    map.set(
      l.name,
      l.features.map((f) => ({
        id: f.id,
        type: f.type,
        ops: f.ops,
        geom: geomSig(f.type, f.ops),
        props: propsSig(f.properties),
      })),
    );
  }
  return map;
}

// ── compare one case; return list of human-readable discrepancies ──────────────
function diffCase(portLayers, binLayers, label) {
  const problems = [];
  const port = canonLayers(portLayers);
  const bin = canonLayers(binLayers);
  const names = new Set([...port.keys(), ...bin.keys()]);
  for (const name of names) {
    const pf = port.get(name);
    const bf = bin.get(name);
    // A polygon feature that renders nothing (fill fully cancelled) is dropped by
    // the binary's wagyu but the port may still emit its (net-zero) rings — not a
    // semantic difference. Filter those from the sole-side accounting.
    const nonEmpty = (arr) => arr.filter((f) => !isEmptyFill(f.type, f.ops));
    if (!pf) {
      const b = nonEmpty(bf);
      if (b.length) problems.push(`layer "${name}": present in binary (${b.length} feats), absent in port`);
      continue;
    }
    if (!bf) {
      const p = nonEmpty(pf);
      if (p.length) problems.push(`layer "${name}": present in port (${p.length} feats), absent in binary`);
      continue;
    }
    // Pair features: prefer id, else by geom signature.
    const byId = pf.every((f) => f.id !== undefined) && bf.every((f) => f.id !== undefined);
    const key = (f) => (byId ? String(f.id) : f.geom);
    const binByKey = new Map();
    for (const f of bf) (binByKey.get(key(f)) ?? binByKey.set(key(f), []).get(key(f))).push(f);
    for (const p of pf) {
      const cands = binByKey.get(key(p));
      if (!cands || cands.length === 0) {
        // No counterpart is only OK for a polygon that renders nothing.
        if (isEmptyFill(p.type, p.ops)) continue;
        problems.push(`layer "${name}" feat ${byId ? `id=${p.id}` : "geom"}: no binary match (geom=${short(p.geom)})`);
        continue;
      }
      // Prefer an exact match, else one whose polygon fill agrees, else first.
      let match =
        cands.find((c) => c.geom === p.geom && c.props === p.props && c.type === p.type) ??
        cands.find((c) => c.type === POLYGON && p.type === POLYGON && fillEqual(c.ops, p.ops)) ??
        cands[0];
      cands.splice(cands.indexOf(match), 1);
      if (p.type !== match.type) problems.push(`layer "${name}" id=${p.id}: type port=${p.type} binary=${match.type}`);
      if (byId && p.id !== match.id) problems.push(`layer "${name}": id port=${p.id} binary=${match.id}`);
      if (p.props !== match.props)
        problems.push(`layer "${name}" id=${p.id}: props differ\n    port:   ${p.props}\n    binary: ${match.props}`);
      // Geometry: exact vertex set for lines/points; filled region for polygons.
      const geomOk =
        p.geom === match.geom ||
        (p.type === POLYGON && match.type === POLYGON && fillEqual(p.ops, match.ops));
      if (!geomOk)
        problems.push(
          `layer "${name}" id=${p.id}: geometry differs\n    port:   ${short(p.geom)}\n    binary: ${short(match.geom)}`,
        );
    }
    // Any binary feature with no port counterpart and a real fill is a miss.
    for (const [, rem] of binByKey)
      for (const c of rem)
        if (!isEmptyFill(c.type, c.ops))
          problems.push(`layer "${name}" ${byId ? `id=${c.id}` : "geom"}: in binary, no port match`);
  }
  return problems;
}
const short = (s) => (s.length > 200 ? s.slice(0, 200) + `… (${s.length} ch)` : s);

// ── the fixture set ────────────────────────────────────────────────────────────
// Each fixture is { name, sources } and lists the (nz,nx,ny) children to test.
function childrenOf(z, x, y, dz) {
  // enumerate the 2^dz × 2^dz children, but cap to corners + center + edges to
  // keep the matrix bounded at large Δz.
  const nz = z + dz;
  const span = 1 << dz;
  const idxs = span <= 2 ? [...Array(span).keys()] : [0, 1, span >> 1, span - 2, span - 1];
  const out = [];
  for (const ix of idxs) for (const iy of idxs) out.push([nz, (x << dz) + ix, (y << dz) + iy]);
  return out;
}

const P = { drval1: 0, drval2: 5, sys: "m", rank: 3, name: "shoal" }; // mixed types
const fixtures = [];

// 1. full-tile polygon (fills child + buffer on every side)
fixtures.push({ name: "poly-full", z: 10, x: 0, y: 0, sources: [src(10, 0, 0, [layer("depare", [poly(1, [FULL], P)])])] });

// 2. depare partition: two adjacent fills sharing an off-grid seam (x=1536) plus
//    a third band, all with distinct rank — the partition-contract canary.
fixtures.push({
  name: "depare-partition",
  z: 10,
  x: 0,
  y: 0,
  sources: [
    src(10, 0, 0, [
      layer("depare", [
        poly(1, [[[0, 0], [1536, 0], [1536, 4096], [0, 4096]]], { drval1: 0, drval2: 5, rank: 1 }),
        poly(2, [[[1536, 0], [2800, 0], [2800, 4096], [1536, 4096]]], { drval1: 5, drval2: 10, rank: 2 }),
        poly(3, [[[2800, 0], [4096, 0], [4096, 4096], [2800, 4096]]], { drval1: 10, drval2: 20, rank: 3 }),
      ]),
    ]),
  ],
});

// 3. tile-edge polygon that already fills its own baked buffer [-80,4176]^2
fixtures.push({
  name: "poly-edge-buffer",
  z: 10,
  x: 0,
  y: 0,
  sources: [
    src(10, 0, 0, [
      layer("depare", [poly(7, [[[-80, -80], [4176, -80], [4176, 4176], [-80, 4176]]], { rank: 0 })]),
    ]),
  ],
});

// 4. polygon with a hole (interior ring) — winding / ring-repair under scale-down
fixtures.push({
  name: "poly-with-hole",
  z: 10,
  x: 0,
  y: 0,
  sources: [
    src(10, 0, 0, [
      layer("depare", [
        poly(
          8,
          [
            [[200, 200], [3800, 200], [3800, 3800], [200, 3800]],
            [[1200, 1200], [1200, 2800], [2800, 2800], [2800, 1200]], // reversed winding = hole
          ],
          { rank: 4 },
        ),
      ]),
    ]),
  ],
});

// 5. lines: a diagonal, a horizontal at a child seam, a zig-zag exiting/re-entering
fixtures.push({
  name: "lines",
  z: 10,
  x: 0,
  y: 0,
  sources: [
    src(10, 0, 0, [
      layer("contours", [
        line(10, [[0, 0], [4096, 4096]], { depth_m: 10, depth_ft: 32.8, sys: "m", label: "10 m" }),
        line(11, [[0, 1024], [4096, 1024]], { depth_m: 3, sys: "m" }),
        line(12, [[100, 100], [2000, 3000], [3000, 500], [4000, 4000]], { depth_m: 20, sys: "m" }),
      ]),
    ]),
  ],
});

// 6. points / multipoint soundings with float + int + string props
fixtures.push({
  name: "soundings",
  z: 10,
  x: 0,
  y: 0,
  sources: [
    src(10, 0, 0, [
      layer("soundings", [
        point(20, 2560, 2560, { depth_m: 12.5, qual: "A" }),
        point(21, 100, 4000, { depth_m: 3, qual: "B" }),
        multipoint(22, [[500, 500], [512, 3600], [3600, 3600]], { depth_m: 7.1, qual: "C" }),
      ]),
    ]),
  ],
});

// 7. multi-parent: neighbour sources leafed at different depths feeding one child
fixtures.push({
  name: "multi-parent",
  z: 12,
  x: 1,
  y: 0,
  fixedChildren: [[12, 1, 0]],
  sources: [
    src(10, 0, 0, [layer("depare", [poly(1, [FULL], { rank: 1 })])]),
    src(11, 0, 0, [
      layer("depare", [poly(2, [FULL], { rank: 2 })]),
      layer("soundings", [point(3, 3072, 100, { depth_m: 7 })]),
    ]),
  ],
});

// ── real production tiles (optional) ───────────────────────────────────────────
async function realCases() {
  // REAL_PMTILES (absolute or cwd-relative) wins; otherwise probe a few known
  // local build outputs. The archive is optional — real tiles enrich the matrix
  // but the harness must not block on data access (plan Verification note).
  const candidates = process.env.REAL_PMTILES
    ? [process.env.REAL_PMTILES]
    : [
        join(import.meta.dirname, "../../output/contours_7.5,53.5,13.5,58.5.pmtiles"),
        join(import.meta.dirname, "../output/contours_7.5,53.5,13.5,58.5.pmtiles"),
      ];
  const abs = candidates.map((c) => (isAbsolute(c) ? c : resolve(process.cwd(), c))).find((c) => existsSync(c));
  if (!abs) {
    console.log(`(no real pmtiles found; skipping real-tile cases — set REAL_PMTILES to add them)`);
    return [];
  }
  const { PMTiles } = await import("pmtiles");
  const fd = openSync(abs, "r");
  const source = {
    getKey: () => abs,
    getBytes: async (offset, length) => {
      const b = Buffer.alloc(length);
      readSync(fd, b, 0, length, offset);
      return { data: b.buffer.slice(b.byteOffset, b.byteOffset + length) };
    },
  };
  const p = new PMTiles(source);
  const h = await p.getHeader();
  const lon2tile = (lon, z) => Math.floor(((lon + 180) / 360) * (1 << z));
  const lat2tile = (lat, z) => {
    const r = (lat * Math.PI) / 180;
    return Math.floor(((1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2) * (1 << z));
  };
  const cases = [];
  // Pull real parent tiles at a couple of zooms in the archive's populated area.
  for (const [lon, lat, z] of [
    [10.5, 54.5, 8],
    [10.5, 54.5, 9],
    [8.2, 54.0, 9],
  ]) {
    if (z > h.maxZoom) continue;
    const x = lon2tile(lon, z),
      y = lat2tile(lat, z);
    const t = await p.getZxy(z, x, y);
    if (!t) continue;
    const raw = Buffer.from(t.data);
    const bytes = raw[0] === 0x1f && raw[1] === 0x8b ? gunzipSync(raw) : raw;
    const layers = decodeTile(bytes);
    if (layers.every((l) => l.features.length === 0)) continue;
    cases.push({ name: `real-${z}/${x}/${y}`, z, x, y, sources: [{ z, x, y, layers }] });
  }
  return cases;
}

// ── driver ─────────────────────────────────────────────────────────────────────
async function main() {
  console.log(`oracle-overzoom: binary = ${BIN}\n`);
  const all = [...fixtures, ...(await realCases())];
  let cases = 0,
    failed = 0;
  const matrix = [];
  for (const fx of all) {
    const children = fx.fixedChildren ?? [
      // Δz = 0 (leaf clean pass), 1, 2, 3, 5 (large), with edge-adjacent children.
      ...[0, 1, 2, 3, 5].flatMap((dz) => childrenOf(fx.z, fx.x, fx.y, dz)),
    ];
    let fxFail = 0;
    for (const [nz, nx, ny] of children) {
      cases++;
      let portLayers, binLayers;
      try {
        binLayers = runBinary(fx.sources, nz, nx, ny);
        portLayers = runPort(fx.sources, nz, nx, ny);
      } catch (e) {
        console.error(`  ${fx.name} → ${nz}/${nx}/${ny}: ERROR ${e.message}`);
        failed++;
        fxFail++;
        continue;
      }
      const problems = diffCase(portLayers, binLayers, `${fx.name} ${nz}/${nx}/${ny}`);
      if (problems.length) {
        failed++;
        fxFail++;
        console.error(`✗ ${fx.name} → ${nz}/${nx}/${ny} (Δz=${nz - fx.z})`);
        for (const p of problems) console.error(`    ${p}`);
      }
    }
    matrix.push(`  ${fx.name.padEnd(20)} ${children.length} children  ${fxFail ? `✗ ${fxFail} failed` : "✓"}`);
  }
  console.log("\nComparison matrix (fixture × children over Δz∈{0,1,2,3,5}):");
  console.log(matrix.join("\n"));
  console.log(`\n${cases - failed}/${cases} cases agreed semantically.`);
  process.exit(failed ? 1 : 0);
}
main();
