#!/usr/bin/env python3
"""
The Forge (CD rip view) driven end to end through the real widgets. The drive,
the disc and MusicBrainz are faked at module level, and ffmpeg stands in for the
disc reader, so the full flow runs anywhere with a display: read the disc, fill
in names, tick tracks, rip into the chosen folder, add to the library.

An opt-in test (BIFROST_RIP_TEST=1) rips a real track from a real disc.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

HAVE_DISPLAY = Gtk.init_check()

if HAVE_DISPLAY:
    from bifrost import app, cd, dsp, engine, musicbrainz  # noqa: E402
    from bifrost.library import Library  # noqa: E402
    from bifrost.settings import Settings  # noqa: E402
    from bifrost.ui.rip_view import RipView  # noqa: E402


def pump(cond, timeout=20.0):
    """Run the GTK main loop until `cond()` or timeout (worker threads report via idle_add)."""
    end = time.time() + timeout
    ctx = GLib.MainContext.default()
    while time.time() < end:
        while ctx.pending():
            ctx.iteration(False)
        if cond():
            return True
        time.sleep(0.02)
    return False


def ffmpeg_source(sleep=0.0):
    def cmd(device, track, wav):
        gen = (f"sleep {sleep}; " if sleep else "") + \
              f"ffmpeg -v error -y -f lavfi -i sine=frequency=330:sample_rate=44100:duration=3 " \
              f"-ac 2 -c:a pcm_s16le '{wav}'"
        return ["sh", "-c", gen]
    return cmd


def toc3(data_last=False):
    tracks = [cd.TocTrack(1, 0, 225), cd.TocTrack(2, 225, 225), cd.TocTrack(3, 450, 225, is_data=data_last)]
    return cd.Toc(tracks, 675)


ALBUM = None
if HAVE_DISPLAY:
    ALBUM = musicbrainz.Album("Test Album", "Test Band", "1999", "mbid",
                              [musicbrainz.TrackInfo(1, "First Song", "Test Band", 3000),
                               musicbrainz.TrackInfo(2, "Second Song", "Test Band", 3000),
                               musicbrainz.TrackInfo(3, "Third Song", "Test Band", 3000)])


@unittest.skipUnless(HAVE_DISPLAY and shutil.which("ffmpeg"), "needs a display and ffmpeg")
class TestForge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bifrost-forge-")
        self.lib = Library(os.path.join(self.tmp, "music"), os.path.join(self.tmp, "data"))
        self.settings = Settings(os.path.join(self.tmp, "data"))
        self.rips = os.path.join(self.tmp, "rips")
        self.settings.set("rip_dir", self.rips)
        self.settings.set("rip_lookup", True)
        self.ctx = app.Context(library=self.lib, chain=dsp.Chain(), settings=self.settings)
        self.ctx.engine = engine.Engine(self.ctx.chain, sink_factory=engine.MemorySink)
        self.patches = [
            mock.patch.object(cd, "find_drives", lambda *a, **k: [cd.Drive("/dev/sr9", "ACME", "Fake")]),
            mock.patch.object(cd, "read_toc", lambda *a, **k: toc3()),
            mock.patch.object(musicbrainz, "lookup", lambda *a, **k: ALBUM),
        ]
        for p in self.patches:
            p.start()
        self.view = RipView(self.ctx)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def load(self):
        self.view.refresh()
        self.assertTrue(pump(lambda: len(self.view.rows) == 3 and self.view.e_album.get_text() == "Test Album"))

    def use_fake_reader(self, sleep=0.0):
        real = cd.rip_track

        def wrapped(device, track, out, fmt, tags, **kw):
            kw["source_cmd"] = ffmpeg_source(sleep)
            return real(device, track, out, fmt, tags, **kw)
        p = mock.patch.object(cd, "rip_track", wrapped)
        p.start()
        self.patches.append(p)

    def test_disc_is_read_and_names_filled_from_musicbrainz(self):
        self.load()
        self.assertEqual(self.view.e_artist.get_text(), "Test Band")
        self.assertEqual(self.view.e_year.get_text(), "1999")
        self.assertEqual([r.title.get_text() for r in self.view.rows],
                         ["First Song", "Second Song", "Third Song"])
        self.assertIn("Matched", self.view.disc_note.get_label())

    def test_rips_the_ticked_tracks_into_the_chosen_folder_and_the_library(self):
        self.use_fake_reader()
        self.load()
        self.view.rows[1].check.set_active(False)                   # leave out track 2
        self.view.dd_fmt.set_selected(0)                            # FLAC
        self.view._on_rip(None)
        self.assertTrue(pump(lambda: not self.view._ripping and len(self.view.ripped) == 2, 40))
        want = [os.path.join(self.rips, "Test Band", "Test Album", "01 - First Song.flac"),
                os.path.join(self.rips, "Test Band", "Test Album", "03 - Third Song.flac")]
        for path in want:
            self.assertTrue(os.path.exists(path), path)
            self.assertTrue(engine.is_audio_file(path))
        self.assertFalse(os.path.exists(os.path.join(self.rips, "Test Band", "Test Album", "02 - Second Song.flac")))
        self.assertEqual(sorted(e.path for e in self.lib.entries), sorted(want))
        e = next(x for x in self.lib.entries if "First" in x.title)
        self.assertEqual((e.album, e.creator, e.license), ("Test Album", "Test Band", "Ripped from own CD"))
        self.assertTrue(e.allows_modification)
        self.assertTrue(self.view.btn_usb.get_sensitive())
        self.assertIn("Ripped 2 of 2", self.view.status.get_label())

    def test_edited_names_are_what_gets_written(self):
        self.use_fake_reader()
        self.load()
        self.view.rows[0].title.set_text("My Own Title")
        for r in self.view.rows[1:]:
            r.check.set_active(False)
        self.view._on_rip(None)
        self.assertTrue(pump(lambda: len(self.view.ripped) == 1, 30))
        self.assertTrue(os.path.exists(os.path.join(self.rips, "Test Band", "Test Album", "01 - My Own Title.flac")))

    def test_the_save_folder_setting_is_used(self):
        self.use_fake_reader()
        other = os.path.join(self.tmp, "elsewhere")
        self.settings.set("rip_dir", other)
        self.load()
        for r in self.view.rows[1:]:
            r.check.set_active(False)
        self.view._on_rip(None)
        self.assertTrue(pump(lambda: len(self.view.ripped) == 1, 30))
        self.assertTrue(self.view.ripped[0].path.startswith(other))

    def test_data_track_cannot_be_selected(self):
        with mock.patch.object(cd, "read_toc", lambda *a, **k: toc3(data_last=True)):
            self.view.refresh()
            self.assertTrue(pump(lambda: len(self.view.rows) == 3))
        last = self.view.rows[2]
        self.assertFalse(last.check.get_active())
        self.assertFalse(last.check.get_sensitive())

    def test_nothing_ticked_does_not_start(self):
        self.load()
        for r in self.view.rows:
            r.check.set_active(False)
        self.view._on_rip(None)
        self.assertFalse(self.view._ripping)
        self.assertIn("Tick at least one", self.view.status.get_label())

    def test_cancel_stops_and_leaves_no_files(self):
        self.use_fake_reader(sleep=8)
        self.load()
        self.view._on_rip(None)
        self.assertTrue(self.view._ripping)
        pump(lambda: False, 0.6)
        self.view._on_rip(None)                                     # the button now says Cancel
        self.assertTrue(pump(lambda: not self.view._ripping, 10))
        self.assertIn("Cancelled", self.view.status.get_label())
        self.assertEqual(self.view.ripped, [])
        found = [f for _, _, fs in os.walk(self.tmp) for f in fs if f.endswith(".flac") or ".rip-" in f]
        self.assertEqual(found, [])

    def test_a_failed_track_is_reported_and_the_others_continue(self):
        calls = {"n": 0}
        real = cd.rip_track

        def flaky(device, track, out, fmt, tags, **kw):
            calls["n"] += 1
            if track.number == 1:
                raise cd.CDError("Reading track 1 failed: sector 99")
            kw["source_cmd"] = ffmpeg_source()
            return real(device, track, out, fmt, tags, **kw)
        p = mock.patch.object(cd, "rip_track", flaky)
        p.start()
        self.patches.append(p)
        self.load()
        self.view._on_rip(None)
        self.assertTrue(pump(lambda: not self.view._ripping, 40))
        self.assertEqual(len(self.view.ripped), 2)
        self.assertIn("Ripped 2 of 3", self.view.status.get_label())

    def test_no_drive_message(self):
        with mock.patch.object(cd, "find_drives", lambda *a, **k: []):
            self.view.refresh()
        self.assertIn("No optical drive", self.view.disc_note.get_label())
        self.assertFalse(self.view.btn_rip.get_sensitive())

    def test_empty_drive_message_comes_from_the_error(self):
        def nodisc(*a, **k):
            raise cd.CDError("No disc in the drive.")
        with mock.patch.object(cd, "read_toc", nodisc):
            self.view.refresh()
            self.assertTrue(pump(lambda: "No disc" in self.view.disc_note.get_label()))
        self.assertFalse(self.view.btn_rip.get_sensitive())

    def test_unknown_disc_leaves_names_editable(self):
        with mock.patch.object(musicbrainz, "lookup", lambda *a, **k: None):
            self.view.refresh()
            self.assertTrue(pump(lambda: "doesn't know this disc" in self.view.disc_note.get_label()))
        self.assertEqual(self.view.rows[0].title.get_text(), "Track 1")
        self.assertTrue(self.view.rows[0].title.get_sensitive())

    def test_lookup_can_be_turned_off(self):
        self.settings.set("rip_lookup", False)
        calls = []
        with mock.patch.object(musicbrainz, "lookup", lambda *a, **k: calls.append(1)):
            self.view.refresh()
            self.assertTrue(pump(lambda: len(self.view.rows) == 3))
            pump(lambda: False, 0.4)
        self.assertEqual(calls, [])

    def test_a_stale_refresh_cannot_overwrite_a_newer_one(self):
        self.view.refresh()
        self.view.refresh()
        self.assertTrue(pump(lambda: len(self.view.rows) == 3))
        pump(lambda: False, 0.5)
        self.assertEqual(len(self.view.rows), 3)                    # not 6


@unittest.skipUnless(HAVE_DISPLAY and os.environ.get("BIFROST_RIP_TEST") == "1", "set BIFROST_RIP_TEST=1")
class TestRealDiscRip(unittest.TestCase):
    """Rips the shortest audio track of whatever disc is in the drive (about a minute)."""

    def test_real_rip(self):
        drives = cd.find_drives()
        if not drives:
            self.skipTest("no drive")
        try:
            toc = cd.read_toc(drives[0].device)
        except cd.CDError as exc:
            self.skipTest(str(exc))
        track = min(toc.audio_tracks, key=lambda t: t.sectors)
        tmp = tempfile.mkdtemp(prefix="bifrost-real-")
        try:
            tags = cd.Tags(title="Real", artist="Disc", album="Test", track=track.number, total=len(toc.tracks))
            seen = []
            path = cd.rip_track(drives[0].device, track, os.path.join(tmp, "x"), "flac", tags,
                                progress=seen.append)
            self.assertTrue(engine.is_audio_file(path))
            self.assertAlmostEqual(engine.probe_duration(path), track.seconds, delta=0.1)
            self.assertEqual(seen[-1], 1.0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
