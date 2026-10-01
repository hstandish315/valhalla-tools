#!/usr/bin/env python3
"""
Render the application icon: Yggdrasil, the world tree, under a storm.

The tree is generated recursively rather than drawn by hand, but the recursion
is kept deliberately shallow and the limbs deliberately thick. An icon has to
survive being scaled to 32 px in the app grid, and a finely branched fractal
turns to mush at that size - silhouette beats detail.
"""
from __future__ import annotations

import math
import os
import random
import sys

import cairo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from valhalla import lightning as L  # noqa: E402
from valhalla import theme as T  # noqa: E402


def render(size: int, path: str):
    s = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(s)
    u = size / 256.0

    # plate
    T.rounded_rect(cr, 8 * u, 8 * u, size - 16 * u, size - 16 * u, 52 * u)
    g = cairo.LinearGradient(0, 0, size * 0.4, size)
    g.add_color_stop_rgb(0.0, *T.PANEL_HI)
    g.add_color_stop_rgb(0.6, *T.DEEP)
    g.add_color_stop_rgb(1.0, *T.VOID)
    cr.set_source(g)
    cr.fill_preserve()
    cr.save()
    cr.clip()

    cx = size / 2
    base_y = size * 0.745        # where the trunk meets the roots
    fork_y = size * 0.505        # where the trunk opens into the canopy
    ring_y = size * 0.515

    # storm halo behind the canopy, faint ground glow beneath the roots
    T.soft_disc(cr, cx, size * 0.36, size * 0.40, T.CYAN, 0.34)
    T.soft_disc(cr, cx, size * 0.80, size * 0.24, T.SEA, 0.14)

    # the encircling world-ring, echoing the dashboard's gauges
    cr.set_line_width(4.5 * u)
    T.rgb(cr, T.BRONZE, 0.55)
    cr.arc(cx, ring_y, size * 0.355, 0, 2 * math.pi)
    cr.stroke()

    # the world tree itself, shared with the header banner (theme.draw_yggdrasil)
    T.draw_yggdrasil(cr, 0, 0, size, glow=7 * u, canopy_depth=5, root_depth=3,
                     sparks=True)

    # the strike: down through the crown, plus a smaller fork to its right
    bolt = L.make_bolt(cx - size * 0.17, size * 0.045, cx - size * 0.02,
                       size * 0.30, color=T.CYAN, width=2.6 * u, life=1.0,
                       detail=4, jag=0.13, fork_chance=0.5,
                       rng=random.Random(5), now=0.0)
    L.draw_bolt(cr, bolt, 0.02, 1.0)
    bolt2 = L.make_bolt(cx + size * 0.22, size * 0.085, cx + size * 0.12,
                        size * 0.30, color=T.BOLT, width=1.5 * u, life=1.0,
                        detail=3, jag=0.16, fork_chance=0.2,
                        rng=random.Random(9), now=0.0)
    L.draw_bolt(cr, bolt2, 0.02, 0.7)

    cr.restore()
    T.rounded_rect(cr, 8 * u, 8 * u, size - 16 * u, size - 16 * u, 52 * u)
    T.rgb(cr, T.BRONZE, 0.85)
    cr.set_line_width(3 * u)
    cr.stroke()

    s.write_to_png(path)
    return path


if __name__ == "__main__":
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(root, "valhalla-monitor.png")
    render(256, out)
    print(f"wrote {out}")
