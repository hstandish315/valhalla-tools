#!/usr/bin/env python3
"""
Export tests: USB discovery against `lsblk` JSON shaped like the real thing, and
the copy engine's behaviour at its edges (FAT names, space, collisions, cancel,
license rules, attribution). Uses temp folders; no USB stick is needed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bifrost import export  # noqa: E402
from bifrost.library import Entry  # noqa: E402

LSBLK = {"blockdevices": [
    {"name": "loop0", "path": "/dev/loop0", "type": "loop", "rm": False, "hotplug": False, "tran": None,
     "fstype": "squashfs", "label": None, "mountpoints": ["/snap/core/1"], "size": 4096, "fsavail": 0},
    {"name": "nvme0n1", "path": "/dev/nvme0n1", "type": "disk", "rm": False, "hotplug": False, "tran": "nvme",
     "fstype": None, "label": None, "mountpoints": [None], "size": 2000398934016, "fsavail": None,
     "children": [{"name": "nvme0n1p2", "path": "/dev/nvme0n1p2", "type": "part", "rm": False,
                   "hotplug": False, "tran": None, "fstype": "ext4", "label": None,
                   "mountpoints": ["/boot"], "size": 2040109465, "fsavail": 1900000000}]},
    {"name": "sr0", "path": "/dev/sr0", "type": "rom", "rm": True, "hotplug": False, "tran": "usb",
     "fstype": None, "label": None, "mountpoints": [None], "size": 494346240, "fsavail": None},
    {"name": "sdb", "path": "/dev/sdb", "type": "disk", "rm": True, "hotplug": True, "tran": "usb",
     "fstype": None, "label": None, "mountpoints": [None], "size": 16008609792, "fsavail": None,
     "children": [{"name": "sdb1", "path": "/dev/sdb1", "type": "part", "rm": True, "hotplug": True,
                   "tran": None, "fstype": "vfat", "label": "MUSIC",
                   "mountpoints": ["/media/user/MUSIC"], "size": 16007561216, "fsavail": 15000000000}]},
    {"name": "sdc", "path": "/dev/sdc", "type": "disk", "rm": True, "hotplug": True, "tran": "usb",
     "fstype": "exfat", "label": "BARE", "mountpoints": [None], "size": 32000000000, "fsavail": None},
    {"name": "sdd", "path": "/dev/sdd", "type": "disk", "rm": True, "hotplug": True, "tran": "usb",
     "fstype": None, "label": None, "mountpoints": [None], "size": 8000000000, "fsavail": None},
    {"name": "sde", "path": "/dev/sde", "type": "disk", "rm": False, "hotplug": False, "tran": "sata",
     "fstype": "ext4", "label": "internal", "mountpoints": ["/data"], "size": 500000000000,
     "fsavail": 400000000000},
]}


class FakeRun:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.calls, self.r = [], subprocess.CompletedProcess([], returncode, stdout, stderr)

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        return self.r


class TestVolumes(unittest.TestCase):
    def vols(self):
        return export.removable_volumes(FakeRun(json.dumps(LSBLK)))

    def test_only_removable_usb_volumes_with_a_filesystem(self):
        names = [v.device for v in self.vols()]
        self.assertEqual(names, ["/dev/sdb1", "/dev/sdc"])
        # internal disks, loop devices, the optical drive and a blank stick are all excluded

    def test_details(self):
        v = {x.device: x for x in self.vols()}
        self.assertEqual(v["/dev/sdb1"].label, "MUSIC")
        self.assertEqual(v["/dev/sdb1"].mountpoint, "/media/user/MUSIC")
        self.assertEqual(v["/dev/sdb1"].disk, "/dev/sdb")
        self.assertTrue(v["/dev/sdb1"].is_fat)
        self.assertFalse(v["/dev/sdc"].is_fat)
        self.assertEqual(v["/dev/sdc"].mountpoint, "")
        self.assertIn("MUSIC", v["/dev/sdb1"].title)

    def test_lsblk_failure_is_a_clean_error(self):
        def boom(*a, **k):
            raise FileNotFoundError("lsblk")
        with self.assertRaisesRegex(export.ExportError, "Could not list drives"):
            export.removable_volumes(boom)

    def test_mount_parses_the_udisks_reply(self):
        v = export.Volume("/dev/sdc", "/dev/sdc")
        run = FakeRun("Mounted /dev/sdc at /media/user/BARE.\n")
        self.assertEqual(export.mount_volume(v, run), "/media/user/BARE")
        self.assertEqual(run.calls[0][:3], ["udisksctl", "mount", "-b"])
        self.assertNotIn("sudo", run.calls[0])

    def test_mounted_volume_is_not_remounted(self):
        run = FakeRun()
        v = export.Volume("/dev/sdb1", "/dev/sdb", mountpoint="/media/user/MUSIC")
        self.assertEqual(export.mount_volume(v, run), "/media/user/MUSIC")
        self.assertEqual(run.calls, [])

    def test_mount_failure_reports_the_reason(self):
        with self.assertRaisesRegex(export.ExportError, "busy"):
            export.mount_volume(export.Volume("/dev/sdc", "/dev/sdc"),
                                FakeRun("", 1, "Error: device is busy"))

    def test_safe_removal_unmounts_then_powers_off_the_disk(self):
        run = FakeRun()
        v = export.Volume("/dev/sdb1", "/dev/sdb", mountpoint="/media/user/MUSIC")
        export.safely_remove(v, run)
        self.assertEqual([c[1] for c in run.calls], ["unmount", "power-off"])
        self.assertEqual(run.calls[1][3], "/dev/sdb")              # the disk, not the partition
        self.assertEqual(v.mountpoint, "")

    def test_safe_removal_failure_is_reported(self):
        with self.assertRaisesRegex(export.ExportError, "still be in use"):
            export.safely_remove(export.Volume("/dev/sdb1", "/dev/sdb"), FakeRun("", 1, ""))


class TestNames(unittest.TestCase):
    def test_fat_safe_replaces_illegal_characters(self):
        self.assertEqual(export.fat_safe('a<b>c:d"e|f?g*h'), "a_b_c_d_e_f_g_h")
        self.assertEqual(export.fat_safe("song. "), "song")
        self.assertEqual(export.fat_safe(""), "_")

    def test_reserved_windows_names_are_defused(self):
        for n in ("CON", "nul", "COM1", "lpt9.txt"):
            self.assertNotEqual(export.fat_safe(n).upper().split(".")[0], n.upper().split(".")[0])

    def test_destination_layouts(self):
        e = Entry(path="/x/a.flac", title="Riverdance", creator="Bill Whelan", album="Roots")
        self.assertEqual(export.destination(e, "/u", "flat", False), "/u/Bill Whelan - Riverdance.flac")
        self.assertEqual(export.destination(e, "/u", "artist", False), "/u/Bill Whelan/Riverdance.flac")
        self.assertEqual(export.destination(e, "/u", "album", False), "/u/Bill Whelan/Roots/Riverdance.flac")

    def test_album_layout_keeps_track_order_with_a_number_prefix(self):
        e = Entry(path="/x/a.flac", title="Riverdance", creator="Bill Whelan", album="Roots", track=9)
        self.assertEqual(export.destination(e, "/u", "album", False),
                         "/u/Bill Whelan/Roots/09 - Riverdance.flac")
        names = sorted(os.path.basename(export.destination(
            Entry(path="/x/a.mp3", title=t, creator="B", album="A", track=n), "/u", "album", True))
            for n, t in ((2, "Alpha"), (10, "Zulu"), (1, "Mike")))
        self.assertEqual(names, ["01 - Mike.mp3", "02 - Alpha.mp3", "10 - Zulu.mp3"])

    def test_hostile_metadata_cannot_escape_the_destination(self):
        e = Entry(path="/x/a.mp3", title="../../../etc/cron.d/x", creator="a/../b", album="..")
        for layout in ("flat", "artist", "album"):
            p = export.destination(e, "/u", layout, True)
            self.assertTrue(os.path.normpath(p).startswith("/u/"), p)


def put(path, data=b"x"):
    with open(path, "wb") as fh:
        fh.write(data)


def fake_render(src, dst):
    """A transform: writes a processed copy to dst (never touches src)."""
    put(dst, b"x")


class CopyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bifrost-exp-")
        self.src = os.path.join(self.tmp, "src")
        self.dst = os.path.join(self.tmp, "usb")
        os.makedirs(self.src)
        os.makedirs(self.dst)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def entry(self, name, size=2048, **kw):
        path = os.path.join(self.src, name)
        with open(path, "wb") as fh:
            fh.write(os.urandom(size))
        kw.setdefault("title", os.path.splitext(name)[0])
        return Entry(path=path, **kw)


class TestCopy(CopyBase):
    def test_copies_bytes_exactly(self):
        e = self.entry("one.mp3", 5000, creator="Band")
        r = export.copy_tracks([e], self.dst)
        self.assertEqual(len(r.copied), 1)
        with open(e.path, "rb") as a, open(r.copied[0], "rb") as b:
            self.assertEqual(a.read(), b.read())
        self.assertEqual(os.path.basename(r.copied[0]), "Band - one.mp3")

    def test_no_part_files_remain(self):
        export.copy_tracks([self.entry("a.mp3"), self.entry("b.mp3")], self.dst)
        left = [f for _, _, fs in os.walk(self.dst) for f in fs if ".part" in f]
        self.assertEqual(left, [])

    def test_identical_existing_file_is_skipped_different_gets_a_new_name(self):
        e = self.entry("a.mp3", 3000)
        export.copy_tracks([e], self.dst)
        r = export.copy_tracks([e], self.dst)
        self.assertEqual((r.copied, [s[1] for s in r.skipped]), ([], ["already there"]))
        with open(e.path, "wb") as fh:                           # same name, different content
            fh.write(os.urandom(3001))
        r = export.copy_tracks([e], self.dst)
        self.assertTrue(r.copied[0].endswith("a (2).mp3"))
        self.assertEqual(len([f for f in os.listdir(self.dst) if f.endswith(".mp3")]), 2)

    def test_fat_mode_cleans_names(self):
        e = self.entry("x.mp3", title='What? Why: "Not"', creator="AC/DC")
        r = export.copy_tracks([e], self.dst, fat=True)
        name = os.path.basename(r.copied[0])
        self.assertFalse(set('<>:"/\\|?*') & set(name))

    def test_insufficient_space_is_refused_before_writing(self):
        e = self.entry("big.mp3", 10_000)
        with self.assertRaisesRegex(export.ExportError, "Not enough space"):
            export.copy_tracks([e], self.dst, free_bytes=1000)
        self.assertEqual(os.listdir(self.dst), [])

    def test_fat_4gib_limit_skips_the_file(self):
        e = self.entry("huge.flac")
        with open(e.path, "wb") as fh:
            fh.truncate(export.FAT_MAX_FILE + 10)               # sparse; never actually copied
        r = export.copy_tracks([e], self.dst, fat=True, free_bytes=10 ** 12)
        self.assertEqual(r.copied, [])
        self.assertIn("4 GiB", r.skipped[0][1])

    def test_missing_source_is_skipped_not_fatal(self):
        good, gone = self.entry("ok.mp3"), self.entry("gone.mp3")
        os.unlink(gone.path)
        r = export.copy_tracks([gone, good], self.dst)
        self.assertEqual(len(r.copied), 1)
        self.assertIn("missing", r.skipped[0][1])

    def test_destination_must_exist_and_be_writable(self):
        with self.assertRaisesRegex(export.ExportError, "doesn't exist"):
            export.copy_tracks([self.entry("a.mp3")], os.path.join(self.tmp, "nope"))

    def test_cancel_stops_mid_copy_and_cleans_up(self):
        e = self.entry("a.mp3", 8 * 1024 * 1024)
        flag = threading.Event()

        def progress(done, total, name):
            flag.set()                                           # cancel after the first chunk

        with self.assertRaisesRegex(export.ExportError, "cancelled"):
            export.copy_tracks([e], self.dst, progress=progress, cancel=flag.is_set)
        self.assertEqual([f for _, _, fs in os.walk(self.dst) for f in fs], [])

    def test_progress_reports_totals(self):
        seen = []
        e1, e2 = self.entry("a.mp3", 3000), self.entry("b.mp3", 5000)
        export.copy_tracks([e1, e2], self.dst, progress=lambda d, t, n: seen.append((d, t)))
        self.assertEqual(seen[-1], (8000, 8000))
        self.assertEqual([d for d, _ in seen], sorted(d for d, _ in seen))

    def test_layouts_create_folders(self):
        e = self.entry("a.flac", creator="Bill", album="Roots", title="Song")
        r = export.copy_tracks([e], self.dst, layout="album")
        self.assertTrue(r.copied[0].endswith(os.path.join("Bill", "Roots", "Song.flac")))


class TestTransformAndLicense(CopyBase):
    def test_transform_renders_the_processed_copy(self):
        e = self.entry("a.mp3", creator="Band", title="Song")

        def render(src, dst):
            with open(dst, "wb") as fh:
                fh.write(b"PROCESSED")
        r = export.copy_tracks([e], self.dst, transform=render, transform_ext="flac")
        self.assertTrue(r.copied[0].endswith("Band - Song.flac"))
        with open(r.copied[0], "rb") as fh:
            self.assertEqual(fh.read(), b"PROCESSED")

    def test_no_derivatives_tracks_are_refused_in_processed_mode_only(self):
        nd = self.entry("nd.mp3", license="CC BY-ND 4.0", title="ND")
        ok = self.entry("ok.mp3", license="CC BY 4.0", title="OK")
        render = fake_render
        r = export.copy_tracks([nd, ok], self.dst, transform=render, transform_ext="mp3")
        self.assertEqual(len(r.copied), 1)
        self.assertIn("no-derivatives", r.skipped[0][1])
        plain = export.copy_tracks([nd], os.path.join(self.tmp, "usb"))     # a plain copy is fine
        self.assertEqual(len(plain.copied), 1)

    def test_a_failing_transform_is_reported_per_track(self):
        a, b = self.entry("a.mp3", title="A"), self.entry("b.mp3", title="B")

        def flaky(src, dst):
            if "a.mp3" in src:
                raise RuntimeError("ffmpeg exploded")
            put(dst, b"ok")
        r = export.copy_tracks([a, b], self.dst, transform=flaky, transform_ext="mp3")
        self.assertEqual(len(r.copied), 1)
        self.assertEqual(r.failed[0][0], "A")
        self.assertEqual([f for _, _, fs in os.walk(self.dst) for f in fs if ".part" in f], [])


class TestAttribution(CopyBase):
    def test_cc_tracks_get_an_attribution_file_and_own_files_do_not(self):
        cc = self.entry("a.mp3", license="CC BY 4.0", attribution='"A" by X, CC BY 4.0', title="A")
        own = self.entry("b.mp3", license="Ripped from own CD", title="B")
        export.copy_tracks([cc, own], self.dst)
        with open(os.path.join(self.dst, export.ATTRIBUTION_FILE), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn('"A" by X, CC BY 4.0', text)
        self.assertNotIn("Ripped", text)

    def test_attribution_is_idempotent(self):
        cc = self.entry("a.mp3", license="CC BY 4.0", attribution="line one", title="A")
        export.copy_tracks([cc], self.dst)
        export.copy_tracks([cc], self.dst)
        with open(os.path.join(self.dst, export.ATTRIBUTION_FILE), encoding="utf-8") as fh:
            self.assertEqual(fh.read().count("line one"), 1)

    def test_no_attribution_file_when_nothing_needs_it(self):
        export.copy_tracks([self.entry("a.mp3", license="Local file")], self.dst)
        self.assertFalse(os.path.exists(os.path.join(self.dst, export.ATTRIBUTION_FILE)))

    def test_skipped_tracks_are_not_credited(self):
        cc = self.entry("a.mp3", license="CC BY-ND 4.0", attribution="ND line", title="A")
        export.copy_tracks([cc], self.dst, transform=fake_render, transform_ext="mp3")
        self.assertFalse(os.path.exists(os.path.join(self.dst, export.ATTRIBUTION_FILE)))


if __name__ == "__main__":
    unittest.main()


class TestProcessingTransform(CopyBase):
    def test_renders_a_real_processed_file_without_touching_the_live_chain(self):
        from bifrost import dsp, engine
        src = os.path.join(self.src, "tone.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=330:sample_rate=44100:duration=2", "-ac", "2", src], check=True)
        e = Entry(path=src, title="Tone", creator="Gen", license="CC BY 4.0")
        chain = dsp.Chain(fade_in_s=3.0)
        chain.pan.phase = 1.234                          # the live chain's state must survive
        r = export.copy_tracks([e], self.dst, transform=export.processing_transform(chain),
                               transform_ext="flac")
        self.assertEqual(len(r.copied), 1, r.failed)
        out = r.copied[0]
        self.assertTrue(engine.is_audio_file(out))
        self.assertAlmostEqual(engine.probe_duration(out), 2.0, delta=0.2)
        self.assertEqual(chain.pan.phase, 1.234)
        a = subprocess.run(["ffmpeg", "-v", "error", "-i", src, "-f", "md5", "-"],
                           capture_output=True, text=True).stdout
        b = subprocess.run(["ffmpeg", "-v", "error", "-i", out, "-f", "md5", "-"],
                           capture_output=True, text=True).stdout
        self.assertNotEqual(a, b, "the export should differ from the original")

    def test_mp3_output_is_a_valid_mp3(self):
        from bifrost import dsp, engine
        src = os.path.join(self.src, "t.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "sine=duration=1.5", "-ac", "2", src], check=True)
        e = Entry(path=src, title="T", creator="G")
        r = export.copy_tracks([e], self.dst, transform=export.processing_transform(dsp.Chain()),
                               transform_ext="mp3")
        self.assertTrue(r.copied[0].endswith(".mp3"))
        self.assertTrue(engine.is_audio_file(r.copied[0]))
