#!/usr/bin/env python3
"""
Source parsers (Openverse, Internet Archive) tested against *real* API
responses saved in tests/fixtures, so the schema assumptions are checked against
what the services actually return. No network access.
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bifrost.sources import archive, openverse  # noqa: E402
from bifrost.sources.common import Track, parse_license  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return json.load(fh)


class FakeHttp:
    def __init__(self, payload):
        self.payload, self.calls = payload, []

    def get_json(self, url, params=None):
        self.calls.append((url, params))
        return self.payload


class TestLicense(unittest.TestCase):
    def test_cc_variants(self):
        self.assertEqual(parse_license("http://creativecommons.org/licenses/by/3.0/")[0], "CC BY 3.0")
        self.assertEqual(parse_license("https://creativecommons.org/licenses/by-nc-sa/4.0/")[0],
                         "CC BY-NC-SA 4.0")

    def test_public_domain(self):
        self.assertEqual(parse_license("https://creativecommons.org/publicdomain/zero/1.0/")[0], "CC0 1.0")
        self.assertEqual(parse_license("https://creativecommons.org/publicdomain/mark/1.0/")[0],
                         "Public Domain")

    def test_unrecognised_is_unlicensed(self):
        for bad in ("", "All rights reserved", "http://example.com/licenses/by/3.0/",
                    "https://evil.example/?x=creativecommons.org"):
            self.assertIsNone(parse_license(bad), bad)

    def test_no_derivatives_blocks_modification(self):
        mk = lambda lic: Track(source="x", ident="", title="", license=lic)
        self.assertFalse(mk("CC BY-ND 4.0").allows_modification)
        self.assertFalse(mk("CC BY-NC-ND 3.0").allows_modification)
        self.assertTrue(mk("CC BY-NC-SA 4.0").allows_modification)
        self.assertTrue(mk("CC0 1.0").allows_modification)
        self.assertTrue(mk("Local file").allows_modification)


class TestHttps(unittest.TestCase):
    def test_http_urls_are_refused_outright(self):
        from bifrost.sources.common import Http
        with self.assertRaisesRegex(ValueError, "non-https"):
            Http().get_json("http://example.invalid/api")

    def test_a_redirect_that_downgrades_to_http_is_refused(self):
        import urllib.error
        import urllib.request
        from bifrost.sources.common import _HttpsOnlyRedirect
        h = _HttpsOnlyRedirect()
        req = urllib.request.Request("https://api.example.invalid/x")
        with self.assertRaisesRegex(urllib.error.URLError, "non-https"):
            h.redirect_request(req, None, 302, "Found", {}, "http://evil.example/steal")

    def test_an_https_redirect_is_followed(self):
        import urllib.request
        from bifrost.sources.common import _HttpsOnlyRedirect
        h = _HttpsOnlyRedirect()
        req = urllib.request.Request("https://api.example.invalid/x")
        new = h.redirect_request(req, None, 302, "Found", {}, "https://other.example/y")
        self.assertEqual(new.full_url, "https://other.example/y")


class TestOpenverse(unittest.TestCase):
    def test_parses_real_response(self):
        tracks = openverse.parse_results(load("openverse_search.json"))
        self.assertGreaterEqual(len(tracks), 1)
        t = tracks[0]
        self.assertEqual(t.source, "openverse")
        self.assertTrue(t.download_url.startswith("https://"))
        self.assertTrue(t.license.startswith("CC") or t.license == "Public Domain")
        self.assertTrue(t.attribution)
        self.assertGreater(t.duration, 0)

    def test_drops_unlicensed_and_mature_and_urlless(self):
        good = load("openverse_search.json")["results"][0]
        payload = {"results": [
            dict(good, license_url="", id="a"),
            dict(good, mature=True, id="b"),
            dict(good, url=None, id="c"),
            dict(good, id="d"),
        ]}
        self.assertEqual([t.ident for t in openverse.parse_results(payload)], ["d"])

    def test_download_hosts_cover_the_real_result(self):
        from urllib.parse import urlparse
        from bifrost.downloader import host_allowed
        for t in openverse.parse_results(load("openverse_search.json")):
            self.assertTrue(host_allowed(urlparse(t.download_url).hostname, openverse.HOSTS),
                            t.download_url)

    def test_search_requests_music_and_excludes_mature(self):
        http = FakeHttp(load("openverse_search.json"))
        tracks, total = openverse.search("ambient", http=http)
        _, params = http.calls[0]
        self.assertEqual(params["category"], "music")
        self.assertEqual(params["mature"], "false")
        self.assertEqual(total, 175)


class TestArchive(unittest.TestCase):
    def test_search_keeps_only_cc_and_pd(self):
        payload = load("archive_search.json")
        tracks, total = archive.parse_search(payload)
        self.assertEqual(len(tracks), 3)
        self.assertEqual(total, 70110)
        payload["response"]["docs"].append(
            {"identifier": "bad", "title": "t", "licenseurl": "All rights reserved"})
        self.assertEqual(len(archive.parse_search(payload)[0]), 3)

    def test_error_payload_raises_instead_of_looking_empty(self):
        with self.assertRaises(ValueError):
            archive.parse_search({"error": "Invalid query", "responseHeader": {"status": 400}})

    def test_query_has_no_slash_wildcards(self):
        # '/' inside a Lucene wildcard is a syntax error on the real server
        http = FakeHttp(load("archive_search.json"))
        archive.search("ambient", http=http)
        for token in http.calls[0][1]["q"].split():
            if "*" in token:
                self.assertNotIn("/", token)

    def test_query_quotes_user_text(self):
        http = FakeHttp(load("archive_search.json"))
        archive.search('x" OR licenseurl:*', http=http)
        q = http.calls[0][1]["q"]
        self.assertNotIn('x" OR', q)
        self.assertIn("licenseurl:*creativecommons.org", q)

    def test_resolve_picks_the_audio_file(self):
        meta = load("archive_metadata.json")
        seed = archive.parse_search(load("archive_search.json"))[0][0]
        out = archive.resolve(seed, meta=meta)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].download_url.startswith("https://archive.org/download/jamendo-592141/"))
        self.assertTrue(out[0].download_url.endswith(".mp3"))
        self.assertEqual(out[0].license, "CC BY 3.0")

    def test_flac_beats_mp3_and_originals_beat_derivatives(self):
        meta = {"metadata": {"licenseurl": "https://creativecommons.org/licenses/by/4.0/"}, "files": [
            {"name": "a.mp3", "format": "VBR MP3", "source": "derivative", "size": "10"},
            {"name": "a.flac", "format": "Flac", "source": "original", "size": "30"},
            {"name": "b.mp3", "format": "VBR MP3", "source": "derivative", "size": "10"},
            {"name": "b.ogg", "format": "Ogg Vorbis", "source": "derivative", "size": "8"},
            {"name": "c.mp3", "format": "128Kbps MP3", "source": "derivative", "size": "5"},
            {"name": "c.mp3x", "format": "MP3", "source": "original", "size": "7"},
            {"name": "cover.jpg", "format": "JPEG", "source": "original"},
        ]}
        names = [n for n, _, _ in archive.pick_files(meta)]
        self.assertEqual(names, ["a.flac", "b.mp3", "c.mp3x"])

    def test_resolve_refuses_when_item_license_is_not_open(self):
        meta = {"metadata": {"licenseurl": "none"}, "files": [
            {"name": "a.mp3", "format": "VBR MP3", "source": "original"}]}
        seed = Track(source="archive", ident="x", title="t", license="CC BY 4.0")
        self.assertEqual(archive.resolve(seed, meta=meta), [])

    def test_path_traversal_names_are_ignored(self):
        meta = {"metadata": {"licenseurl": "https://creativecommons.org/licenses/by/4.0/"}, "files": [
            {"name": "../../etc/x.mp3", "format": "VBR MP3", "source": "original"}]}
        self.assertEqual(archive.pick_files(meta), [])


if __name__ == "__main__":
    unittest.main()
