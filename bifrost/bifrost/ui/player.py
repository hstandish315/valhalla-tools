"""Player view: now-playing, the bridge visualiser, the resonance knobs, the session timer."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

from .. import presets  # noqa: E402
from .. import theme as T  # noqa: E402
from ..session import Session  # noqa: E402
from .widgets import Bridge, Card, Knob  # noqa: E402

SHAPES = ("sine", "triangle", "pingpong")
SHAPE_LABELS = ("Sine", "Triangle", "Ping-pong")
MODES = ("inphase", "alternate")
MODE_LABELS = ("In phase", "Alternating")
SESSION_MINUTES = (15, 25, 45, 60, 90)


def fmt_time(s: float) -> str:
    s = max(0, int(s))
    return f"{s // 60}:{s % 60:02d}"


def label(text="", css=(), xalign=0.0, wrap=False, ellipsize=False) -> Gtk.Label:
    lb = Gtk.Label(label=text, xalign=xalign)
    for c in css:
        lb.add_css_class(c)
    if wrap:
        lb.set_wrap(True)
        lb.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    if ellipsize:
        lb.set_ellipsize(Pango.EllipsizeMode.END)
    return lb


class PlayerView(Gtk.Box):
    def __init__(self, ctx):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.ctx = ctx
        self.chain = ctx.chain
        self._seeking = False
        self._seek_timer = 0
        self._syncing = False
        self.set_margin_start(12)
        self.set_margin_end(12)
        self.set_margin_top(12)
        self.set_margin_bottom(6)

        top = Gtk.Box(spacing=10)
        top.set_vexpand(True)
        top.append(self._build_now_playing())
        top.append(self._build_bridge())
        self.append(top)

        bottom = Gtk.Box(spacing=10)
        bottom.append(self._build_resonance())
        bottom.append(self._build_session())
        self.append(bottom)

        self.preset_btns[0].set_active(True)     # toggled handler applies the Focus preset

    # ----------------------------------------------------------- building ---
    def _build_now_playing(self) -> Card:
        card = Card("Now playing", "", T.GOLD_HI, "fehu")
        card.set_hexpand(False)
        card.set_size_request(400, -1)
        b = card.body
        self.title = label("Nothing playing", ["title-big"], wrap=True)
        self.artist = label("Pick a track from the Library or Browse.", ["artist", "dim"], wrap=True)
        self.badge = label("", ["badge"])
        self.badge.set_halign(Gtk.Align.START)
        self.badge.set_visible(False)
        self.attrib = label("", ["status", "mute"], wrap=True)
        for w in (self.title, self.artist, self.badge, self.attrib):
            b.append(w)

        spacer = Gtk.Box()
        spacer.set_vexpand(True)
        b.append(spacer)

        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 0.001)
        self.scale.set_draw_value(False)
        self.scale.connect("change-value", self._on_scrub)
        b.append(self.scale)
        times = Gtk.Box()
        self.t_pos = label("0:00", ["mono", "dim"])
        self.t_len = label("0:00", ["mono", "dim"], xalign=1.0)
        self.t_len.set_hexpand(True)
        times.append(self.t_pos)
        times.append(self.t_len)
        b.append(times)

        row = Gtk.Box(spacing=8)
        row.set_halign(Gtk.Align.CENTER)
        self.btn_prev = Gtk.Button(label="⏮")
        self.btn_play = Gtk.Button(label="▶  Play")
        self.btn_play.add_css_class("primary")
        self.btn_stop = Gtk.Button(label="⏹")
        self.btn_next = Gtk.Button(label="⏭")
        self.btn_prev.connect("clicked", lambda *_: self.ctx.prev())
        self.btn_play.connect("clicked", lambda *_: self.ctx.toggle_play())
        self.btn_stop.connect("clicked", lambda *_: self.ctx.stop())
        self.btn_next.connect("clicked", lambda *_: self.ctx.next())
        for w in (self.btn_prev, self.btn_play, self.btn_stop, self.btn_next):
            row.append(w)
        b.append(row)
        return card

    def _build_bridge(self) -> Card:
        card = Card("The Bridge", "sweep · pulse", T.CYAN, "raido")
        self.bridge = Bridge()
        card.body.append(self.bridge)
        return card

    def _build_resonance(self) -> Card:
        card = Card("Resonance", "shape the sweep and the pulse", T.AMBER, "sowilo")
        b = card.body

        pre = Gtk.Box(spacing=8)
        self.preset_btns: list[Gtk.ToggleButton] = []
        first = None
        for p in presets.PRESETS:
            tb = Gtk.ToggleButton(label=p.name)
            tb.add_css_class("preset")
            tb.set_tooltip_text(p.blurb)
            if first is None:
                first = tb
            else:
                tb.set_group(first)
            tb.connect("toggled", lambda w, p=p: w.get_active() and self.apply_preset(p))
            self.preset_btns.append(tb)
            pre.append(tb)
        b.append(pre)

        knobs = Gtk.Box(spacing=4)
        knobs.set_halign(Gtk.Align.CENTER)

        def knob(*a, **kw):
            k = Knob(*a, **kw)
            knobs.append(k)
            return k

        self.k_rate = knob("Sweep rate", T.CYAN, 0.05, 2.0, 0.25, lambda v: f"{v:.2f}", "Hz",
                           step=0.03, log=True, on_change=lambda v: setattr(self.chain.pan, "rate", v))
        self.k_width = knob("Width", T.CYAN, 0.0, 1.0, 0.8, lambda v: f"{v * 100:.0f}", "%",
                            on_change=lambda v: setattr(self.chain.pan, "width", v))
        self.k_real = knob("Realism", T.CYAN, 0.0, 1.0, 0.8, lambda v: f"{v * 100:.0f}", "%",
                           on_change=lambda v: presets.set_realism(self.chain, v))
        self.k_freq = knob("Pulse rate", T.AMBER, 8.0, 40.0, 16.0, lambda v: f"{v:.0f}", "Hz",
                           step=0.03, on_change=self._on_pulse_freq)
        self.k_depth = knob("Depth", T.AMBER, 0.0, 0.5, 0.25, lambda v: f"{v * 100:.0f}", "%",
                            on_change=self._on_pulse_depth)
        self.k_vol = knob("Volume", T.SEA, 0.0, 1.0, 0.7, lambda v: f"{v * 100:.0f}", "%",
                          on_change=lambda v: setattr(self.chain, "volume", v))
        b.append(knobs)

        row = Gtk.Box(spacing=14)
        row.set_halign(Gtk.Align.CENTER)

        def switch(text, active, cb):
            box = Gtk.Box(spacing=8)
            sw = Gtk.Switch(active=active)
            sw.set_valign(Gtk.Align.CENTER)
            sw.connect("notify::active", lambda s, _: cb(s.get_active()))
            box.append(label(text, ["dim"]))
            box.append(sw)
            row.append(box)
            return sw

        def dropdown(items, cb):
            dd = Gtk.DropDown.new_from_strings(list(items))
            dd.connect("notify::selected", lambda d, _: cb(d.get_selected()))
            row.append(dd)
            return dd

        self.sw_pan = switch("Sweep", True, self._on_pan_on)
        self.dd_shape = dropdown(SHAPE_LABELS, lambda i: setattr(self.chain.pan, "shape", SHAPES[i]))
        self.sw_am = switch("Pulse", True, self._on_am_on)
        self.dd_mode = dropdown(MODE_LABELS, lambda i: setattr(self.chain.am, "mode", MODES[i]))
        b.append(row)

        note = label("A focus aid, not a treatment. Keep the volume comfortable; "
                     "stereo headphones give the full effect.", ["status", "mute"], xalign=0.5)
        b.append(note)
        return card

    def _build_session(self) -> Card:
        card = Card("Session", "gentle fade at the end", T.SEA, "tiwaz")
        card.set_hexpand(False)
        card.set_size_request(300, -1)
        self.session = Session(25)
        self.ctx.session = self.session
        b = card.body
        self.clock = label(self.session.label(), ["readout"], xalign=0.5)
        b.append(self.clock)

        row = Gtk.Box(spacing=8)
        row.set_halign(Gtk.Align.CENTER)
        row.append(label("Length", ["dim"]))
        self.dd_len = Gtk.DropDown.new_from_strings([f"{m} min" for m in SESSION_MINUTES])
        self.dd_len.set_selected(SESSION_MINUTES.index(25))
        self.dd_len.connect("notify::selected", self._on_len)
        row.append(self.dd_len)
        b.append(row)

        self.btn_session = Gtk.Button(label="Start session")
        self.btn_session.add_css_class("primary")
        self.btn_session.connect("clicked", lambda *_: self.ctx.toggle_session())
        b.append(self.btn_session)
        self.session_note = label("Starts playback if nothing is playing.", ["status", "mute"],
                                  xalign=0.5, wrap=True)
        b.append(self.session_note)
        return card

    # ------------------------------------------------------------ handlers ---
    def _on_pulse_freq(self, v):
        self.chain.am.freq = v
        self.bridge.am_freq = v

    def _on_pulse_depth(self, v):
        self.chain.am.depth = v
        self.bridge.am_depth = v

    def _on_pan_on(self, on):
        self.chain.pan_enabled = on
        self.bridge.pan_on = on

    def _on_am_on(self, on):
        self.chain.am_enabled = on
        self.bridge.am_on = on

    def _on_len(self, dd, _):
        if not self.session.running:
            self.session.minutes = SESSION_MINUTES[dd.get_selected()]
            self.clock.set_label(self.session.label())

    def _on_scrub(self, scale, scroll_type, value):
        entry = self.ctx.current
        if entry is None or entry.duration <= 0:
            return False
        self._seeking = True
        self.t_pos.set_label(fmt_time(value * entry.duration))
        if self._seek_timer:
            GLib.source_remove(self._seek_timer)
        self._seek_timer = GLib.timeout_add(250, self._commit_seek, value)
        return False

    def _commit_seek(self, value):
        self._seek_timer = 0
        self._seeking = False
        entry = self.ctx.current
        if entry is not None:
            self.ctx.engine.seek(value * entry.duration)
        return False

    # -------------------------------------------------------------- public ---
    def apply_preset(self, p: presets.Preset) -> None:
        presets.apply(self.chain, p)
        self.sync_from_chain()

    def sync_from_chain(self) -> None:
        """Push chain values into the controls without echoing them back."""
        c = self.chain
        self._syncing = True
        self.k_rate.set_value(c.pan.rate)
        self.k_width.set_value(c.pan.width)
        self.k_real.set_value(presets.realism_of(c))
        self.k_freq.set_value(c.am.freq)
        self.k_depth.set_value(c.am.depth)
        self.k_vol.set_value(c.volume)
        self.sw_pan.set_active(c.pan_enabled)
        self.sw_am.set_active(c.am_enabled)
        self.dd_shape.set_selected(SHAPES.index(c.pan.shape))
        self.dd_mode.set_selected(MODES.index(c.am.mode))
        self._syncing = False
        self.bridge.am_freq, self.bridge.am_depth = c.am.freq, c.am.depth
        self.bridge.pan_on, self.bridge.am_on = c.pan_enabled, c.am_enabled

    def show_entry(self, entry) -> None:
        if entry is None:
            self.title.set_label("Nothing playing")
            self.artist.set_label("Pick a track from the Library or Browse.")
            self.badge.set_visible(False)
            self.attrib.set_label("")
            return
        self.title.set_label(entry.title)
        self.artist.set_label(entry.creator or "Local file")
        self.badge.set_label(entry.license.upper())
        self.badge.set_visible(True)
        if entry.allows_modification:
            self.badge.remove_css_class("nd")
        else:
            self.badge.add_css_class("nd")
        self.attrib.set_label(entry.attribution)
        self.t_len.set_label(fmt_time(entry.duration))

    def show_live(self) -> None:
        self.title.set_label("Live audio")
        self.artist.set_label("Processing whatever you send to Bifrost Live (Live tab).")
        self.badge.set_visible(False)
        self.attrib.set_label("Nothing is recorded or saved.")
        self.t_len.set_label("live")

    def refresh(self) -> None:
        eng, entry = self.ctx.active_engine(), self.ctx.current
        if eng is self.ctx.live:
            entry = None
        playing = eng.state == "playing"
        self.btn_play.set_label("⏸  Pause" if playing else "▶  Play")
        self.bridge.active = playing
        self.bridge.feed(self.chain.pan.position, *eng.levels)
        if eng is self.ctx.live:
            self.scale.set_value(0.0)
            self.t_pos.set_label("live")
        elif entry and entry.duration > 0 and not self._seeking and eng.state != "stopped":
            self.scale.set_value(min(1.0, eng.position / entry.duration))
            self.t_pos.set_label(fmt_time(eng.position))
        elif eng.state == "stopped" and not self._seeking:
            self.scale.set_value(0.0)
            self.t_pos.set_label("0:00")
        s = self.session
        self.clock.set_label(s.label())
        self.btn_session.set_label("End session" if s.running else "Start session")
