# Shallow-band coarsening for marsh coasts — STUB

_Written 2026-07-30. **This is a stub — the mechanism is sketched and the safety argument is
worked out, but the parameters, gates, and cartographic review are not.** Do not implement
from this without finishing the open questions._

## Why

At native z15 (2.4 m/px) a fragmented marsh coast resolves thousands of individual mud
islands, ponds, and bayou fingers, and every one becomes its own ring in the 0 m band. The
Louisiana delta is the worst case in the covering:

| stem | bbox (W,S,E,N) | what happened |
| --- | --- | --- |
| `8-63-105-15` | `-91.4062,29.5352,-90.0000,30.7513` | Atchafalaya → Barataria. 9.94 GB window, 2.5 h, 33 GB peak, then failed |
| `8-63-106-15` | `-91.4062,28.3044,-90.0000,29.5352` | delta mouth. failed |
| `8-64-106-15` | `-90.0000,28.3044,-88.5938,29.5352` | Birdfoot → Breton Sound. failed |
| `8-64-105-15` | `-90.0000,29.5352,-88.5938,30.7513` | Pontchartrain. 30.6 GB, 2 h 19 m, OK |
| `8-61-105-15` | `-94.2188,29.5352,-92.8125,30.7513` | Galveston. 209,481 polygons, OK |

Measured costs on this class (run 30506340413, ccx63):

- Depth-band crossings per raster row concentrate almost entirely in the shallow ladder:
  **0 m = 7.8, −2 m = 7.4, −5 m = 4.8**, against ~1–3 for every level deeper than −10 m.
- The whole partition set reaches **730 MB** for one stem, with 40k+ parts in a single band
  (vs. a few thousand for NY harbor).
- Three stems produced a single band feature over GDAL's 200 MB GeoJSON ceiling and could not
  be written at all until `OGR_GEOJSON_MAX_OBJ_SIZE` was lifted (`93da2d3`). That fix stops
  the failure; it does not make these stems cheap.

**Navigationally this detail carries no information.** Which particular mud island is which in
Terrebonne Bay does not matter to a mariner; the channels *through* the marsh do. This is the
same argument `deep_coarsen` already makes below −250 m, applied at the other end of the
ladder.

## Mechanism (sketch)

Mirror `smooth.deep_coarsen` with the comparison inverted and the statistic changed:

```python
# deep (existing): pixels DEEPER than the threshold take their block MEAN
mask = (arr != nd) & (arr <= threshold) & (up != nd) & (up <= threshold)   # up = block mean

# shallow (proposed): pixels SHALLOWER than the threshold take their block MAX
mask = (arr != nd) & (arr >= threshold) & (up != nd) & (up >= threshold)   # up = block max
```

Three properties follow:

1. **Bias-shallow by construction.** Block *maximum* elevation is the shallowest value in the
   block, so a coarsened pixel is only ever charted shallower than truth. The deep version's
   block *mean* must NOT be reused here: averaging a 1 m marsh with a 5 m hole charts the
   marsh at 3 m — deeper than reality, and unsafe.
2. **Channels survive pixel-exactly.** `arr >= threshold` means a pixel deeper than the
   threshold is never touched, whatever its neighbours are. With a −2 m threshold the GIWW and
   the bayou channels keep full resolution while the marsh around them flattens. This is the
   same failure mode as the land clamp erasing the ICW ([[icw-landmask-clamp-gap]]) and must
   be verified, not assumed.
3. **Seams still match.** Reuse the origin-anchored block grid, so overlapping neighbour
   windows coarsen identical blocks and band edges still abut at macrotile seams.

It belongs in the shared `fork_window`, not in depare alone: band edges and contour lines
coincide by construction, so coarsening one and not the other breaks that invariant. Contours
and soundings therefore change too — soundings stay safe for the same bias-shallow reason
(they take the shallowest value in a cell, and this only shoals).

## Open questions — none of these are settled

1. **Threshold.** −2 m is a guess chosen to sit on a ladder level and clear the GIWW's 3.6 m
   project depth. Needs a cartographic decision, not a round number: what is the shallowest
   depth a chart consumer might navigate, and does the answer differ by vessel class? Note the
   threshold interacts with `DRYING_CAP` and the 0 m band, which share edges.
2. **Factor.** `deep_coarsen` uses 8×. At z15 that is 19 m blocks — coarser than real marsh
   features. 4× (9.6 m) is the suggested start, but nobody has looked at rendered output at
   either setting.
3. **Does it actually fix the cost?** Unmeasured. Run it on the preserved delta windows
   (`store/profile/depare-pathology/`, plus the Gulf windows on the volume) and measure part
   count, partition-set size, peak RSS, and wall before committing to it.
4. **Does it erase anything real?** The channel-preservation argument is structural but
   untested. Required: a rendered A/B over the delta bboxes above, specifically checking the
   GIWW, Bayou Lafourche, and the Southwest Pass approaches still read as continuous water.
5. **Interaction with drying.** Drying is `[0, DRYING_CAP]` cut against effective land, and its
   0 m edge is shared with the metre bands. Coarsening across that edge could open cracks —
   the drying redesign ([[drying-geometry-plan]]) rebuilds this geometry anyway, so the two
   should be sequenced together or explicitly kept apart.
6. **Alternative not yet compared:** post-polygonization generalization (merge parts below an
   area threshold) instead of raster coarsening. Morphologically it must be a *closing* —
   filling gaps is safe, dropping a shoal is not — which makes it near-equivalent to block-max
   coarsening but operating on vectors. Cheaper to gate, harder to keep seam-exact.

## Gates before it ships

- `ab_depare.py` decoded A/B on the delta stems: band sets identical, and every geometry change
  strictly shoaling (charted ≤ true) — a hard assertion, not a tolerance.
- `seam_check check_depare` and `check_contours` across a coarsened/uncoarsened boundary.
- Rendered eyeball over the five bboxes in the table, channels specifically.
- Full fork rebuild: this invalidates window, contour, soundings, and depare for every stem at
  or above the child_z gate. Sequence it with another change that already forces that rebuild
  rather than paying for it twice.

## Status

Not started. Approved in principle 2026-07-30 ("no value in preserving detail of these shallow
marshes"); the mechanism above is a sketch from a code read of `smooth.deep_coarsen`, not a
tested design.
