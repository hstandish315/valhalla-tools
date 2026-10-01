#!/usr/bin/env python3
"""
Generate every figure used by SETUP.md.

These are true renders, not screenshots: they call the same draw functions the
running app calls, so they cannot drift from what the window shows. That is also
the only option here — Wayland blocks desktop capture on the author's Wayland desktop and GTK4
dropped Gtk.OffscreenWindow.

    python3 tools/make_docs.py
"""
from __future__ import annotations

import math
import os
import subprocess
import sys

import cairo
import gi

gi.require_version("Gtk", "4.0")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from valhalla import theme as T          # noqa: E402
from valhalla.gauges import RadialGauge  # noqa: E402
from valhalla.metrics import Sampler  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _demo  # noqa: E402  (synthetic host identity for published figures)
from valhalla.panels import HeaderBar    # noqa: E402

DOCS = os.path.join(ROOT, "docs")


def _preview(out, *args):
    cmd = [sys.executable, os.path.join(ROOT, "tools", "render_preview.py"),
           os.path.join(DOCS, out), *args]
    subprocess.run(cmd, check=True, capture_output=True)
    print(f"  docs/{out}")


def dashboards():
    _preview("preview.png", "--load", "0.72")
    _preview("dashboard-idle.png")
    _preview("dashboard-storm.png", "--load", "0.96")
    _preview("compact.png", "--load", "0.45", "--width", "1200", "--height", "820")


def banner(frames=3):
    W, H = 1480, 72
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, (H + 8) * frames)
    cr = cairo.Context(surface)
    T.rgb(cr, T.VOID, 1.0)
    cr.paint()
    snap = _demo.anonymise(Sampler(lambda *a: None).sample())
    hd = HeaderBar(_demo.HOST)
    hd.get_width = lambda: W
    hd.get_height = lambda: H
    hd.update(snap)
    hd.storm.rng.seed(4)
    row = 0
    for i in range(1, 900):
        hd.tick(1 / 30.0, i / 30.0)
        if i % 43 == 0 and row < frames:
            cr.save()
            cr.translate(0, row * (H + 8))
            cr.rectangle(0, 0, W, H)
            cr.clip()
            hd._draw(hd, cr, W, H)
            cr.restore()
            row += 1
    surface.write_to_png(os.path.join(DOCS, "banner.png"))
    print("  docs/banner.png")


def icon_sheet():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "mi", os.path.join(ROOT, "tools", "make_icon.py"))
    mi = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mi)
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 470, 268)
    cr = cairo.Context(surface)
    T.rgb(cr, T.VOID, 1.0)
    cr.paint()
    tmp = os.path.join(DOCS, "_tmp_icon.png")
    for size, x, y in ((256, 4, 6), (96, 274, 6), (48, 274, 112), (32, 334, 112)):
        mi.render(size, tmp)
        img = cairo.ImageSurface.create_from_png(tmp)
        cr.save()
        cr.translate(x, y)
        cr.set_source_surface(img, 0, 0)
        cr.paint()
        cr.restore()
        T.draw_text(cr, x + size / 2, y + size + 4, f"{size}px", 10, T.TEXT_MUTE,
                    align="center")
    os.remove(tmp)
    surface.write_to_png(os.path.join(DOCS, "icon.png"))
    print("  docs/icon.png")


def gauge_anatomy():
    """A single gauge with callouts, so the guide can name each element."""
    GW, GH = 360, 330
    W, H = 980, 350
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    cr = cairo.Context(surface)
    T.rgb(cr, T.VOID, 1.0)
    cr.paint()

    g = RadialGauge("SURTR", "GPU · core utilisation", T.AMBER, "sowilo")
    g.get_width = lambda: GW
    g.get_height = lambda: GH
    g.update(68, [("temp", "63°"), ("power", "260W"), ("vram", "10.5/16G")],
             corner="RTX 5080")
    for _ in range(200):
        g.tick(1 / 60.0, 8.0)

    ox, oy = 14, 10
    cr.save()
    cr.translate(ox, oy)
    cr.rectangle(0, 0, GW, GH)
    cr.clip()
    g._draw(g, cr, GW, GH)
    cr.restore()

    cx, cy, r = g._geometry(GW, GH)
    cx += ox
    cy += oy

    def callout(px, py, tx, ty, title, body):
        cr.save()
        cr.set_line_width(1.0)
        T.rgb(cr, T.STEEL, 0.95)
        cr.move_to(px, py)
        cr.line_to(tx - 10, ty + 6)
        cr.line_to(tx - 4, ty + 6)
        cr.stroke()
        T.rgb(cr, T.AMBER, 0.95)
        cr.arc(px, py, 2.6, 0, 2 * math.pi)
        cr.fill()
        cr.restore()
        T.draw_tracked(cr, tx, ty, title, 9.0, T.TEXT, tracking=1.8)
        T.draw_text(cr, tx, ty + 13, body, 10.0, T.TEXT_DIM)

    frac = g.shown / 100.0
    ang_end = math.radians(135) + 1.5 * math.pi * frac
    ang_mid = math.radians(135) + 1.5 * math.pi * frac * 0.55
    tx = ox + GW + 62
    # every anchor sits on structure - a rune stroke, an arc, a divider - so no
    # marker lands on top of a label it is meant to be pointing at
    callout(ox + 22, oy + 38, tx, 18, "channel rune + name",
            "Huginn / Surtr / Mímir / Fáfnir")
    callout(ox + GW - 34, oy + 36, tx, 58, "device",
            "the hardware or volume behind this channel")
    callout(cx + math.cos(math.radians(152)) * (r + 20),
            cy + math.sin(math.radians(152)) * (r + 20), tx, 98, "rune ticks",
            "lit as far as the current reading")
    callout(cx + math.cos(ang_mid) * r, cy + math.sin(ang_mid) * r, tx, 138,
            "sweep arc", "own colour to 75%, warms past it, red past 88%")
    callout(cx + math.cos(ang_end) * r, cy + math.sin(ang_end) * r, tx, 178,
            "leading spark", "the live edge of the reading")
    callout(cx + 6, cy + 42, tx, 218, "readout", "the number, eased so it glides")
    callout(ox + 16 + (GW - 32) / 3 * 2, oy + GH - 34, tx, 258, "stat chips",
            "three secondary figures for the channel")
    T.draw_text(cr, tx, 300, "Past 75% the ring warms and throws lightning;",
                10.0, T.TEXT_MUTE)
    T.draw_text(cr, tx, 315, "past 88% it goes red. An alarm, not decoration.",
                10.0, T.TEXT_MUTE)

    surface.write_to_png(os.path.join(DOCS, "gauge-anatomy.png"))
    print("  docs/gauge-anatomy.png")


if __name__ == "__main__":
    os.makedirs(DOCS, exist_ok=True)
    print("generating figures:")
    dashboards()
    banner()
    icon_sheet()
    gauge_anatomy()
    print("done")
