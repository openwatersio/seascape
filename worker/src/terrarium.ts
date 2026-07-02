// Terrarium elevation packing: height = R*256 + G + B/256 - 32768 (metres).
// Resampling Terrarium safely means decode → interpolate height → re-encode; the
// packed bytes themselves can't be averaged (G wraps at 256 and corrupts the height).

export function unpackTerrarium(r: number, g: number, b: number): number {
  return r * 256 + g + b / 256 - 32768;
}

// Cubic B-spline (approximating) over the 4-tap neighbourhood; t in [0,1] between p1,p2.
// The four weights are non-negative and sum to 1, so every output is a convex blend of the
// taps — it can't overshoot, so a sharp shelf edge can't ring into a halo (unlike Catmull-
// Rom / cubic-convolution, whose negative lobes are exactly the ring). It's also C2, the
// smoothest of the practical kernels, so the derived hillshade has no slope creases. The
// trade: it's non-interpolating, so it smooths (blurs) rather than passing through the
// samples — it still reproduces flats and linear ramps exactly, only curvature is rounded.
// This is the kernel GDAL calls `cubicspline`.
export function cubicBSpline(
  p0: number,
  p1: number,
  p2: number,
  p3: number,
  t: number,
): number {
  const t2 = t * t,
    t3 = t2 * t;
  const w0 = (1 - 3 * t + 3 * t2 - t3) / 6, // (1-t)^3 / 6
    w1 = (4 - 6 * t2 + 3 * t3) / 6,
    w2 = (1 + 3 * t + 3 * t2 - 3 * t3) / 6,
    w3 = t3 / 6;
  return w0 * p0 + w1 * p1 + w2 * p2 + w3 * p3;
}

// Writes R,G,B,A straight into an RGBA buffer at byte offset `di` (avoids a per-pixel
// array allocation in the overzoom loop — ~260k pixels/tile).
export function packTerrariumInto(
  out: Uint8ClampedArray,
  di: number,
  height: number,
): void {
  let v = Math.round((height + 32768) * 256); // height in 1/256 m above the -32768 datum
  if (v < 0) v = 0;
  else if (v > 0xffffff) v = 0xffffff;
  out[di] = (v >> 16) & 0xff;
  out[di + 1] = (v >> 8) & 0xff;
  out[di + 2] = v & 0xff;
  out[di + 3] = 255;
}
