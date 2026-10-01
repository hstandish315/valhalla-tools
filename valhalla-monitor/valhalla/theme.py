"""
Mjolnir theme - palette, typography and Cairo drawing primitives.

Everything in this dashboard is hand-painted with Cairo rather than styled with
GTK CSS: the look depends on glows, gradients and procedural runework that CSS
cannot express. This module holds the shared vocabulary those painters use.
"""

from __future__ import annotations

import math

import cairo
import gi

gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Pango, PangoCairo  # noqa: E402


# ---------------------------------------------------------------- palette ---

def _hex(code: str) -> tuple[float, float, float]:
    code = code.lstrip("#")
    return tuple(int(code[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


VOID      = _hex("#04060c")   # window ground, deepest night
DEEP      = _hex("#070c16")   # header / recessed areas
PANEL     = _hex("#0a1120")   # panel body
PANEL_HI  = _hex("#111b30")   # panel body, lit corner
EDGE      = _hex("#1b2a45")   # hairline borders
STEEL     = _hex("#26374f")   # inert metal
TRACK     = _hex("#141e33")   # unfilled portion of any meter

TEXT      = _hex("#c9d8ef")   # primary readouts
TEXT_DIM  = _hex("#7288a8")   # labels
TEXT_MUTE = _hex("#42536e")   # tertiary / units

BRONZE    = _hex("#9a7434")   # runework, frames
GOLD      = _hex("#c8963c")
GOLD_HI   = _hex("#f2c877")

CYAN      = _hex("#4fd6ff")   # CPU  - Huginn, the thought
AMBER     = _hex("#ffab2e")   # GPU  - Surtr, the fire
VIOLET    = _hex("#9d7bff")   # RAM  - Mimir, the memory
SEA       = _hex("#31d3a4")   # DISK - Fafnir, the hoard
EMBER     = _hex("#ff7a3c")   # hotter amber, for GPU power draw

BOLT      = _hex("#eaf8ff")   # lightning core, near-white
BLOOD     = _hex("#ff4d3d")   # danger
WARN      = _hex("#ffb547")

# Load bands. Below WARN_AT everything sits in its own accent colour; past
# STORM_AT the widget enters "storm" state and starts throwing lightning.
WARN_AT  = 0.75
STORM_AT = 0.88

FONT_UI   = "Ubuntu Sans"
FONT_NUM  = "Ubuntu Mono"   # tabular figures, so readouts do not jitter


def mix(a, b, t: float):
    """Linear blend between two RGB triples."""
    t = max(0.0, min(1.0, t))
    return (a[0] + (b[0] - a[0]) * t,
            a[1] + (b[1] - a[1]) * t,
            a[2] + (b[2] - a[2]) * t)


def heat(accent, frac: float):
    """
    Accent colour pushed toward warning, then danger, as load rises.

    The warning blend is capped well short of pure amber: a metric at 80% still
    has to read as *its own* channel, and mixing violet all the way to amber
    just produces an unidentifiable pink. Only genuine danger goes red.
    """
    if frac <= WARN_AT:
        return accent
    if frac <= STORM_AT:
        t = (frac - WARN_AT) / (STORM_AT - WARN_AT)
        return mix(accent, WARN, 0.45 * t)
    t = min(1.0, (frac - STORM_AT) / (1.0 - STORM_AT))
    return mix(mix(accent, WARN, 0.45), BLOOD, 0.15 + 0.6 * t)


def rgb(cr, color, alpha: float = 1.0):
    cr.set_source_rgba(color[0], color[1], color[2], alpha)


# --------------------------------------------------------------- geometry ---

def rounded_rect(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def clipped_rect(cr, x, y, w, h, c):
    """Rectangle with the corners cut off - a shield plate, not a rounded box."""
    cr.new_path()
    cr.move_to(x + c, y)
    cr.line_to(x + w - c, y)
    cr.line_to(x + w, y + c)
    cr.line_to(x + w, y + h - c)
    cr.line_to(x + w - c, y + h)
    cr.line_to(x + c, y + h)
    cr.line_to(x, y + h - c)
    cr.line_to(x, y + c)
    cr.close_path()


def glow_stroke(cr, color, width: float, layers: int = 4, spread: float = 3.0,
                alpha: float = 0.5):
    """
    Stroke the current path several times, wide-and-faint to narrow-and-bright.

    Cairo has no blur, so a bloom is faked by stacking strokes. The path is
    preserved between passes so callers can keep using it afterwards.
    """
    for i in range(layers, 0, -1):
        t = i / layers
        cr.set_line_width(width + spread * width * t)
        rgb(cr, color, alpha * (1.0 - t) ** 2 * 0.9)
        cr.stroke_preserve()
    cr.set_line_width(width)
    rgb(cr, color, 1.0)
    cr.stroke()


def soft_disc(cr, cx, cy, r, color, alpha=0.5):
    """Radial bloom, brightest at the centre."""
    g = cairo.RadialGradient(cx, cy, 0, cx, cy, max(r, 0.1))
    g.add_color_stop_rgba(0.0, color[0], color[1], color[2], alpha)
    g.add_color_stop_rgba(0.45, color[0], color[1], color[2], alpha * 0.32)
    g.add_color_stop_rgba(1.0, color[0], color[1], color[2], 0.0)
    cr.set_source(g)
    cr.arc(cx, cy, r, 0, 2 * math.pi)
    cr.fill()


# --------------------------------------------------------------- typography ---

_FONT_CACHE: dict[tuple, Pango.FontDescription] = {}
# Creating a Pango layout per draw call dominated the frame cost: the dashboard
# issues tens of thousands of short text draws per second, almost all of them
# repeats of the same label at the same size. Layouts are therefore reused per
# (family, size, weight), and measurements are memoised outright.
_LAYOUTS: dict[tuple, "Pango.Layout"] = {}
_SIZE_CACHE: dict[tuple, tuple[int, int]] = {}
_SIZE_CACHE_MAX = 4096


def _font(family: str, size: float, bold: bool, italic: bool = False):
    key = (family, round(size, 2), bold, italic)
    fd = _FONT_CACHE.get(key)
    if fd is None:
        fd = Pango.FontDescription()
        fd.set_family(family)
        fd.set_absolute_size(size * Pango.SCALE)
        fd.set_weight(Pango.Weight.BOLD if bold else Pango.Weight.NORMAL)
        if italic:
            fd.set_style(Pango.Style.ITALIC)
        _FONT_CACHE[key] = fd
    return fd


def _layout(cr, family: str, size: float, bold: bool):
    """A reusable layout per font key, re-synced to the current target."""
    key = (family, round(size, 2), bold)
    layout = _LAYOUTS.get(key)
    if layout is None:
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(_font(family, size, bold))
        _LAYOUTS[key] = layout
    else:
        PangoCairo.update_layout(cr, layout)
    return layout


def text_size(cr, s: str, size: float, family: str = FONT_UI, bold: bool = False):
    key = (s, round(size, 2), family, bold)
    hit = _SIZE_CACHE.get(key)
    if hit is not None:
        return hit
    layout = _layout(cr, family, size, bold)
    layout.set_text(s, -1)
    wh = tuple(layout.get_pixel_size())
    if len(_SIZE_CACHE) >= _SIZE_CACHE_MAX:
        _SIZE_CACHE.clear()
    _SIZE_CACHE[key] = wh
    return wh


def draw_text(cr, x, y, s, size, color, *, family=FONT_UI, bold=False,
              align="left", valign="top", alpha=1.0, glow=0.0):
    """
    Draw a single line. `align` is left|center|right, `valign` top|middle|baseline.
    `glow` > 0 adds a coloured bloom behind the glyphs.
    """
    layout = _layout(cr, family, size, bold)
    layout.set_text(s, -1)
    w, h = layout.get_pixel_size()

    if align == "center":
        x -= w / 2
    elif align == "right":
        x -= w
    if valign == "middle":
        y -= h / 2
    elif valign == "baseline":
        y -= layout.get_baseline() / Pango.SCALE

    if glow > 0.0:
        for i in (3, 2, 1):
            cr.save()
            rgb(cr, color, glow * 0.10 * (4 - i))
            cr.move_to(x, y)
            PangoCairo.layout_path(cr, layout)
            cr.set_line_width(i * 1.6)
            cr.set_line_join(cairo.LINE_JOIN_ROUND)
            cr.stroke()
            cr.restore()

    rgb(cr, color, alpha)
    cr.move_to(x, y)
    PangoCairo.show_layout(cr, layout)
    return w, h


def draw_tracked(cr, x, y, s, size, color, *, tracking=2.4, family=FONT_UI,
                 bold=True, align="left", alpha=1.0, upper=True):
    """
    Letter-spaced caps for headings.

    Spacing is applied by advancing manually per glyph rather than through Pango
    attributes, which keeps the result identical across Pango versions.
    """
    if upper:
        s = s.upper()
    widths = [text_size(cr, ch, size, family, bold)[0] for ch in s]
    total = sum(widths) + tracking * max(0, len(s) - 1)
    if align == "center":
        x -= total / 2
    elif align == "right":
        x -= total
    for ch, w in zip(s, widths):
        draw_text(cr, x, y, ch, size, color, family=family, bold=bold, alpha=alpha)
        x += w + tracking
    return total


# ------------------------------------------------------------------- runes ---
# Elder Futhark glyphs drawn as line segments. Hand-drawing them avoids
# depending on a Runic-capable font, which many systems do not have, and
# lets the strokes carry the same glow treatment as everything else.

_RUNES: dict[str, list[list[tuple[float, float]]]] = {
    # coordinates in a 0..1 box, origin top-left
    "fehu":     [[(0.15, 0.0), (0.15, 1.0)], [(0.15, 0.16), (0.85, 0.0)],
                 [(0.15, 0.55), (0.85, 0.39)]],
    "sowilo":   [[(0.82, 0.0), (0.20, 0.0), (0.78, 0.5), (0.18, 0.5), (0.80, 1.0)]],
    "mannaz":   [[(0.12, 1.0), (0.12, 0.0)], [(0.88, 1.0), (0.88, 0.0)],
                 [(0.12, 0.0), (0.88, 0.62)], [(0.88, 0.0), (0.12, 0.62)]],
    "hagalaz":  [[(0.14, 0.0), (0.14, 1.0)], [(0.86, 0.0), (0.86, 1.0)],
                 [(0.14, 0.36), (0.86, 0.64)]],
    "algiz":    [[(0.5, 1.0), (0.5, 0.0)], [(0.5, 0.34), (0.06, 0.0)],
                 [(0.5, 0.34), (0.94, 0.0)]],
    "tiwaz":    [[(0.5, 1.0), (0.5, 0.0)], [(0.5, 0.0), (0.08, 0.36)],
                 [(0.5, 0.0), (0.92, 0.36)]],
    "thurisaz": [[(0.16, 0.0), (0.16, 1.0)], [(0.16, 0.18), (0.84, 0.44), (0.16, 0.70)]],
    "isa":      [[(0.5, 0.0), (0.5, 1.0)]],
    "raido":    [[(0.16, 1.0), (0.16, 0.0), (0.78, 0.14), (0.16, 0.46)],
                 [(0.30, 0.44), (0.82, 1.0)]],
    "dagaz":    [[(0.10, 1.0), (0.10, 0.0), (0.90, 1.0), (0.90, 0.0), (0.10, 1.0)]],
    "eihwaz":   [[(0.30, 0.0), (0.72, 0.0)], [(0.55, 0.0), (0.55, 1.0)],
                 [(0.28, 1.0), (0.55, 1.0)]],
}


def draw_rune(cr, name, x, y, size, color, width=1.6, alpha=1.0, glow=0.0):
    """Draw a rune with its bounding box top-left at (x, y)."""
    strokes = _RUNES.get(name)
    if not strokes:
        return
    cr.save()
    cr.set_line_width(width)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    cr.new_path()
    for seg in strokes:
        cr.move_to(x + seg[0][0] * size, y + seg[0][1] * size)
        for px, py in seg[1:]:
            cr.line_to(x + px * size, y + py * size)
    if glow > 0.0:
        rgb(cr, color, alpha * glow * 0.35)
        cr.set_line_width(width + 4.0)
        cr.stroke_preserve()
        cr.set_line_width(width)
    rgb(cr, color, alpha)
    cr.stroke()
    cr.restore()


def rune_width(name: str, size: float) -> float:
    return size


# ------------------------------------------------------------- ornaments ---

def knot_divider(cr, x, y, w, color, alpha=0.5):
    """A thin interlace band used to separate regions inside a panel."""
    cr.save()
    cr.set_line_width(1.0)
    rgb(cr, color, alpha * 0.45)
    cr.move_to(x, y)
    cr.line_to(x + w, y)
    cr.stroke()

    step, amp = 14.0, 3.4
    rgb(cr, color, alpha)
    cr.set_line_width(1.2)
    cx = x + w / 2 - step * 2
    cr.move_to(cx - step, y)
    for i in range(4):
        sign = -1 if i % 2 == 0 else 1
        cr.rel_curve_to(step * 0.35, sign * amp, step * 0.65, sign * amp, step, 0)
    cr.stroke()
    cr.restore()


def corner_brackets(cr, x, y, w, h, size, color, alpha=0.75, width=1.4):
    """Forged corner brackets - the frame motif for every panel."""
    cr.save()
    cr.set_line_width(width)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    rgb(cr, color, alpha)
    for sx, sy, ax, ay in ((x, y, 1, 1), (x + w, y, -1, 1),
                           (x, y + h, 1, -1), (x + w, y + h, -1, -1)):
        cr.move_to(sx + ax * size, sy)
        cr.line_to(sx + ax * 5, sy)
        cr.line_to(sx, sy + ay * 5)
        cr.line_to(sx, sy + ay * size)
        cr.stroke()
    cr.restore()


# ------------------------------------------------------------- yggdrasil ---
# The world tree, shared by the app icon and the header banner so both carry
# the same mark. Geometry is generated once in a unit box and cached: the
# banner repaints on most frames, and rebuilding a recursive tree per frame
# would be pure waste. Recursion is kept shallow and limbs thick on purpose —
# the mark has to survive being drawn at 32 px.

import random as _random

_TREE_CACHE: dict[tuple, tuple] = {}

_BASE_Y = 0.745        # trunk meets the roots
_FORK_Y = 0.505        # trunk opens into the canopy


def _limbs(x, y, ang, length, width, depth, rng, spread, out):
    """Collect (x1, y1, x2, y2, width) segments, all in unit space."""
    if depth <= 0 or length < 0.006:
        return
    x2 = x + math.cos(ang) * length
    y2 = y + math.sin(ang) * length
    out.append((x, y, x2, y2, width))
    for side in (-1, 1):
        _limbs(x2, y2, ang + side * spread * rng.uniform(0.72, 1.28),
               length * rng.uniform(0.62, 0.76), width * 0.62, depth - 1,
               rng, spread * 0.92, out)


def yggdrasil_geometry(canopy_depth: int = 5, root_depth: int = 3):
    """Cached unit-space geometry: (canopy limbs, root limbs)."""
    key = (canopy_depth, root_depth)
    hit = _TREE_CACHE.get(key)
    if hit is not None:
        return hit

    roots: list = []
    for i, ang in enumerate((math.pi / 2 - 0.92, math.pi / 2, math.pi / 2 + 0.92)):
        _limbs(0.5, _BASE_Y - 0.008, ang, 0.075 if i == 1 else 0.092,
               10.0 / 256, root_depth, _random.Random(31 + i), 0.78, roots)

    canopy: list = []
    for i, ang in enumerate((-math.pi / 2 - 0.66, -math.pi / 2, -math.pi / 2 + 0.66)):
        _limbs(0.5, _FORK_Y + 0.016, ang, 0.135 if i == 1 else 0.115,
               9.5 / 256, canopy_depth, _random.Random(101 + i * 7), 0.60, canopy)

    _TREE_CACHE[key] = (canopy, roots)
    return canopy, roots


def _stroke_limbs(cr, limbs, x, y, size, color, glow=0.0, alpha=1.0):
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    if glow > 0.0:
        for x1, y1, x2, y2, w in limbs:
            cr.set_line_width(w * size + glow)
            rgb(cr, color, 0.16 * alpha)
            cr.move_to(x + x1 * size, y + y1 * size)
            cr.line_to(x + x2 * size, y + y2 * size)
            cr.stroke()
    for x1, y1, x2, y2, w in limbs:
        cr.set_line_width(max(0.6, w * size))
        rgb(cr, color, alpha)
        cr.move_to(x + x1 * size, y + y1 * size)
        cr.line_to(x + x2 * size, y + y2 * size)
        cr.stroke()


def draw_yggdrasil(cr, x, y, size, *, color=None, root_color=None, glow=0.0,
                   canopy_depth=5, root_depth=3, alpha=1.0, sparks=False,
                   spark_color=None):
    """Draw the world tree with its bounding box top-left at (x, y)."""
    color = color or GOLD_HI
    root_color = root_color or mix(BRONZE, VOID, 0.2)
    canopy, roots = yggdrasil_geometry(canopy_depth, root_depth)

    cr.save()
    _stroke_limbs(cr, roots, x, y, size, root_color, glow * 0.7, alpha)

    # tapered trunk, drawn as a filled plate rather than a constant-width stroke
    w_base, w_fork = 15.0 / 256 * size, 8.0 / 256 * size
    cxp = x + 0.5 * size
    by, fy = y + _BASE_Y * size, y + _FORK_Y * size
    mid = (by - fy) * 0.5
    cr.new_path()
    cr.move_to(cxp - w_base, by)
    cr.curve_to(cxp - w_base * 0.7, by - mid, cxp - w_fork, fy + mid * 0.35,
                cxp - w_fork, fy)
    cr.line_to(cxp + w_fork, fy)
    cr.curve_to(cxp + w_fork, fy + mid * 0.35, cxp + w_base * 0.7, by - mid,
                cxp + w_base, by)
    cr.close_path()
    tg = cairo.LinearGradient(cxp - w_base, 0, cxp + w_base, 0)
    tg.add_color_stop_rgba(0.0, *mix(color, VOID, 0.45), alpha)
    tg.add_color_stop_rgba(0.45, *color, alpha)
    tg.add_color_stop_rgba(1.0, *mix(color, VOID, 0.3), alpha)
    cr.set_source(tg)
    cr.fill()

    _stroke_limbs(cr, canopy, x, y, size, color, glow, alpha)

    if sparks:
        for x1, y1, x2, y2, w in canopy:
            if w < 2.6 / 256:          # only the finest tips carry sparks
                soft_disc(cr, x + x2 * size, y + y2 * size, size * 0.031,
                          spark_color or CYAN, 0.55 * alpha)
    cr.restore()
