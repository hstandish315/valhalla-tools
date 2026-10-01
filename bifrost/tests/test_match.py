#!/usr/bin/env python3
"""
Search honesty. The headline case is the one that went wrong in real use:
asking for Evanescence's "Bring Me to Life" and being handed an unrelated free
song of the same name.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bifrost.sources import match  # noqa: E402
from bifrost.sources.common import Track  # noqa: E402


def T(title, creator):
    return Track(source="openverse", ident=f"{title}|{creator}", title=title, creator=creator)


# what Openverse actually returned for this query (see the earlier probe)
REAL = [T("Bring Me To Life", "Lost Pilgrim"), T("Bring Me To Life", "Lost Pilgrim"),
        T("Bring Me Life", "PM music Prod")]


class TestNormalisation(unittest.TestCase):
    def test_case_accents_and_punctuation(self):
        self.assertEqual(match.norm("  Mímir's  WELL!! "), "mimir s well")
        self.assertEqual(match.tokens("Beyoncé – Halo"), ["beyonce", "halo"])

    def test_filler_words_dropped_only_when_asked(self):
        self.assertEqual(match.tokens("The Roots of Riverdance"), ["the", "roots", "of", "riverdance"])
        self.assertEqual(match.tokens("The Roots of Riverdance", drop_filler=True), ["roots", "riverdance"])

    def test_empty_inputs_are_safe(self):
        self.assertEqual(match.norm(""), "")
        self.assertTrue(match.artist_matches(T("x", ""), ""))
        self.assertEqual(match.title_coverage(T("x", ""), ""), 1.0)


class TestTheEvanescenceCase(unittest.TestCase):
    def test_title_match_by_the_wrong_artist_is_not_exact(self):
        hits = match.annotate(REAL, "Bring Me to Life", "Evanescence")
        self.assertFalse(any(h.exact for h in hits))
        self.assertEqual(hits[0].note, "Different artist")

    def test_the_right_artist_is_exact_and_sorts_first(self):
        real = T("Bring Me to Life", "Evanescence")
        hits = match.annotate(REAL + [real], "Bring Me to Life", "Evanescence")
        self.assertTrue(hits[0].exact)
        self.assertIs(hits[0].track, real)
        self.assertEqual(sum(h.exact for h in hits), 1)

    def test_single_box_query_is_held_to_the_same_standard(self):
        hits = match.annotate(REAL, "bring me to life evanescence")
        self.assertFalse(any(h.exact for h in hits))
        self.assertTrue(all(h.note == "Partial match" for h in hits))

    def test_single_box_title_only_matches_normally(self):
        hits = match.annotate(REAL, "bring me to life")
        self.assertTrue(hits[0].exact and hits[1].exact)
        self.assertFalse(hits[2].exact)                       # "Bring Me Life" lacks "to"

    def test_artist_match_is_word_based_and_tolerates_the_and_featuring(self):
        self.assertTrue(match.artist_matches(T("x", "Evanescence"), "evanescence"))
        self.assertTrue(match.artist_matches(T("x", "The Roots"), "Roots"))
        self.assertTrue(match.artist_matches(T("x", "Bill Whelan"), "bill whelan"))
        self.assertFalse(match.artist_matches(T("x", "Lost Pilgrim"), "Evanescence"))
        self.assertFalse(match.artist_matches(T("x", "Evanescence Tribute Band"), "Evanescence Cover"))


class TestOrdering(unittest.TestCase):
    def test_exact_first_then_by_coverage_and_stable_within_ties(self):
        a, b, c, d = (T("Moon River", "A"), T("Moon", "B"), T("Moon River Waltz", "C"), T("Sun", "D"))
        hits = match.annotate([d, b, a, c], "moon river")
        self.assertEqual([h.track.creator for h in hits], ["A", "C", "B", "D"])

    def test_hit_count_is_preserved(self):
        self.assertEqual(len(match.annotate(REAL, "x", "y")), len(REAL))


class TestDescribe(unittest.TestCase):
    def test_no_exact_match_explains_commercial_music_and_offers_routes(self):
        hits = match.annotate(REAL, "Bring Me to Life", "Evanescence")
        msg = match.describe(hits, "Bring Me to Life", "Evanescence", hidden=True)
        self.assertIn("No openly licensed match", msg)
        self.assertIn("Live mode", msg)
        self.assertIn("Rip CD", msg)

    def test_shown_nonmatches_are_called_out_as_other_songs(self):
        hits = match.annotate(REAL, "Bring Me to Life", "Evanescence")
        self.assertIn("other songs", match.describe(hits, "Bring Me to Life", "Evanescence", hidden=False))

    def test_matches_are_counted_and_hidden_ones_mentioned(self):
        hits = match.annotate(REAL + [T("Bring Me to Life", "Evanescence")], "Bring Me to Life", "Evanescence")
        msg = match.describe(hits, "Bring Me to Life", "Evanescence", hidden=True)
        self.assertIn("1 match for", msg)
        self.assertIn("3 others hidden", msg)

    def test_empty_results(self):
        self.assertIn("Nothing found", match.describe([], "x", "", hidden=False))


if __name__ == "__main__":
    unittest.main()
