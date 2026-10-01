"""
Painted widgets: the Card frame, the rotary Knob and the Bifrost visualiser.

Painting follows Valhalla's convention - a widget exposes `_draw(widget, cr, w, h)`
and, if it animates, `tick(dt, now) -> bool` (True = needs a repaint) - so the
offscreen preview tool can blit them without a display.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Callable, Optional

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from .. import theme as T  # noqa: E402

HEAD = 38
PAD = 16


class Ease:
    """Exponential smoothing so nothing on screen snaps."""

    def __init__(self, rate: float = 10.0, start: float = 0.0):
        self.v = self.t = start
        self.rate = rate

    def set(self, target: float) -> None:
        self.t = target

    def step(self, dt: float) -> bool:
        delta = self.t - self.v
        self.v += delta * min(1.0, dt * self.rate)
        if abs(delta) < 0.002:
            self.v = self.t
            return False
        return True


# -------------------------------------------------------------------- card ---

def paint_card(cr, w, h, title: str, subtitle: str, accent, rune: str) -> None:
    """The Valhalla plate: cut-corner shield, forged brackets, runed header."""
    T.clipped_rect(cr, 1.5, 1.5, w - 3, h - 3, 14)
    import cairo as _c
    g = _c.LinearGradient(0, 0, w * 0.3, h)
    g.add_color_stop_rgba(0.0, *T.PANEL_HI, 1.0)
    g.add_color_stop_rgba(0.6, *T.PANEL, 1.0)
    g.add_color_stop_rgba(1.0, *T.DEEP, 1.0)
    cr.set_source(g)
    cr.fill_preserve()
    T.rgb(cr, T.EDGE, 0.95)
    cr.set_line_width(1.2)
    cr.stroke()
    T.corner_brackets(cr, 8, 8, w - 16, h - 16, 15, T.BRONZE, 0.42, 1.1)
    if title:
        T.draw_rune(cr, rune, 15, 14, 14, accent, 1.5, 1.0, glow=0.9)
        tw = T.draw_tracked(cr, 38, 14, title, 12, T.TEXT, tracking=2.4)
        if subtitle:
            T.draw_text(cr, 38 + tw + 12, 17, subtitle, 9.5, T.TEXT_MUTE)
        y = HEAD - 6
        T.rgb(cr, T.EDGE, 0.9)
        cr.set_line_width(1.0)
        cr.move_to(PAD, y + 0.5)
        cr.line_to(w - PAD, y + 0.5)
        cr.stroke()
        T.rgb(cr, accent, 0.9)
        cr.set_line_width(2.0)
        cr.move_to(PAD, y + 0.5)
        cr.line_to(PAD + 34, y + 0.5)
        cr.stroke()


class Card(Gtk.Overlay):
    """A painted plate with a real GTK box on top, so inputs sit inside the art."""

    def __init__(self, title: str, subtitle: str = "", accent=T.CYAN, rune: str = "hagalaz",
                 orientation=Gtk.Orientation.VERTICAL, spacing: int = 10):
        super().__init__()
        self._meta = (title, subtitle, accent, rune)
        bg = Gtk.DrawingArea()
        bg.set_draw_func(lambda a, cr, w, h: paint_card(cr, w, h, *self._meta))
        self.set_child(bg)
        self.body = Gtk.Box(orientation=orientation, spacing=spacing)
        self.body.set_margin_start(PAD)
        self.body.set_margin_end(PAD)
        self.body.set_margin_bottom(PAD)
        self.body.set_margin_top(HEAD + 6 if title else PAD)
        self.add_overlay(self.body)
        self.set_measure_overlay(self.body, True)
        self.set_hexpand(True)


# -------------------------------------------------------------------- knob ---

ARC_START = 0.75 * math.pi
ARC_SWEEP = 1.5 * math.pi


class Knob(Gtk.DrawingArea):
    """
    Rotary control. Drag vertically, scroll, or use the arrow keys; double-click
    resets. `fmt` renders the value for the readout; `log` makes the travel
    exponential, which suits rates that span a decade or more.
    """

    def __init__(self, label: str, accent, lo: float, hi: float, value: float,
                 fmt: Callable[[float], str], unit: str = "", step: float = 0.02,
                 log: bool = False, on_change: Optional[Callable[[float], None]] = None):
        super().__init__()
        self.label, self.accent, self.unit, self.fmt = label, accent, unit, fmt
        self.lo, self.hi, self.log, self.step = lo, hi, log, step
        self.default = value
        self.on_change = on_change
        self._frac = self._to_frac(value)
        self._ease = Ease(14.0, self._frac)
        self._drag0 = 0.0
        self._hot = False
        self.set_content_width(112)
        self.set_content_height(132)
        self.set_focusable(True)
        self.set_draw_func(lambda a, cr, w, h: self._draw(a, cr, w, h))

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag)
        self.add_controller(drag)
        click = Gtk.GestureClick()
        click.connect("pressed", lambda g, n, x, y: (self.reset() if n == 2 else self.grab_focus()))
        self.add_controller(click)
        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", lambda c, dx, dy: (self._nudge(-dy * self.step), True)[1])
        self.add_controller(scroll)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *a: self._set_hot(True))
        motion.connect("leave", lambda *a: self._set_hot(False))
        self.add_controller(motion)
        self._tick_id = 0

    # value <-> fraction ----------------------------------------------------
    def _to_frac(self, v: float) -> float:
        v = max(self.lo, min(self.hi, v))
        if self.log:
            return math.log(v / self.lo) / math.log(self.hi / self.lo)
        return (v - self.lo) / (self.hi - self.lo)

    def _from_frac(self, f: float) -> float:
        f = max(0.0, min(1.0, f))
        if self.log:
            return self.lo * (self.hi / self.lo) ** f
        return self.lo + f * (self.hi - self.lo)

    @property
    def value(self) -> float:
        return self._from_frac(self._frac)

    def set_value(self, v: float, notify: bool = False) -> None:
        self._frac = self._to_frac(v)
        self._kick()
        if notify and self.on_change:
            self.on_change(self.value)

    def reset(self) -> None:
        self.set_value(self.default, notify=True)

    # input -----------------------------------------------------------------
    def _nudge(self, df: float) -> None:
        self._frac = max(0.0, min(1.0, self._frac + df))
        self._kick()
        if self.on_change:
            self.on_change(self.value)

    def _on_drag_begin(self, g, x, y):
        self._drag0 = self._frac
        self.grab_focus()

    def _on_drag(self, g, dx, dy):
        self._frac = max(0.0, min(1.0, self._drag0 - dy / 160.0))
        self._kick()
        if self.on_change:
            self.on_change(self.value)

    def _on_key(self, c, keyval, code, state):
        if keyval in (Gdk.KEY_Up, Gdk.KEY_Right):
            self._nudge(self.step)
        elif keyval in (Gdk.KEY_Down, Gdk.KEY_Left):
            self._nudge(-self.step)
        elif keyval in (Gdk.KEY_Home, Gdk.KEY_Escape):
            self.reset()
        else:
            return False
        return True

    def _set_hot(self, hot: bool) -> None:
        self._hot = hot
        self.queue_draw()

    # animation -------------------------------------------------------------
    def _kick(self) -> None:
        self._ease.set(self._frac)
        if not self._tick_id:
            self._tick_id = GLib.timeout_add(16, self._anim)
        self.queue_draw()

    def _anim(self) -> bool:
        moving = self._ease.step(1 / 60.0)
        self.queue_draw()
        if not moving:
            self._tick_id = 0
        return moving

    # painting --------------------------------------------------------------
    def _draw(self, area, cr, w, h) -> None:
        cx, cy = w / 2, h / 2 - 8
        r = min(w, h) * 0.34
        f = self._ease.v

        T.soft_disc(cr, cx, cy, r * 1.5, self.accent, 0.10 + (0.08 if self._hot else 0.0))

        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.arc(cx, cy, r, ARC_START, ARC_START + ARC_SWEEP)
        T.rgb(cr, T.TRACK, 1.0)
        cr.set_line_width(6)
        cr.stroke()

        if f > 0.004:
            cr.new_path()
            cr.arc(cx, cy, r, ARC_START, ARC_START + ARC_SWEEP * f)
            T.glow_stroke(cr, T.mix(self.accent, T.GOLD_HI, 0.15 * f), 6, layers=4,
                          spread=1.6, alpha=0.55)

        a = ARC_START + ARC_SWEEP * f
        cr.set_line_width(2.0)
        T.rgb(cr, T.BOLT, 0.95)
        cr.move_to(cx + math.cos(a) * (r - 14), cy + math.sin(a) * (r - 14))
        cr.line_to(cx + math.cos(a) * (r - 4), cy + math.sin(a) * (r - 4))
        cr.stroke()

        T.draw_text(cr, cx, cy - 2, self.fmt(self.value), 15, T.TEXT, family=T.FONT_NUM,
                    align="center", valign="middle", glow=0.3 if self._hot else 0.0)
        if self.unit:
            T.draw_text(cr, cx, cy + 14, self.unit, 9, T.TEXT_MUTE, align="center")
        T.draw_tracked(cr, cx, h - 26, self.label, 9.5,
                       T.TEXT if self.has_focus() else T.TEXT_DIM, tracking=1.8, align="center")


# ------------------------------------------------------------------ bridge ---

class Bridge(Gtk.DrawingArea):
    """
    The visualiser. A luminous orb travels an arch between two ears; each ear
    glows with its channel's level, and a pulse strip underneath shows the
    amplitude modulation. The orb's trail makes the sweep's speed and shape
    readable at a glance, which is the point: it lets you see what the knobs do.
    """

    def __init__(self):
        super().__init__()
        self.set_content_width(520)
        self.set_content_height(260)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.pos = Ease(18.0, 0.0)
        self.level_l = Ease(16.0)
        self.level_r = Ease(16.0)
        self.gain = Ease(14.0, 1.0)
        self.trail: deque[float] = deque(maxlen=44)
        self.active = False          # audio playing
        self.pan_on = True
        self.am_on = True
        self.am_freq = 16.0
        self.am_depth = 0.25
        self._t = 0.0
        self._wave = 0.0
        self.set_draw_func(lambda a, cr, w, h: self._draw(a, cr, w, h))

    def feed(self, position: float, level_l: float, level_r: float) -> None:
        self.pos.set(position if self.active else 0.0)
        self.level_l.set(min(1.0, level_l * 1.6) if self.active else 0.0)
        self.level_r.set(min(1.0, level_r * 1.6) if self.active else 0.0)
        depth = self.am_depth if self.am_on else 0.0
        self.gain.set(1.0 - depth * 0.5)

    def tick(self, dt: float, now: float) -> bool:
        self._t = now
        # The pulse strip scrolls at a readable fraction of the real rate; the
        # true 16 Hz would just alias against a 30 fps repaint.
        self._wave = (self._wave + dt * 2.2) % (2 * math.pi)
        moved = self.pos.step(dt)
        self.level_l.step(dt)
        self.level_r.step(dt)
        self.gain.step(dt)
        if self.active:
            self.trail.append(self.pos.v)
        elif self.trail:
            self.trail.popleft()
        return True

    # geometry: an arch from the left post to the right post
    @staticmethod
    def _arch(x0, x1, base, rise, u):
        x = x0 + (x1 - x0) * (u * 0.5 + 0.5)
        y = base - rise * (1 - u * u)
        return x, y

    def _draw(self, area, cr, w, h) -> None:
        pad = 34
        x0, x1 = pad + 24, w - pad - 24
        base = h * 0.64
        rise = h * 0.40

        # the bridge: a bundle of faint arcs, cyan on the left to violet on the right
        for i, off in enumerate((-9, -6, -3, 0, 3, 6, 9)):
            col = T.mix(T.CYAN, T.VIOLET, i / 6.0)
            cr.new_path()
            for k in range(0, 61):
                u = k / 30.0 - 1.0
                x, y = self._arch(x0, x1, base + off * 0.6, rise, u)
                (cr.move_to if k == 0 else cr.line_to)(x, y)
            T.rgb(cr, col, 0.30 if self.pan_on else 0.12)
            cr.set_line_width(1.4)
            cr.stroke()

        # ear posts
        for side, ease, accent in ((-1, self.level_l, T.CYAN), (1, self.level_r, T.VIOLET)):
            ex = x0 if side < 0 else x1
            T.soft_disc(cr, ex, base + 6, 40 + 30 * ease.v, accent, 0.10 + 0.55 * ease.v)
            cr.set_line_width(2.0)
            T.rgb(cr, T.mix(T.STEEL, accent, ease.v), 1.0)
            cr.arc(ex, base + 6, 11, 0, 2 * math.pi)
            cr.stroke()
            T.draw_tracked(cr, ex, base + 26, "L" if side < 0 else "R", 10, T.TEXT_DIM,
                           tracking=1.0, align="center")

        # trail then orb
        if self.pan_on:
            n = len(self.trail)
            for i, p in enumerate(self.trail):
                a = (i + 1) / max(1, n)
                x, y = self._arch(x0, x1, base, rise, p)
                T.soft_disc(cr, x, y, 4 + 10 * a, T.mix(T.CYAN, T.VIOLET, p * 0.5 + 0.5), 0.05 + 0.20 * a)
            p = self.pos.v
            x, y = self._arch(x0, x1, base, rise, p)
            glow = 0.55 + 0.45 * self.gain.v
            T.soft_disc(cr, x, y, 46, T.mix(T.CYAN, T.VIOLET, p * 0.5 + 0.5), 0.55 * glow)
            T.soft_disc(cr, x, y, 14, T.BOLT, 0.9)
        elif self.active:
            x, y = self._arch(x0, x1, base, rise, 0.0)
            T.soft_disc(cr, x, y, 40, T.GOLD_HI, 0.45)
            T.soft_disc(cr, x, y, 12, T.BOLT, 0.85)

        self._pulse_strip(cr, pad, h - 54, w - 2 * pad, 34)

    def _pulse_strip(self, cr, x, y, w, h) -> None:
        T.draw_tracked(cr, x, y - 15, "PULSE", 9, T.TEXT_MUTE, tracking=1.6)
        T.draw_text(cr, x + w, y - 15, f"{self.am_freq:.0f} Hz", 9.5, T.AMBER if self.am_on else T.TEXT_MUTE,
                    family=T.FONT_NUM, align="right")
        T.rounded_rect(cr, x, y, w, h, 6)
        T.rgb(cr, T.TRACK, 1.0)
        cr.fill()
        cycles = 7.0
        depth = self.am_depth if self.am_on else 0.0
        # The real modulation is only 10-50%, which reads as a flat slab at this
        # size, so the display exaggerates it. It is a picture of the *rate* and
        # on/off state; the audio carries the true depth.
        vis = min(1.0, depth * 2.2)
        color = T.AMBER if self.am_on else T.STEEL
        cr.save()
        T.rounded_rect(cr, x, y, w, h, 6)
        cr.clip()
        bar, gap = 4.0, 2.0
        n = int(w // (bar + gap))
        for i in range(n):
            ph = (i / n) * cycles * 2 * math.pi - self._wave * 3.0
            m = 0.5 + 0.5 * math.sin(ph)
            g = 1.0 - vis * (1.0 - m)
            amp = (h / 2 - 3) * g * (1.0 if self.active else 0.4)
            bx = x + 3 + i * (bar + gap)
            T.rgb(cr, color, (0.35 + 0.5 * g) if self.am_on else 0.30)
            cr.rectangle(bx, y + h / 2 - amp, bar, amp * 2)
            cr.fill()
        cr.restore()
        T.rounded_rect(cr, x, y, w, h, 6)
        T.rgb(cr, T.EDGE, 1.0)
        cr.set_line_width(1.0)
        cr.stroke()


# ------------------------------------------------------------------ banner ---

class Banner(Gtk.DrawingArea):
    """Header mark: the world tree, the name, and a one-line status."""

    def __init__(self):
        super().__init__()
        self.status = "idle"
        self.set_content_width(330)
        self.set_content_height(40)
        self.set_draw_func(lambda a, cr, w, h: self._draw(a, cr, w, h))

    def set_status(self, text: str) -> None:
        if text != self.status:
            self.status = text
            self.queue_draw()

    def _draw(self, area, cr, w, h) -> None:
        T.draw_yggdrasil(cr, 4, 2, 36, glow=2.0, canopy_depth=4, root_depth=2)
        T.draw_tracked(cr, 50, 5, "BIFROST", 15, T.GOLD_HI, tracking=4.0)
        T.draw_text(cr, 51, 25, "bilateral focus audio  ·  " + self.status, 9.5, T.TEXT_MUTE)
