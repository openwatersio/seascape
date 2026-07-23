// Fixed-grid overlay routing. Overlay archives are one per populated cell of the
// build's OVERLAY_SPLIT_Z grid (see pipelines/bundle.py), so the archive holding a
// tile is computed from the tile address — no footprint search, no source identity.
export interface OverlayIndex {
  split_z: number;
  cells: Record<string, number>; // "z-x-y" cell -> its deepest zoom
}

/** The overlay archive holding (z,x,y) and its max zoom, or null if the tile's
 * cell is unpopulated. Callers only ask for z above the planet cap, which is at
 * or deeper than split_z (bundle.py enforces split_z <= cap + 1). */
export function overlayFor(
  overlay: OverlayIndex,
  z: number,
  x: number,
  y: number,
): { file: string; maxZoom: number } | null {
  const d = z - overlay.split_z;
  const cell = `${overlay.split_z}-${x >> d}-${y >> d}`;
  const maxZoom = overlay.cells[cell];
  return maxZoom === undefined
    ? null
    : { file: `overlay-${cell}.pmtiles`, maxZoom };
}

/** Preview routing. On the preview Worker (bound to the data bucket) the leading
 * path segment is a build's git sha, and that build's bundle lives at
 * data:bathymetry/build/<sha>/. Peel the sha off `rel` and return the R2 prefix,
 * the remaining tile path, and the sha-folded mount (so emitted TileJSON/style
 * URLs point back correctly) — or null if the segment is absent/malformed (→ 404).
 * The 7–40 hex bound validates the sha AND stops a bare z/x/y path (single digits)
 * from being mistaken for one. A `-bbox` suffix selects a regional bbox build's
 * stage (build.yml stages those under build/<sha>-bbox/, unpromotable by release). */
export function previewRoute(
  rel: string,
  mount: string,
): { prefix: string; rel: string; mount: string } | null {
  const s = rel.match(/^\/([0-9a-f]{7,40}(?:-bbox)?)(?=\/|$)/);
  if (!s) return null;
  const sha = s[1];
  return {
    prefix: `bathymetry/build/${sha}/`,
    rel: rel.slice(sha.length + 1) || "/",
    mount: `${mount}/${sha}`,
  };
}
