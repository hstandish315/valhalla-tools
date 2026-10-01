"""
Detail panels and the header bar.

Every panel is a single DrawingArea that paints its own frame, title and body.
Using one surface per panel rather than nested GTK widgets keeps the ornament
(corner brackets, knotwork, glow bleed) continuous across the whole plate.
"""

from __future__ import annotations

import math

import cairo
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from . import lightning as L  # noqa: E402
from . import theme as T  # noqa: E402
from .metrics import fmt_bytes, fmt_ibytes, fmt_rate, fmt_uptime  # noqa: E402


class Ease:
    """Exponential smoothing, so nothing on screen ever snaps."""

    __slots__ = ("v", "t", "rate")


    def __init__(self, rate: float = 8.0, start: float = 0.0):
        self.v = self.t = start
        self.rate = rate

    def set(self, target: float):
        self.t = target

    SETTLED = 0.15      # percent; below this the change is sub-pixel on screen

    def step(self, dt: float) -> bool:
        """
        Advance; returns True while the value is still visibly in motion.

        The settle threshold is deliberately coarse. Live CPU and I/O readings
        jitter constantly, so a tight threshold means easing never completes and
        the panels repaint forever over changes too small to see.
        """
        delta = self.t - self.v
        self.v += delta * min(1.0, dt * self.rate)
        if abs(delta) <= self.SETTLED:
            self.v = self.t
            return False
        return True


# ------------------------------------------------------------- primitives ---

def meter(cr, x, y, w, h, frac, color, *, track=None, glow=True, radius=None):
    """Horizontal bar with a bloom and a bright leading edge."""
    frac = max(0.0, min(1.0, frac))
    r = radius if radius is not None else h / 2
    T.rounded_rect(cr, x, y, w, h, r)
    T.rgb(cr, track or T.TRACK, 1.0)
    cr.fill()

    if frac <= 0.002:
        T.rounded_rect(cr, x, y, w, h, r)
        T.rgb(cr, T.EDGE, 0.9)
        cr.set_line_width(1.0)
        cr.stroke()
        return

    fw = max(h, w * frac)
    if glow:
        cr.save()
        T.rounded_rect(cr, x - 3, y - 3, fw + 6, h + 6, r + 3)
        T.rgb(cr, color, 0.16)
        cr.fill()
        cr.restore()

    T.rounded_rect(cr, x, y, fw, h, r)
    g = cairo.LinearGradient(x, 0, x + fw, 0)
    g.add_color_stop_rgb(0.0, *T.mix(color, T.VOID, 0.55))
    g.add_color_stop_rgb(0.65, *color)
    g.add_color_stop_rgb(1.0, *T.mix(color, T.BOLT, 0.45))
    cr.set_source(g)
    cr.fill()

    cr.save()                       # leading edge spark
    cr.rectangle(x + fw - 2.5, y - 1, 3.0, h + 2)
    T.rgb(cr, T.BOLT, 0.85)
    cr.fill()
    cr.restore()

    T.rounded_rect(cr, x, y, w, h, r)
    T.rgb(cr, T.EDGE, 0.9)
    cr.set_line_width(1.0)
    cr.stroke()


def segbar(cr, x, y, w, h, frac, color, segments=11, vertical=True):
    """Segmented LED column - used for the per-thread CPU matrix."""
    frac = max(0.0, min(1.0, frac))
    gap = 1.6
    if vertical:
        sh = (h - gap * (segments - 1)) / segments
        for i in range(segments):
            t = (i + 0.5) / segments
            sy = y + h - (i + 1) * sh - i * gap
            lit = t <= frac
            c = T.heat(color, t) if lit else T.TRACK
            a = 1.0 if lit else 0.75
            cr.rectangle(x, sy, w, sh)
            T.rgb(cr, c, a)
            cr.fill()
            if lit and t > frac - (1.0 / segments):     # brightest at the top
                cr.rectangle(x, sy, w, sh)
                T.rgb(cr, T.BOLT, 0.30)
                cr.fill()
    else:
        sw = (w - gap * (segments - 1)) / segments
        for i in range(segments):
            t = (i + 0.5) / segments
            sx = x + i * (sw + gap)
            lit = t <= frac
            cr.rectangle(sx, y, sw, h)
            T.rgb(cr, T.heat(color, t) if lit else T.TRACK, 1.0 if lit else 0.75)
            cr.fill()


def graph(cr, x, y, w, h, series, color, *, scale=100.0, label="", fill=True,
          grid=True, dashed_at=None):
    """Filled area trend chart. `series` is oldest-first."""
    cr.save()
    cr.rectangle(x, y, w, h)
    cr.clip()

    T.rgb(cr, T.VOID, 0.55)
    cr.rectangle(x, y, w, h)
    cr.fill()

    if grid:
        cr.set_line_width(1.0)
        T.rgb(cr, T.EDGE, 0.55)
        for i in range(1, 4):
            gy = y + h * i / 4.0
            cr.move_to(x, gy)
            cr.line_to(x + w, gy)
            cr.stroke()
        for i in range(1, 6):
            gx = x + w * i / 6.0
            T.rgb(cr, T.EDGE, 0.28)
            cr.move_to(gx, y)
            cr.line_to(gx, y + h)
            cr.stroke()

    pts = list(series)
    n = len(pts)
    if n >= 2 and scale > 0:
        step = w / (n - 1)

        def yy(v):
            return y + h - max(0.0, min(1.0, v / scale)) * (h - 2) - 1

        if fill:
            cr.new_path()
            cr.move_to(x, y + h)
            for i, v in enumerate(pts):
                cr.line_to(x + i * step, yy(v))
            cr.line_to(x + w, y + h)
            cr.close_path()
            g = cairo.LinearGradient(0, y, 0, y + h)
            g.add_color_stop_rgba(0.0, color[0], color[1], color[2], 0.42)
            g.add_color_stop_rgba(1.0, color[0], color[1], color[2], 0.02)
            cr.set_source(g)
            cr.fill()

        cr.new_path()
        for i, v in enumerate(pts):
            (cr.move_to if i == 0 else cr.line_to)(x + i * step, yy(v))
        T.rgb(cr, color, 0.22)
        cr.set_line_width(4.0)
        cr.stroke_preserve()
        T.rgb(cr, color, 1.0)
        cr.set_line_width(1.5)
        cr.stroke()

        ex, ey = x + w, yy(pts[-1])
        T.soft_disc(cr, ex, ey, 10, color, 0.6)
        T.rgb(cr, T.BOLT, 0.9)
        cr.arc(ex, ey, 2.2, 0, 2 * math.pi)
        cr.fill()

    if dashed_at is not None and scale > 0:
        cr.save()
        cr.set_dash([3.0, 3.0])
        cr.set_line_width(1.0)
        T.rgb(cr, T.WARN, 0.5)
        gy = y + h - (dashed_at / scale) * h
        cr.move_to(x, gy)
        cr.line_to(x + w, gy)
        cr.stroke()
        cr.restore()

    cr.restore()
    T.rgb(cr, T.EDGE, 0.9)
    cr.set_line_width(1.0)
    cr.rectangle(x + 0.5, y + 0.5, w - 1, h - 1)
    cr.stroke()
    if label:
        T.draw_tracked(cr, x + 7, y + 5, label, 8.0, T.TEXT_MUTE, tracking=1.6)


def stat(cr, x, y, w, label, value, *, color=None, unit="", size=12.5):
    """A label/value line with a dotted leader between them."""
    T.draw_tracked(cr, x, y + 2, label, 8.5, T.TEXT_MUTE, tracking=1.5)
    vw, _ = T.text_size(cr, value, size, T.FONT_NUM, True)
    uw = 0
    if unit:
        uw, _ = T.text_size(cr, unit, size * 0.72, T.FONT_UI, False)
    T.draw_text(cr, x + w - vw - uw, y - 2, value, size, color or T.TEXT,
                family=T.FONT_NUM, bold=True)
    if unit:
        T.draw_text(cr, x + w - uw, y + 2, unit, size * 0.72, T.TEXT_MUTE)

    lw, _ = T.text_size(cr, label.upper(), 8.5, T.FONT_UI, True)
    lead_x = x + lw + 1.5 * max(0, len(label) - 1) + 8
    lead_end = x + w - vw - uw - 8
    if lead_end > lead_x:
        cr.save()
        cr.set_dash([1.0, 3.0])
        cr.set_line_width(1.0)
        T.rgb(cr, T.EDGE, 1.0)
        cr.move_to(lead_x, y + 7)
        cr.line_to(lead_end, y + 7)
        cr.stroke()
        cr.restore()


# ------------------------------------------------------------------ panel ---

class Panel(Gtk.DrawingArea):
    """Base plate: frame, rune, title, then `body()` in the content rect."""

    PAD = 16
    HEAD = 38

    def __init__(self, title: str, rune: str, accent, subtitle: str = "",
                 width: int = 400, height: int = 300):
        super().__init__()
        self.title = title
        self.rune = rune
        self.accent = accent
        self.subtitle = subtitle
        self.snap = None
        self.hist = None
        self._t = 0.0
        self._dirty = True
        self.set_content_width(width)
        self.set_content_height(height)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_draw_func(self._draw)

    def update(self, snap, hist):
        self.snap = snap
        self.hist = hist
        self._dirty = True          # new sample -> the graphs advanced

    def tick(self, dt: float, now: float) -> bool:
        self._t = now
        return self._consume()

    def _consume(self, moving: bool = False) -> bool:
        """A panel only repaints on a fresh sample or while easing is in flight."""
        dirty = self._dirty or moving
        self._dirty = False
        return dirty

    def _draw(self, _area, cr, w, h):
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        T.clipped_rect(cr, 1.5, 1.5, w - 3, h - 3, 14)
        g = cairo.LinearGradient(0, 0, w * 0.4, h)
        g.add_color_stop_rgb(0.0, *T.PANEL_HI)
        g.add_color_stop_rgb(0.6, *T.PANEL)
        g.add_color_stop_rgb(1.0, *T.DEEP)
        cr.set_source(g)
        cr.fill_preserve()
        T.rgb(cr, T.EDGE, 0.95)
        cr.set_line_width(1.2)
        cr.stroke()

        T.draw_rune(cr, self.rune, 15, 14, 14, self.accent, width=1.6,
                    alpha=0.9, glow=0.5)
        tw = T.draw_tracked(cr, 38, 13, self.title, 12.0, T.TEXT, tracking=2.4)
        if self.subtitle:
            T.draw_text(cr, 38 + tw + 10, 15, self.subtitle, 9.5, T.TEXT_MUTE)

        cr.set_line_width(1.0)
        T.rgb(cr, T.EDGE, 1.0)
        cr.move_to(14, self.HEAD - 6)
        cr.line_to(w - 14, self.HEAD - 6)
        cr.stroke()
        T.rgb(cr, self.accent, 0.85)
        cr.set_line_width(2.0)
        cr.move_to(14, self.HEAD - 6)
        cr.line_to(14 + 34, self.HEAD - 6)
        cr.stroke()

        T.corner_brackets(cr, 8, 8, w - 16, h - 16, 15, T.BRONZE, 0.42, 1.1)

        if self.snap is not None:
            cr.save()
            cr.rectangle(self.PAD, self.HEAD, w - 2 * self.PAD, h - self.HEAD - 12)
            cr.clip()
            self.body(cr, self.PAD, self.HEAD, w - 2 * self.PAD,
                      h - self.HEAD - 12)
            cr.restore()

    def body(self, cr, x, y, w, h):        # pragma: no cover - overridden
        raise NotImplementedError


# ------------------------------------------------------------- CPU panel ----

class CPUPanel(Panel):
    def __init__(self):
        super().__init__("HUGINN", "hagalaz", T.CYAN, "cores & threads",
                         width=520, height=330)
        self.cores: list[Ease] = []

    def tick(self, dt, now):
        self._t = now
        if self.snap:
            per = self.snap.cpu.per_core
            while len(self.cores) < len(per):
                self.cores.append(Ease(rate=12.0))
            for e, v in zip(self.cores, per):
                e.set(v)
        moving = False
        for e in self.cores:
            moving |= e.step(dt)
        return self._consume(moving)

    def body(self, cr, x, y, w, h):
        cpu = self.snap.cpu
        n = len(self.cores)
        if not n:
            return

        # -- thread matrix
        rows = 1 if w / n >= 15 else 2
        per_row = math.ceil(n / rows)
        # The matrix is the panel's signature element, so it scales with the
        # panel instead of sitting at a fixed height under an oversized graph.
        avail = h - 26
        bar_h = max(40.0, min(158.0, avail * (0.36 if rows == 1 else 0.20)))
        slot = w / per_row
        bw = max(4.0, slot - 4.0)
        for i, e in enumerate(self.cores):
            r, c = divmod(i, per_row)
            bx = x + c * slot
            by = y + 10 + r * (bar_h + 16)
            segbar(cr, bx, by, bw, bar_h, e.v / 100.0, T.CYAN,
                   segments=max(7, min(16, int(bar_h / 11))))
            if per_row <= 12 or i % 2 == 0:
                T.draw_text(cr, bx + bw / 2, by + bar_h + 2, str(i), 7.5,
                            T.TEXT_MUTE, family=T.FONT_NUM, align="center")

        gy = y + 10 + rows * (bar_h + 16) + 6
        T.knot_divider(cr, x, gy, w, T.BRONZE, 0.3)

        # -- stats column + trend
        sy = gy + 12
        col_w = 190
        graph_w = w - col_w - 18
        graph(cr, x, sy, graph_w, h - (sy - y) - 4, self.hist.get("cpu"), T.CYAN,
              scale=100.0, label=f"load  {self.hist.span_label()}")

        sx = x + graph_w + 18
        rows_out = [
            ("package", f"{cpu.temp:.0f}" if cpu.temp else "--", "°C",
             T.heat(T.CYAN, (cpu.temp or 0) / 95.0)),
            ("clock", f"{cpu.freq_mhz/1000:.2f}", "GHz", T.TEXT),
            ("load 1m", f"{cpu.load[0]:.2f}", "", T.TEXT),
            ("load 5m", f"{cpu.load[1]:.2f}", "", T.TEXT_DIM),
            ("load 15m", f"{cpu.load[2]:.2f}", "", T.TEXT_DIM),
            ("processes", f"{cpu.procs}", "", T.TEXT),
            ("threads", f"{cpu.running}/{cpu.threads}", "", T.TEXT),
        ]
        limit = y + h - 14          # sections drop off cleanly on a short panel
        for label, value, unit, color in rows_out:
            if sy > limit:
                break
            stat(cr, sx, sy, col_w, label, value, color=color, unit=unit, size=12.0)
            sy += 21
        for name, temp in cpu.ccd:
            if sy > limit:
                break
            stat(cr, sx, sy, col_w, name.lower(), f"{temp:.0f}", unit="°C",
                 color=T.TEXT_DIM, size=11.0)
            sy += 19

        if self.snap.procs and sy < y + h - 46:
            sy += 4
            T.draw_tracked(cr, sx, sy, "top consumers", 8.0, T.TEXT_MUTE,
                           tracking=1.6)
            sy += 14
            for p in self.snap.procs[:5]:
                if sy > y + h - 14:
                    break
                T.draw_text(cr, sx, sy, p.name[:16], 10.0, T.TEXT_DIM)
                T.draw_text(cr, sx + col_w - 44, sy, fmt_bytes(p.mem), 9.5,
                            T.TEXT_MUTE, family=T.FONT_NUM, align="right")
                T.draw_text(cr, sx + col_w, sy, f"{p.cpu:.0f}%", 10.0, T.CYAN,
                            family=T.FONT_NUM, align="right")
                sy += 16


# ------------------------------------------------------------- GPU panel ----

class GPUPanel(Panel):
    def __init__(self):
        super().__init__("SURTR", "sowilo", T.AMBER, "graphics core",
                         width=380, height=330)
        self.util = Ease(9.0)
        self.vram = Ease(9.0)
        self.power = Ease(9.0)
        self.storm = L.Storm(4)

    def tick(self, dt, now):
        self._t = now
        if self.snap:
            g = self.snap.gpu
            self.util.set(g.util)
            self.vram.set((g.mem_used / g.mem_total * 100.0) if g.mem_total else 0.0)
            self.power.set((g.power / g.power_cap * 100.0)
                           if (g.power and g.power_cap) else 0.0)
        moving = False
        for e in (self.util, self.vram, self.power):
            moving |= e.step(dt)
        self.storm.prune(now)
        return self._consume(moving or self.storm.active)

    def body(self, cr, x, y, w, h):
        g = self.snap.gpu
        if not g.present:
            T.draw_tracked(cr, x + w / 2, y + h / 2 - 8, "no cuda device", 11,
                           T.TEXT_MUTE, tracking=2.5, align="center")
            return

        T.draw_text(cr, x, y + 2, g.name.replace("NVIDIA ", ""), 12.0, T.TEXT,
                    bold=True)
        if g.driver:
            T.draw_text(cr, x + w, y + 4, f"driver {g.driver}", 9.0, T.TEXT_MUTE,
                        align="right")

        cy = y + 26
        for label, ease, value_text, color in (
            ("core", self.util, f"{g.util:.0f}%", T.AMBER),
            ("vram", self.vram,
             f"{g.mem_used/1024:.1f} / {g.mem_total/1024:.1f} GiB", T.AMBER),
            ("power", self.power,
             f"{g.power:.0f} / {g.power_cap:.0f} W" if g.power else "--", T.EMBER),
        ):
            T.draw_tracked(cr, x, cy, label, 8.5, T.TEXT_MUTE, tracking=1.6)
            T.draw_text(cr, x + w, cy - 2, value_text, 10.5, T.TEXT_DIM,
                        family=T.FONT_NUM, align="right")
            meter(cr, x, cy + 13, w, 9, ease.v / 100.0,
                  T.heat(color, ease.v / 100.0))
            cy += 34

        cy += 2
        T.knot_divider(cr, x, cy, w, T.BRONZE, 0.3)
        cy += 12

        half = (w - 14) / 2
        stat(cr, x, cy, half, "temp", f"{g.temp:.0f}" if g.temp else "--", unit="°C",
             color=T.heat(T.AMBER, (g.temp or 0) / 90.0), size=12.0)
        stat(cr, x + half + 14, cy, half, "fan",
             f"{g.fan:.0f}" if g.fan is not None else "--", unit="%", size=12.0)
        cy += 21
        stat(cr, x, cy, half, "sm clk",
             f"{g.clock_sm:.0f}" if g.clock_sm else "--", unit="MHz", size=12.0)
        stat(cr, x + half + 14, cy, half, "mem clk",
             f"{g.clock_mem:.0f}" if g.clock_mem else "--", unit="MHz", size=12.0)
        cy += 26

        remaining = y + h - cy - 2
        if remaining > 40:
            graph(cr, x, cy, w, remaining, self.hist.get("gpu"), T.AMBER,
                  scale=100.0, label=f"core  {self.hist.span_label()}")


# ---------------------------------------------------------- memory panel ----

class MemoryPanel(Panel):
    def __init__(self):
        super().__init__("MIMIR", "mannaz", T.VIOLET, "system memory",
                         width=420, height=160)
        self.ram = Ease(9.0)
        self.swap = Ease(9.0)

    def tick(self, dt, now):
        self._t = now
        if self.snap:
            self.ram.set(self.snap.ram.percent)
            self.swap.set(self.snap.ram.swap_percent)
        moving = self.ram.step(dt) | self.swap.step(dt)
        return self._consume(moving)

    def body(self, cr, x, y, w, h):
        m = self.snap.ram
        used_txt = fmt_ibytes(m.used)
        T.draw_text(cr, x, y - 2, used_txt, 22.0, T.TEXT, bold=True, glow=0.35)
        used_w, _ = T.text_size(cr, used_txt, 22.0, T.FONT_UI, True)
        T.draw_text(cr, x + used_w + 8, y + 10, f"of {fmt_ibytes(m.total)}", 10.0,
                    T.TEXT_MUTE)
        T.draw_text(cr, x + w, y + 2, f"{m.percent:.0f}%", 17.0,
                    T.heat(T.VIOLET, m.percent / 100.0), family=T.FONT_NUM,
                    bold=True, align="right")

        meter(cr, x, y + 32, w, 12, self.ram.v / 100.0,
              T.heat(T.VIOLET, self.ram.v / 100.0))

        cy = y + 52
        half = (w - 14) / 2
        if cy + 14 > y + h:
            return
        stat(cr, x, cy, half, "available", fmt_ibytes(m.available), size=11.5)
        stat(cr, x + half + 14, cy, half, "cached", fmt_ibytes(m.cached),
             size=11.5)
        cy += 22

        if cy + 22 <= y + h:
            T.draw_tracked(cr, x, cy, "swap", 8.5, T.TEXT_MUTE, tracking=1.6)
            T.draw_text(cr, x + w, cy - 2,
                        f"{fmt_ibytes(m.swap_used)} / {fmt_ibytes(m.swap_total)}",
                        10.5, T.TEXT_DIM, family=T.FONT_NUM, align="right")
            meter(cr, x, cy + 13, w, 7, self.swap.v / 100.0,
                  T.heat(T.SEA, self.swap.v / 100.0))
            cy += 28

        remaining = y + h - cy - 2
        if remaining > 26:
            graph(cr, x, cy, w, remaining, self.hist.get("ram"), T.VIOLET,
                  scale=100.0, label=f"memory  {self.hist.span_label()}")


# --------------------------------------------------------- storage panel ----

class StoragePanel(Panel):
    def __init__(self):
        super().__init__("FAFNIR", "fehu", T.SEA, "the hoard",
                         width=420, height=190)
        self.bars: dict[str, Ease] = {}
        self.rd = Ease(6.0)
        self.wr = Ease(6.0)

    def tick(self, dt, now):
        self._t = now
        if self.snap:
            for m in self.snap.disk.mounts:
                self.bars.setdefault(m.label, Ease(9.0)).set(m.percent)
            self.rd.set(self.snap.disk.read_bps)
            self.wr.set(self.snap.disk.write_bps)
        moving = False
        for e in self.bars.values():
            moving |= e.step(dt)
        moving |= self.rd.step(dt) | self.wr.step(dt)
        return self._consume(moving)

    def body(self, cr, x, y, w, h):
        d = self.snap.disk
        cy = y
        for m in d.mounts[:3]:
            if cy + 26 > y + h:      # a half-drawn meter looks broken; skip it
                break
            e = self.bars.get(m.label)
            frac = (e.v if e else m.percent) / 100.0
            T.draw_text(cr, x, cy - 2, m.label, 11.5, T.TEXT, bold=True)
            lw, _ = T.text_size(cr, m.label, 11.5, T.FONT_UI, True)
            cap = f"{fmt_ibytes(m.used)} / {fmt_ibytes(m.total)}"
            cw, _ = T.text_size(cr, cap, 10.5, T.FONT_NUM, True)
            T.draw_text(cr, x + w, cy - 2, cap, 10.5, T.TEXT_DIM,
                        family=T.FONT_NUM, align="right")

            note = m.disk or m.device.split("/")[-1]
            if m.temp is not None:
                note += f"  ·  {m.temp:.0f}°C"
            note_x = x + lw + 8
            room = (x + w - cw - 10) - note_x
            if room > 24:                     # clip rather than overlap the capacity
                cr.save()
                cr.rectangle(note_x, cy - 4, room, 18)
                cr.clip()
                T.draw_text(cr, note_x, cy + 1, note, 8.5, T.TEXT_MUTE)
                cr.restore()
            meter(cr, x, cy + 15, w, 9, frac, T.heat(T.SEA, frac))
            cy += 34

        if cy + 30 > y + h:
            return
        cy += 2
        T.knot_divider(cr, x, cy, w, T.BRONZE, 0.3)
        cy += 12

        half = (w - 14) / 2
        T.draw_tracked(cr, x, cy, "read", 8.5, T.TEXT_MUTE, tracking=1.6)
        T.draw_text(cr, x + half, cy - 3, fmt_rate(self.rd.v), 12.5, T.SEA,
                    family=T.FONT_NUM, bold=True, align="right")
        T.draw_tracked(cr, x + half + 14, cy, "write", 8.5, T.TEXT_MUTE, tracking=1.6)
        T.draw_text(cr, x + w, cy - 3, fmt_rate(self.wr.v), 12.5, T.AMBER,
                    family=T.FONT_NUM, bold=True, align="right")
        cy += 20

        remaining = y + h - cy - 2
        if remaining > 26:
            io = self.hist.get("io")
            peak = max(max(io), 1.0)
            graph(cr, x, cy, w, remaining, io, T.SEA, scale=peak * 1.15,
                  label=f"i/o  peak {peak:.0f} MiB/s")


# ------------------------------------------------------------------ header ---

class HeaderBar(Gtk.DrawingArea):
    """Title block with an ambient storm running along the underside."""

    def __init__(self, info: dict[str, str]):
        super().__init__()
        self.info = info
        self.snap = None
        self.storm = L.Storm(8)
        self._flash = 0.0
        self._t = 0.0
        self._dirty = True
        self.set_content_height(72)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)

    def update(self, snap, _hist=None):
        self.snap = snap
        self._dirty = True

    def tick(self, dt, now):
        self._t = now
        self.storm.prune(now)
        self._flash = max(0.0, self._flash - dt * 2.6)
        w = self.get_width()
        if w > 20 and self.storm.due(now, 0.55, base=1.25, fastest=0.28):
            self._strike(now, w)
        dirty = self._dirty or self.storm.active or self._flash > 0.01
        self._dirty = False
        return dirty

    def _strike(self, now, w):
        """
        Three kinds of strike so the banner does not read as one looping effect:
        a bolt running along the rail, one falling from the top edge into it,
        and an occasional short arc behind the title.
        """
        rng = self.storm.rng
        roll = rng.random()
        h = 72
        if roll < 0.45:                                    # along the rail
            x0 = rng.uniform(-60, w * 0.85)
            self.storm.add(L.make_bolt(
                x0, h - 3, x0 + rng.uniform(160, 520), h - 3, color=T.CYAN,
                width=1.4, life=0.75, detail=5, jag=0.05, fork_chance=0.45,
                rng=rng, now=now))
        elif roll < 0.82:                                  # falling into the rail
            x0 = rng.uniform(w * 0.12, w * 0.98)
            self.storm.add(L.make_bolt(
                x0, -4, x0 + rng.uniform(-70, 70), h - 3,
                color=T.CYAN if rng.random() < 0.7 else T.GOLD_HI,
                width=1.5, life=0.6, detail=5, jag=0.14, fork_chance=0.5,
                rng=rng, now=now))
        else:                                              # short arc by the title
            x0 = rng.uniform(60, 300)
            y0 = rng.uniform(12, 52)
            self.storm.add(L.make_bolt(
                x0, y0, x0 + rng.uniform(70, 200), y0 + rng.uniform(-18, 26),
                color=T.BOLT, width=1.1, life=0.42, detail=4, jag=0.2,
                fork_chance=0.3, rng=rng, now=now))
        self._flash = 1.0

    def _draw(self, _area, cr, w, h):
        g = cairo.LinearGradient(0, 0, 0, h)
        g.add_color_stop_rgb(0.0, *T.DEEP)
        g.add_color_stop_rgb(1.0, *T.VOID)
        cr.set_source(g)
        cr.rectangle(0, 0, w, h)
        cr.fill()

        gw = cairo.LinearGradient(0, 0, w, 0)
        gw.add_color_stop_rgba(0.0, *T.CYAN, 0.10)
        gw.add_color_stop_rgba(0.5, *T.CYAN, 0.0)
        cr.set_source(gw)
        cr.rectangle(0, 0, w, h)
        cr.fill()

        # the same world tree as the app icon, at a shallower recursion so it
        # still reads at banner size
        T.soft_disc(cr, 44, 36, 33, T.CYAN, 0.16)
        T.draw_yggdrasil(cr, 14, 6, 60, glow=3.0, canopy_depth=4, root_depth=2,
                         root_color=T.BRONZE)
        T.draw_tracked(cr, 86, 14, "VALHALLA", 25.0, T.TEXT, tracking=7.0)
        T.draw_tracked(cr, 88, 46, "mjolnir system monitor", 9.0, T.GOLD,
                       tracking=3.4)

        # right-hand identity block
        if self.snap:
            items = [
                (self.info["host"], f"{self.info['distro']}"),
                (f"kernel {self.info['kernel']}", self.info["arch"]),
                (fmt_uptime(self.snap.uptime), "uptime"),
            ]
            rx = w - 20
            for idx, (value, label) in enumerate(reversed(items)):
                vw, _ = T.text_size(cr, value, 11.0, T.FONT_NUM, True)
                lw, _ = T.text_size(cr, label, 8.5, T.FONT_UI, False)
                block = max(vw, lw)
                T.draw_text(cr, rx, 22, value, 11.0, T.TEXT_DIM,
                            family=T.FONT_NUM, bold=True, align="right")
                T.draw_text(cr, rx, 39, label, 8.5, T.TEXT_MUTE, align="right")
                rx -= block + 30
                if idx < len(items) - 1:
                    T.rgb(cr, T.EDGE, 0.9)
                    cr.set_line_width(1.0)
                    cr.move_to(rx + 15, 20)
                    cr.line_to(rx + 15, 44)
                    cr.stroke()

        cr.save()
        cr.rectangle(0, 0, w, h)
        cr.clip()
        self.storm.draw(cr, self._t, 1.0)
        cr.restore()

        f = self._flash
        gb = cairo.LinearGradient(0, 0, w, 0)
        gb.add_color_stop_rgba(0.0, *T.CYAN, 0.0)
        gb.add_color_stop_rgba(0.35, *T.CYAN, min(1.0, 0.8 + 0.2 * f))
        gb.add_color_stop_rgba(0.75, *T.GOLD, 0.55 + 0.35 * f)
        gb.add_color_stop_rgba(1.0, *T.CYAN, 0.0)
        cr.set_source(gb)
        cr.rectangle(0, h - 2, w, 1.6 + 0.9 * f)
        cr.fill()
        if f > 0.01:                       # afterglow bleeding up from the rail
            gf = cairo.LinearGradient(0, h - 20, 0, h)
            gf.add_color_stop_rgba(0.0, *T.CYAN, 0.0)
            gf.add_color_stop_rgba(1.0, *T.CYAN, 0.20 * f)
            cr.set_source(gf)
            cr.rectangle(0, h - 20, w, 20)
            cr.fill()
