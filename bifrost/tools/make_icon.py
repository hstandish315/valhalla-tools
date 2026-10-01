#!/usr/bin/env python3
"""Render the 256px app icon: the bridge arch, an orb mid-sweep, an ear at each end.

    venv/bin/python3 tools/make_icon.py [out.png]
"""
from __future__ import annotations

import math
import os
import sys

import cairo
import gi

gi.require_version("Gtk", "4.0")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bifrost import theme as T  # noqa: E402


def main() -> None:
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bifrost-audio.png")
    S = 256
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, S, S)
    cr = cairo.Context(surf)

    T.rounded_rect(cr, 6, 6, S - 12, S - 12, 44)
    g = cairo.LinearGradient(0, 0, S, S)
    g.add_color_stop_rgb(0.0, *T.PANEL_HI)
    g.add_color_stop_rgb(1.0, *T.VOID)
    cr.set_source(g)
    cr.fill_preserve()
    T.rgb(cr, T.BRONZE, 0.8)
    cr.set_line_width(3)
    cr.stroke()

    x0, x1, base, rise = 58, S - 58, 168, 96
    for i, off in enumerate((-9, -6, -3, 0, 3, 6, 9)):
        cr.new_path()
        for k in range(61):
            u = k / 30.0 - 1.0
            x = x0 + (x1 - x0) * (u * 0.5 + 0.5)
            y = base + off * 0.7 - rise * (1 - u * u)
            (cr.move_to if k == 0 else cr.line_to)(x, y)
        T.rgb(cr, T.mix(T.CYAN, T.VIOLET, i / 6.0), 0.9)
        cr.set_line_width(3.2)
        cr.stroke()

    for ex, col in ((x0, T.CYAN), (x1, T.VIOLET)):
        T.soft_disc(cr, ex, base + 6, 34, col, 0.5)
        T.rgb(cr, col, 1.0)
        cr.set_line_width(4)
        cr.arc(ex, base + 6, 11, 0, 2 * math.pi)
        cr.stroke()

    ox = x0 + (x1 - x0) * 0.34
    u = (ox - x0) / (x1 - x0) * 2 - 1
    oy = base - rise * (1 - u * u)
    T.soft_disc(cr, ox, oy, 46, T.CYAN, 0.7)
    T.soft_disc(cr, ox, oy, 15, T.BOLT, 1.0)

    T.draw_tracked(cr, S / 2, 202, "BIFROST", 19, T.GOLD_HI, tracking=5.0, align="center")
    surf.write_to_png(out)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
