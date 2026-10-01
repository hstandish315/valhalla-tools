#!/usr/bin/env python3
"""
Hardware smoke test: runs only when an optical drive with an *audio* disc is
attached, and is read-only (it reads the table of contents, nothing else).
Asserts structural facts so it passes for any audio CD.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bifrost import cd  # noqa: E402


def _toc():
    for d in cd.find_drives():
        try:
            return d, cd.read_toc(d.device)
        except cd.CDError:
            continue
    return None, None


DRIVE, TOC = _toc()


@unittest.skipIf(TOC is None, "no optical drive with an audio disc")
class TestRealDisc(unittest.TestCase):
    def test_toc_is_sane(self):
        self.assertGreaterEqual(len(TOC.audio_tracks), 1)
        numbers = [t.number for t in TOC.tracks]
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))
        for t in TOC.tracks:
            self.assertGreater(t.sectors, 0)
        starts = [t.lba for t in TOC.tracks]
        self.assertEqual(starts, sorted(starts))
        self.assertGreater(TOC.leadout_lba, starts[-1])
        self.assertLess(TOC.minutes, 100)

    def test_disc_id_is_well_formed_and_stable(self):
        a, b = cd.disc_id(TOC), cd.disc_id(TOC)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 28)
        self.assertTrue(a.endswith("-"))


if __name__ == "__main__":
    unittest.main()
