"""Device icons, drawn at start-up rather than shipped as binary assets.

The settings page shows each room's icon next to its name.
Each bridged room gets the line drawing of its own model - a Five looks like a
Five, a Beam like a Beam, a stereo pair like two of them - taken from the
outlines in :mod:`calderabridge.speakers` and encoded as a PNG with nothing but
arithmetic and :mod:`zlib`.
"""

from __future__ import annotations

import hashlib
import math
import struct
import zlib
from collections.abc import Sequence
from functools import lru_cache

from .speakers import DEFAULT_KIND, VIEWBOX, Path, classify, glyph

BACKGROUND = (24, 26, 31)
FOREGROUND = (240, 242, 245)

#: Stroke weight as a fraction of the icon's edge, with a floor so the smallest
#: icon still has a visible line.  The drawings are three-quarter views with
#: real line work in them, so they want a finer stroke than a flat glyph would.
STROKE_RATIO = 0.040
MIN_STROKE = 1.5


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _encode_png(width: int, height: int, pixels: bytearray) -> bytes:
    """Encode RGBA rows (already filter-prefixed) into a PNG byte string."""
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(pixels), 9))
        + _chunk(b"IEND", b"")
    )


def _blend(base: tuple[int, int, int], top: tuple[int, int, int], alpha: float):
    alpha = max(0.0, min(1.0, alpha))
    return tuple(round(b + (t - b) * alpha) for b, t in zip(base, top, strict=True))


def _stroke_coverage(size: int, paths: Sequence[Path], width: float) -> list[float]:
    """Anti-aliased coverage for a set of round-capped polylines.

    Each segment is a capsule: coverage falls off with the distance from the
    segment, and segments combine by taking the strongest.  Only the pixels
    inside a segment's bounding box are visited, which keeps a 120px icon well
    under a millisecond's worth of arithmetic per stroke.
    """
    scale = size / VIEWBOX
    half = width / 2.0
    coverage = [0.0] * (size * size)
    for path in paths:
        points = [(x * scale, y * scale) for x, y in path]
        for (ax, ay), (bx, by) in zip(points, points[1:], strict=False):
            dx, dy = bx - ax, by - ay
            length_sq = dx * dx + dy * dy
            x_from = max(0, int(math.floor(min(ax, bx) - half - 1)))
            x_to = min(size - 1, int(math.ceil(max(ax, bx) + half + 1)))
            y_from = max(0, int(math.floor(min(ay, by) - half - 1)))
            y_to = min(size - 1, int(math.ceil(max(ay, by) + half + 1)))
            for y in range(y_from, y_to + 1):
                py = y + 0.5
                row = y * size
                for x in range(x_from, x_to + 1):
                    px = x + 0.5
                    if length_sq > 1e-12:
                        t = ((px - ax) * dx + (py - ay) * dy) / length_sq
                        t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
                    else:
                        t = 0.0
                    distance = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
                    alpha = half + 0.5 - distance
                    if alpha > 0.0:
                        if alpha > 1.0:
                            alpha = 1.0
                        if alpha > coverage[row + x]:
                            coverage[row + x] = alpha
    return coverage


@lru_cache(maxsize=64)
def render(size: int = 48, kind: str = DEFAULT_KIND, pair: bool = False) -> bytes:
    """Draw *kind* as a line glyph on a rounded tile, *size* x *size* pixels."""
    radius = size * 0.22  # corner rounding of the tile
    stroke = max(MIN_STROKE, size * STROKE_RATIO)
    coverage = _stroke_coverage(size, glyph(kind, pair), stroke)

    rows = bytearray()
    for y in range(size):
        rows.append(0)  # PNG filter type 0 (None) for this scanline
        row = y * size
        for x in range(size):
            px, py = x + 0.5, y + 0.5

            # Rounded-square mask.
            dx = max(radius - px, px - (size - radius), 0.0)
            dy = max(radius - py, py - (size - radius), 0.0)
            corner = math.hypot(dx, dy)
            tile_alpha = max(0.0, min(1.0, radius - corner + 0.5)) if corner > 0 else 1.0

            ink = coverage[row + x]
            r, g, b = _blend(BACKGROUND, FOREGROUND, ink) if ink else BACKGROUND
            rows.extend((r, g, b, round(255 * tile_alpha)))

    return _encode_png(size, size, rows)


def render_for_model(size: int, model: str, pair: bool = False) -> bytes:
    return render(size, classify(model), pair)


#: UPnP asks for 48 and 120; control points that draw a large tile pick the
#: biggest they are offered, and upscaling a 120px line drawing looks it.
ICON_SIZES = (48, 120, 240, 512)


@lru_cache(maxsize=128)
def token(kind: str = DEFAULT_KIND, pair: bool = False) -> str:
    """A short fingerprint of a drawing, for the icon's URL.

    Control points cache icons hard, and rightly so - but the URL used to stay
    the same when the drawing behind it changed, which left them showing an old
    icon indefinitely.  Putting the fingerprint in the path means a new drawing
    is a new URL, so a cache can never shadow it.
    """
    outline = repr([[(round(x, 2), round(y, 2)) for x, y in path]
                    for path in glyph(kind, pair)]).encode("utf-8")
    return hashlib.blake2s(outline, digest_size=4).hexdigest()


def icon_list_xml(base_url: str, kind: str = DEFAULT_KIND, pair: bool = False) -> str:
    """The ``<iconList>`` fragment for a device description document."""
    stamp = token(kind, pair)
    parts = ["<iconList>"]
    for size in ICON_SIZES:
        parts.append(
            "<icon><mimetype>image/png</mimetype>"
            f"<width>{size}</width><height>{size}</height><depth>32</depth>"
            f"<url>{base_url}/icon/{stamp}/{size}.png</url></icon>"
        )
    parts.append("</iconList>")
    return "".join(parts)
