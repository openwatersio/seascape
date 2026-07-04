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
