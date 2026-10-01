#!/usr/bin/env python3
"""
GUI wiring tests: do the controls actually drive the audio chain?

A screenshot proves the widgets draw; it cannot prove that turning a knob changes
the sound. These tests build the real PlayerView against a real Context (with a
silent in-memory sink and a temp library) and poke the controls. Skipped when no
display is available (e.g. a bare CI shell).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

HAVE_DISPLAY = Gtk.init_check()

if HAVE_DISPLAY:
    from bifrost import app, dsp, engine, presets  # noqa: E402
    from bifrost.library import Library  # noqa: E402
    from bifrost.ui.player import PlayerView  # noqa: E402
    from bifrost.ui.widgets import Knob  # noqa: E402


@unittest.skipUnless(HAVE_DISPLAY, "no display")
class TestPlayerWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bifrost-gui-")
        self.lib = Library(os.path.join(self.tmp, "m"), os.path.join(self.tmp, "d"))
        self.ctx = app.Context(library=self.lib, chain=dsp.Chain(fade_in_s=0.0))
        self.sink = engine.MemorySink()
        self.ctx.engine = engine.Engine(self.ctx.chain, sink_factory=lambda: self.sink)
        self.view = PlayerView(self.ctx)
        self.ctx.player = self.view            # the real window does this
        self.chain = self.ctx.chain

    def tearDown(self):
        self.ctx.engine.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_focus_preset_is_selected_and_applied_at_startup(self):
        self.assertTrue(self.view.preset_btns[0].get_active())
        self.assertEqual(self.chain.am.freq, 16.0)
        self.assertAlmostEqual(self.chain.pan.rate, presets.PRESETS[0].rate)

    def test_choosing_a_preset_moves_chain_and_knobs(self):
        self.view.preset_btns[3].set_active(True)               # Reset
        self.assertEqual(self.chain.pan.shape, "pingpong")
        self.assertFalse(self.chain.am_enabled)
        self.assertAlmostEqual(self.view.k_rate.value, 0.5, places=3)
        self.assertFalse(self.view.sw_am.get_active())

    def test_knobs_drive_the_chain(self):
        v = self.view
        v.k_freq._nudge(0.1)
        self.assertAlmostEqual(self.chain.am.freq, v.k_freq.value)
        self.assertGreater(self.chain.am.freq, 16.0)
        v.k_depth._nudge(0.2)
        self.assertAlmostEqual(self.chain.am.depth, v.k_depth.value)
        v.k_vol._nudge(-0.1)
        self.assertAlmostEqual(self.chain.volume, v.k_vol.value)
        v.k_real._nudge(-1.0)
        self.assertEqual((self.chain.pan.itd_ms, self.chain.pan.shadow), (0.0, 0.0))

    def test_knob_values_stay_in_range_and_reset_restores_default(self):
        k = self.view.k_depth
        k._nudge(5.0)
        self.assertAlmostEqual(k.value, 0.5)
        k._nudge(-9.0)
        self.assertAlmostEqual(k.value, 0.0)
        k.set_value(0.4)
        k.reset()
        self.assertAlmostEqual(k.value, k.default)
        self.assertAlmostEqual(self.chain.am.depth, k.default)

    def test_log_knob_spans_its_range_geometrically(self):
        k = Knob("x", (1, 1, 1), 0.05, 2.0, 0.05, lambda v: "", log=True)
        k._frac = 0.5
        self.assertAlmostEqual(k.value, (0.05 * 2.0) ** 0.5, places=6)

    def test_switches_and_dropdowns_drive_the_chain(self):
        v = self.view
        v.sw_pan.set_active(False)
        self.assertFalse(self.chain.pan_enabled)
        v.sw_am.set_active(False)
        self.assertFalse(self.chain.am_enabled)
        v.dd_shape.set_selected(2)
        self.assertEqual(self.chain.pan.shape, "pingpong")
        v.dd_mode.set_selected(1)
        self.assertEqual(self.chain.am.mode, "alternate")

    def test_toggle_play_with_an_empty_library_reports_instead_of_crashing(self):
        msgs = []
        self.ctx._status_cb = lambda m, e: msgs.append((m, e))
        self.ctx.toggle_play()
        self.assertEqual(self.ctx.engine.state, "stopped")
        self.assertTrue(msgs and msgs[-1][1])

    def test_play_pause_resume_stop_through_the_context(self):
        tone = os.path.join(self.tmp, "t.mp3")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=330:duration=30", tone], check=True)
        entry = self.lib.add_local(tone)

        class Slow(engine.MemorySink):
            def write(self, data):
                import time
                time.sleep(0.01)
                super().write(data)

        self.ctx.engine = engine.Engine(self.chain, sink_factory=Slow)
        self.ctx.play_entry(entry)
        self.assertEqual(self.ctx.engine.state, "playing")
        self.assertEqual(self.view.title.get_label(), entry.title)
        self.ctx.toggle_play()
        self.assertEqual(self.ctx.engine.state, "paused")
        self.ctx.toggle_play()
        self.assertEqual(self.ctx.engine.state, "playing")
        self.ctx.stop()
        self.assertEqual(self.ctx.engine.state, "stopped")

    def test_session_fades_then_stops_playback(self):
        tone = os.path.join(self.tmp, "t.mp3")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=330:duration=30", tone], check=True)
        self.lib.add_local(tone)
        clock = [0.0]
        self.ctx.session._clock = lambda: clock[0]
        self.ctx.session.minutes, self.ctx.session.fade_s = 1, 20

        class Slow(engine.MemorySink):
            def write(self, data):
                import time
                time.sleep(0.01)
                super().write(data)

        self.ctx.engine = engine.Engine(self.chain, sink_factory=Slow)
        self.ctx.toggle_session()                              # starts playback too
        self.assertTrue(self.ctx.session.running)
        self.assertEqual(self.ctx.engine.state, "playing")
        clock[0] = 45.0
        self.ctx.poll_session()
        self.assertIsNotNone(self.chain._fade_out_left)         # fade-out began
        clock[0] = 61.0
        self.ctx.poll_session()
        self.assertFalse(self.ctx.session.running)
        self.assertEqual(self.ctx.engine.state, "stopped")
        self.assertIsNone(self.chain._fade_out_left)            # reset for next time


if __name__ == "__main__":
    unittest.main()
