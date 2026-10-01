#!/usr/bin/env python3
"""
Cover art, tag-preserving exports, MP3 conversion, and the audio_edits folder.

The motivating case is a car stereo: a bilateral export that loses its tags shows bare
filenames in alphabetical order. The first test below pins that this can't happen.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from bifrost import cd, dsp, engine, export, musicbrainz  # noqa: E402
from bifrost.library import Entry  # noqa: E402


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def probe(path, entries="format_tags"):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", f"{entries}", "-of", "json", path],
                         capture_output=True, text=True).stdout
    import json
    return json.loads(out)


def tags_of(path):
    t = probe(path).get("format", {}).get("tags", {})
    return {k.lower(): v for k, v in t.items()}


def has_cover(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
                          "stream=codec_name", "-of", "csv=p=0", path], capture_output=True, text=True).stdout
    return bool(out.strip())


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bifrost-polish-")
        self.cover = os.path.join(self.tmp, "cover.jpg")
        ff("-f", "lavfi", "-i", "color=c=red:s=64x64", "-frames:v", "1", self.cover)
        with open(self.cover, "rb") as fh:
            self.cover_bytes = fh.read()
        # a tagged FLAC with embedded art, like a ripped track
        self.src = os.path.join(self.tmp, "src.flac")
        ff("-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-i", self.cover, "-map", "0:a", "-map", "1:v",
           "-c:a", "flac", "-c:v", "copy", "-disposition:v", "attached_pic",
           "-metadata", "title=Real Title", "-metadata", "artist=Real Artist", "-metadata", "album=Real Album",
           "-metadata", "track=4/12", "-metadata", "date=1999", self.src)
        self.out = os.path.join(self.tmp, "out")
        os.makedirs(self.out)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestBilateralKeepsTagsAndArt(Fixture):
    def test_the_rendered_mp3_keeps_title_artist_album_track_and_cover(self):
        dst = os.path.join(self.out, "bilateral.mp3")
        engine.render_offline(dsp.Chain(fade_in_s=0), self.src, dst)
        t = tags_of(dst)
        self.assertEqual((t.get("title"), t.get("artist"), t.get("album")),
                         ("Real Title", "Real Artist", "Real Album"))
        self.assertEqual(t.get("track"), "4/12")
        self.assertTrue(has_cover(dst), "the cover art must travel with the audio")

    def test_the_rendered_flac_keeps_them_too(self):
        dst = os.path.join(self.out, "bilateral.flac")
        engine.render_offline(dsp.Chain(fade_in_s=0), self.src, dst)
        self.assertEqual(tags_of(dst).get("artist"), "Real Artist")
        self.assertTrue(has_cover(dst))

    def test_mp3_carries_an_id3v1_tag_as_well_for_old_head_units(self):
        dst = os.path.join(self.out, "b.mp3")
        engine.render_offline(dsp.Chain(fade_in_s=0), self.src, dst)
        with open(dst, "rb") as fh:
            fh.seek(-128, os.SEEK_END)
            self.assertEqual(fh.read(3), b"TAG")

    def test_ogg_and_opus_render_without_art_but_do_not_fail(self):
        for ext in ("ogg", "opus"):
            dst = os.path.join(self.out, f"b.{ext}")
            engine.render_offline(dsp.Chain(fade_in_s=0), self.src, dst)
            self.assertTrue(engine.is_audio_file(dst), ext)

    def test_explicit_tags_override_the_source(self):
        dst = os.path.join(self.out, "b.mp3")
        engine.render_offline(dsp.Chain(fade_in_s=0), self.src, dst, tags={"title": "Override", "track": "9"})
        t = tags_of(dst)
        self.assertEqual((t["title"], t["track"], t["artist"]), ("Override", "9", "Real Artist"))

    def test_an_encoder_failure_is_raised_not_swallowed(self):
        with self.assertRaisesRegex(RuntimeError, "encoding failed"):
            engine.render_offline(dsp.Chain(fade_in_s=0), self.src, os.path.join(self.out, "nodir", "x.mp3"))

    def test_audio_is_actually_processed(self):
        dst = os.path.join(self.out, "b.flac")
        engine.render_offline(dsp.Chain(fade_in_s=0), self.src, dst)
        md5 = lambda p: subprocess.run(["ffmpeg", "-v", "error", "-i", p, "-map", "0:a", "-f", "md5", "-"],
                                       capture_output=True, text=True).stdout
        self.assertNotEqual(md5(self.src), md5(dst))


class TestTagsFor(Fixture):
    def test_ripped_entries_use_the_library_metadata_including_track_number(self):
        e = Entry(path=self.src, title="Library Title", creator="Lib Artist", album="Lib Album",
                  track=7, source="cd", license="Ripped from own CD")
        t = export.tags_for(e, self.src)
        self.assertEqual((t["title"], t["artist"], t["album"], t["track"], t["album_artist"]),
                         ("Library Title", "Lib Artist", "Lib Album", "7", "Lib Artist"))

    def test_a_local_file_with_its_own_title_keeps_its_own_tags(self):
        e = Entry(path=self.src, title="file-name-stem", source="local")
        self.assertEqual(export.tags_for(e, self.src), {})

    def test_a_local_file_with_no_title_tag_gets_one_from_the_library(self):
        bare = os.path.join(self.tmp, "bare.mp3")
        ff("-f", "lavfi", "-i", "sine=duration=1", bare)
        e = Entry(path=bare, title="my song", source="local")
        self.assertEqual(export.tags_for(e, bare), {"title": "my song"})

    def test_no_entry_means_no_overrides(self):
        self.assertEqual(export.tags_for(None, self.src), {})


class TestTransforms(Fixture):
    def test_processing_transform_applies_library_tags_and_keeps_art(self):
        e = Entry(path=self.src, title="Lib Title", creator="Lib Artist", album="Lib Album", track=7, source="cd",
                  license="Ripped from own CD")
        r = export.copy_tracks([e], self.out, layout="album", fat=True, safe_names=True,
                               transform=export.processing_transform(dsp.Chain(), [e]), transform_ext="mp3")
        self.assertEqual(len(r.copied), 1, r.failed)
        path = r.copied[0]
        self.assertTrue(path.endswith(os.path.join("Lib Artist", "Lib Album", "07 - Lib Title.mp3")), path)
        t = tags_of(path)
        self.assertEqual((t["title"], t["artist"], t["album"], t["track"]),
                         ("Lib Title", "Lib Artist", "Lib Album", "7"))
        self.assertTrue(has_cover(path))

    def test_conversion_transform_makes_an_mp3_with_tags_and_art_and_no_effects(self):
        e = Entry(path=self.src, title="Real Title", creator="Real Artist", source="cd")
        r = export.copy_tracks([e], self.out, transform=export.conversion_transform([e]), transform_ext="mp3")
        self.assertEqual(len(r.copied), 1, r.failed)
        self.assertTrue(r.copied[0].endswith(".mp3"))
        self.assertTrue(has_cover(r.copied[0]))
        self.assertEqual(tags_of(r.copied[0])["artist"], "Real Artist")
        # no effects: decoded audio equals the source's, within MP3 encoding error
        a = subprocess.run(["ffmpeg", "-v", "error", "-i", self.src, "-map", "0:a", "-f", "f32le", "-"],
                           capture_output=True).stdout
        b = subprocess.run(["ffmpeg", "-v", "error", "-i", r.copied[0], "-map", "0:a", "-f", "f32le", "-"],
                           capture_output=True).stdout
        import numpy as np
        n = min(len(a), len(b)) // 4 - 4410
        xa, xb = np.frombuffer(a, np.float32)[2205:2205 + n], np.frombuffer(b, np.float32)[2205 + 1105:2205 + 1105 + n]
        corr = np.corrcoef(xa[:20000], xb[:20000])[0, 1]
        self.assertGreater(abs(corr), 0.9, "the converted audio should still be the original tone")

    def test_an_mp3_source_is_copied_not_re_encoded(self):
        mp3 = os.path.join(self.tmp, "s.mp3")
        ff("-f", "lavfi", "-i", "sine=duration=1", mp3)
        e = Entry(path=mp3, title="s", source="local")
        r = export.copy_tracks([e], self.out, transform=export.conversion_transform([e]), transform_ext="mp3")
        with open(mp3, "rb") as a, open(r.copied[0], "rb") as b:
            self.assertEqual(a.read(), b.read())

    def test_conversion_refuses_no_derivatives_tracks(self):
        e = Entry(path=self.src, title="t", license="CC BY-ND 4.0", source="openverse")
        r = export.copy_tracks([e], self.out, transform=export.conversion_transform([e]), transform_ext="mp3")
        self.assertEqual(r.copied, [])
        self.assertIn("no-derivatives", r.skipped[0][1])

    def test_a_failing_conversion_is_reported(self):
        bad = os.path.join(self.tmp, "bad.flac")
        with open(bad, "wb") as fh:
            fh.write(b"not audio" * 100)
        e = Entry(path=bad, title="bad", source="cd")
        r = export.copy_tracks([e], self.out, transform=export.conversion_transform([e]), transform_ext="mp3")
        self.assertEqual(r.copied, [])
        self.assertEqual(len(r.failed), 1)


class FakeOpener:
    def __init__(self, data=b"", status=200, exc=None):
        self.data, self.status, self.exc, self.urls = data, status, exc, []

    def open(self, req, timeout=None):
        self.urls.append(req.full_url)
        if self.exc:
            raise self.exc
        outer = self

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n=-1): return outer.data[:n] if n >= 0 else outer.data
        return Resp()


MBID = "6019b737-9018-4e89-a4f5-ffb5ac783613"


def http_error(code):
    import io
    return urllib.error.HTTPError("https://x", code, "x", {}, io.BytesIO(b""))


class TestFetchCover(unittest.TestCase):
    JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 100
    PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 100

    def test_returns_real_images(self):
        self.assertEqual(musicbrainz.fetch_cover(MBID, FakeOpener(self.JPEG)), self.JPEG)
        self.assertEqual(musicbrainz.fetch_cover(MBID, FakeOpener(self.PNG)), self.PNG)

    def test_the_url_is_the_cover_art_archive_front_image(self):
        op = FakeOpener(self.JPEG)
        musicbrainz.fetch_cover(MBID.upper(), op)
        self.assertEqual(op.urls, [f"https://coverartarchive.org/release/{MBID}/front-500"])

    def test_a_release_id_that_is_not_a_uuid_is_refused_before_any_request(self):
        for bad in ("", "../../etc/passwd", MBID + "/../x", "not-a-uuid", MBID[:-1], None):
            op = FakeOpener(self.JPEG)
            self.assertIsNone(musicbrainz.fetch_cover(bad, op), bad)
            self.assertEqual(op.urls, [])

    def test_html_or_junk_is_not_accepted_as_a_cover(self):
        for junk in (b"<html>captcha</html>", b"GIF89a....", b"", b"\xff\xd8"):
            self.assertIsNone(musicbrainz.fetch_cover(MBID, FakeOpener(junk)), junk)

    def test_an_oversize_image_is_rejected(self):
        big = b"\xff\xd8\xff" + b"\0" * (musicbrainz.MAX_COVER_BYTES + 10)
        self.assertIsNone(musicbrainz.fetch_cover(MBID, FakeOpener(big)))

    def test_no_art_for_the_release_is_none_not_an_error(self):
        for code in (404, 403):
            self.assertIsNone(musicbrainz.fetch_cover(MBID, FakeOpener(exc=http_error(code))))

    def test_other_http_errors_propagate(self):
        with self.assertRaises(urllib.error.HTTPError):
            musicbrainz.fetch_cover(MBID, FakeOpener(exc=http_error(503)))

    def test_image_kind_goes_by_the_bytes_not_a_claim(self):
        self.assertEqual(musicbrainz.image_kind(self.JPEG), "jpg")
        self.assertEqual(musicbrainz.image_kind(self.PNG), "png")
        self.assertIsNone(musicbrainz.image_kind(b"GIF89a"))

    def test_only_https_hosts_on_the_allowlist_are_followed(self):
        # the redirect guard is the downloader's, configured with the cover hosts
        from bifrost.downloader import host_allowed
        self.assertTrue(host_allowed("ia800909.us.archive.org", musicbrainz.COVER_HOSTS))
        self.assertTrue(host_allowed("coverartarchive.org", musicbrainz.COVER_HOSTS))
        self.assertFalse(host_allowed("evil.example", musicbrainz.COVER_HOSTS))
        self.assertFalse(host_allowed("archive.org.evil.example", musicbrainz.COVER_HOSTS))


def ffmpeg_source(device, track, wav):
    return ["sh", "-c", f"ffmpeg -v error -y -f lavfi -i sine=frequency=330:sample_rate=44100:duration=3 "
                        f"-ac 2 -c:a pcm_s16le '{wav}'"]


class TestRipWithCover(Fixture):
    track = cd.TocTrack(2, 1000, 225)
    tags = cd.Tags(title="Bridge", artist="Band", album="LP", track=2, total=5, year="1997")

    def rip(self, fmt, cover):
        return cd.rip_track("/dev/null", self.track, os.path.join(self.out, f"t-{fmt}"), fmt, self.tags,
                            source_cmd=ffmpeg_source, cover=cover)

    def test_flac_and_mp3_get_the_cover_and_keep_their_tags(self):
        for fmt in ("flac", "mp3"):
            path = self.rip(fmt, self.cover_bytes)
            self.assertTrue(has_cover(path), fmt)
            self.assertEqual(tags_of(path).get("title"), "Bridge", fmt)
            self.assertAlmostEqual(engine.probe_duration(path), 3.0, delta=0.3)

    def test_ogg_and_opus_rip_fine_without_art(self):
        for fmt in ("ogg", "opus"):
            path = self.rip(fmt, self.cover_bytes)
            self.assertTrue(engine.is_audio_file(path), fmt)
            self.assertFalse(has_cover(path), fmt)

    def test_no_cover_means_no_picture_stream(self):
        self.assertFalse(has_cover(self.rip("flac", None)))

    def test_a_png_cover_works(self):
        png = os.path.join(self.tmp, "c.png")
        ff("-f", "lavfi", "-i", "color=c=blue:s=64x64", "-frames:v", "1", png)
        with open(png, "rb") as fh:
            self.assertTrue(has_cover(self.rip("flac", fh.read())))

    def test_temp_files_including_the_cover_are_cleaned_up(self):
        self.rip("flac", self.cover_bytes)
        left = [f for _, _, fs in os.walk(self.out) for f in fs if f.startswith(".rip-")]
        self.assertEqual(left, [])

    def test_a_cover_never_changes_the_audio_length_check(self):
        long_track = cd.TocTrack(2, 1000, 750)               # the disc says 10 s, the reader gives 3 s
        with self.assertRaisesRegex(cd.CDError, "incomplete"):
            cd.rip_track("/dev/null", long_track, os.path.join(self.out, "x"), "flac", self.tags,
                         source_cmd=ffmpeg_source, cover=self.cover_bytes)


class TestEmbedCoverIntoExistingFiles(Fixture):
    """`cd art`: add a picture to files that were ripped without one, never at the audio's expense."""

    def plain(self, ext="flac", **tagkw):
        path = os.path.join(self.tmp, f"plain.{ext}")
        codec = ["-c:a", "flac"] if ext == "flac" else ["-c:a", "libmp3lame", "-q:a", "2"]
        ff("-f", "lavfi", "-i", "sine=frequency=520:duration=3", *codec,
           "-metadata", "title=Old Rip", "-metadata", "artist=Some Band", "-metadata", "album=Some LP",
           "-metadata", "track=3/11", path)
        return path

    def read(self, p):
        with open(p, "rb") as fh:
            return fh.read()

    def temp_left(self):
        return [f for f in os.listdir(self.tmp) if f.startswith(".art-")]

    def test_a_flac_gets_the_picture_with_audio_and_tags_unchanged(self):
        p = self.plain("flac")
        before_md5, before_tags = cd._audio_md5(p), cd._tags(p)
        os.chmod(p, 0o640)
        self.assertFalse(cd.has_cover(p))
        self.assertTrue(cd.embed_cover(p, self.cover_bytes))
        self.assertTrue(cd.has_cover(p))
        self.assertEqual(cd._audio_md5(p), before_md5, "the decoded audio must be bit-identical")
        self.assertEqual(cd._tags(p), before_tags)
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o640, "permissions must be preserved")
        self.assertEqual(self.temp_left(), [])

    def test_an_mp3_gets_it_too_without_being_re_encoded(self):
        p = self.plain("mp3")
        before = cd._audio_md5(p)
        self.assertTrue(cd.embed_cover(p, self.cover_bytes))
        self.assertTrue(cd.has_cover(p))
        self.assertEqual(cd._audio_md5(p), before)
        self.assertEqual(cd._tags(p).get("title"), "Old Rip")
        with open(p, "rb") as fh:
            fh.seek(-128, os.SEEK_END)
            self.assertEqual(fh.read(3), b"TAG")

    def test_a_file_that_already_has_art_is_left_alone_unless_asked(self):
        p = self.plain("flac")
        self.assertTrue(cd.embed_cover(p, self.cover_bytes))
        snapshot = self.read(p)
        self.assertFalse(cd.embed_cover(p, self.cover_bytes))
        self.assertEqual(self.read(p), snapshot)
        self.assertTrue(cd.embed_cover(p, self.cover_bytes, replace=True))
        self.assertTrue(cd.has_cover(p))

    def test_formats_that_cannot_carry_a_picture_are_refused_untouched(self):
        for ext, codec in (("ogg", ["-c:a", "libvorbis"]), ("opus", ["-c:a", "libopus"])):
            p = os.path.join(self.tmp, f"x.{ext}")
            ff("-f", "lavfi", "-i", "sine=duration=1", *codec, p)
            snap = self.read(p)
            self.assertFalse(cd.embed_cover(p, self.cover_bytes), ext)
            self.assertEqual(self.read(p), snap)

    def test_a_corrupt_cover_leaves_the_original_byte_for_byte_untouched(self):
        p = self.plain("flac")
        snap = self.read(p)
        self.assertFalse(cd.embed_cover(p, b"this is not an image" * 50))
        self.assertEqual(self.read(p), snap)
        self.assertEqual(self.temp_left(), [])

    def test_empty_cover_is_refused(self):
        p = self.plain("flac")
        snap = self.read(p)
        self.assertFalse(cd.embed_cover(p, b""))
        self.assertEqual(self.read(p), snap)

    def test_if_the_audio_checksum_ever_differed_nothing_is_replaced(self):
        p = self.plain("flac")
        snap = self.read(p)
        calls = {"n": 0}
        real = cd._audio_md5

        def lying(path):
            calls["n"] += 1
            return real(path) if calls["n"] == 1 else "deadbeef"      # the new file "decodes differently"
        with mock_patch(cd, "_audio_md5", lying):
            self.assertFalse(cd.embed_cover(p, self.cover_bytes))
        self.assertEqual(self.read(p), snap)
        self.assertEqual(self.temp_left(), [])

    def test_if_the_tags_ever_differed_nothing_is_replaced(self):
        p = self.plain("flac")
        snap = self.read(p)
        calls = {"n": 0}

        def differing(path):
            calls["n"] += 1
            return {"title": "A"} if calls["n"] == 1 else {"title": "B"}
        with mock_patch(cd, "_tags", differing):
            self.assertFalse(cd.embed_cover(p, self.cover_bytes))
        self.assertEqual(self.read(p), snap)

    def test_a_missing_file_is_a_clean_false(self):
        self.assertFalse(cd.embed_cover(os.path.join(self.tmp, "nope.flac"), self.cover_bytes))


class mock_patch:
    """Tiny context-managed attribute patch (keeps this file free of extra imports)."""

    def __init__(self, obj, name, value):
        self.obj, self.name, self.value = obj, name, value

    def __enter__(self):
        self.old = getattr(self.obj, self.name)
        setattr(self.obj, self.name, self.value)

    def __exit__(self, *a):
        setattr(self.obj, self.name, self.old)


class TestDefaults(unittest.TestCase):
    def test_settings_defaults(self):
        from bifrost.settings import DEFAULTS
        self.assertTrue(DEFAULTS["rip_cover"])
        self.assertEqual(DEFAULTS["export_layout_choice"], "", "no remembered layout until the user picks one")

    def test_edits_folder_name(self):
        self.assertEqual(export.EDITS_FOLDER, "audio_edits")

    def test_mp3_rips_carry_id3v1_and_v2_3(self):
        args = cd.FORMATS["mp3"][1]
        self.assertIn("-id3v2_version", args)
        self.assertEqual(args[args.index("-id3v2_version") + 1], "3")
        self.assertIn("-write_id3v1", args)


if __name__ == "__main__":
    unittest.main()
