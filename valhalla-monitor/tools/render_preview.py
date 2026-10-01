#!/usr/bin/env python3
"""
Offscreen preview renderer.

Wayland blocks desktop capture on the author's Wayland desktop, and GTK4 dropped
Gtk.OffscreenWindow, so the way to actually look at the dashboard while
iterating is to drive the widgets' draw functions straight onto an
ImageSurface using the same geometry the real window lays out.

    python3 tools/render_preview.py out.png [--load 0.85]
"""
from __future__ import annotations

import argparse
import math
import os
import sys

import cairo
import gi

gi.require_version("Gtk", "4.0")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from valhalla import theme as T          # noqa: E402
from valhalla.gauges import RadialGauge  # noqa: E402
from valhalla.metrics import Sampler, fmt_bytes  # noqa: E402
import _demo  # noqa: E402  (synthetic host identity for published figures)
from valhalla.panels import (CPUPanel, GPUPanel, HeaderBar,  # noqa: E402
                             MemoryPanel, StoragePanel)

W, H = 1480, 940
HEADER_H = 72
GAUGE_H = 320
MARGIN = 12
GAP = 10


class Fake:
    """Stands in for the GTK allocation the widgets normally query."""

    def __init__(self, w, h):
        self._w, self._h = w, h

    def get_width(self):
        return self._w

    def get_height(self):
        return self._h


def blit(cr, widget, x, y, w, h, t=0.0):
    widget._t = t
    widget.get_width = lambda: w
    widget.get_height = lambda: h
    cr.save()
    cr.translate(x, y)
    cr.rectangle(0, 0, w, h)
    cr.clip()
    widget._draw(widget, cr, w, h)
    cr.restore()


def synthesise(snap, load: float):
    """Push the machine into a hypothetical load so the storm states are visible."""
    import random
    rng = random.Random(7)
    snap.cpu.per_core = [max(0.0, min(100.0, load * 100 * rng.uniform(0.45, 1.25)))
                         for _ in snap.cpu.per_core]
    snap.cpu.percent = sum(snap.cpu.per_core) / len(snap.cpu.per_core)
    snap.cpu.temp = 38 + load * 52
    snap.cpu.ccd = [("Tccd1", 36 + load * 50), ("Tccd2", 35 + load * 48)]
    snap.cpu.freq_mhz = 3600 + load * 1900
    snap.gpu.util = load * 100 * 0.96
    snap.gpu.mem_used = snap.gpu.mem_total * (0.12 + load * 0.75)
    snap.gpu.temp = 34 + load * 40
    snap.gpu.power = 30 + load * 320
    snap.gpu.clock_sm = 900 + load * 1900
    snap.gpu.fan = load * 78
    snap.ram.percent = 8 + load * 78
    snap.ram.used = int(snap.ram.total * snap.ram.percent / 100)
    snap.ram.available = snap.ram.total - snap.ram.used
    snap.disk.read_bps = load * 900e6
    snap.disk.write_bps = load * 420e6
    for m in snap.disk.mounts:
        m.percent = min(99.0, m.percent + load * 70)
        m.used = int(m.total * m.percent / 100)
    snap.disk.percent = snap.disk.mounts[0].percent if snap.disk.mounts else 0
    snap.disk.used = int(snap.disk.total * snap.disk.percent / 100)
    return snap


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?", default="preview.png")
    ap.add_argument("--load", type=float, default=None,
                    help="0..1 synthetic load; omit to render live values")
    ap.add_argument("--width", type=int, default=W)
    ap.add_argument("--height", type=int, default=H)
    args = ap.parse_args()

    sampler = Sampler(lambda *a: None)
    sampler.sample()                 # prime cpu deltas
    snap = _demo.anonymise(sampler.sample())
    hist = sampler.history
    if args.load is not None:
        snap = synthesise(snap, args.load)
        for key, base in (("cpu", snap.cpu.percent), ("gpu", snap.gpu.util),
                          ("ram", snap.ram.percent), ("io", 180 * args.load)):
            series = hist.get(key)
            series.clear()
            for i in range(series.maxlen):
                p = i / series.maxlen
                wave = (math.sin(p * 11) * 0.22 + math.sin(p * 3.1) * 0.3 +
                        math.sin(p * 27) * 0.08)
                series.append(max(0.0, min(100.0 if key != "io" else 1e9,
                                           base * (0.55 + 0.45 * (0.5 + wave)))))

    w, h = args.width, args.height
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(surface)
    T.rgb(cr, T.VOID, 1.0)
    cr.rectangle(0, 0, w, h)
    cr.fill()

    header = HeaderBar(_demo.HOST)
    gauges = [
        RadialGauge("HUGINN", "CPU · Ryzen 9 9900X", T.CYAN, "hagalaz"),
        RadialGauge("SURTR", "GPU · core utilisation", T.AMBER, "sowilo"),
        RadialGauge("MIMIR", "MEMORY · system RAM", T.VIOLET, "mannaz",
                    core_label="used"),
        RadialGauge("FAFNIR", "STORAGE · root volume", T.SEA, "fehu",
                    core_label="used"),
    ]
    p_cpu, p_gpu, p_ram, p_dsk = CPUPanel(), GPUPanel(), MemoryPanel(), StoragePanel()

    header.update(snap, hist)
    g = snap.gpu
    gauges[0].update(snap.cpu.percent,
                     [("temp", f"{snap.cpu.temp:.0f}°"),
                      ("clock", f"{snap.cpu.freq_mhz/1000:.2f}G"),
                      ("load", f"{snap.cpu.load[0]:.2f}")],
                     corner=f"{len(snap.cpu.per_core)}T")
    gauges[1].update(g.util,
                     [("temp", f"{g.temp:.0f}°"), ("power", f"{g.power:.0f}W"),
                      ("vram", f"{g.mem_used/1024:.1f}/{g.mem_total/1024:.0f}G")],
                     corner=g.name.replace("NVIDIA GeForce ", ""))
    gauges[2].update(snap.ram.percent,
                     [("used", fmt_bytes(snap.ram.used)),
                      ("free", fmt_bytes(snap.ram.available)),
                      ("swap", f"{snap.ram.swap_percent:.0f}%")],
                     corner=f"{fmt_bytes(snap.ram.total)}iB")
    gauges[3].update(snap.disk.percent,
                     [("used", fmt_bytes(snap.disk.used)),
                      ("free", fmt_bytes(snap.disk.total - snap.disk.used)),
                      ("i/o", f"{(snap.disk.read_bps+snap.disk.write_bps)/(1<<20):.0f}M")],
                     corner=f"{fmt_bytes(snap.disk.total)}iB")
    for p in (p_cpu, p_gpu, p_ram, p_dsk):
        p.update(snap, hist)

    # settle the easing so the still frame shows final values
    for widget in [header, *gauges, p_cpu, p_gpu, p_ram, p_dsk]:
        widget.get_width = lambda: 300
        widget.get_height = lambda: 300
        for _ in range(120):
            widget.tick(1 / 60.0, 12.0)

    blit(cr, header, 0, 0, w, HEADER_H, 12.0)

    gw = (w - 2 * MARGIN - 3 * GAP) / 4
    for i, gauge in enumerate(gauges):
        blit(cr, gauge, MARGIN + i * (gw + GAP), HEADER_H + MARGIN, gw, GAUGE_H, 12.0)

    by = HEADER_H + MARGIN + GAUGE_H + GAP
    bh = h - by - MARGIN
    total = w - 2 * MARGIN - 2 * GAP
    cpu_w = total * 0.40
    gpu_w = total * 0.29
    right_w = total - cpu_w - gpu_w
    blit(cr, p_cpu, MARGIN, by, cpu_w, bh, 12.0)
    blit(cr, p_gpu, MARGIN + cpu_w + GAP, by, gpu_w, bh, 12.0)
    rx = MARGIN + cpu_w + GAP + gpu_w + GAP
    ram_h = (bh - GAP) * 0.46
    blit(cr, p_ram, rx, by, right_w, ram_h, 12.0)
    blit(cr, p_dsk, rx, by + ram_h + GAP, right_w, bh - ram_h - GAP, 12.0)

    surface.write_to_png(args.out)
    print(f"wrote {args.out} ({w}x{h})")


if __name__ == "__main__":
    main()
