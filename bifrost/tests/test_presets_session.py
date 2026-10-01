#!/usr/bin/env python3
"""Presets stay inside the DSP's ranges; the session timer fires once, on time."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bifrost import dsp, presets  # noqa: E402
from bifrost.session import Session  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TestPresets(unittest.TestCase):
    def test_every_preset_is_within_range(self):
        for p in presets.PRESETS:
            with self.subTest(p.name):
                self.assertTrue(0.05 <= p.rate <= 2.0)
                self.assertTrue(0.0 <= p.width <= 1.0)
                self.assertIn(p.shape, ("sine", "triangle", "pingpong"))
                self.assertTrue(0.0 <= p.realism <= 1.0)
                self.assertTrue(8.0 <= p.am_freq <= 40.0)
                self.assertTrue(0.0 <= p.am_depth <= 0.5)
                self.assertIn(p.am_mode, ("inphase", "alternate"))

    def test_names_are_unique(self):
        names = [p.name for p in presets.PRESETS]
        self.assertEqual(len(names), len(set(names)))

    def test_apply_sets_the_chain(self):
        c = dsp.Chain()
        presets.apply(c, presets.PRESETS[3])            # Reset
        self.assertEqual(c.pan.shape, "pingpong")
        self.assertFalse(c.am_enabled)
        self.assertTrue(c.pan_enabled)

    def test_realism_round_trips_and_respects_head_size(self):
        c = dsp.Chain()
        presets.set_realism(c, 1.0)
        self.assertLessEqual(c.pan.itd_ms, 0.7 + 1e-9)
        self.assertLess(c.pan.itd_ms, dsp.MAX_ITD_MS)
        self.assertAlmostEqual(presets.realism_of(c), 1.0)
        presets.set_realism(c, 0.0)
        self.assertEqual((c.pan.itd_ms, c.pan.shadow), (0.0, 0.0))
        presets.set_realism(c, 7.0)                       # clamped
        self.assertAlmostEqual(presets.realism_of(c), 1.0)


class TestSession(unittest.TestCase):
    def test_counts_down_and_fires_fade_then_done_once(self):
        clk = Clock()
        s = Session(minutes=1, fade_s=20, clock=clk)
        s.start()
        events = []
        for _ in range(70):
            events.append(s.poll())
            clk.t += 1.0
        self.assertEqual([e for e in events if e], ["fade", "done"])
        self.assertEqual(events.index("fade"), 40)        # 20 s left of 60
        self.assertEqual(events.index("done"), 60)

    def test_idle_session_reports_nothing(self):
        s = Session(clock=Clock())
        self.assertEqual(s.poll(), "")
        self.assertFalse(s.running)
        self.assertEqual(s.label(), "25:00")

    def test_stop_resets(self):
        clk = Clock()
        s = Session(minutes=2, clock=clk)
        s.start()
        clk.t += 30
        self.assertEqual(s.label(), "01:30")
        s.stop()
        self.assertEqual(s.label(), "02:00")
        self.assertFalse(s.running)

    def test_session_shorter_than_fade_window(self):
        clk = Clock()
        s = Session(minutes=0.1, fade_s=20, clock=clk)    # 6 s
        s.start()
        self.assertEqual(s.poll(), "fade")
        clk.t += 6
        self.assertEqual(s.poll(), "done")

    def test_progress(self):
        clk = Clock()
        s = Session(minutes=1, clock=clk)
        s.start()
        clk.t += 15
        self.assertAlmostEqual(s.progress(), 0.25)


if __name__ == "__main__":
    unittest.main()
