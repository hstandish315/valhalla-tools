#!/usr/bin/env python3
"""
Library selection, after a real bug report: "I selected one song, then clicked several and
they exported together, but the first one I hadn't selected that time exported again."

Cause: the list used GTK's multi-select, where every plain click is *added* to the selection
(and never removed by a plain click), with a faint highlight; and a single click also played the
track. Now every track has an explicit checkbox, and the export uses exactly the ticked ones.
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
gi.require_version("Graphene", "1.0")
from gi.repository import GLib, Graphene, Gtk  # noqa: E402

HAVE_DISPLAY = Gtk.init_check()

if HAVE_DISPLAY:
    from bifrost import app, dsp, engine  # noqa: E402
    from bifrost.library import Library  # noqa: E402
    from bifrost.settings import Settings  # noqa: E402
    from bifrost.ui import library_view  # noqa: E402


def pump(t=0.3):
    ctx = GLib.MainContext.default()
    end = time.time() + t
    while time.time() < end:
        while ctx.pending():
            ctx.iteration(False)
        time.sleep(0.01)


class RecordingDialog:
    """Stands in for ExportDialog and records what it was asked to export."""
    calls: list = []

    def __init__(self, ctx, entries, destination="folder"):
        RecordingDialog.calls.append(([e.title for e in entries], destination))

    def present(self):
        pass


@unittest.skipUnless(HAVE_DISPLAY and shutil.which("ffmpeg"), "needs a display and ffmpeg")
class TestLibrarySelection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bifrost-sel-")
        self.lib = Library(os.path.join(self.tmp, "m"), os.path.join(self.tmp, "d"))
        self.ctx = app.Context(library=self.lib, chain=dsp.Chain(), settings=Settings(os.path.join(self.tmp, "d")))
        self.ctx.window = None
        self.ctx._status_cb = lambda m, e: None
        for name in ("A", "B", "C", "D"):
            p = os.path.join(self.tmp, f"{name}.mp3")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=duration=1", p], check=True)
            self.lib.add_ripped(p, f"Song {name}", "Band", "LP", "ABCD".index(name) + 1)
        RecordingDialog.calls = []
        self.patch = mock.patch.object(library_view, "ExportDialog", RecordingDialog)
        self.patch.start()
        self.view = library_view.LibraryView(self.ctx)
        self.ctx.libview = self.view                 # the real window does this
        self.rows = list(self.view._rows())

    def tearDown(self):
        self.patch.stop()
        self.ctx.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def tick(self, *names):
        for r in self.rows:
            r.check.set_active(r.entry.title in {f"Song {n}" for n in names})

    def titles(self):
        return [e.title for e in self.view.selected_entries()]

    # ---- the reported scenario ------------------------------------------------
    def test_the_first_song_is_not_exported_again_after_choosing_others(self):
        self.tick("A")
        self.view._export("usb")
        self.assertEqual(RecordingDialog.calls[-1][0], ["Song A"])
        # now choose a different set: A is no longer ticked
        self.rows[0].check.set_active(False)
        self.rows[1].check.set_active(True)
        self.rows[2].check.set_active(True)
        self.view._export("usb")
        self.assertEqual(RecordingDialog.calls[-1][0], ["Song B", "Song C"])
        self.assertNotIn("Song A", RecordingDialog.calls[-1][0])

    def test_a_plain_mouse_click_on_a_row_neither_selects_nor_plays(self):
        win = Gtk.Window()
        win.set_default_size(600, 400)
        win.set_child(self.view)
        win.present()
        pump(0.6)
        gestures = [c for c in self.view.list.observe_controllers() if isinstance(c, Gtk.GestureClick)]
        played = []
        with mock.patch.object(self.ctx, "play_entry", lambda e: played.append(e.title)):
            r = self.view.list.get_first_child()
            ok, pt = r.compute_point(self.view.list, Graphene.Point().init(8, 8))
            for g in gestures:
                g.emit("pressed", 1, pt.x, pt.y)
                g.emit("released", 1, pt.x, pt.y)
            pump(0.2)
        self.assertEqual(self.titles(), [], "clicking a row must not tick it")
        self.assertEqual(played, [], "a single click must not start playback")
        win.destroy()

    def test_ticking_is_independent_per_track_and_unticking_works(self):
        self.tick("A", "B", "C")
        self.assertEqual(self.titles(), ["Song A", "Song B", "Song C"])
        self.rows[1].check.set_active(False)
        self.assertEqual(self.titles(), ["Song A", "Song C"])
        self.rows[0].check.set_active(False)
        self.rows[2].check.set_active(False)
        self.assertEqual(self.titles(), [])

    # ---- what the buttons say -------------------------------------------------
    def test_buttons_state_how_many_tracks_they_will_use(self):
        self.assertIn("(all 4)", self.view.btn_usb.get_label())
        self.assertIn("(all 4)", self.view.btn_folder.get_label())
        self.tick("B", "D")
        self.assertIn("(2)", self.view.btn_usb.get_label())
        self.assertIn("(2)", self.view.btn_folder.get_label())
        self.assertIn("(2)", self.view.btn_remove.get_label())
        self.tick()
        self.assertEqual(self.view.btn_remove.get_label(), "Remove")
        self.assertFalse(self.view.btn_remove.get_sensitive())

    def test_nothing_ticked_exports_everything_and_says_so(self):
        self.view._export("folder")
        self.assertEqual(RecordingDialog.calls[-1][0], ["Song A", "Song B", "Song C", "Song D"])
        self.assertIn("everything (4)", self.view.detail.get_label())

    def test_select_all_and_none(self):
        self.view.btn_all.emit("clicked")
        self.assertEqual(len(self.titles()), 4)
        self.assertEqual(self.view.btn_all.get_label(), "Select none")
        self.view.btn_all.emit("clicked")
        self.assertEqual(self.titles(), [])
        self.assertEqual(self.view.btn_all.get_label(), "Select all")
        self.tick("A")
        self.view.btn_all.emit("clicked")                     # some ticked: the button completes the set
        self.assertEqual(len(self.titles()), 4)

    def test_the_detail_line_shows_attribution_for_one_ticked_track(self):
        self.tick("C")
        self.assertIn("Song C", self.view.detail.get_label())
        self.tick("A", "B")
        self.assertIn("2 tracks ticked", self.view.detail.get_label())

    # ---- playing and removing -------------------------------------------------
    def test_double_click_or_enter_plays_and_ticking_does_not(self):
        played = []
        with mock.patch.object(self.ctx, "play_entry", lambda e: played.append(e.title)):
            self.tick("A", "B")
            self.assertEqual(played, [])
            self.assertFalse(self.view.list.get_activate_on_single_click())
            self.view.list.emit("row-activated", self.view.list.get_first_child())
        self.assertEqual(played, ["Song A"])

    def test_remove_acts_only_on_ticked_tracks_and_keeps_the_rest_ticked(self):
        self.tick("B", "C")
        self.view._on_remove(None)
        self.assertEqual([e.title for e in self.lib.entries], ["Song A", "Song D"])
        self.rows = list(self.view._rows())
        self.assertEqual(len(self.rows), 2, "the list is rebuilt without the removed tracks")
        self.assertEqual(self.titles(), [], "the removed tracks leave nothing behind ticked")

    def test_ticks_survive_a_refresh(self):
        self.tick("B", "D")
        self.ctx.library_changed()
        self.assertEqual(self.titles(), ["Song B", "Song D"])

    def test_refresh_after_a_new_track_is_added_keeps_existing_ticks(self):
        self.tick("A")
        p = os.path.join(self.tmp, "E.mp3")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=duration=1", p], check=True)
        self.lib.add_ripped(p, "Song E", "Band", "LP", 5)
        self.ctx.library_changed()
        self.assertEqual(self.titles(), ["Song A"])
        self.assertEqual(len(list(self.view._rows())), 5)

    def test_there_is_no_list_selection_left_to_linger(self):
        self.assertEqual(self.view.list.get_selection_mode(), Gtk.SelectionMode.NONE)
        self.assertEqual(self.view.list.get_selected_rows(), [])


if __name__ == "__main__":
    unittest.main()
