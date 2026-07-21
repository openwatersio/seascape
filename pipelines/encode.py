"""Terrarium tile encoding with per-zoom vertical quantization.

Adapted from mapterhorn's ``save_terrarium_tile`` (BSD-3, (c) 2025 mapterhorn —
see LICENSE.mapterhorn). Replaces the old Mapbox Terrain-RGB encoding
(``rio rgbify -b -10000 -i 0.1``).

Terrarium packs an elevation (metres) into an RGB triple with a 1/256 m LSB:

    height = R*256 + G + B/256 - 32768

The +32768 offset keeps the byte arithmetic non-negative for our full
bathymetric range (down to -10000 m), so depths need no special handling.

Per-zoom quantization rounds the height to a multiple of

    factor(z) = 2**(19 - z) / 256

so coarse zooms (deep abyssal plains, viewed small) cost few bits while fine
zooms (shallow coastal detail) keep full precision. At z<=19 the rounded value
is an exact multiple of the 1/256 LSB, so the RGB packing is lossless.

Bathymetry adaptation — *depth-aware step cap*. The zoom schedule alone was
tuned for terrain (kilometres of relief); applied to a shelf it terraces the
shallows: a region whose best source tops out at z10 bakes 2 m steps into
water that is itself only a few metres deep, and on a gentle seabed (1:1000)
every step is a plateau hundreds of metres wide — blocky shorelines at the
very zooms mariners use. So the step is capped per-pixel at SHALLOW_REL of the
local depth (floored at SHALLOW_MIN_STEP, snapped up to a power of two so it
stays a multiple of the LSB): deep water keeps the byte-saving coarse steps,
shallow water keeps chart detail at every zoom.

Bathymetry adaptation — *conservative rounding*. A chart must bias shallow:
charted depth <= true depth. So by default we round the height
*toward shallower* (ceil, i.e. toward the surface / less-negative), guaranteeing
the decoded elevation is never deeper than the truth. The cost is an up-to-one-
step shallow bias, which is sub-perceptual at the zoom each step is applied
(z0 step = 2048 m on a 0..-10000 m range; z12 step = 0.5 m). Pass
``conservative=False`` for minimal-error round-to-nearest.

One exception to the pure rounding: water never quantizes to 0. The published
non-negative domain is categorical (terrain.py: 0 unknown-depth water, 1 drying,
2 land), so a negative input that would round to >= 0 — water shallower than its
local per-pixel step, i.e. min(zoom step, depth cap), at most SHALLOW_MIN_STEP
and smaller still at fine zooms — is capped at -LSB instead. Still shoal-biased;
the only inputs it deepens are true depths shallower than the ~4 mm LSB, which
are noise.
"""

import numpy as np

FULL_RESOLUTION_ZOOM = 19
TERRARIUM_OFFSET = 32768.0
LSB = 1.0 / 256.0  # Terrarium's vertical resolution (metres)

# Depth-aware step cap: never quantize coarser than this fraction of the local
# depth, floored at SHALLOW_MIN_STEP. 1/16 keeps the step within one ramp band
# everywhere (band edges 2/5/10/20/50 m); 0.25 m matches the shoal-band label
# precision (one decimal below 6 m).
SHALLOW_REL = 1.0 / 16.0
SHALLOW_MIN_STEP = 0.25  # metres; a power of two, so an exact multiple of LSB


def quantization_factor(z):
    """Vertical step (metres) at zoom ``z``."""
    return 2.0 ** (FULL_RESOLUTION_ZOOM - z) / 256.0


def quantize(data, z, conservative=True):
    """Round elevations (metres) to the per-zoom vertical step, capped per-pixel
    at SHALLOW_REL of the local depth (see module docstring) so shallow water
    never terraces at coarse zooms.

    conservative=True rounds toward shallower (never deepens); False is
    round-to-nearest. Water stays water either way: a negative input that
    would round to >= 0 (land) caps at -LSB instead.
    """
    factor = quantization_factor(z)
    data = np.asarray(data, dtype=np.float64)
    # Snap the depth cap UP to a power of two so every step is a power-of-two
    # multiple of the LSB and the RGB packing stays lossless.
    depth_cap = 2.0 ** np.ceil(np.log2(np.maximum(np.abs(data) * SHALLOW_REL, SHALLOW_MIN_STEP)))
    step = np.minimum(factor, depth_cap)
    scaled = data / step
    rounded = (np.ceil(scaled) if conservative else np.round(scaled)) * step
    return np.where((data < 0) & (rounded >= 0), -LSB, rounded)


def encode(data, z, conservative=True):
    """Elevation array (metres, any shape) -> uint8 Terrarium RGB (..., 3).

    Values must lie within [-32768, 32767]; bathymetry (-10000..~9000) is safe.
    """
    height = quantize(data, z, conservative) + TERRARIUM_OFFSET
    floor = np.floor(height)
    rgb = np.empty(height.shape + (3,), dtype=np.uint8)
    rgb[..., 0] = (floor // 256).astype(np.uint8)
    rgb[..., 1] = (floor % 256).astype(np.uint8)
    # fractional metre -> 1/256 count (0..255); exact integer for z<=19.
    rgb[..., 2] = np.round((height - floor) * 256.0).astype(np.uint8)
    return rgb


def decode(rgb):
    """Terrarium RGB (..., 3) -> elevation array (metres)."""
    rgb = np.asarray(rgb, dtype=np.float64)
    return rgb[..., 0] * 256.0 + rgb[..., 1] + rgb[..., 2] / 256.0 - TERRARIUM_OFFSET


def save_terrarium_tile(data, z, filepath, conservative=True):
    """Encode a 512x512 elevation array and write it as a lossless WebP tile.

    imagecodecs is imported lazily so the pure-array functions above (and their
    self-check) run with numpy alone.
    """
    import imagecodecs

    rgb = encode(data, z, conservative)
    with open(filepath, "wb") as f:
        f.write(imagecodecs.webp_encode(rgb, lossless=True))


def _check():
    # Representative bathymetric depths + land, and the zoom range we tile.
    depths = np.array([-10000, -5000, -1000, -200, -100, -50, -10, -5, -1, 0, 100], float)
    zooms = range(0, 16)

    for z in zooms:
        step = quantization_factor(z)
        rgb = encode(depths, z, conservative=True)
        back = decode(rgb)
        # Lossless round-trip of the quantized value.
        q = quantize(depths, z, conservative=True)
        assert np.allclose(back, q, atol=1e-9), (z, back, q)
        # Never deepen: decoded elevation >= true (charted depth <= true depth).
        assert np.all(back >= depths - 1e-9), (z, depths[back < depths - 1e-9])
        # Shallow bias is bounded by one quantization step.
        assert np.all(back - depths <= step + 1e-9), (z, np.max(back - depths), step)
        # Water stays water: negative input never quantizes to 0/land.
        assert np.all(back[depths < 0] < 0), (z, back)
        # Bytes are valid and decode is exact for a dense random field too.
        rnd = np.random.default_rng(z).uniform(-10000, 100, size=(64, 64))
        rgb2 = encode(rnd, z, conservative=True)
        assert rgb2.dtype == np.uint8 and rgb2.min() >= 0 and rgb2.max() <= 255
        back2 = decode(rgb2)
        # Never-deepen holds except sub-LSB water, which caps at -LSB.
        assert np.all((back2 >= rnd - 1e-9) | ((rnd > -LSB) & (rnd < 0)))
        assert np.all(back2[rnd < 0] < 0), z

    # Non-conservative round-to-nearest CAN deepen — confirms the modes differ.
    e = np.array([-100.0 - 3.0])  # z9 step 4 m at ~100 m depth (cap 8 m > factor 4 m)
    assert decode(encode(e, 9, conservative=False))[0] < e[0]
    assert decode(encode(e, 9, conservative=True))[0] >= e[0]

    # Depth-aware cap: shallow water keeps fine steps at coarse zooms instead of
    # terracing. -3.7 m at z10 (zoom step 2 m) quantizes at the 0.25 m floor;
    # deep water is untouched by the cap (-1000 m at z6: 32 m zoom step wins).
    assert decode(encode(np.array([-3.7]), 10))[0] == -3.5
    assert decode(encode(np.array([-1000.0]), 6))[0] == -992.0  # ceil(-1000/32)*32

    # The water floor: water shallower than the local step, which would round to
    # 0 (a category code), caps at -LSB and survives the RGB round-trip exactly.
    assert decode(encode(np.array([-0.2]), 9))[0] == -LSB
    assert decode(encode(np.array([-0.001]), 12))[0] == -LSB

    # The published category codes (terrain.py: 0 unknown water, 1 drying, 2 land) survive
    # quantization exactly at every zoom — multiples of the 0.25 m floor, and ceil is exact there.
    for z in range(0, 16):
        assert np.array_equal(decode(encode(np.array([0.0, 1.0, 2.0]), z)),
                              [0.0, 1.0, 2.0]), z

    # Render-path contract (terrain.py clamps land to the sentinel DRYING_CAP+1 and nudges
    # land-side exact-0 to +LSB before this encode): at EVERY zoom the sentinel decodes above the
    # cap (so v > DRYING_CAP => land holds despite ceil rounding lifting +17 to +18 at coarse
    # zooms), exact 0 decodes exactly 0 (water of unknown depth), and +LSB decodes strictly
    # positive (land). config's cap is the anchor.
    import config
    for z in range(0, FULL_RESOLUTION_ZOOM + 1):
        assert decode(encode(np.array([config.DRYING_CAP + 1.0]), z))[0] > config.DRYING_CAP, z
        assert decode(encode(np.array([0.0]), z))[0] == 0.0, z
        assert decode(encode(np.array([LSB]), z))[0] > 0.0, z

    print("encode.py self-check ok")


if __name__ == "__main__":
    _check()
