# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Lewis Menzies (Music Duck / MusicD)
"""Device drawings for every Sonos model the bridge knows about.

Each speaker is described as the face you look at plus the depth it stands in,
and folded back through one shared three-quarter projection, so the whole set
is drawn from the same viewpoint: a Five is a wide cabinet, an Era 300 keeps
its cinched waist, a Sub the slot cut through it, a soundbar its length.  Only
the edges you could actually see are stroked.

The shapes are composed in a generous square and then fitted to a 32 x 32 grid,
which :mod:`calderabridge.icon` rasterises into the PNGs served on the settings page and
``tools/gen_device_icons.py`` writes out as an SVG sprite.  Nothing here needs a
drawing library, and there are no binary assets to keep in sync.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from functools import cache

Point = tuple[float, float]
Path = tuple[Point, ...]

VIEW = 32.0          # the drawings sit on a VIEW x VIEW grid
MARGIN = 2.0
DESIGN = 100.0       # ...but are composed in this larger square first


# ----------------------------------------------------------------------
# Geometry helpers
#
# Depth recedes up and to the right, foreshortened - a three-quarter view from
# slightly above, the way hardware is drawn in a device picker.
# ----------------------------------------------------------------------
DEPTH = (0.80, 0.52)


def _arc_points(cx: float, cy: float, rx: float, ry: float,
                start: float, end: float, steps: int) -> list[Point]:
    span = end - start
    return [
        (cx + rx * math.cos(start + span * i / steps),
         cy + ry * math.sin(start + span * i / steps))
        for i in range(steps + 1)
    ]


def circle(cx: float, cy: float, r: float, steps: int = 40) -> Path:
    return tuple(_arc_points(cx, cy, r, r, 0.0, math.tau, steps))


def ellipse(cx: float, cy: float, rx: float, ry: float, steps: int = 48) -> Path:
    return tuple(_arc_points(cx, cy, rx, ry, 0.0, math.tau, steps))


def ellipse_arc(cx: float, cy: float, rx: float, ry: float,
                start: float, end: float, steps: int = 24) -> Path:
    return tuple(_arc_points(cx, cy, rx, ry, start, end, steps))


def line(x1: float, y1: float, x2: float, y2: float) -> Path:
    return ((x1, y1), (x2, y2))


def _round_corners(points: Sequence[Point], radii: float | Sequence[float],
                   closed: bool, steps: int) -> Path:
    """Sample a polygon or polyline into a polyline with rounded corners.

    Concave corners round just as happily as convex ones, which is what gives
    the Era 300 its waist.  On an open path the two ends are left alone.
    """
    count = len(points)
    if isinstance(radii, int | float):
        radii = [float(radii)] * count
    out: list[Point] = []
    for index, point in enumerate(points):
        if not closed and index in (0, count - 1):
            out.append(point)
            continue
        before = points[index - 1]
        after = points[(index + 1) % count]
        to_before = (before[0] - point[0], before[1] - point[1])
        to_after = (after[0] - point[0], after[1] - point[1])
        len_before = math.hypot(*to_before)
        len_after = math.hypot(*to_after)
        if len_before < 1e-9 or len_after < 1e-9:
            out.append(point)
            continue
        u1 = (to_before[0] / len_before, to_before[1] / len_before)
        u2 = (to_after[0] / len_after, to_after[1] / len_after)
        angle = math.acos(max(-1.0, min(1.0, u1[0] * u2[0] + u1[1] * u2[1])))
        if angle < 1e-6 or abs(angle - math.pi) < 1e-6:
            out.append(point)  # a straight-through vertex needs no arc
            continue
        half = math.tan(angle / 2.0)
        radius = min(radii[index], len_before / 2.0 * half, len_after / 2.0 * half)
        if radius < 1e-9:
            out.append(point)
            continue
        tangent = radius / half
        start_point = (point[0] + u1[0] * tangent, point[1] + u1[1] * tangent)
        end_point = (point[0] + u2[0] * tangent, point[1] + u2[1] * tangent)
        bisector = (u1[0] + u2[0], u1[1] + u2[1])
        length = math.hypot(*bisector)
        if length < 1e-9:
            out.append(point)
            continue
        distance = radius / math.sin(angle / 2.0)
        centre = (point[0] + bisector[0] / length * distance,
                  point[1] + bisector[1] / length * distance)
        a1 = math.atan2(start_point[1] - centre[1], start_point[0] - centre[0])
        a2 = math.atan2(end_point[1] - centre[1], end_point[0] - centre[0])
        sweep = (a2 - a1 + math.pi) % math.tau - math.pi  # the short way round
        out.extend(_arc_points(centre[0], centre[1], radius, radius, a1, a1 + sweep, steps))
    if closed:
        out.append(out[0])
    return tuple(out)


def poly(points: Sequence[Point], radii: float | Sequence[float], steps: int = 6) -> Path:
    """A closed polygon with rounded corners."""
    return _round_corners(points, radii, closed=True, steps=steps)


def polyline(points: Sequence[Point], radii: float | Sequence[float] = 0.0,
             steps: int = 5) -> Path:
    """An open path with rounded joins - used for the folded-back faces."""
    return _round_corners(points, radii, closed=False, steps=steps)


def rrect(x: float, y: float, w: float, h: float, r: float) -> Path:
    return poly(((x, y), (x + w, y), (x + w, y + h), (x, y + h)), r)


def _unit_depth() -> Point:
    length = math.hypot(*DEPTH)
    return (DEPTH[0] / length, -DEPTH[1] / length)


def extrude(front: Path, depth: float) -> list[Path]:
    """Give a flat outline a body, by folding it back away from the viewer.

    Only the edges you could actually see are drawn: the front outline (which
    the caller already has), the part of the back outline that clears it, and
    the two lines joining them.  Which part of the back clears the front falls
    out of the geometry - an edge is visible when its outward normal leans away
    from the viewer.
    """
    ux, uy = _unit_depth()
    points = list(front[:-1]) if front[0] == front[-1] else list(front)
    count = len(points)
    if count < 3 or depth <= 0:
        return []

    def faces_away(index: int) -> bool:
        ax, ay = points[index]
        bx, by = points[(index + 1) % count]
        # For a path drawn clockwise on screen the outward normal is (dy, -dx).
        return (by - ay) * ux + (ax - bx) * uy > 0

    visible = [faces_away(i) for i in range(count)]
    if all(visible) or not any(visible):
        return []

    # A waisted shape such as the Era 300 turns away from the viewer and back
    # again, so collect every visible run, not just the first.
    paths: list[Path] = []
    for start in (i for i in range(count) if visible[i] and not visible[i - 1]):
        run = [points[start]]
        index = start
        while visible[index]:
            index = (index + 1) % count
            run.append(points[index])
        back = tuple((x + ux * depth, y + uy * depth) for x, y in run)
        paths.append(back)
        paths.append(line(run[0][0], run[0][1], back[0][0], back[0][1]))
        paths.append(line(run[-1][0], run[-1][1], back[-1][0], back[-1][1]))
    return paths


def box(corners: Sequence[Point], depth: float, radius: float = 3.0) -> list[Path]:
    """A cabinet in three-quarter view.

    *corners* are the front face, clockwise from the top left.
    """
    front = poly(corners, radius)
    return [front, *extrude(front, depth)]


def upright(x: float, y: float, w: float, h: float, depth: float,
            radius: float = 3.0, taper: float = 0.0) -> list[Path]:
    """A box standing on the floor, optionally narrower at the top."""
    return box(
        ((x + taper, y), (x + w - taper, y), (x + w, y + h), (x, y + h)),
        depth,
        radius,
    )


def cylinder(cx: float, y: float, rx: float, ry: float, h: float) -> list[Path]:
    """A drum seen slightly from above: a full top ellipse and a front skirt."""
    return [
        ellipse(cx, y + ry, rx, ry),
        polyline(((cx - rx, y + ry), (cx - rx, y + h - ry))),
        polyline(((cx + rx, y + ry), (cx + rx, y + h - ry))),
        ellipse_arc(cx, y + h - ry, rx, ry, 0.0, math.pi),
    ]


# ----------------------------------------------------------------------
# The models
#
# Sizes are relative, not literal: an Arc is drawn longer than a Beam because
# it is longer, but a Roam is not drawn as the thumbnail its real size would
# make it.  Each glyph is centred in the 100x100 box when it is handed out.
# ----------------------------------------------------------------------
def d_generic() -> list[Path]:
    paths = upright(28, 22, 40, 54, 12, 4)
    paths.append(circle(48, 40, 10))
    paths.append(circle(48, 62, 4.5))
    return paths


def d_five() -> list[Path]:
    # The widest box in the range, lying on its long edge.
    return upright(8, 40, 68, 30, 15, 4)


def d_play3() -> list[Path]:
    return upright(14, 40, 56, 28, 14, 5)


def d_one() -> list[Path]:
    return upright(30, 22, 30, 52, 13, 5)


def d_play1() -> list[Path]:
    # The same footprint as a One, tapering in towards the top.
    return upright(30, 24, 30, 50, 13, 5, taper=2)


def d_era100() -> list[Path]:
    # An oval drum rather than a box.
    return cylinder(48, 20, 15, 6, 56)


def d_era300() -> list[Path]:
    # Cinched at the waist - the most recognisable shape in the range.
    front = poly(((18, 36), (76, 36), (72, 52), (76, 68), (18, 68), (22, 52)),
                 (8, 8, 7, 8, 8, 7))
    return [front, *extrude(front, 10)]


def _bar(x: float, y: float, w: float, h: float, depth: float, radius: float) -> list[Path]:
    return upright(x, y, w, h, depth, radius)


def d_beam() -> list[Path]:
    return _bar(10, 48, 74, 13, 12, 4)


def d_arc() -> list[Path]:
    return _bar(6, 50, 84, 11, 9, 3)


def d_ray() -> list[Path]:
    return _bar(18, 48, 60, 13, 11, 3)


def d_playbar() -> list[Path]:
    paths = _bar(9, 47, 78, 15, 13, 2.5)
    paths.append(line(19, 50, 19, 59))  # the end caps either side of the grille
    paths.append(line(77, 50, 77, 59))
    return paths


def d_playbase() -> list[Path]:
    # A plinth for the television to stand on: flat, deep, flared to the floor.
    return box(((14, 52), (80, 52), (83, 62), (11, 62)), 13, 2.5)


def d_move() -> list[Path]:
    return upright(32, 20, 32, 56, 13, 7, taper=2)


def d_roam() -> list[Path]:
    return upright(38, 26, 22, 46, 9, 5, taper=2)


def d_sub() -> list[Path]:
    paths = upright(20, 24, 52, 54, 12, 8)
    paths.append(rrect(38, 38, 16, 26, 8))  # the slot straight through the middle
    return paths


def d_submini() -> list[Path]:
    paths = cylinder(46, 22, 17, 7, 54)
    paths.append(circle(46, 50, 9))
    return paths


def d_amp() -> list[Path]:
    paths = upright(14, 44, 62, 22, 16, 3)
    paths.append(circle(64, 55, 5.5))  # the volume dial
    return paths


def d_port() -> list[Path]:
    paths = upright(20, 48, 50, 15, 15, 3)
    paths.append(circle(60, 55.5, 3))
    return paths


def d_bookshelf() -> list[Path]:
    return upright(24, 26, 44, 46, 12, 2)


def d_lamp() -> list[Path]:
    # A shade over a drum: the IKEA table lamp.
    shade = poly(((36, 18), (60, 18), (68, 42), (28, 42)), 3)
    base = cylinder(48, 42, 11, 4.5, 36)
    return [shade, ellipse(48, 20, 12, 4.5), *base[1:]]


def d_frame() -> list[Path]:
    paths = box(((24, 20), (68, 20), (68, 78), (24, 78)), 6, 2)
    paths.append(rrect(30, 26, 32, 46, 1.5))
    return paths



DEVICES = [
    ("one", d_one), ("play1", d_play1), ("era100", d_era100), ("era300", d_era300),
    ("five", d_five), ("play3", d_play3),
    ("arc", d_arc), ("beam", d_beam), ("ray", d_ray),
    ("playbar", d_playbar), ("playbase", d_playbase),
    ("sub", d_sub), ("submini", d_submini),
    ("move", d_move), ("roam", d_roam),
    ("amp", d_amp), ("port", d_port),
    ("bookshelf", d_bookshelf), ("lamp", d_lamp), ("frame", d_frame),
    ("generic", d_generic),
]


# ----------------------------------------------------------------------
# Fitting and flattening
# ----------------------------------------------------------------------
BUILDERS = dict(DEVICES)
ICON_NAMES = tuple(name for name, _ in DEVICES)
FALLBACK = "generic"


#: How much of the grid a device fills, relative to the largest in its family.
#: Icons are seen one at a time beside a room name, so drawing each to its real
#: size would leave a Roam as a smudge - but flattening everything to the same
#: span would make an Arc, a Beam and a Ray the same soundbar.  These are the
#: real lengths, compressed hard, so the order survives and nothing shrinks far.
RELATIVE = {
    "arc": 1.00,      # 114 cm
    "playbar": 0.93,  #  90 cm
    "playbase": 0.87, #  72 cm
    "beam": 0.85,     #  65 cm
    "ray": 0.81,      #  56 cm
    "five": 1.00,     #  36 cm across the front
    "play3": 0.90,    #  27 cm
    "sub": 1.00,
    "submini": 0.86,  # two thirds of a Sub, and it should look it
    "amp": 1.00,      #  22 cm across
    "port": 0.84,     #  14 cm
    "move": 1.00,     #  24 cm tall
    "roam": 0.82,     #  17 cm, and it should not look like a Move
}


@cache
def fitted(name: str) -> tuple[Path, ...]:
    """Build a device and scale it to sit centred in the 32-unit grid."""
    runs = BUILDERS.get(name, BUILDERS[FALLBACK])()
    xs = [x for run in runs for x, _ in run]
    ys = [y for run in runs for _, y in run]
    span = max(max(xs) - min(xs), max(ys) - min(ys), 1e-6)
    scale = (VIEW - 2 * MARGIN) * RELATIVE.get(name, 1.0) / span
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    return tuple(
        tuple((VIEW / 2 + (x - cx) * scale, VIEW / 2 + (y - cy) * scale) for x, y in run)
        for run in runs
    )


@cache
def polylines(name: str) -> tuple[Path, ...]:
    """A device as plain point runs, for anything that cannot draw curves."""
    return fitted(name)


def svg_paths(name: str) -> list[str]:
    """The ``d`` attribute of every stroke in a device, ready for an SVG."""
    out = []
    for run in fitted(name):
        closed = len(run) > 2 and run[0] == run[-1]
        points = run[:-1] if closed else run
        drawn = " ".join(f"{x:.2f} {y:.2f}" for x, y in points)
        out.append(f"M{drawn}" + ("Z" if closed else ""))
    return out
