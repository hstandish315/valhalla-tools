"""
Bifrost application shell: window, header, view switching, and the Context that
the views share (engine, chain, library, session).

The UI thread never touches audio directly. It flips parameters on the chain
and state on the engine; the engine's worker thread owns decoding and output.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from . import presets  # noqa: E402
from . import theme as T  # noqa: E402
from .dsp import Chain  # noqa: E402
from .engine import Engine  # noqa: E402
from .live import LiveEngine, LiveError, LiveRouter, missing_tools  # noqa: E402
from .library import Entry, Library  # noqa: E402
from .settings import Settings  # noqa: E402
from .ui.browse import BrowseView  # noqa: E402
from .ui.library_view import LibraryView  # noqa: E402
from .ui.live_view import LiveView  # noqa: E402
from .ui.player import PlayerView, label  # noqa: E402
from .ui.rip_view import RipView  # noqa: E402
from .ui.style import CSS  # noqa: E402
from .ui.widgets import Banner  # noqa: E402

APP_ID = "dev.valhalla.Bifrost"
VIEWS = (("player", "PLAYER"), ("browse", "BROWSE"), ("library", "LIBRARY"), ("rip", "RIP"), ("live", "LIVE"))


class Context:
    """Shared state and the actions every view calls into."""

    def __init__(self, library: Optional[Library] = None, chain: Optional[Chain] = None,
                 settings: Optional[Settings] = None):
        self.chain = chain or Chain()
        self.library = library or Library()
        self.settings = settings or Settings(self.library.data_dir)
        self.engine = Engine(self.chain, on_end=self._on_track_end)
        self.router = LiveRouter()
        self.live = LiveEngine(self.chain, self.router, on_end=self._on_live_end)
        self.session = None                      # set by PlayerView
        self.current: Optional[Entry] = None
        self.window: Optional[Gtk.Window] = None
        self.player: Optional[PlayerView] = None
        self.libview: Optional[LibraryView] = None
        self.auto_next = True
        self._status_cb = None

    # -------------------------------------------------------------- status ---
    def status(self, msg: str, error: bool = False):
        if self._status_cb:
            self._status_cb(msg, error)
        return False

    # ------------------------------------------------------------ playback ---
    @property
    def live_running(self) -> bool:
        return self.router.active and self.live.state == "playing"

    def active_engine(self):
        return self.live if self.live.state == "playing" else self.engine

    def play_entry(self, entry: Entry) -> None:
        if self.router.active:
            self.live_stop()
        self.current = entry
        self.chain.reset_stream()
        self.engine.play(entry.path)
        if self.player:
            self.player.show_entry(entry)
        self.status(f"Playing {entry.title}")

    def toggle_play(self) -> None:
        if self.live_running:                    # a live stream can't be paused, only ended
            self.live_stop()
            return
        st = self.engine.state
        if st == "playing":
            self.engine.pause()
        elif st == "paused":
            self.engine.resume()
        elif self.current:
            self.play_entry(self.current)
        elif self.library.entries:
            self.play_entry(self.library.entries[0])
        else:
            self.status("Library is empty — add files or browse for music.", error=True)

    def stop(self) -> None:
        if self.router.active:
            self.live_stop()
        self.engine.stop()

    # ---------------------------------------------------------- live mode ---
    def live_start(self) -> None:
        missing = missing_tools()
        if missing:
            self.status("Live mode needs PipeWire tools: " + ", ".join(missing), error=True)
            return
        self.engine.stop()
        try:
            self.router.create_sink()
        except LiveError as exc:
            self.status(str(exc), error=True)
            return
        self.chain.reset_stream()
        self.live.start()
        if self.player:
            self.player.show_live()
        self.status("Live mode on. Send an app to Bifrost on the Live tab.")

    def live_stop(self) -> None:
        self.live.stop()
        self.router.destroy_sink()               # returns every moved app to the speakers first
        if self.player:
            self.player.show_entry(self.current)
        self.status("Live mode off. Apps are back on your speakers.")

    def _on_live_end(self) -> None:
        GLib.idle_add(self.status, "Live capture stopped unexpectedly.", True)

    def shutdown(self) -> None:
        """Called on exit: apps must never be left parked on a sink nobody is reading."""
        self.live.stop()
        self.router.destroy_sink()
        self.engine.stop()

    def _index(self) -> int:
        paths = [e.path for e in self.library.entries]
        return paths.index(self.current.path) if self.current and self.current.path in paths else -1

    def next(self) -> None:
        n = len(self.library.entries)
        if n:
            self.play_entry(self.library.entries[(self._index() + 1) % n])

    def prev(self) -> None:
        n = len(self.library.entries)
        if n:
            self.play_entry(self.library.entries[(self._index() - 1) % n])

    def _on_track_end(self) -> None:
        # Runs on the engine thread: hop to the UI thread before touching anything.
        if self.session is not None and self.session.running and self.chain.finished:
            return
        if self.auto_next:
            GLib.idle_add(self._advance)

    def _advance(self):
        if self.engine.state == "ended" and len(self.library.entries) > 1:
            self.next()
        return False

    # ------------------------------------------------------------- session ---
    def toggle_session(self) -> None:
        s = self.session
        if s.running:
            s.stop()
            self.chain.reset_fade()
            self.status("Session ended")
            return
        s.start()
        self.chain.reset_fade()
        if self.live_running:
            pass                                 # the session just times what's already playing
        elif self.engine.state in ("stopped", "ended"):
            self.toggle_play()
        elif self.engine.state == "paused":
            self.engine.resume()
        self.status(f"Session started: {s.minutes:g} minutes")

    def poll_session(self) -> None:
        s = self.session
        if s is None:
            return
        ev = s.poll()
        if ev == "fade":
            self.chain.fade_out(s.fade_s)
            self.status("Session ending — fading out")
        elif ev == "done":
            s.stop()
            self.stop()
            self.chain.reset_fade()
            self.status("Session complete")

    def library_changed(self) -> None:
        if self.libview:
            self.libview.refresh()


class BifrostWindow(Gtk.ApplicationWindow):
    def __init__(self, app, ctx: Context, fps: int):
        super().__init__(application=app, title="Bifrost · bilateral focus audio")
        self.ctx = ctx
        ctx.window = self
        self.add_css_class("bifrost")
        self.set_default_size(1280, 880)
        self.set_size_request(1100, 760)
        self.fps = fps

        header = Gtk.HeaderBar()
        header.add_css_class("bifrost")
        header.set_show_title_buttons(True)
        self.banner = Banner()
        header.pack_start(self.banner)
        tabs = Gtk.Box()
        self._tab_btns: dict[str, Gtk.ToggleButton] = {}
        first = None
        for key, text in VIEWS:
            tb = Gtk.ToggleButton(label=text)
            tb.add_css_class("tab")
            if first is None:
                first = tb
            else:
                tb.set_group(first)
            tb.connect("toggled", lambda w, k=key: w.get_active() and self.show_view(k))
            tabs.append(tb)
            self._tab_btns[key] = tb
        header.set_title_widget(tabs)
        self.set_titlebar(header)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(140)
        self.player = PlayerView(ctx)
        self.browse = BrowseView(ctx)
        self.libview = LibraryView(ctx)
        self.rip = RipView(ctx)
        self.livev = LiveView(ctx)
        ctx.player, ctx.libview = self.player, self.libview
        self.stack.add_named(self.player, "player")
        self.stack.add_named(self.browse, "browse")
        self.stack.add_named(self.libview, "library")
        self.stack.add_named(self.rip, "rip")
        self.stack.add_named(self.livev, "live")
        root.append(self.stack)

        self.statusbar = label("Ready.", ["status"])
        self.statusbar.set_margin_start(16)
        self.statusbar.set_margin_bottom(8)
        root.append(self.statusbar)
        self.set_child(root)
        ctx._status_cb = self._set_status

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

        self._tab_btns["player"].set_active(True)
        self._t_last = time.monotonic()
        self._timer = GLib.timeout_add(max(8, int(1000 / fps)), self._tick)
        self.connect("close-request", self._on_close)

    def show_view(self, key: str) -> None:
        self.stack.set_visible_child_name(key)
        if key == "browse":
            self.browse.focus_search()
        elif key == "rip":
            self.rip.on_show()

    def _set_status(self, msg: str, error: bool = False) -> None:
        self.statusbar.set_label(msg)
        (self.statusbar.add_css_class if error else self.statusbar.remove_css_class)("error")

    def _on_key(self, ctl, keyval, code, state):
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if ctrl and keyval in (Gdk.KEY_1, Gdk.KEY_2, Gdk.KEY_3, Gdk.KEY_4, Gdk.KEY_5):
            self._tab_btns[VIEWS[keyval - Gdk.KEY_1][0]].set_active(True)
            return True
        if ctrl and keyval == Gdk.KEY_q:
            self.close()
            return True
        if ctrl and keyval == Gdk.KEY_space:
            self.ctx.toggle_play()
            return True
        return False

    def _tick(self):
        now = time.monotonic()
        dt, self._t_last = min(0.1, now - self._t_last), now
        self.ctx.poll_session()
        self.player.refresh()
        self.player.bridge.tick(dt, now)
        self.player.bridge.queue_draw()
        st = "live" if self.ctx.live.state == "playing" else self.ctx.engine.state
        self.banner.set_status({"playing": "playing", "paused": "paused", "live": "live"}.get(st, "idle"))
        for eng in (self.ctx.engine, self.ctx.live):
            if eng.error:
                self.ctx.status(eng.error, error=True)
                eng.error = None
        return True

    def _on_close(self, *_):
        GLib.source_remove(self._timer)
        self.ctx.shutdown()
        return False


class BifrostApp(Gtk.Application):
    def __init__(self, fps: int = 30, ctx: Optional[Context] = None):
        super().__init__(application_id=APP_ID)
        self.fps = fps
        self._ctx = ctx

    def do_startup(self):
        Gtk.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self):
        win = self.props.active_window
        if win is None:
            ctx = self._ctx or Context()
            win = BifrostWindow(self, ctx, self.fps)
        win.present()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bifrost", description="Bilateral focus audio")
    ap.add_argument("--fps", type=int, default=30, help="visualiser frame rate (default 30)")
    args = ap.parse_args(argv)
    return BifrostApp(fps=max(10, min(60, args.fps))).run([sys.argv[0]])
