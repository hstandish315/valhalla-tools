#!/usr/bin/env python3
"""Settings persist atomically, ignore unknown keys, and survive corruption."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bifrost.settings import DEFAULTS, Settings  # noqa: E402


class TestSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bifrost-set-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_defaults_when_nothing_saved(self):
        self.assertEqual(Settings(self.tmp)["rip_format"], "flac")

    def test_values_persist_across_instances(self):
        Settings(self.tmp).set("rip_dir", "/music/rips")
        self.assertEqual(Settings(self.tmp)["rip_dir"], "/music/rips")

    def test_unknown_keys_are_refused_and_ignored_on_load(self):
        s = Settings(self.tmp)
        with self.assertRaises(KeyError):
            s.set("nope", 1)
        with open(s.path, "w") as fh:
            fh.write('{"rip_format": "mp3", "evil": 1}')
        loaded = Settings(self.tmp)
        self.assertEqual(loaded["rip_format"], "mp3")
        self.assertNotIn("evil", loaded.values)

    def test_corrupt_file_falls_back_to_defaults(self):
        os.makedirs(self.tmp, exist_ok=True)
        with open(os.path.join(self.tmp, "settings.json"), "w") as fh:
            fh.write("{not json")
        self.assertEqual(Settings(self.tmp).values, DEFAULTS)

    def test_no_temp_files_left_behind(self):
        Settings(self.tmp).set("rip_eject", True)
        self.assertEqual([f for f in os.listdir(self.tmp) if f.startswith(".settings-")], [])


if __name__ == "__main__":
    unittest.main()
