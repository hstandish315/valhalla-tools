#!/usr/bin/env python3
"""
Silent filler tracks, after a real report: ripping a CD "directly to the USB" produced 93 files,
82 of them named "silence", "silence (2)" ... "silence (82)", "some not even on the cd".

They were on the disc: Antichrist Superstar carries tracks 17-98 as [silence] (about 4 s each)
and a hidden track 99 [unknown]. 98 - 17 + 1 = 82. Three things made it a mess, all covered here:
every track was ticked for ripping; the flat layout has no track number, so identical titles became
"(2)", "(3)"...; and a saved layout from an old default overrode the better new one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from bifrost import export  # noqa: E402
from bifrost.library import Entry  # noqa: E402


class TestIsShort(unittest.TestCase):
    def test_threshold(self):
        mk = lambda d: Entry(path="/x", title="t", duration=d)
        self.assertTrue(export.is_short(mk(4.0)))
        self.assertTrue(export.is_short(mk(9.9)))
        self.assertFalse(export.is_short(mk(10.0)))
        self.assertFalse(export.is_short(mk(240.0)))

    def test_an_unknown_duration_is_never_called_short(self):
        self.assertFalse(export.is_short(Entry(path="/x", title="t", duration=0)))


class TestCollidingNames(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bifrost-fill-")
        self.dst = os.path.join(self.tmp, "out")
        os.makedirs(self.dst)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def entry(self, n, title="[silence]", creator="Marilyn Manson", album="Antichrist Superstar", payload=None):
        p = os.path.join(self.tmp, f"t{n}-{title.strip('[]')}.bin")
        with open(p, "wb") as fh:
            fh.write(payload or os.urandom(100 + n))
        return Entry(path=p, title=title, creator=creator, album=album, track=n)

    def names(self):
        return sorted(f for f in os.listdir(self.dst) if not f.startswith("."))

    def test_identical_titles_get_their_track_numbers_not_anonymous_counters(self):
        es = [self.entry(17), self.entry(18), self.entry(19)]
        export.copy_tracks(es, self.dst, layout="flat")
        self.assertEqual(self.names(), ["Marilyn Manson - [silence] (17).bin", "Marilyn Manson - [silence] (18).bin",
                                        "Marilyn Manson - [silence] (19).bin"])

    def test_names_that_do_not_collide_are_left_alone(self):
        es = [self.entry(2, title="The Beautiful People"), self.entry(17), self.entry(18)]
        export.copy_tracks(es, self.dst, layout="flat")
        self.assertIn("Marilyn Manson - The Beautiful People.bin", self.names())
        self.assertEqual(len([n for n in self.names() if "silence" in n]), 2)

    def test_collisions_are_detected_ignoring_case(self):
        es = [self.entry(1, title="Silence"), self.entry(2, title="silence")]
        export.copy_tracks(es, self.dst, layout="flat")
        self.assertEqual(len(self.names()), 2)
        self.assertTrue(all("(0" in n for n in self.names()), self.names())

    def test_without_track_numbers_it_falls_back_to_the_old_counter(self):
        es = [Entry(path=self.entry(1).path, title="x", creator="a", track=0),
              Entry(path=self.entry(2).path, title="x", creator="a", track=0)]
        export.copy_tracks(es, self.dst, layout="flat")
        self.assertEqual(self.names(), ["a - x (2).bin", "a - x.bin"])

    def test_the_album_layout_never_collides_because_of_the_track_prefix(self):
        es = [self.entry(17), self.entry(18)]
        r = export.copy_tracks(es, self.dst, layout="album")
        rel = sorted(os.path.relpath(p, self.dst) for p in r.copied)
        self.assertEqual(rel, [os.path.join("Marilyn Manson", "Antichrist Superstar", "17 - [silence].bin"),
                               os.path.join("Marilyn Manson", "Antichrist Superstar", "18 - [silence].bin")])

    def test_brackets_survive_into_names_on_windows_safe_filesystems_too(self):
        es = [self.entry(17)]
        r = export.copy_tracks(es, self.dst, layout="album", fat=True, safe_names=True)
        self.assertIn("[silence]", r.copied[0])


try:
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk
    HAVE_DISPLAY = Gtk.init_check()
except Exception:                                            # pragma: no cover
    HAVE_DISPLAY = False

if HAVE_DISPLAY:
    from test_gui_rip import ALBUM, pump  # noqa: E402

    from bifrost import app, cd, dsp, engine, musicbrainz  # noqa: E402
    from bifrost.library import Library  # noqa: E402
    from bifrost.settings import Settings  # noqa: E402
    from bifrost.ui import library_view  # noqa: E402
    from bifrost.ui.export_dialog import LAYOUTS, ExportDialog  # noqa: E402
    from bifrost.ui.rip_view import RipView  # noqa: E402


def tone(path, seconds):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=duration={seconds}", path], check=True)


@unittest.skipUnless(HAVE_DISPLAY and shutil.which("ffmpeg"), "needs a display and ffmpeg")
class GuiBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bifrost-fillgui-")
        self.lib = Library(os.path.join(self.tmp, "m"), os.path.join(self.tmp, "d"))
        self.settings = Settings(os.path.join(self.tmp, "d"))
        self.ctx = app.Context(library=self.lib, chain=dsp.Chain(), settings=self.settings)
        self.ctx.window = None
        self.ctx._status_cb = lambda m, e: None

    def tearDown(self):
        self.ctx.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def album_with_silence(self, n_real=2, n_silence=5):
        """Like the real disc: a few songs, then a run of 4-second silent tracks."""
        out = []
        for i in range(1, n_real + n_silence + 1):
            real = i <= n_real
            p = os.path.join(self.tmp, f"{i:02d}.mp3")
            tone(p, 12 if real else 4)
            out.append(self.lib.add_ripped(p, f"Song {i}" if real else "[silence]", "Manson", "AS", i))
        return out


class TestExportDialogSkipsShortTracks(GuiBase):
    def run_export(self, dlg):
        dlg._on_go(None)
        self.assertTrue(pump(lambda: not dlg._busy, 60))

    def files(self, d):
        return sorted(f for r, _, fs in os.walk(d) for f in fs if f.endswith(".mp3"))

    def test_short_tracks_are_skipped_by_default_with_a_visible_option(self):
        dest = os.path.join(self.tmp, "o"); os.makedirs(dest)
        dlg = ExportDialog(self.ctx, self.album_with_silence(2, 5), "folder")
        dlg.folder = dest
        self.assertTrue(dlg.chk_skip.get_visible() and dlg.chk_skip.get_active())
        self.assertIn("5 tracks under 10 seconds", dlg.chk_skip.get_label())
        self.run_export(dlg)
        self.assertEqual(len(self.files(dest)), 2, self.files(dest))

    def test_unticking_the_option_exports_them_with_track_numbers_in_the_names(self):
        dest = os.path.join(self.tmp, "o"); os.makedirs(dest)
        dlg = ExportDialog(self.ctx, self.album_with_silence(1, 3), "folder")
        dlg.folder = dest
        dlg.dd_layout.set_selected(0)                                # flat: the layout that used to pile up (2)(3)
        dlg.chk_skip.set_active(False)
        self.run_export(dlg)
        names = self.files(dest)
        self.assertEqual(len(names), 4)
        self.assertEqual([n for n in names if "silence" in n],
                         [f"Manson - [silence] ({i:02d}).mp3" for i in (2, 3, 4)])
        self.assertFalse([n for n in names if "(2)" in n or "(3)" in n and "silence ("], "no anonymous counters")

    def test_the_option_is_hidden_when_there_is_nothing_short(self):
        dlg = ExportDialog(self.ctx, self.album_with_silence(3, 0), "folder")
        self.assertFalse(dlg.chk_skip.get_visible())

    def test_if_every_track_is_short_it_says_so_instead_of_exporting_nothing_silently(self):
        dest = os.path.join(self.tmp, "o"); os.makedirs(dest)
        dlg = ExportDialog(self.ctx, self.album_with_silence(0, 3), "folder")
        dlg.folder = dest
        dlg._on_go(None)
        self.assertFalse(dlg._busy)
        self.assertIn("Nothing to export", dlg.status.get_label())
        self.assertEqual(self.files(dest), [])


class TestLayoutIsNotSticky(GuiBase):
    def test_an_old_saved_layout_is_ignored_and_the_default_is_album(self):
        # the user's real settings file held "export_layout": "flat", saved when flat was the default
        os.makedirs(os.path.dirname(self.settings.path), exist_ok=True)
        with open(self.settings.path, "w") as fh:
            fh.write('{"export_layout": "flat"}')
        s = Settings(os.path.dirname(self.settings.path))
        self.ctx.settings = s
        dlg = ExportDialog(self.ctx, self.album_with_silence(2, 0), "folder")
        self.assertEqual(LAYOUTS[dlg.dd_layout.get_selected()][0], "album")

    def test_exporting_without_touching_the_layout_stores_nothing(self):
        dest = os.path.join(self.tmp, "o"); os.makedirs(dest)
        dlg = ExportDialog(self.ctx, self.album_with_silence(2, 0), "folder")
        dlg.folder = dest
        dlg._on_go(None)
        pump(lambda: False, 0.2)
        self.assertTrue(pump(lambda: not dlg._busy, 60))
        self.assertEqual(self.settings["export_layout_choice"], "")

    def test_a_deliberate_choice_is_remembered(self):
        dlg = ExportDialog(self.ctx, self.album_with_silence(2, 0), "folder")
        dlg.dd_layout.set_selected(0)
        self.assertEqual(self.settings["export_layout_choice"], "flat")
        again = ExportDialog(self.ctx, self.album_with_silence(2, 0), "folder")
        self.assertEqual(LAYOUTS[again.dd_layout.get_selected()][0], "flat")


class TestRipTabUntickssFiller(GuiBase):
    """A TOC shaped like the real disc: songs, a run of 4 s silences, and a hidden 99 s track."""

    def setUp(self):
        super().setUp()
        self.settings.set("rip_dir", os.path.join(self.tmp, "rips"))
        s = 75
        lengths = [180, 4, 4, 9, 99, 30]          # song, silence, silence, 9 s, hidden track, 30 s titled [silence]
        starts, pos = [], 0
        for L in lengths:
            starts.append(pos); pos += L * s
        self.toc = cd.Toc([cd.TocTrack(i + 1, starts[i], lengths[i] * s) for i in range(len(lengths))], pos)
        titles = ["A Real Song", "[silence]", "[silence]", "Short Thing", "[unknown]", "[silence]"]
        self.album = musicbrainz.Album("AS", "Manson", "1996", "mbid",
                                       [musicbrainz.TrackInfo(i + 1, t, "Manson") for i, t in enumerate(titles)])
        self.patches = [mock.patch.object(cd, "find_drives", lambda *a, **k: [cd.Drive("/dev/sr9", "A", "F")]),
                        mock.patch.object(cd, "read_toc", lambda *a, **k: self.toc),
                        mock.patch.object(musicbrainz, "lookup", lambda *a, **k: self.album)]
        for p in self.patches:
            p.start()
        self.view = RipView(self.ctx)
        self.view.refresh()
        self.assertTrue(pump(lambda: len(self.view.rows) == 6 and self.view.e_album.get_text() == "AS"))

    def tearDown(self):
        for p in self.patches:
            p.stop()
        super().tearDown()

    def ticks(self):
        return [r.check.get_active() for r in self.view.rows]

    def test_short_and_silent_tracks_start_unticked_but_the_hidden_track_does_not(self):
        # song T | 4 s F | 4 s F | 9 s F | hidden 99 s T | 30 s but MusicBrainz says "[silence]" F
        self.assertEqual(self.ticks(), [True, False, False, False, True, False])

    def test_the_reason_is_shown_on_each_row_and_in_the_note(self):
        self.assertEqual([r.state.get_label() for r in self.view.rows],
                         ["", "silence?", "silence?", "silence?", "", "silence?"])
        self.assertIn("4 very short or silent tracks are unticked", self.view.disc_note.get_label())

    def test_select_all_still_lets_the_user_have_them(self):
        self.view._on_toggle_all(None)
        self.assertEqual(self.ticks(), [True] * 6)

    def test_ripping_by_default_takes_only_the_ticked_tracks(self):
        rippd = []

        def fake(device, track, out, fmt, tags, **kw):
            rippd.append(track.number)
            p = os.path.splitext(out)[0] + ".mp3"
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tone(p, 1)
            return p
        with mock.patch.object(cd, "rip_track", fake):
            self.view._on_rip(None)
            self.assertTrue(pump(lambda: not self.view._ripping, 30))
        self.assertEqual(sorted(rippd), [1, 5])

    def test_the_tick_is_the_users_to_override(self):
        self.view.rows[1].check.set_active(True)
        self.assertTrue(self.view.rows[1].check.get_active())


class TestLibraryTickShort(GuiBase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(library_view, "ExportDialog", mock.MagicMock())
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_button_appears_only_when_short_tracks_exist_and_ticks_exactly_them(self):
        self.album_with_silence(2, 4)
        view = library_view.LibraryView(self.ctx)
        self.ctx.libview = view
        self.assertTrue(view.btn_short.get_visible())
        self.assertIn("(4)", view.btn_short.get_label())
        view._on_tick_short(None)
        ticked = [e.title for e in view.selected_entries()]
        self.assertEqual(ticked, ["[silence]"] * 4)
        view._on_remove(None)                                      # clears just those from the list
        self.assertEqual([e.title for e in self.lib.entries], ["Song 1", "Song 2"])
        self.assertFalse(view.btn_short.get_visible())

    def test_no_button_when_every_track_is_a_real_length(self):
        self.album_with_silence(3, 0)
        self.assertFalse(library_view.LibraryView(self.ctx).btn_short.get_visible())


if __name__ == "__main__":
    unittest.main()
