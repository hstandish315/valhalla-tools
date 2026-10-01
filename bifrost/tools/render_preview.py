#!/usr/bin/env python3
"""
Visual preview: run the real app and snapshot each view to a PNG.

Wayland blocks desktop capture on the author's Wayland desktop, so instead of screenshotting the
screen the app asks GTK to render its own window to a texture. Audio is replaced
by a silent sink that sleeps in real time, and the disc drive, MusicBrainz and
PipeWire are replaced by synthetic data, so the views show realistic content with
nothing played, read or rerouted.

    venv/bin/python3 tools/render_preview.py OUT_DIR [--track FILE ...]
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest import mock  # noqa: E402

from gi.repository import GLib, Graphene, Gtk  # noqa: E402

from bifrost import app, cd, engine, live, musicbrainz, presets  # noqa: E402
from bifrost.dsp import SAMPLE_RATE, Chain  # noqa: E402
from bifrost.library import Library  # noqa: E402
from bifrost.settings import Settings  # noqa: E402
from bifrost.sources import match  # noqa: E402
from bifrost.sources.common import Track  # noqa: E402
from bifrost.ui.export_dialog import ExportDialog  # noqa: E402


class PacedSilentSink(engine.MemorySink):
    """Discards audio but blocks for its real duration, like a sound card would."""

    def write(self, data: bytes) -> None:
        time.sleep(len(data) / engine.BYTES_PER_FRAME / SAMPLE_RATE)


class FakeRouter:
    """Shows two playing apps, one of them already sent through Bifrost."""
    active = True
    moved = {91}

    def streams(self, exclude_pids=()):
        return [live.Stream(91, "Spotify", "spotify", "Focus Flow", routed=True),
                live.Stream(92, "LibreWolf", "librewolf", "Lofi beats to study to")]

    def move(self, i): pass
    def restore(self, i): pass
    def destroy_sink(self): pass


SYN_TOC = cd.Toc([cd.TocTrack(1, 0, 25982), cd.TocTrack(2, 25982, 19178), cd.TocTrack(3, 45160, 25690),
                  cd.TocTrack(4, 70850, 15525), cd.TocTrack(5, 86375, 21345)], 107720)
SYN_ALBUM = musicbrainz.Album("Evening Tide", "Example Ensemble", "1998", "mbid", [
    musicbrainz.TrackInfo(1, "Harbour Lights", "Example Ensemble"),
    musicbrainz.TrackInfo(2, "The Long Crossing", "Example Ensemble"),
    musicbrainz.TrackInfo(3, "Salt and Rope", "Example Ensemble"),
    musicbrainz.TrackInfo(4, "Low Water", "Example Ensemble"),
    musicbrainz.TrackInfo(5, "Morning Bells", "Example Ensemble")])


def snapshot(win: Gtk.Window, path: str) -> None:
    w, h = win.get_width(), win.get_height()
    paintable = Gtk.WidgetPaintable.new(win)
    snap = Gtk.Snapshot()
    paintable.snapshot(snap, w, h)
    node = snap.to_node()
    texture = win.get_native().get_renderer().render_texture(node, Graphene.Rect().init(0, 0, w, h))
    texture.save_to_png(path)
    print(f"wrote {path} ({w}x{h})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", help="directory for the PNGs")
    ap.add_argument("--track", action="append", default=[], help="audio file(s) to put in the library")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    tmp = tempfile.mkdtemp(prefix="bifrost-preview-")
    # A nominal path: it is shown on screen, and published screenshots must not
    # carry the real home directory.
    lib = Library("/home/user/Music/Bifrost", os.path.join(tmp, "data"))
    names = ["Harbour Lights", "The Long Crossing", "Salt and Rope", "Low Water", "Morning Bells"]
    for t in args.track:
        e = lib.add_local(t)
        if e:
            n = len(lib.entries)
            e.title, e.creator, e.album, e.track = names[(n - 1) % len(names)], "Example Ensemble", "Evening Tide", n
            e.license, e.source = "Ripped from own CD", "cd"
    settings = Settings(os.path.join(tmp, "data"))
    settings.set("rip_dir", "/home/user/Music/Bifrost/Rips")
    settings.set("export_dir", "/home/user/Music")

    patches = [mock.patch.object(cd, "find_drives", lambda *a, **k: [cd.Drive("/dev/sr0", "ACME", "USB DVD-RW")]),
               mock.patch.object(cd, "read_toc", lambda *a, **k: SYN_TOC),
               mock.patch.object(musicbrainz, "lookup", lambda *a, **k: SYN_ALBUM),
               mock.patch.object(live, "missing_tools", lambda *a, **k: [])]
    for p in patches:
        p.start()

    chain = Chain(fade_in_s=0.5)
    ctx = app.Context(library=lib, chain=chain, settings=settings)
    ctx.engine = engine.Engine(chain, sink_factory=PacedSilentSink, on_end=ctx._on_track_end)
    application = app.BifrostApp(fps=30, ctx=ctx)
    win = lambda: application.props.active_window

    def snap(name, window=None):
        def go():
            snapshot(window() if callable(window) else (window or win()), os.path.join(args.out, name))
            return False
        return go

    def step_player():
        if lib.entries:
            ctx.play_entry(lib.entries[0])
        win().player.apply_preset(presets.PRESETS[0])
        return False

    def show_browse():
        w = win()
        w._tab_btns["browse"].set_active(True)
        mk = lambda t, c: Track(source="openverse", ident=t + c, title=t, creator=c, license="CC BY 3.0",
                                provider="jamendo", duration=264.0, download_url="https://example.invalid/x")
        view = w.browse
        view.entry.set_text("Bring Me to Life")
        view.artist.set_text("Evanescence")
        view._title, view._artist, view._source, view._hits = "Bring Me to Life", "Evanescence", 0, []
        view.list.remove_all()
        view.hide.set_active(False)                 # show the labelled non-matches
        view._search_done([mk("Bring Me To Life", "Lost Pilgrim"), mk("Bring Me To Life", "Lost Pilgrim"),
                           mk("Bring Me Life", "PM music Prod")], 3, "", True)
        return False

    def show(tab):
        def go():
            win()._tab_btns[tab].set_active(True)
            return False
        return go

    def live_on():
        ctx.router = FakeRouter()
        ctx.live.state = "playing"
        win().livev.sync()
        return False

    dialog = {}

    def open_export():
        w = win()
        w._tab_btns["library"].set_active(True)
        ctx.live.state = "stopped"
        d = ExportDialog(ctx, lib.entries, "usb")
        d.volumes = [export_volume()]
        d.dd_usb.set_model(Gtk.StringList.new([v.title for v in d.volumes]))
        d.usb_note.set_label("")
        d.present()
        dialog["d"] = d
        return False

    def export_volume():
        from bifrost import export
        return export.Volume("/dev/sdb1", "/dev/sdb", "MUSIC", "vfat", 16 * 1024 ** 3, 15 * 1024 ** 3, "/media/user/MUSIC")

    def close_dialog():
        dialog["d"].close()
        return False

    def finish():
        ctx.live.state = "stopped"
        ctx.engine.stop()
        application.quit()
        return False

    def kickoff():
        t = GLib.timeout_add
        t(300, step_player)
        t(2600, snap("player.png"))
        t(2900, show_browse)
        t(3700, snap("browse.png"))
        t(3900, show("library"))
        t(4700, snap("library.png"))
        t(4900, show("rip"))
        t(6400, snap("rip.png"))
        t(6600, live_on)
        t(6700, show("live"))
        t(8400, snap("live.png"))
        t(8600, open_export)
        t(9600, lambda: snap("export.png", lambda: dialog["d"])())
        t(9900, close_dialog)
        t(10100, finish)
        return False

    GLib.idle_add(kickoff)
    try:
        application.run([sys.argv[0]])
    finally:
        for p in patches:
            p.stop()
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
