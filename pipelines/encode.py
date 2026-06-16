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

Bathymetry adaptation — *conservative rounding*. A chart must bias shallow:
charted depth <= true depth (PLAN.md). So by default we round the height
*toward shallower* (ceil, i.e. toward the surface / less-negative), guaranteeing
the decoded elevation is never deeper than the truth. The cost is an up-to-one-
step shallow bias, which is sub-perceptual at the zoom each step is applied
(z0 step = 2048 m on a 0..-10000 m range; z12 step = 0.5 m). Pass
``conservative=False`` for minimal-error round-to-nearest.
"""

import numpy as np

FULL_RESOLUTION_ZOOM = 19
TERRARIUM_OFFSET = 32768.0


def quantization_factor(z):
    """Vertical step (metres) at zoom ``z``."""
    return 2.0 ** (FULL_RESOLUTION_ZOOM - z) / 256.0


def quantize(data, z, conservative=True):
    """Round elevations (metres) to the per-zoom vertical step.

    conservative=True rounds toward shallower (never deepens); False is
    round-to-nearest.
    """
    factor = quantization_factor(z)
    scaled = np.asarray(data, dtype=np.float64) / factor
    rounded = np.ceil(scaled) if conservative else np.round(scaled)
    return rounded * factor


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
        # Bytes are valid and decode is exact for a dense random field too.
        rnd = np.random.default_rng(z).uniform(-10000, 100, size=(64, 64))
        rgb2 = encode(rnd, z, conservative=True)
        assert rgb2.dtype == np.uint8 and rgb2.min() >= 0 and rgb2.max() <= 255
        assert np.all(decode(rgb2) >= rnd - 1e-9)

    # Non-conservative round-to-nearest CAN deepen — confirms the modes differ.
    e = np.array([-7.0])  # z9 step = 4 m; nearest -> -8 (deeper), ceil -> -4 (shallower)
    assert decode(encode(e, 9, conservative=False))[0] < e[0]
    assert decode(encode(e, 9, conservative=True))[0] >= e[0]

    print("encode.py self-check ok")


if __name__ == "__main__":
    _check()
