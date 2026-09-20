"""Line drawings of the Sonos models the bridge can bridge.

The shapes themselves come from :mod:`calderabridge.deviceicons`, where each
cabinet is drawn in three-quarter view, so a Five is recognisably a Five and
not just a wide box.  This module scales those outlines into the 100x100 box
the rest of the bridge works in, matches a reported model name to one of them,
and lays two side by side for a room that is a bonded stereo pair.

Both renderers work from the single description: :mod:`calderabridge.icon`
rasterises it into the PNG that UPnP control points ask for, and :func:`svg`
emits the same outline for the status page or any other UI.  Nothing here needs
a drawing library, and there are no binary assets to keep in sync.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from functools import lru_cache

from . import deviceicons

Point = tuple[float, float]
Path = tuple[Point, ...]

# Everything is drawn in this box and scaled at render time.
VIEWBOX = 100.0


# ----------------------------------------------------------------------
# The models
# ----------------------------------------------------------------------
def _outline(kind: str) -> list[Path]:
    """A device's outlines, scaled from the drawing grid into the 100x100 box."""
    scale = VIEWBOX / deviceicons.VIEW
    return [
        tuple((x * scale, y * scale) for x, y in run)
        for run in deviceicons.polylines(kind)
    ]


KINDS: tuple[str, ...] = deviceicons.ICON_NAMES

DEFAULT_KIND = deviceicons.FALLBACK


#: Human-readable name for each glyph, used by the status page and the docs.
LABELS: dict[str, str] = {
    "five": "Five / Play:5",
    "play3": "Play:3",
    "one": "One / One SL",
    "play1": "Play:1",
    "era100": "Era 100",
    "era300": "Era 300",
    "beam": "Beam",
    "arc": "Arc / Arc Ultra",
    "ray": "Ray",
    "playbar": "Playbar",
    "playbase": "Playbase",
    "move": "Move / Move 2",
    "roam": "Roam / Roam 2",
    "sub": "Sub",
    "submini": "Sub Mini",
    "amp": "Amp / Connect:Amp",
    "port": "Port / Connect",
    "bookshelf": "Symfonisk bookshelf",
    "lamp": "Symfonisk lamp",
    "frame": "Symfonisk picture frame",
    "generic": "Sonos player",
}

# ----------------------------------------------------------------------
# Matching a model name to a glyph
# ----------------------------------------------------------------------
# Ordered longest-phrase-first: "Era 300" must win over "Era", "Sub Mini" over
# "Sub", "Connect:Amp" over both "Connect" and "Amp".
_RULES: tuple[tuple[str, str], ...] = (
    ("era 300", "era300"),
    ("era 100", "era100"),
    ("arc ultra", "arc"),
    ("arc", "arc"),
    ("beam", "beam"),
    ("ray", "ray"),
    ("playbar", "playbar"),
    ("play bar", "playbar"),
    ("playbase", "playbase"),
    ("play base", "playbase"),
    ("sub mini", "submini"),
    ("submini", "submini"),
    ("sub", "sub"),
    ("move", "move"),
    ("roam", "roam"),
    ("five", "five"),
    ("play 5", "five"),
    ("play 3", "play3"),
    ("play 1", "play1"),
    ("one", "one"),
    ("picture frame", "frame"),
    ("frame", "frame"),
    ("table lamp", "lamp"),
    ("lamp", "lamp"),
    ("bookshelf", "bookshelf"),
    ("connect amp", "amp"),
    ("zp120", "amp"),
    ("zp100", "amp"),
    ("amp", "amp"),
    ("port", "port"),
    ("connect", "port"),
    ("zp90", "port"),
    ("zp80", "port"),
    ("symfonisk", "bookshelf"),
)


def _normalise(model: str) -> str:
    """Fold a model name down to lowercase words: ``Sonos PLAY:5`` -> ``sonos play 5``."""
    return " ".join(re.sub(r"[^0-9a-z]+", " ", (model or "").lower()).split())


def classify(model: str) -> str:
    """Pick the glyph that best matches a Sonos ``modelName``.

    Unknown or missing models fall back to a generic speaker, so a firmware that
    invents a new name still gets a sensible icon.
    """
    text = _normalise(model)
    if not text:
        return DEFAULT_KIND
    for phrase, kind in _RULES:
        if re.search(rf"(?<![0-9a-z]){re.escape(phrase)}(?![0-9a-z])", text):
            return kind
    return DEFAULT_KIND


# ----------------------------------------------------------------------
# Assembling a drawing
# ----------------------------------------------------------------------
def _bbox(paths: Iterable[Path]) -> tuple[float, float, float, float]:
    xs = [x for path in paths for x, _ in path]
    ys = [y for path in paths for _, y in path]
    return min(xs), min(ys), max(xs), max(ys)


def _scaled(paths: Sequence[Path], scale: float, cx: float, cy: float,
            to_x: float, to_y: float) -> list[Path]:
    return [
        tuple(((x - cx) * scale + to_x, (y - cy) * scale + to_y) for x, y in path)
        for path in paths
    ]


PAIR_GAP = 7.0
PAIR_MARGIN = 6.0
PAIR_MAX_SCALE = 0.78


def _as_pair(paths: Sequence[Path]) -> list[Path]:
    """Two copies of a glyph, side by side, sized to fit the same box."""
    x0, y0, x1, y1 = _bbox(paths)
    width = max(x1 - x0, 1e-6)
    height = max(y1 - y0, 1e-6)
    available = (VIEWBOX - 2 * PAIR_MARGIN - PAIR_GAP) / 2
    scale = min(available / width, (VIEWBOX - 2 * PAIR_MARGIN) / height, PAIR_MAX_SCALE)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    offset = (width * scale + PAIR_GAP) / 2
    centre = VIEWBOX / 2
    return (
        _scaled(paths, scale, cx, cy, centre - offset, centre)
        + _scaled(paths, scale, cx, cy, centre + offset, centre)
    )


@lru_cache(maxsize=128)
def glyph(kind: str = DEFAULT_KIND, pair: bool = False) -> tuple[Path, ...]:
    """The outline for *kind*, in a 100x100 box.  ``pair=True`` draws two."""
    paths = _outline(kind if kind in KINDS else DEFAULT_KIND)
    return tuple(_as_pair(paths) if pair else paths)


def glyph_for_model(model: str, pair: bool = False) -> tuple[Path, ...]:
    return glyph(classify(model), pair)


def label(kind: str) -> str:
    return LABELS.get(kind, LABELS[DEFAULT_KIND])


# ----------------------------------------------------------------------
# SVG
# ----------------------------------------------------------------------
def svg(
    kind: str = DEFAULT_KIND,
    pair: bool = False,
    size: int = 64,
    colour: str = "currentColor",
    stroke: float = 3.4,
    title: str = "",
    auto_theme: bool = False,
) -> str:
    """The same glyph as a standalone SVG document.

    Strokes default to ``currentColor`` so an inline icon follows whatever text
    colour the surrounding page is using.  ``auto_theme`` adds the light/dark
    rule a standalone file needs when there is no page to inherit from.
    """
    paths = []
    for path in glyph(kind, pair):
        points = " ".join(f"{x:.2f} {y:.2f}" for x, y in path)
        closed = len(path) > 2 and path[0] == path[-1]
        paths.append(f'<path d="M {points}{" Z" if closed else ""}"/>')
    heading = f"<title>{title}</title>" if title else ""
    if auto_theme:
        heading += (
            "<style>svg{color:#16181d}"
            "@media (prefers-color-scheme:dark){svg{color:#f0f2f5}}</style>"
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" '
        f'width="{size}" height="{size}" fill="none" stroke="{colour}" '
        f'stroke-width="{stroke:g}" stroke-linecap="round" stroke-linejoin="round" '
        f'role="img" aria-label="{title or label(kind)}">'
        f"{heading}{''.join(paths)}</svg>"
    )
