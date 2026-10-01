#!/usr/bin/env python3
"""
CD tests without a drive: the kernel is faked at the ioctl boundary and the
rip pipeline uses ffmpeg to stand in for the disc reader. The disc-ID algorithm
is checked against real MusicBrainz TOC -> ID pairs. A separate hardware test
(test_cd_hardware.py) runs only when a drive with a disc is attached.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bifrost import cd, engine, musicbrainz  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


class FakeKernel:
    """Answers the three CD-ROM ioctls from a synthetic disc."""

    def __init__(self, tracks, leadout, status=4, data=()):
        self.tracks, self.leadout, self.status, self.data = tracks, leadout, status, set(data)

    def ioctl(self, fd, req, arg):
        if req == cd.CDROM_DRIVE_STATUS:
            return self.status
        if req == cd.CDROMREADTOCHDR:
            return struct.pack("BB", 1, len(self.tracks))
        if req == cd.CDROMREADTOCENTRY:
            track, *_ = struct.unpack(cd._TOC_ENTRY, arg)
            if track == cd.LEADOUT_TRACK:
                lba, ctrl = self.leadout, 0
            else:
                lba = self.tracks[track - 1]
                ctrl = 4 if track in self.data else 0
            return struct.pack(cd._TOC_ENTRY, track, ctrl << 4, cd.CDROM_LBA, lba, 0)
        raise OSError(25, "bad ioctl")

    def read(self, device="/dev/sr9"):
        return cd.read_toc(device, ioctl=self.ioctl, opener=lambda *a: 3, closer=lambda fd: None)


class TestDiscId(unittest.TestCase):
    # (sectors incl. 150 lead-in, id) taken from real MusicBrainz responses
    VECTORS = [(10001, "QwoXrxAzl.585vCe5RxOfe1DYKU-"),
               (9468, "O6TbNLfgjkCC_UO05daVadcLikg-"),
               (10024, "XSfsRWlj2uHrkcetNzF5HBMewIs-")]

    def test_matches_real_musicbrainz_ids(self):
        for sectors, want in self.VECTORS:
            toc = cd.Toc([cd.TocTrack(1, 0, sectors - 150)], sectors - 150)
            self.assertEqual(cd.disc_id(toc), want, sectors)

    def test_id_shape(self):
        toc = FakeKernel([0, 20000, 41000], 70000).read()
        did = cd.disc_id(toc)
        self.assertEqual(len(did), 28)
        self.assertTrue(did.endswith("-"))
        self.assertNotRegex(did, r"[+/=]")

    def test_different_discs_get_different_ids(self):
        a = cd.disc_id(FakeKernel([0, 20000], 50000).read())
        b = cd.disc_id(FakeKernel([0, 20001], 50000).read())
        self.assertNotEqual(a, b)


class TestToc(unittest.TestCase):
    def test_lengths_come_from_the_next_track_and_the_leadout(self):
        toc = FakeKernel([0, 25982, 45160], 70850).read()
        self.assertEqual([t.sectors for t in toc.tracks], [25982, 19178, 25690])
        self.assertAlmostEqual(toc.tracks[0].seconds, 25982 / 75)
        self.assertAlmostEqual(toc.minutes, 70850 / 75 / 60)

    def test_data_track_is_flagged_and_excluded_from_audio(self):
        toc = FakeKernel([0, 20000, 40000], 60000, data={3}).read()
        self.assertTrue(toc.tracks[2].is_data)
        self.assertEqual([t.number for t in toc.audio_tracks], [1, 2])

    def test_no_disc_and_tray_messages(self):
        for status, text in ((1, "No disc"), (2, "tray is open"), (3, "isn't ready")):
            with self.assertRaisesRegex(cd.CDError, text):
                FakeKernel([0], 1000, status=status).read()

    def test_all_data_disc_is_rejected(self):
        with self.assertRaisesRegex(cd.CDError, "no audio tracks"):
            FakeKernel([0, 100], 5000, data={1, 2}).read()

    def test_permission_error_explains_the_cdrom_group(self):
        def deny(*a):
            raise PermissionError(13, "denied")
        with self.assertRaisesRegex(cd.CDError, "cdrom"):
            cd.read_toc("/dev/sr0", opener=deny)

    def test_missing_device_is_a_clean_error(self):
        with self.assertRaisesRegex(cd.CDError, "Cannot open"):
            cd.read_toc("/dev/does-not-exist")

    def test_find_drives_reads_sysfs(self):
        root = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(root, "sr0", "device"))
            os.makedirs(os.path.join(root, "sda", "device"))
            for name, value in (("vendor", "ACME  \n"), ("model", "SuperDrive 9\n")):
                with open(os.path.join(root, "sr0", "device", name), "w") as fh:
                    fh.write(value)
            drives = cd.find_drives(root)
            self.assertEqual([d.device for d in drives], ["/dev/sr0"])
            self.assertEqual(drives[0].label, "ACME SuperDrive 9 (/dev/sr0)")
        finally:
            shutil.rmtree(root)


class TestTagsAndPaths(unittest.TestCase):
    def test_ffmpeg_tags(self):
        t = cd.Tags(title="Song", artist="Band", album="LP", track=3, total=12, year="1997")
        args = t.ffmpeg_args()
        self.assertIn("title=Song", args)
        self.assertIn("track=3/12", args)
        self.assertNotIn("album_artist=", " ".join(args))        # blank tags are omitted

    def test_control_characters_are_stripped_from_tags(self):
        args = cd.Tags(title="A\x00B\nC").ffmpeg_args()
        self.assertEqual(args[1], "title=ABC")

    def test_track_path_is_sanitised_and_contained(self):
        t = cd.Tags(title="../../etc/passwd", artist="a/b", album="..", track=5)
        p = cd.track_path("/music", t, "flac")
        self.assertTrue(p.startswith("/music/"))
        self.assertNotIn("..", os.path.relpath(p, "/music").split(os.sep))
        self.assertTrue(p.endswith(".flac"))

    def test_track_path_layout(self):
        p = cd.track_path("/m", cd.Tags(title="Riverdance", artist="Bill Whelan",
                                        album="Roots", track=12), "flac")
        self.assertEqual(p, "/m/Bill Whelan/Roots/12 - Riverdance.flac")


def ffmpeg_source(seconds=3.0, sleep=0.0):
    """Stand-in for the disc reader: writes a known-length WAV, optionally slowly."""
    def cmd(device, track, wav):
        gen = (f"sleep {sleep}; " if sleep else "") + \
              f"ffmpeg -v error -y -f lavfi -i sine=frequency=440:sample_rate=44100:duration={seconds} " \
              f"-ac 2 -c:a pcm_s16le '{wav}'"
        return ["sh", "-c", gen]
    return cmd


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg required")
class TestRip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bifrost-rip-")
        self.track = cd.TocTrack(2, 1000, 225)                # 3.0 s
        self.tags = cd.Tags(title="Bridge", artist="Band", album="LP", track=2, total=5, year="1997")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def out(self, name="x"):
        return os.path.join(self.tmp, "Band", "LP", name)

    def leftovers(self):
        found = []
        for root, _, files in os.walk(self.tmp):
            found += [f for f in files if f.startswith(".rip-")]
        return found

    def test_every_format_produces_a_valid_tagged_file_of_the_right_length(self):
        for fmt, (ext, _) in cd.FORMATS.items():
            with self.subTest(fmt):
                path = cd.rip_track("/dev/null", self.track, self.out(f"t-{fmt}"), fmt, self.tags,
                                    source_cmd=ffmpeg_source())
                self.assertTrue(path.endswith("." + ext))
                self.assertTrue(engine.is_audio_file(path))
                self.assertAlmostEqual(engine.probe_duration(path), 3.0, delta=0.3)
                tags = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                       "format_tags:stream_tags", "-of", "json", path],
                                      capture_output=True, text=True).stdout.lower()
                self.assertIn("bridge", tags)
                self.assertIn("band", tags)
        self.assertEqual(self.leftovers(), [])

    def test_flac_is_bit_identical_to_the_read(self):
        wav_holder = {}

        def src(device, track, wav):
            wav_holder["wav"] = wav
            return ffmpeg_source()(device, track, wav)

        path = cd.rip_track("/dev/null", self.track, self.out("lossless"), "flac", self.tags,
                            source_cmd=src)
        a = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "md5", "-"],
                           capture_output=True, text=True).stdout
        ref = os.path.join(self.tmp, "ref.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=440:sample_rate=44100:duration=3.0", "-ac", "2",
                        "-c:a", "pcm_s16le", ref], check=True)
        b = subprocess.run(["ffmpeg", "-v", "error", "-i", ref, "-f", "md5", "-"],
                           capture_output=True, text=True).stdout
        self.assertEqual(a, b)

    def test_short_read_is_rejected_and_leaves_nothing(self):
        long_track = cd.TocTrack(2, 1000, 750)                # disc says 10 s, reader gives 3 s
        with self.assertRaisesRegex(cd.CDError, "incomplete"):
            cd.rip_track("/dev/null", long_track, self.out(), "flac", self.tags,
                         source_cmd=ffmpeg_source())
        self.assertEqual(self.leftovers(), [])
        self.assertFalse(os.path.exists(self.out() + ".flac"))

    def test_failing_reader_reports_its_error(self):
        def bad(device, track, wav):
            return ["sh", "-c", "echo 'drive error: sector 123' >&2; exit 3"]
        with self.assertRaisesRegex(cd.CDError, "sector 123"):
            cd.rip_track("/dev/null", self.track, self.out(), "flac", self.tags, source_cmd=bad)
        self.assertEqual(self.leftovers(), [])

    def test_cancel_stops_promptly_and_cleans_up(self):
        flag = threading.Event()
        threading.Timer(0.4, flag.set).start()
        t0 = time.time()
        with self.assertRaisesRegex(cd.CDError, "cancelled"):
            cd.rip_track("/dev/null", self.track, self.out(), "flac", self.tags,
                         cancel=flag.is_set, source_cmd=ffmpeg_source(sleep=10))
        self.assertLess(time.time() - t0, 3.0)
        self.assertEqual(self.leftovers(), [])

    def test_progress_is_monotonic_and_reaches_one(self):
        seen = []
        cd.rip_track("/dev/null", self.track, self.out("p"), "flac", self.tags,
                     progress=seen.append, source_cmd=ffmpeg_source())
        self.assertEqual(seen[-1], 1.0)
        self.assertEqual(seen, sorted(seen))

    def test_unknown_format_is_refused(self):
        with self.assertRaisesRegex(cd.CDError, "Unknown format"):
            cd.rip_track("/dev/null", self.track, self.out(), "wma", self.tags,
                         source_cmd=ffmpeg_source())


class TestMusicBrainz(unittest.TestCase):
    ID = "QwoXrxAzl.585vCe5RxOfe1DYKU-"

    def payload(self):
        with open(os.path.join(FIX, "musicbrainz_discid.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_parses_the_real_exact_match(self):
        album = musicbrainz.parse(self.payload(), self.ID)
        self.assertIsNotNone(album)
        self.assertEqual(album.title, "Dear...")
        self.assertTrue(album.artist)
        self.assertEqual(album.year, "2014")
        self.assertEqual(album.track(1).title, "死闘 -from GRIEVA-")
        self.assertEqual(album.track(1).length_ms, 131346)

    def test_a_release_that_does_not_carry_this_disc_is_not_a_match(self):
        self.assertIsNone(musicbrainz.parse(self.payload(), "NotThisDiscAtAll_______________-"))

    def test_lookup_returns_none_on_404(self):
        class Http404:
            def get_json(self, url, params=None):
                raise urllib.error.HTTPError(url, 404, "nf", {}, io.BytesIO(b"{}"))
        self.assertIsNone(musicbrainz.lookup(self.ID, Http404()))

    def test_other_errors_propagate(self):
        class Http503:
            def get_json(self, url, params=None):
                raise urllib.error.HTTPError(url, 503, "busy", {}, io.BytesIO(b"{}"))
        with self.assertRaises(urllib.error.HTTPError) as cm:
            musicbrainz.lookup(self.ID, Http503())
        cm.exception.close()

    def test_uses_the_exact_endpoint_not_the_fuzzy_toc_one(self):
        calls = []

        class Spy:
            def get_json(self, url, params=None):
                calls.append((url, params))
                return {"releases": []}
        musicbrainz.lookup(self.ID, Spy())
        self.assertIn(self.ID, calls[0][0])
        self.assertNotIn("toc", calls[0][0])
        self.assertNotIn("toc", json.dumps(calls[0][1]))


if __name__ == "__main__":
    unittest.main()
