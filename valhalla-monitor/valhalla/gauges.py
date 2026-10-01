"""
The radial gauges - the four hero readouts along the top of the dashboard.

Cairo has no conic gradient, so the sweep is built from ~64 short arc segments
whose colours are interpolated along the sweep. Seams are hidden by overlapping
each segment slightly. The whole thing is drawn twice: once wide and faint for
the bloom, once tight and bright for the trace.
"""

from __future__ import annotations

import math

import cairo
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from . import lightning as L  # noqa: E402
from . import theme as T  # noqa: E402

ARC_START = 0.75 * math.pi          # 135 deg
ARC_SWEEP = 1.50 * math.pi          # 270 deg, gap at the bottom
SEGMENTS = 64


class RadialGauge(Gtk.DrawingArea):
    """One metric: a big arc, a numeric core, and three stat chips beneath."""

    def __init__(self, title: str, subtitle: str, accent, rune: str,
                 unit: str = "%", core_label: str = "load"):
        super().__init__()
        self.title = title
        self.subtitle = subtitle
        self.accent = accent
        self.rune = rune
        self.unit = unit
        self.core_label = core_label

        self.target = 0.0        # 0..100, set from the sampler
        self.shown = 0.0         # eased toward target for smooth motion
        self.chips: list[tuple[str, str]] = []
        self.corner = ""         # tiny top-right annotation (e.g. "16.0G")
        self.offline = False
        self._dirty = True

        self.storm = L.Storm(max_bolts=5)
        self._t = 0.0
        self._spin = 0.0
        self.calm = False        # when set, drop the ambient spin and idle out
        self._plate_cache: tuple | None = None

        self.set_content_width(300)
        self.set_content_height(320)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)

    # -- state ------------------------------------------------------------

    def update(self, value: float, chips, corner: str = "", offline: bool = False):
        self._dirty = True
        self.target = max(0.0, min(100.0, float(value)))
        self.chips = list(chips)[:3]
        self.corner = corner
        self.offline = offline

    def tick(self, dt: float, now: float) -> bool:
        """Advance animation. Returns True when a redraw is warranted."""
        self._t = now
        prev = self.shown
        self.shown += (self.target - self.shown) * min(1.0, dt * 7.0)
        if not self.calm:
            self._spin = (self._spin + dt * 0.35) % (2 * math.pi)

        frac = self.shown / 100.0
        self.storm.prune(now)
        if not self.offline and frac > T.WARN_AT:
            pressure = (frac - T.WARN_AT) / (1.0 - T.WARN_AT)
            if self.storm.due(now, pressure, base=1.9, fastest=0.12):
                self._strike(now, pressure)

        if self.calm:
            # Fully idle between changes - no repaint, no CPU.
            dirty = self._dirty or self.storm.active or abs(self.shown - prev) > 0.01
            self._dirty = False
            return dirty
        return True

    def _strike(self, now: float, pressure: float):
        w, h = self.get_width(), self.get_height()
        if w < 10:
            return
        cx, cy, r = self._geometry(w, h)
        a0 = ARC_START
        a1 = ARC_START + ARC_SWEEP * (self.shown / 100.0)
        rng = self.storm.rng
        span = max(0.25, (a1 - a0) * rng.uniform(0.25, 0.7))
        s = rng.uniform(a0, max(a0, a1 - span))
        self.storm.add(L.make_arc_bolt(cx, cy, r, s, s + span,
                                       color=T.heat(self.accent, self.shown / 100.0),
                                       width=1.5, life=0.42 + 0.25 * pressure,
                                       jitter=r * 0.06, rng=rng, now=now))
        if rng.random() < 0.4 * pressure:
            ang = rng.uniform(0, 2 * math.pi)
            self.storm.add(L.make_bolt(
                cx + math.cos(ang) * r * 0.9, cy + math.sin(ang) * r * 0.9,
                cx - math.cos(ang) * r * 0.55, cy - math.sin(ang) * r * 0.55,
                color=T.BOLT, width=1.2, life=0.3, detail=4, jag=0.16,
                fork_chance=0.3, rng=rng, now=now))

    def _geometry(self, w, h):
        """
        The ring must clear the header text above and the stat chips below.
        Ticks reach r + 23, so that headroom is subtracted from the radius
        rather than letting the ring grow into the type.
        """
        top, bottom = 46.0, h - 58.0
        cy = (top + bottom) / 2.0
        r = max(38.0, min(w * 0.34, (bottom - top) / 2.0 - 23.0))
        return w / 2.0, cy, r

    # -- painting ---------------------------------------------------------

    def _draw(self, _area, cr, w, h):
        frac = self.shown / 100.0
        col = T.heat(self.accent, frac) if not self.offline else T.STEEL
        cr.set_line_cap(cairo.LINE_CAP_ROUND)

        self._blit_plate(cr, w, h, col, frac)
        self._header(cr, w, col)

        cx, cy, r = self._geometry(w, h)
        self._ticks(cr, cx, cy, r, col, frac)
        self._track(cr, cx, cy, r)
        if not self.offline:
            self._sweep(cr, cx, cy, r, col, frac)
        self._core(cr, cx, cy, r, col, frac)

        cr.save()
        cr.rectangle(0, 0, w, h)
        cr.clip()
        self.storm.draw(cr, self._t, 1.0)
        cr.restore()

        self._chips(cr, w, h, col)

    def _blit_plate(self, cr, w, h, col, frac):
        """
        The plate is static apart from a load-driven accent wash, so it is
        rendered once into an image surface and re-blitted. Quantising the key
        means slow easing reuses the same surface for many frames.
        """
        key = (w, h, round(col[0], 2), round(col[1], 2), round(col[2], 2),
               round(frac, 2))
        cached = self._plate_cache
        if cached is None or cached[0] != key:
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(w), int(h))
            pcr = cairo.Context(surface)
            pcr.set_line_cap(cairo.LINE_CAP_ROUND)
            self._plate(pcr, w, h, col, frac)
            cached = (key, surface)
            self._plate_cache = cached
        cr.set_source_surface(cached[1], 0, 0)
        cr.paint()

    def _plate(self, cr, w, h, col, frac):
        T.clipped_rect(cr, 1.5, 1.5, w - 3, h - 3, 16)
        g = cairo.LinearGradient(0, 0, w * 0.35, h)
        g.add_color_stop_rgb(0.0, *T.PANEL_HI)
        g.add_color_stop_rgb(0.55, *T.PANEL)
        g.add_color_stop_rgb(1.0, *T.DEEP)
        cr.set_source(g)
        cr.fill_preserve()

        # accent wash from the top edge, brighter as the metric loads up
        gw = cairo.LinearGradient(0, 0, 0, h * 0.7)
        gw.add_color_stop_rgba(0.0, col[0], col[1], col[2], 0.11 + 0.13 * frac)
        gw.add_color_stop_rgba(1.0, col[0], col[1], col[2], 0.0)
        cr.set_source(gw)
        cr.fill_preserve()

        T.rgb(cr, T.EDGE, 0.95)
        cr.set_line_width(1.2)
        cr.stroke()

        # lit top rail
        cr.save()
        T.clipped_rect(cr, 1.5, 1.5, w - 3, h - 3, 16)
        cr.clip()
        gr = cairo.LinearGradient(w * 0.15, 0, w * 0.85, 0)
        gr.add_color_stop_rgba(0.0, col[0], col[1], col[2], 0.0)
        gr.add_color_stop_rgba(0.5, col[0], col[1], col[2], 0.75 + 0.25 * frac)
        gr.add_color_stop_rgba(1.0, col[0], col[1], col[2], 0.0)
        cr.set_source(gr)
        cr.rectangle(0, 1.5, w, 2.0)
        cr.fill()
        cr.restore()

        T.corner_brackets(cr, 9, 9, w - 18, h - 18, 17, T.BRONZE, 0.5, 1.2)

    def _header(self, cr, w, col):
        T.draw_rune(cr, self.rune, 16, 17, 15, col, width=1.7, alpha=0.95, glow=0.7)
        T.draw_tracked(cr, 40, 15, self.title, 14.5, T.TEXT, tracking=2.6, bold=True)
        T.draw_text(cr, 40, 33, self.subtitle, 9.5, T.TEXT_MUTE)
        if self.corner:
            T.draw_text(cr, w - 16, 17, self.corner, 10.0, T.TEXT_DIM,
                        family=T.FONT_NUM, align="right")

    def _ticks(self, cr, cx, cy, r, col, frac):
        """
        49 ticks in four groups (lit/unlit x minor/major).

        Stroking each tick individually cost more than the rest of the gauge
        put together, so the ticks are accumulated into one path per group and
        stroked once.
        """
        outer = r + 15
        n = 48
        groups = {"lit_minor": [], "lit_major": [], "off_minor": [], "off_major": []}
        for i in range(n + 1):
            t = i / n
            ang = ARC_START + ARC_SWEEP * t
            major = (i % 8 == 0)
            lit = t <= frac and not self.offline
            length = 8.0 if major else 4.0
            ca, sa = math.cos(ang), math.sin(ang)
            groups[f"{'lit' if lit else 'off'}_{'major' if major else 'minor'}"].append(
                (cx + ca * outer, cy + sa * outer,
                 cx + ca * (outer + length), cy + sa * (outer + length)))

        cr.save()
        cr.set_line_cap(cairo.LINE_CAP_BUTT)
        for key, color, alpha, width in (
            ("off_minor", T.STEEL, 0.32, 1.1),
            ("off_major", T.BRONZE, 0.55, 1.7),
            ("lit_minor", col, 0.95, 1.1),
            ("lit_major", col, 0.95, 1.7),
        ):
            segs = groups[key]
            if not segs:
                continue
            cr.new_path()
            for x0, y0, x1, y1 in segs:
                cr.move_to(x0, y0)
                cr.line_to(x1, y1)
            cr.set_line_width(width)
            T.rgb(cr, color, alpha)
            cr.stroke()
        cr.restore()

    def _track(self, cr, cx, cy, r):
        cr.set_line_width(13)
        T.rgb(cr, T.TRACK, 1.0)
        cr.arc(cx, cy, r, ARC_START, ARC_START + ARC_SWEEP)
        cr.stroke()
        cr.set_line_width(1.0)
        T.rgb(cr, T.EDGE, 0.9)
        for rr in (r - 7.0, r + 7.0):
            cr.arc(cx, cy, rr, ARC_START, ARC_START + ARC_SWEEP)
            cr.stroke()

    def _sweep(self, cr, cx, cy, r, col, frac):
        if frac <= 0.001:
            return
        end = ARC_START + ARC_SWEEP * frac
        cool = T.mix(col, T.mix(col, T.VOID, 0.45), 0.5)

        # bloom
        cr.set_line_width(26)
        T.rgb(cr, col, 0.13)
        cr.arc(cx, cy, r, ARC_START, end)
        cr.stroke()
        cr.set_line_width(18)
        T.rgb(cr, col, 0.20)
        cr.arc(cx, cy, r, ARC_START, end)
        cr.stroke()

        # segmented sweep gradient
        cr.set_line_width(12)
        steps = max(2, int(min(SEGMENTS, r * 0.5) * frac))
        span = (end - ARC_START) / steps
        for i in range(steps):
            a = ARC_START + span * i
            t = i / max(1, steps - 1)
            T.rgb(cr, T.mix(cool, col, 0.25 + 0.75 * t), 1.0)
            cr.arc(cx, cy, r, a, a + span * 1.06)
            cr.stroke()

        # inner highlight rail
        cr.set_line_width(2.0)
        T.rgb(cr, T.mix(col, T.BOLT, 0.55), 0.5)
        cr.arc(cx, cy, r - 4.0, ARC_START, end)
        cr.stroke()

        # leading spark
        ex, ey = cx + math.cos(end) * r, cy + math.sin(end) * r
        T.soft_disc(cr, ex, ey, 22, col, 0.55)
        T.rgb(cr, T.BOLT, 0.95)
        cr.arc(ex, ey, 3.4, 0, 2 * math.pi)
        cr.fill()

    def _core(self, cr, cx, cy, r, col, frac):
        # inner wash
        T.soft_disc(cr, cx, cy, r * 0.95, col, 0.05 + 0.16 * frac)

        # slow scan ring, so an idle machine still looks alive
        cr.save()
        cr.set_line_width(1.0)
        for k in range(3):
            a = self._spin + k * 2.094
            T.rgb(cr, col, 0.18)
            cr.arc(cx, cy, r * 0.72, a, a + 0.55)
            cr.stroke()
        cr.restore()

        cr.set_line_width(1.0)
        T.rgb(cr, T.EDGE, 0.8)
        cr.arc(cx, cy, r * 0.6, 0, 2 * math.pi)
        cr.stroke()

        if self.offline:
            T.draw_text(cr, cx, cy - 6, "--", 40, T.STEEL, bold=True, align="center")
            T.draw_tracked(cr, cx, cy + 26, "OFFLINE", 9, T.TEXT_MUTE,
                           tracking=3.0, align="center")
            return

        value = f"{self.shown:.0f}" if self.unit == "%" else f"{self.shown:.0f}"
        size = 50 if r > 74 else 40
        vw, _ = T.text_size(cr, value, size, T.FONT_UI, True)
        uw, _ = T.text_size(cr, self.unit, size * 0.42, T.FONT_UI, True)
        left = cx - (vw + uw + 3) / 2.0
        T.draw_text(cr, left, cy - size * 0.62, value, size, T.TEXT, bold=True,
                    glow=0.5 * min(1.0, frac * 1.4 + 0.15))
        T.draw_text(cr, left + vw + 3, cy - size * 0.10, self.unit, size * 0.42,
                    col, bold=True)
        T.draw_tracked(cr, cx, cy + size * 0.42, self.core_label, 9.0, T.TEXT_MUTE,
                       tracking=3.2, align="center")

    def _chips(self, cr, w, h, col):
        if not self.chips:
            return
        y = h - 46
        T.knot_divider(cr, 22, y - 8, w - 44, T.BRONZE, 0.35)
        n = len(self.chips)
        cw = (w - 32) / n
        for i, (label, value) in enumerate(self.chips):
            cx = 16 + cw * i + cw / 2
            if i:
                T.rgb(cr, T.EDGE, 0.8)
                cr.set_line_width(1.0)
                cr.move_to(16 + cw * i, y + 4)
                cr.line_to(16 + cw * i, y + 30)
                cr.stroke()
            T.draw_text(cr, cx, y + 4, value, 14.0, T.TEXT, family=T.FONT_NUM,
                        bold=True, align="center")
            T.draw_tracked(cr, cx, y + 23, label, 8.0, T.TEXT_MUTE, tracking=1.8,
                           align="center")
