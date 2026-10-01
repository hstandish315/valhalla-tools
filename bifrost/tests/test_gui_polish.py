#!/usr/bin/env python3
"""
The car-stereo scenario through the real widgets: rip albums, export bilateral versions to a
USB stick (simulated by a folder), and check what a head unit would see - an audio_edits folder,
MP3 files named in track order, with title/artist/album/track tags, cover art and an ID3v1 tag.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

HAVE_DISPLAY = Gtk.init_check()

if HAVE_DISPLAY:
    from test_gui_rip import ALBUM, ffmpeg_source, pump, toc3  # noqa: E402
    from test_polish import has_cover, tags_of  # noqa: E402

    from bifrost import app, cd, dsp, engine, export, musicbrainz  # noqa: E402
    from bifrost.library import Library  # noqa: E402
    from bifrost.settings import Settings  # noqa: E402
    from bifrost.ui.export_dialog import ExportDialog  # noqa: E402
    from bifrost.ui.rip_view import RipView  # noqa: E402


def ff(*a):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *a], check=True)


@unittest.skipUnless(HAVE_DISPLAY and shutil.which("ffmpeg"), "needs a display and ffmpeg")
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bifrost-gp-")
        self.lib = Library(os.path.join(self.tmp, "m"), os.path.join(self.tmp, "d"))
        self.settings = Settings(os.path.join(self.tmp, "d"))
        self.ctx = app.Context(library=self.lib, chain=dsp.Chain(), settings=self.settings)
        self.ctx.window = None
        self.cover = os.path.join(self.tmp, "cover.jpg")
        ff("-f", "lavfi", "-i", "color=c=green:s=64x64", "-frames:v", "1", self.cover)
        with open(self.cover, "rb") as fh:
            self.cover_bytes = fh.read()

    def tearDown(self):
        self.ctx.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ripped_album(self, n=3):
        """n tracks like real rips: FLAC, tagged, with cover, in the library with album and track."""
        out = []
        for i in range(1, n + 1):
            p = os.path.join(self.tmp, f"{i:02d}.flac")
            ff("-f", "lavfi", "-i", f"sine=frequency={300 + 50 * i}:duration=2", "-i", self.cover,
               "-map", "0:a", "-map", "1:v", "-c:a", "flac", "-c:v", "copy", "-disposition:v", "attached_pic",
               "-metadata", f"title=Song {i}", "-metadata", "artist=P!nk", "-metadata", "album=Can’t Take Me Home",
               "-metadata", f"track={i}/{n}", p)
            out.append(self.lib.add_ripped(p, f"Song {i}", "P!nk", "Can’t Take Me Home", i))
        return out


class TestCarScenario(Base):
    def stick(self):
        d = os.path.join(self.tmp, "stick")
        os.makedirs(d)
        return d, export.Volume("/dev/sdz1", "/dev/sdz", "CAR", "ntfs", 8 * 1024 ** 3, 7 * 1024 ** 3, d)

    def open_usb(self, entries, vol):
        with mock.patch.object(export, "removable_volumes", lambda *a, **k: [vol]):
            return ExportDialog(self.ctx, entries, "usb")

    def do_export(self, dlg):
        dlg._on_go(None)
        self.assertTrue(pump(lambda: not dlg._busy, 60), "export did not finish")

    def test_usb_defaults_are_car_friendly(self):
        d, vol = self.stick()
        dlg = self.open_usb(self.ripped_album(), vol)
        self.assertEqual(dlg.dd_fmt.get_selected(), 1, "MP3 should be the default for a USB drive")
        self.assertEqual(export.__dict__["EDITS_FOLDER"], "audio_edits")
        from bifrost.ui.export_dialog import LAYOUTS
        self.assertEqual(LAYOUTS[dlg.dd_layout.get_selected()][0], "album")

    def test_bilateral_versions_land_in_audio_edits_in_track_order_with_tags_and_art(self):
        d, vol = self.stick()
        dlg = self.open_usb(self.ripped_album(), vol)
        dlg.btn_proc.set_active(True)
        self.do_export(dlg)
        edits = os.path.join(d, "audio_edits")
        self.assertTrue(os.path.isdir(edits))
        self.assertFalse(os.path.exists(os.path.join(d, "Bifrost")), "originals were not requested")
        files = sorted(os.path.join(r, f) for r, _, fs in os.walk(edits) for f in fs if f.endswith(".mp3"))
        self.assertEqual(len(files), 3, dlg.status.get_label())
        album_dir = os.path.join(edits, "P!nk", "Can’t Take Me Home")
        self.assertEqual([os.path.basename(f) for f in files],
                         ["01 - Song 1.mp3", "02 - Song 2.mp3", "03 - Song 3.mp3"])
        self.assertTrue(all(os.path.dirname(f) == album_dir for f in files))
        for i, f in enumerate(files, 1):
            t = tags_of(f)
            self.assertEqual((t["title"], t["artist"], t["album"]), (f"Song {i}", "P!nk", "Can’t Take Me Home"))
            self.assertEqual(t["track"], str(i))
            self.assertTrue(has_cover(f), "cover art must be embedded for the head unit")
            with open(f, "rb") as fh:
                fh.seek(-128, os.SEEK_END)
                self.assertEqual(fh.read(3), b"TAG", "ID3v1 for older head units")
            self.assertTrue(engine.is_audio_file(f))

    def test_originals_and_bilateral_versions_live_in_separate_folders(self):
        d, vol = self.stick()
        entries = self.ripped_album()
        dlg = self.open_usb(entries, vol)
        self.do_export(dlg)                                                   # originals -> Bifrost/
        dlg.btn_proc.set_active(True)
        self.do_export(dlg)                                                   # bilateral -> audio_edits/
        top = sorted(os.listdir(d))
        self.assertEqual(top, ["Bifrost", "audio_edits"])
        flacs = [f for _, _, fs in os.walk(os.path.join(d, "Bifrost")) for f in fs if f.endswith(".flac")]
        mp3s = [f for _, _, fs in os.walk(os.path.join(d, "audio_edits")) for f in fs if f.endswith(".mp3")]
        self.assertEqual((len(flacs), len(mp3s)), (3, 3))

    def test_original_to_mp3_converts_without_effects_into_the_normal_folder(self):
        d, vol = self.stick()
        dlg = self.open_usb(self.ripped_album(2), vol)
        dlg.btn_conv.set_active(True)
        self.do_export(dlg)
        files = [os.path.join(r, f) for r, _, fs in os.walk(os.path.join(d, "Bifrost")) for f in fs if f.endswith(".mp3")]
        self.assertEqual(len(files), 2, dlg.status.get_label())
        self.assertTrue(all(has_cover(f) and tags_of(f)["artist"] == "P!nk" for f in files))
        self.assertFalse(os.path.exists(os.path.join(d, "audio_edits")))

    def test_the_folder_destination_also_uses_audio_edits_for_bilateral(self):
        dest = os.path.join(self.tmp, "somewhere")
        os.makedirs(dest)
        dlg = ExportDialog(self.ctx, self.ripped_album(2), "folder")
        dlg.folder = dest
        dlg.btn_proc.set_active(True)
        dlg.dd_fmt.set_selected(1)
        self.do_export(dlg)
        self.assertEqual(os.listdir(dest), ["audio_edits"])

    def test_creative_commons_attribution_travels_into_audio_edits(self):
        d, vol = self.stick()
        p = os.path.join(self.tmp, "cc.mp3")
        ff("-f", "lavfi", "-i", "sine=duration=2", p)
        e = self.lib.add_local(p)
        e.license, e.attribution, e.title, e.creator = "CC BY 4.0", "line for the CC track", "CC", "Someone"
        e.source = "openverse"
        dlg = self.open_usb([e], vol)
        dlg.btn_proc.set_active(True)
        self.do_export(dlg)
        with open(os.path.join(d, "audio_edits", export.ATTRIBUTION_FILE), encoding="utf-8") as fh:
            self.assertIn("line for the CC track", fh.read())

    def test_status_names_the_audio_edits_folder(self):
        d, vol = self.stick()
        dlg = self.open_usb(self.ripped_album(1), vol)
        dlg.btn_proc.set_active(True)
        self.assertIn("audio_edits", dlg.mode_note.get_label())
        self.do_export(dlg)
        self.assertIn(os.path.join(d, "audio_edits"), dlg.status.get_label())


class TestRipTabCover(Base):
    def setUp(self):
        super().setUp()
        self.rips = os.path.join(self.tmp, "rips")
        self.settings.set("rip_dir", self.rips)
        self.settings.set("rip_lookup", True)
        self.patches = [
            mock.patch.object(cd, "find_drives", lambda *a, **k: [cd.Drive("/dev/sr9", "ACME", "Fake")]),
            mock.patch.object(cd, "read_toc", lambda *a, **k: toc3()),
            mock.patch.object(musicbrainz, "lookup", lambda *a, **k: ALBUM),
        ]
        real = cd.rip_track

        def wrapped(device, track, out, fmt, tags, **kw):
            kw["source_cmd"] = ffmpeg_source()
            return real(device, track, out, fmt, tags, **kw)
        self.patches.append(mock.patch.object(cd, "rip_track", wrapped))
        for p in self.patches:
            p.start()
        self.fetched = []
        self.view = RipView(self.ctx)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        super().tearDown()

    def load(self, cover):
        def fake_fetch(mbid, *a, **k):
            self.fetched.append(mbid)
            if isinstance(cover, Exception):
                raise cover
            return cover
        self.fetch_patch = mock.patch.object(musicbrainz, "fetch_cover", fake_fetch)
        self.fetch_patch.start()
        self.addCleanup(self.fetch_patch.stop)
        self.view.refresh()
        self.assertTrue(pump(lambda: len(self.view.rows) == 3 and self.view.e_album.get_text() == "Test Album"))
        pump(lambda: self.view.cover is not None or isinstance(cover, Exception) or cover is None, 5)

    def rip_one(self, fmt_index=0):
        for r in self.view.rows[1:]:
            r.check.set_active(False)
        self.view.dd_fmt.set_selected(fmt_index)
        self.view._on_rip(None)
        self.assertTrue(pump(lambda: not self.view._ripping and len(self.view.ripped) == 1, 40))
        return self.view.ripped[0].path

    def test_the_cover_is_fetched_for_the_matched_release_and_embedded(self):
        self.load(self.cover_bytes)
        self.assertEqual(self.fetched, ["mbid"])
        self.assertIn("Cover art found", self.view.disc_note.get_label())
        path = self.rip_one()
        self.assertTrue(has_cover(path))
        self.assertEqual(tags_of(path)["album"], "Test Album")

    def test_unticking_embed_cover_art_leaves_the_files_plain(self):
        self.load(self.cover_bytes)
        self.view.chk_cover.set_active(False)
        self.assertFalse(has_cover(self.rip_one()))
        self.assertFalse(self.settings["rip_cover"], "the choice is remembered")

    def test_no_cover_lookup_at_all_when_the_setting_is_off(self):
        self.settings.set("rip_cover", False)
        self.load(None)
        self.assertEqual(self.fetched, [])

    def test_a_failed_cover_download_does_not_stop_the_rip(self):
        self.load(urllib.error.URLError("offline"))
        path = self.rip_one()
        self.assertTrue(engine.is_audio_file(path))
        self.assertFalse(has_cover(path))

    def test_a_release_without_art_still_rips(self):
        self.load(None)
        self.assertTrue(engine.is_audio_file(self.rip_one()))

    def test_opus_ignores_the_cover_without_failing(self):
        self.load(self.cover_bytes)
        keys = list(cd.FORMATS)
        path = self.rip_one(keys.index("opus"))
        self.assertTrue(path.endswith(".opus"))
        self.assertTrue(engine.is_audio_file(path))
        self.assertFalse(has_cover(path))

    def test_a_new_disc_discards_the_old_cover(self):
        self.load(self.cover_bytes)
        self.assertIsNotNone(self.view.cover)
        self.view._clear()
        self.assertIsNone(self.view.cover)


if __name__ == "__main__":
    unittest.main()
