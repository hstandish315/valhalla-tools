#!/usr/bin/env python3
"""
Downloader safety tests against a local HTTP server (plain http is enabled only
through the `schemes` test hook; production allows https alone). Also covers the
library index.
"""

from __future__ import annotations

import http.server
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bifrost import downloader as dl  # noqa: E402
from bifrost.library import Library  # noqa: E402
from bifrost.sources.common import Track  # noqa: E402

STATE = {"audio": b""}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def do_GET(self):
        p = self.path
        if p == "/good.mp3":
            self._send(200, "audio/mpeg", STATE["audio"])
        elif p == "/noext":
            self._send(200, "audio/mpeg", STATE["audio"])
        elif p == "/page.html":
            self._send(200, "text/html", b"<html>hi</html>")
        elif p == "/fake.mp3":
            self._send(200, "audio/mpeg", b"this is not audio at all" * 100)
        elif p == "/exe":
            self._send(200, "application/octet-stream", b"MZ" + b"\0" * 100)
        elif p == "/redirect-out":
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{self.server.server_port}/good.mp3")
            self.end_headers()
        elif p == "/redirect-in":
            self.send_response(302)
            self.send_header("Location", "/good.mp3")
            self.end_headers()
        elif p == "/stream-forever":          # no Content-Length: close-delimited
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.end_headers()
            try:
                for _ in range(10_000):
                    self.wfile.write(b"\0" * 8192)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self._send(404, "text/plain", b"nope")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TestHelpers(unittest.TestCase):
    def test_host_allowlist_is_a_suffix_match_on_label_boundaries(self):
        a = ("jamendo.com", "archive.org")
        self.assertTrue(dl.host_allowed("prod-1.storage.jamendo.com", a))
        self.assertTrue(dl.host_allowed("archive.org", a))
        self.assertTrue(dl.host_allowed("ARCHIVE.ORG.", a))
        self.assertFalse(dl.host_allowed("jamendo.com.evil.example", a))
        self.assertFalse(dl.host_allowed("notjamendo.com", a))
        self.assertFalse(dl.host_allowed("", a))

    def test_separators_become_hyphens_so_words_stay_apart(self):
        self.assertEqual(dl.sanitise_filename("AC/DC"), "AC-DC")
        self.assertEqual(dl.sanitise_filename("Electro/Vocal Mix"), "Electro-Vocal Mix")
        self.assertEqual(dl.sanitise_filename("a\\b"), "a-b")
        self.assertEqual(dl.sanitise_filename('What? Why: "Not"'), "What Why Not")

    def test_safe_punctuation_is_kept_so_real_names_survive(self):
        self.assertEqual(dl.sanitise_filename("P!nk"), "P!nk")
        self.assertEqual(dl.sanitise_filename("Can\u2019t Take Me Home"), "Can\u2019t Take Me Home")
        self.assertEqual(dl.sanitise_filename("I'd Come for You"), "I'd Come for You")
        self.assertEqual(dl.sanitise_filename("Rock & Roll, Pt. 2 (Live) [Remastered]"),
                         "Rock & Roll, Pt. 2 (Live) [Remastered]")
        self.assertEqual(dl.sanitise_filename("S.E.X."), "S.E.X")

    def test_characters_a_filesystem_rejects_are_still_removed(self):
        for bad in '<>:"|?*\x00\x1f':
            self.assertNotIn(bad, dl.sanitise_filename(f"a{bad}b"))

    def test_sanitised_names_cannot_traverse(self):
        for nasty in ("../../etc/passwd", "a/b\\c", "..", "", "x\x00y", "  . "):
            name = dl.sanitise_filename(nasty)
            self.assertNotIn("/", name)
            self.assertNotIn("\\", name)
            self.assertNotIn("\x00", name)
            self.assertNotIn("..", name)
            self.assertTrue(name)


class TestDownload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="bifrost-dl-")
        wav = os.path.join(cls.tmp, "src.mp3")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=1", wav], check=True)
        with open(wav, "rb") as fh:
            STATE["audio"] = fh.read()
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_port
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.dest = tempfile.mkdtemp(prefix="dest-", dir=self.tmp)

    def _go(self, path, **kw):
        t = Track(source="x", ident="1", title=kw.pop("title", "Song"), creator="Me",
                  download_url=f"http://127.0.0.1:{self.port}{path}")
        kw.setdefault("allowed_hosts", ("127.0.0.1",))
        kw.setdefault("schemes", ("http", "https"))
        return dl.download(t, self.dest, **kw)

    def _left_over(self):
        return [f for f in os.listdir(self.dest) if f.endswith(".part") or f.startswith(".dl-")]

    def test_good_audio_downloads_validates_and_is_named_from_metadata(self):
        path = self._go("/good.mp3")
        self.assertEqual(os.path.basename(path), "Me - Song.mp3")
        self.assertEqual(self._left_over(), [])

    def test_extension_comes_from_content_type_when_url_has_none(self):
        self.assertTrue(self._go("/noext").endswith(".mp3"))

    def test_name_collisions_do_not_overwrite(self):
        a, b = self._go("/good.mp3"), self._go("/good.mp3")
        self.assertNotEqual(a, b)
        self.assertTrue(os.path.exists(a) and os.path.exists(b))

    def test_default_scheme_policy_rejects_plain_http(self):
        with self.assertRaisesRegex(dl.DownloadError, "not allowed"):
            self._go("/good.mp3", schemes=("https",))

    def test_unlisted_host_is_rejected_before_any_request(self):
        with self.assertRaisesRegex(dl.DownloadError, "allowlist"):
            self._go("/good.mp3", allowed_hosts=("example.org",))

    def test_redirect_to_unlisted_host_is_blocked(self):
        with self.assertRaisesRegex(dl.DownloadError, "allowlist"):
            self._go("/redirect-out")
        self.assertEqual(self._left_over(), [])

    def test_redirect_within_allowed_host_is_followed(self):
        self.assertTrue(os.path.exists(self._go("/redirect-in")))

    def test_non_audio_types_are_rejected(self):
        for p in ("/page.html", "/exe"):
            with self.assertRaisesRegex(dl.DownloadError, "not an audio file"):
                self._go(p)
        self.assertEqual(os.listdir(self.dest), [])

    def test_audio_content_type_with_junk_bytes_fails_ffprobe(self):
        with self.assertRaisesRegex(dl.DownloadError, "not decodable audio"):
            self._go("/fake.mp3")
        self.assertEqual(os.listdir(self.dest), [])

    def test_declared_size_over_cap_is_rejected(self):
        with self.assertRaisesRegex(dl.DownloadError, "limit"):
            self._go("/good.mp3", max_bytes=100)

    def test_undeclared_size_is_capped_while_streaming(self):
        with self.assertRaisesRegex(dl.DownloadError, "exceeded"):
            self._go("/stream-forever", max_bytes=200_000)
        self.assertEqual(os.listdir(self.dest), [])

    def test_cancel_removes_the_partial_file(self):
        with self.assertRaisesRegex(dl.DownloadError, "cancelled"):
            self._go("/stream-forever", cancel=lambda: True)
        self.assertEqual(os.listdir(self.dest), [])

    def test_progress_reports_bytes(self):
        seen = []
        self._go("/good.mp3", progress=lambda d, t: seen.append((d, t)))
        self.assertTrue(seen)
        self.assertEqual(seen[-1][0], len(STATE["audio"]))

    def test_404_is_a_clean_error(self):
        with self.assertRaises(dl.DownloadError):
            self._go("/missing.mp3")


class TestLibrary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bifrost-lib-")
        self.music = os.path.join(self.tmp, "music")
        self.lib = Library(self.music, os.path.join(self.tmp, "data"))
        self.tone = os.path.join(self.tmp, "tone.mp3")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=330:duration=1", self.tone], check=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_local_import_persists_and_rejects_non_audio(self):
        e = self.lib.add_local(self.tone)
        self.assertIsNotNone(e)
        self.assertAlmostEqual(e.duration, 1.0, delta=0.1)
        junk = os.path.join(self.tmp, "x.mp3")
        with open(junk, "wb") as fh:
            fh.write(b"junk" * 100)
        self.assertIsNone(self.lib.add_local(junk))
        again = Library(self.music, os.path.join(self.tmp, "data"))
        self.assertEqual([x.path for x in again.entries], [self.tone])

    def test_download_entry_keeps_license_and_attribution(self):
        t = Track(source="openverse", ident="1", title="T", creator="C",
                  license="CC BY-ND 4.0", license_url="u", attribution="by C")
        e = self.lib.add_download(t, self.tone)
        self.assertEqual((e.license, e.attribution), ("CC BY-ND 4.0", "by C"))
        self.assertFalse(e.allows_modification)

    def test_vanished_files_are_dropped_on_load(self):
        self.lib.add_local(self.tone)
        os.unlink(self.tone)
        self.assertEqual(Library(self.music, os.path.join(self.tmp, "data")).entries, [])

    def test_remove_never_deletes_files_outside_the_music_dir(self):
        self.lib.add_local(self.tone)
        self.lib.remove(self.tone, delete_file=True)
        self.assertTrue(os.path.exists(self.tone))

    def test_copy_import_lands_in_music_dir(self):
        e = self.lib.add_local(self.tone, copy=True)
        self.assertTrue(e.path.startswith(self.music))
        self.lib.remove(e.path, delete_file=True)
        self.assertFalse(os.path.exists(e.path))

    def test_corrupt_index_does_not_crash(self):
        os.makedirs(self.lib.data_dir, exist_ok=True)
        with open(self.lib.index_path, "w") as fh:
            fh.write("{not json")
        self.assertEqual(Library(self.music, self.lib.data_dir).entries, [])


if __name__ == "__main__":
    unittest.main()
