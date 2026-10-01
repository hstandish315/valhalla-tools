"""
Live view: send any app's audio through the sweep and pulse as it plays.

Start live mode, then send an app (Spotify, a browser tab, a video player) to
"Bifrost Live" with one click; it returns to your speakers the moment you stop
or close Bifrost. Nothing is recorded or saved - this is a real-time effect.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from .. import live, theme as T  # noqa: E402
from .player import label  # noqa: E402
from .widgets import Card  # noqa: E402

REFRESH_MS = 1500


class StreamRow(Gtk.Box):
    def __init__(self, view: "LiveView", stream: live.Stream):
        super().__init__(spacing=12)
        self.set_margin_top(6)
        self.set_margin_bottom(6)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.set_hexpand(True)
        text.append(label(stream.app, [], ellipsize=True))
        text.append(label(stream.media or "playing audio", ["status"], ellipsize=True))
        self.append(text)
        if stream.routed:
            tag = label("THROUGH BIFROST", ["badge"])
            tag.set_valign(Gtk.Align.CENTER)
            self.append(tag)
        btn = Gtk.Button(label="Return to speakers" if stream.routed else "Send to Bifrost")
        btn.set_valign(Gtk.Align.CENTER)
        if not stream.routed:
            btn.add_css_class("primary")
        btn.connect("clicked", lambda *_: view.toggle(stream))
        self.append(btn)


class LiveView(Gtk.Box):
    def __init__(self, ctx):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.ctx = ctx
        self._timer = 0
        self.set_margin_start(12)
        self.set_margin_end(12)
        self.set_margin_top(12)
        self.set_margin_bottom(6)

        card = Card("Live Bridge", "any app, in real time", T.CYAN, "raido")
        card.set_vexpand(True)
        b = card.body
        b.append(label("Apply the sweep and pulse to whatever is already playing — Spotify, a browser, "
                       "a video — without downloading or saving anything. It works like an equaliser: "
                       "audio passes through Bifrost on its way to your speakers.", ["dim"], wrap=True))

        row = Gtk.Box(spacing=12)
        self.btn = Gtk.Button(label="Start live mode")
        self.btn.add_css_class("primary")
        self.btn.connect("clicked", lambda *_: self.toggle_live())
        row.append(self.btn)
        self.state = label("", ["status"], wrap=True)
        self.state.set_hexpand(True)
        row.append(self.state)
        b.append(row)

        self.list = Gtk.ListBox()
        self.list.set_selection_mode(Gtk.SelectionMode.NONE)
        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        sw.set_child(self.list)
        b.append(sw)
        self.empty = label("", ["status", "mute"], wrap=True)
        b.append(self.empty)
        b.append(label("Tip: you can also pick “Bifrost Live” as the output device in Settings → Sound "
                       "for any app. Expect about 0.1–0.2 s of added delay, so video lip-sync will drift; "
                       "it's best for music.", ["status", "mute"], wrap=True))
        self.append(card)
        self.connect("map", lambda *_: self._start_timer())
        self.connect("unmap", lambda *_: self._stop_timer())
        self.sync()

    # -------------------------------------------------------------- state ---
    def toggle_live(self) -> None:
        if self.ctx.live_running:
            self.ctx.live_stop()
        else:
            self.ctx.live_start()
        self.sync()

    def toggle(self, stream: live.Stream) -> None:
        try:
            (self.ctx.router.restore if stream.routed else self.ctx.router.move)(stream.node_id)
        except live.LiveError as exc:
            self.ctx.status(str(exc), error=True)
        self.sync()

    def sync(self) -> None:
        running = self.ctx.live_running
        self.btn.set_label("Stop live mode" if running else "Start live mode")
        self.list.remove_all()
        if not running:
            self.state.set_label("Off. Start live mode, then send an app to Bifrost.")
            self.empty.set_label("")
            return
        try:
            streams = self.ctx.router.streams(self.ctx.live.own_pids)
        except live.LiveError as exc:
            self.state.set_label(str(exc))
            return
        routed = sum(1 for s in streams if s.routed)
        self.state.set_label(f"Live mode is on. {routed} app{'s' if routed != 1 else ''} going through Bifrost.")
        for s in streams:
            self.list.append(StreamRow(self, s))
        self.empty.set_label("" if streams else
                             "Nothing is playing right now. Start music in Spotify or your browser; "
                             "it will appear here.")

    # ------------------------------------------------------------ refresh ---
    def _start_timer(self) -> None:
        if not self._timer:
            self._timer = GLib.timeout_add(REFRESH_MS, self._tick)
        self.sync()

    def _stop_timer(self) -> None:
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0

    def _tick(self) -> bool:
        if self.ctx.live_running:
            self.sync()
        return True
