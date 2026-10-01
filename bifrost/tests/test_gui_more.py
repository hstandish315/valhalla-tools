#!/usr/bin/env python3
"""
Wiring tests for Live mode, the search view and the export dialog, driven
through the real widgets with PipeWire, the network and the USB stick faked.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

HAVE_DISPLAY = Gtk.init_check()

if HAVE_DISPLAY:
    from test_gui_rip import pump  # noqa: E402
    from test_live import SPOTIFY, FakePipeWire, ToneLive  # noqa: E402

    from bifrost import app, dsp, engine, export  # noqa: E402
    from bifrost.library import Entry, Library  # noqa: E402
    from bifrost.settings import Settings  # noqa: E402
    from bifrost.sources.common import Track  # noqa: E402
    from bifrost.ui.browse import BrowseView  # noqa: E402
    from bifrost.ui.export_dialog import ExportDialog  # noqa: E402
    from bifrost.ui.live_view import LiveView  # noqa: E402
    from bifrost.ui.player import PlayerView  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bifrost-gm-")
        self.lib = Library(os.path.join(self.tmp, "music"), os.path.join(self.tmp, "data"))
        self.settings = Settings(os.path.join(self.tmp, "data"))
        self.ctx = app.Context(library=self.lib, chain=dsp.Chain(fade_in_s=0.0), settings=self.settings)
        self.ctx.engine = engine.Engine(self.ctx.chain, sink_factory=engine.MemorySink)
        self.msgs = []
        self.ctx._status_cb = lambda m, e: self.msgs.append((m, e))

    def tearDown(self):
        self.ctx.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def tone_entry(self, name="t.mp3", seconds=30, **kw):
        path = os.path.join(self.tmp, name)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"sine=frequency=330:duration={seconds}", path], check=True)
        e = self.lib.add_local(path)
        for k, v in kw.items():
            setattr(e, k, v)
        return e


@unittest.skipUnless(HAVE_DISPLAY and shutil.which("ffmpeg"), "needs a display and ffmpeg")
class TestLiveWiring(Base):
    def setUp(self):
        super().setUp()
        self.fp = FakePipeWire([SPOTIFY])
        self.ctx.router = self.fp.router()
        self.ctx.live = ToneLive(self.ctx.chain, self.ctx.router, sink_factory=engine.MemorySink)
        self.player = PlayerView(self.ctx)
        self.ctx.player = self.player

    def test_start_creates_the_sink_runs_the_engine_and_labels_the_player(self):
        self.ctx.live_start()
        self.assertTrue(self.ctx.live_running)
        self.assertTrue(self.ctx.router.active)
        self.assertEqual(self.player.title.get_label(), "Live audio")
        self.assertIs(self.ctx.active_engine(), self.ctx.live)

    def test_playing_a_file_ends_live_mode_and_restores_apps(self):
        self.ctx.live_start()
        self.ctx.router.move(91)
        self.ctx.play_entry(self.tone_entry())
        self.assertFalse(self.ctx.router.active)
        self.assertTrue(self.fp.cli.terminated)
        self.assertIn(["pw-metadata", "-d", "91", "target.object"], self.fp.cmds)
        self.assertEqual(self.ctx.engine.state, "playing")

    def test_the_transport_button_ends_live_mode(self):
        self.ctx.live_start()
        self.ctx.toggle_play()
        self.assertFalse(self.ctx.live_running)
        self.assertFalse(self.ctx.router.active)

    def test_shutdown_returns_every_app_before_exit(self):
        self.ctx.live_start()
        self.ctx.router.move(91)
        self.ctx.shutdown()
        self.assertEqual(self.ctx.router.moved, set())
        self.assertTrue(self.fp.cli.terminated)

    def test_a_failed_start_reports_and_does_not_leave_a_half_started_engine(self):
        bad = FakePipeWire(create_works=False)
        self.ctx.router = bad.router()
        self.ctx.live = ToneLive(self.ctx.chain, self.ctx.router, sink_factory=engine.MemorySink)
        self.ctx.live_start()
        self.assertFalse(self.ctx.live_running)
        self.assertEqual(self.ctx.live.state, "stopped")
        self.assertTrue(self.msgs and self.msgs[-1][1])

    def test_missing_tools_are_named_in_the_message(self):
        with mock.patch("bifrost.app.missing_tools", lambda: ["pw-cli"]):
            self.ctx.live_start()
        self.assertIn("pw-cli", self.msgs[-1][0])
        self.assertFalse(self.ctx.router.active)

    def test_a_session_does_not_start_file_playback_over_live_audio(self):
        self.tone_entry()
        self.ctx.live_start()
        self.ctx.toggle_session()
        self.assertTrue(self.ctx.session.running)
        self.assertEqual(self.ctx.engine.state, "stopped")
        self.assertTrue(self.ctx.live_running)

    def test_the_live_tab_lists_playing_apps_and_moves_them(self):
        view = LiveView(self.ctx)
        self.assertIn("Off", view.state.get_label())
        view.toggle_live()
        self.assertTrue(self.ctx.live_running)
        self.assertIn("Live mode is on", view.state.get_label())
        rows = []
        r = view.list.get_first_child()
        while r is not None:
            rows.append(r)
            r = r.get_next_sibling()
        self.assertEqual(len(rows), 1)
        view.toggle(self.ctx.router.streams()[0])
        self.assertIn(91, self.ctx.router.moved)
        self.assertIn("1 stream", view.state.get_label())
        view.toggle(self.ctx.router.streams()[0])
        self.assertEqual(self.ctx.router.moved, set())
        view.toggle_live()
        self.assertFalse(self.ctx.live_running)


@unittest.skipUnless(HAVE_DISPLAY, "needs a display")
class TestBrowseHonesty(Base):
    """The case from real use: asking for Evanescence returned another band's song."""

    def setUp(self):
        super().setUp()
        self.view = BrowseView(self.ctx)
        self.mk = lambda title, creator: Track(source="openverse", ident=f"{title}{creator}", title=title,
                                               creator=creator, license="CC BY 3.0", provider="jamendo",
                                               download_url="https://example.invalid/x.mp3")

    def visible_rows(self):
        out, r = [], self.view.list.get_first_child()
        while r is not None:
            if self.view._row_visible(r):
                out.append(r.get_child().track.creator)
            r = r.get_next_sibling()
        return out

    def search_with(self, tracks, title, artist):
        self.view.entry.set_text(title)
        self.view.artist.set_text(artist)
        self.view._title, self.view._artist, self.view._source = title, artist, 0
        self.view._hits = []
        self.view.list.remove_all()
        self.view.hide.set_active(bool(artist))
        self.view._search_done(tracks, len(tracks), "", True)

    def test_wrong_artist_results_are_hidden_and_the_status_explains(self):
        self.search_with([self.mk("Bring Me To Life", "Lost Pilgrim"),
                          self.mk("Bring Me To Life", "Lost Pilgrim")], "Bring Me to Life", "Evanescence")
        self.assertEqual(self.visible_rows(), [])
        msg = self.view.status.get_label()
        self.assertIn("No openly licensed match", msg)
        self.assertIn("Rip CD", msg)

    def test_unhiding_shows_them_labelled_as_a_different_artist(self):
        self.search_with([self.mk("Bring Me To Life", "Lost Pilgrim")], "Bring Me to Life", "Evanescence")
        self.view.hide.set_active(False)
        self.assertEqual(self.visible_rows(), ["Lost Pilgrim"])
        row = self.view.list.get_first_child().get_child()
        self.assertEqual(row.hit.note, "Different artist")
        self.assertIn("other songs", self.view.status.get_label())

    def test_the_real_artist_is_shown_and_sorted_first(self):
        self.search_with([self.mk("Bring Me To Life", "Lost Pilgrim"),
                          self.mk("Bring Me to Life", "Evanescence")], "Bring Me to Life", "Evanescence")
        self.assertEqual(self.visible_rows(), ["Evanescence"])
        first = self.view.list.get_first_child().get_child()
        self.assertTrue(first.hit.exact)

    def test_without_an_artist_nothing_is_hidden_by_default(self):
        self.search_with([self.mk("Ambient Flight", "Zeropage")], "ambient", "")
        self.assertEqual(self.visible_rows(), ["Zeropage"])
        self.assertFalse(self.view.hide.get_active())

    def test_search_errors_are_shown_not_swallowed(self):
        self.view._title = "x"
        self.view._search_done([], 0, "Search failed: boom", True)
        self.assertIn("boom", self.view.status.get_label())


@unittest.skipUnless(HAVE_DISPLAY and shutil.which("ffmpeg"), "needs a display and ffmpeg")
class TestExportDialog(Base):
    def entries(self):
        a = self.tone_entry("a.mp3", 12, title="What? Why", creator="AC/DC", license="CC BY 4.0",
                            attribution='"What? Why" by AC/DC, CC BY 4.0')
        b = self.tone_entry("b.mp3", 12, title="Second", creator="Band", album="LP", track=2)
        return [a, b]

    def run_export(self, dlg):
        dlg._on_go(None)
        self.assertTrue(pump(lambda: not dlg._busy, 40))

    def test_exports_to_a_chosen_folder(self):
        dest = os.path.join(self.tmp, "folder")
        os.makedirs(dest)
        dlg = ExportDialog(self.ctx, self.entries(), "folder")
        dlg.folder = dest
        dlg.dd_layout.set_selected(0)                               # flat: the default is now Artist/Album
        self.run_export(dlg)
        files = sorted(os.listdir(dest))
        self.assertIn("Band - Second.mp3", files)
        self.assertTrue(any(f.startswith("AC") and f.endswith("Why.mp3") for f in files), files)
        self.assertEqual(len([f for f in files if f.endswith(".mp3")]), 2)
        self.assertIn("Copied 2 tracks", dlg.status.get_label())
        self.assertTrue(dlg.btn_open.get_visible())
        self.assertFalse(dlg.btn_remove.get_visible())

    def test_exports_to_a_simulated_usb_stick_with_fat_safe_names_and_attribution(self):
        stick = os.path.join(self.tmp, "stick")
        os.makedirs(stick)
        vol = export.Volume("/dev/sdz1", "/dev/sdz", "MUSIC", "vfat", 8 * 1024 ** 3, 7 * 1024 ** 3, stick)
        removed = []
        with mock.patch.object(export, "removable_volumes", lambda *a, **k: [vol]), \
                mock.patch.object(export, "safely_remove", lambda v, *a, **k: removed.append(v.device)):
            dlg = ExportDialog(self.ctx, self.entries(), "usb")
            self.assertEqual(len(dlg.volumes), 1)
            self.assertTrue(dlg.use_usb)
            self.run_export(dlg)
            names = os.listdir(os.path.join(stick, export_dialog_folder()))
            self.assertTrue(all(not set('<>:"/\\|?*') & set(n) for n in names), names)
            self.assertIn(export.ATTRIBUTION_FILE, names)
            self.assertTrue(dlg.btn_remove.get_visible())
            dlg._on_remove(None)
            self.assertEqual(removed, ["/dev/sdz1"])
            self.assertFalse(dlg.btn_remove.get_visible())
            self.assertIn("safe to unplug", dlg.status.get_label())

    def test_usb_with_no_drive_says_so_and_does_not_export(self):
        with mock.patch.object(export, "removable_volumes", lambda *a, **k: []):
            dlg = ExportDialog(self.ctx, self.entries(), "usb")
            self.assertIn("No USB drive found", dlg.usb_note.get_label())
            dlg._on_go(None)
        self.assertFalse(dlg._busy)
        self.assertIn("Plug in", dlg.status.get_label())

    def test_processed_mode_renders_bilateral_copies(self):
        dest = os.path.join(self.tmp, "proc")
        os.makedirs(dest)
        dlg = ExportDialog(self.ctx, self.entries(), "folder")
        dlg.folder = dest
        dlg.btn_proc.set_active(True)
        dlg.dd_fmt.set_selected(0)                                  # FLAC
        dlg.dd_layout.set_selected(0)
        self.run_export(dlg)
        edits = os.path.join(dest, "audio_edits")                   # bilateral versions live apart
        flacs = [f for f in os.listdir(edits) if f.endswith(".flac")]
        self.assertEqual(len(flacs), 2)
        self.assertTrue(all(engine.is_audio_file(os.path.join(edits, f)) for f in flacs))
        self.assertEqual([f for f in os.listdir(dest) if f.endswith((".flac", ".mp3"))], [],
                         "nothing but the audio_edits folder should appear next to the originals")

    def test_no_derivatives_tracks_are_skipped_in_processed_mode_with_a_visible_reason(self):
        dest = os.path.join(self.tmp, "nd")
        os.makedirs(dest)
        nd = self.tone_entry("nd.mp3", 12, title="ND Song", license="CC BY-ND 4.0")
        dlg = ExportDialog(self.ctx, [nd], "folder")
        dlg.folder = dest
        dlg.btn_proc.set_active(True)
        self.assertIn("no-derivatives", dlg.mode_note.get_label())
        self.run_export(dlg)
        found = [f for _, _, fs in os.walk(dest) for f in fs if f.endswith((".mp3", ".flac"))]
        self.assertEqual(found, [])
        self.assertIn("no-derivatives", dlg.status.get_label())

    def test_cancel_during_export_removes_partial_files(self):
        dest = os.path.join(self.tmp, "cancel")
        os.makedirs(dest)
        big = self.tone_entry("big.mp3", 2)
        with open(big.path, "ab") as fh:
            fh.write(os.urandom(12 * 1024 * 1024))
        dlg = ExportDialog(self.ctx, [big], "folder")
        dlg.folder = dest
        dlg._on_go(None)
        dlg._cancel.set()
        self.assertTrue(pump(lambda: not dlg._busy, 20))
        self.assertEqual([f for _, _, fs in os.walk(dest) for f in fs if f.endswith(".mp3")], [])

    def test_settings_remember_a_layout_the_user_actually_picked(self):
        dest = os.path.join(self.tmp, "lay")
        os.makedirs(dest)
        dlg = ExportDialog(self.ctx, self.entries(), "folder")
        dlg.folder = dest
        dlg.dd_layout.set_selected(1)                               # Artist / Title (not the default)
        self.assertEqual(self.settings["export_layout_choice"], "artist")
        self.run_export(dlg)
        self.assertTrue(os.path.exists(os.path.join(dest, "Band", "Second.mp3")))
        again = ExportDialog(self.ctx, self.entries(), "folder")      # the next dialog opens on that choice
        from bifrost.ui.export_dialog import LAYOUTS
        self.assertEqual(LAYOUTS[again.dd_layout.get_selected()][0], "artist")


def export_dialog_folder() -> str:
    from bifrost.ui.export_dialog import USB_FOLDER
    return USB_FOLDER


if __name__ == "__main__":
    unittest.main()
