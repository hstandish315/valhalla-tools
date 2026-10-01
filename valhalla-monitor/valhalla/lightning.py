"""
Procedural lightning.

Bolts are built by recursive midpoint displacement: take a segment, push its
midpoint sideways by a random amount proportional to the segment length, and
recurse. That is what gives real lightning its self-similar jaggedness, and it
costs nothing to generate. Forks are the same process run on a shorter, dimmer
child branch.

Each bolt is generated once and then only re-drawn with a decaying alpha and a
flicker envelope, so animating a screenful of them stays cheap.
"""

from __future__ import annotations

import math
import random

import cairo

from . import theme as T


class Bolt:
    """One strike: a main channel plus forks, with a short flickering life."""

    __slots__ = ("main", "forks", "born", "life", "width", "color", "seed")

    def __init__(self, points, forks, born, life, width, color):
        self.main = points
        self.forks = forks
        self.born = born
        self.life = life
        self.width = width
        self.color = color
        self.seed = random.random() * 100.0

    def age(self, now: float) -> float:
        return (now - self.born) / self.life

    def alive(self, now: float) -> bool:
        return now - self.born < self.life

    def envelope(self, now: float) -> float:
        """Bright flash, quick decay, plus a stutter so it reads as electrical."""
        a = self.age(now)
        if a < 0.0:
            return 0.0
        decay = (1.0 - a) ** 1.8
        flicker = 0.72 + 0.28 * math.sin((now * 47.0) + self.seed * 6.3)
        if 0.25 < a < 0.34:      # the momentary gap between strokes
            flicker *= 0.35
        return max(0.0, decay * flicker)


def _displace(points, roughness, amount, rng):
    """One subdivision pass over a polyline."""
    out = [points[0]]
    for i in range(len(points) - 1):
        (x1, y1), (x2, y2) = points[i], points[i + 1]
        mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy) or 1.0
        # offset along the segment normal
        off = rng.uniform(-amount, amount)
        mx += -dy / length * off
        my += dx / length * off
        out.append((mx, my))
        out.append((x2, y2))
    return out


def make_bolt(x1, y1, x2, y2, *, color=None, width=1.7, life=0.55,
              detail=5, jag=0.22, fork_chance=0.55, depth=0, rng=None,
              now=0.0) -> Bolt:
    """Build a bolt from (x1, y1) to (x2, y2)."""
    rng = rng or random
    color = color or T.BOLT
    span = math.hypot(x2 - x1, y2 - y1)
    points = [(x1, y1), (x2, y2)]
    amount = span * jag
    for _ in range(detail):
        points = _displace(points, 0.5, amount, rng)
        amount *= 0.52

    forks = []
    if depth < 2:
        for i in range(2, len(points) - 2, 3):
            if rng.random() > fork_chance:
                continue
            px, py = points[i]
            ang = math.atan2(y2 - y1, x2 - x1) + rng.uniform(-1.15, 1.15)
            reach = span * rng.uniform(0.16, 0.38)
            child = make_bolt(px, py, px + math.cos(ang) * reach,
                              py + math.sin(ang) * reach, color=color,
                              width=width * 0.55, life=life, detail=3,
                              jag=jag * 1.2, fork_chance=0.25, depth=depth + 1,
                              rng=rng, now=now)
            forks.append(child.main)
            forks.extend(child.forks)

    return Bolt(points, forks, now, life, width, color)


def make_arc_bolt(cx, cy, radius, a0, a1, *, color=None, width=1.6, life=0.5,
                  jitter=6.0, rng=None, now=0.0) -> Bolt:
    """A bolt that crawls along a circular arc - used to electrify a gauge ring."""
    rng = rng or random
    steps = max(6, int(abs(a1 - a0) * radius / 9.0))
    pts = []
    for i in range(steps + 1):
        t = i / steps
        ang = a0 + (a1 - a0) * t
        r = radius + rng.uniform(-jitter, jitter) * (0.35 + 0.65 * math.sin(t * math.pi))
        pts.append((cx + math.cos(ang) * r, cy + math.sin(ang) * r))
    bolt = Bolt(pts, [], now, life, width, color or T.BOLT)
    return bolt


def _trace(cr, pts):
    cr.move_to(*pts[0])
    for p in pts[1:]:
        cr.line_to(*p)


def draw_bolt(cr, bolt: Bolt, now: float, intensity: float = 1.0):
    """Halo pass, then a near-white core, so the strike reads as hot."""
    e = bolt.envelope(now) * intensity
    if e <= 0.01:
        return
    cr.save()
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)

    for pts in bolt.forks:
        cr.new_path()
        _trace(cr, pts)
        T.rgb(cr, bolt.color, 0.22 * e)
        cr.set_line_width(bolt.width * 2.6)
        cr.stroke_preserve()
        T.rgb(cr, T.BOLT, 0.65 * e)
        cr.set_line_width(bolt.width * 0.7)
        cr.stroke()

    cr.new_path()
    _trace(cr, bolt.main)
    T.rgb(cr, bolt.color, 0.16 * e)
    cr.set_line_width(bolt.width * 7.0)
    cr.stroke_preserve()
    T.rgb(cr, bolt.color, 0.34 * e)
    cr.set_line_width(bolt.width * 3.2)
    cr.stroke_preserve()
    T.rgb(cr, T.BOLT, 0.95 * e)
    cr.set_line_width(bolt.width)
    cr.stroke()
    cr.restore()


class Storm:
    """
    Keeps a small pool of live bolts for one widget.

    `pressure` (0..1) drives how often a strike happens - at idle the dashboard
    is calm, under load it crackles.
    """

    def __init__(self, max_bolts: int = 6):
        self.bolts: list[Bolt] = []
        self.max_bolts = max_bolts
        self._next = 0.0
        self.rng = random.Random()

    def add(self, bolt: Bolt):
        self.bolts.append(bolt)
        if len(self.bolts) > self.max_bolts:
            del self.bolts[0:len(self.bolts) - self.max_bolts]

    def prune(self, now: float):
        if self.bolts:
            self.bolts = [b for b in self.bolts if b.alive(now)]

    def due(self, now: float, pressure: float, base: float = 2.6,
            fastest: float = 0.16) -> bool:
        """True at most once per interval; interval shortens as pressure rises."""
        if now < self._next:
            return False
        p = max(0.0, min(1.0, pressure))
        gap = base * (1.0 - p) ** 2 + fastest
        self._next = now + gap * self.rng.uniform(0.55, 1.5)
        return True

    def draw(self, cr, now: float, intensity: float = 1.0):
        for b in self.bolts:
            draw_bolt(cr, b, now, intensity)

    @property
    def active(self) -> bool:
        return bool(self.bolts)
