#!/usr/bin/env python3
"""
Engine tests against real ffmpeg and a synthesized tone, with an in-memory sink
so no audio hardware is touched. Run: venv/bin/python3 -m unittest discover -s tests
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bifrost import dsp, engine  # noqa: E402

SR = dsp.SAMPLE_RATE


def make_tone(path: str, seconds: float = 2.0, hz: int = 440) -> None:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"sine=frequency={hz}:sample_rate={SR}:duration={seconds}", path],
        check=True)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg required")
class TestEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="bifrost-test-")
        cls.tone = os.path.join(cls.dir, "tone.wav")
        make_tone(cls.tone, 2.0)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def _play(self, chain=None):
        sink = engine.MemorySink()
        eng = engine.Engine(chain or dsp.Chain(fade_in_s=0.0), sink_factory=lambda: sink)
        eng.play(self.tone)
        eng.join(20)
        return eng, sink.audio()

    def test_plays_the_whole_file_and_ends(self):
        eng, audio = self._play()
        self.assertEqual(eng.state, "ended")
        self.assertAlmostEqual(len(audio) / SR, 2.0, delta=0.05)

    def test_on_end_fires(self):
        fired = []
        sink = engine.MemorySink()
        eng = engine.Engine(dsp.Chain(fade_in_s=0.0), sink_factory=lambda: sink,
                            on_end=lambda: fired.append(True))
        eng.play(self.tone)
        eng.join(20)
        self.assertEqual(fired, [True])

    def test_output_is_bilateral_and_modulated(self):
        chain = dsp.Chain(fade_in_s=0.0)
        chain.pan.rate = 1.0
        chain.pan.itd_ms = 0.0
        chain.pan.shadow = 0.0
        _, audio = self._play(chain)
        blk = int(0.05 * SR)
        m = len(audio) // blk
        e = (audio[:m * blk] ** 2).reshape(m, blk, 2).mean(axis=1)
        self.assertGreater(e[:, 0].max() / max(e[:, 0].min(), 1e-9), 5.0,
                           "left ear level must swing")
        self.assertGreater(e[:, 1].max() / max(e[:, 1].min(), 1e-9), 5.0,
                           "right ear level must swing")
        self.assertLessEqual(np.abs(audio).max(), chain.limiter.ceiling + 1e-5)

    def test_stop_halts_playback_promptly(self):
        sink = engine.MemorySink()

        class Slow(engine.MemorySink):
            def write(self, data):
                time.sleep(0.02)
                super().write(data)

        slow = Slow()
        eng = engine.Engine(dsp.Chain(fade_in_s=0.0), sink_factory=lambda: slow)
        eng.play(self.tone)
        time.sleep(0.3)
        t0 = time.time()
        eng.stop()
        self.assertLess(time.time() - t0, 2.0)
        self.assertEqual(eng.state, "stopped")
        self.assertLess(len(slow.audio()) / SR, 1.9, "must not have played to the end")

    def test_pause_stops_consuming_and_resume_continues(self):
        class Slow(engine.MemorySink):
            def write(self, data):
                time.sleep(0.01)
                super().write(data)

        slow = Slow()
        eng = engine.Engine(dsp.Chain(fade_in_s=0.0), sink_factory=lambda: slow)
        eng.play(self.tone)
        time.sleep(0.2)
        eng.pause()
        time.sleep(0.1)
        n1 = len(slow.chunks)
        time.sleep(0.3)
        self.assertEqual(len(slow.chunks), n1, "no writes while paused")
        eng.resume()
        eng.join(20)
        self.assertEqual(eng.state, "ended")

    def test_seek_skips_ahead(self):
        sink = engine.MemorySink()
        eng = engine.Engine(dsp.Chain(fade_in_s=0.0), sink_factory=lambda: sink)
        eng.play(self.tone, start=1.0)
        eng.join(20)
        self.assertAlmostEqual(len(sink.audio()) / SR, 1.0, delta=0.06)

    def test_probe_and_audio_validation(self):
        self.assertAlmostEqual(engine.probe_duration(self.tone), 2.0, delta=0.05)
        self.assertTrue(engine.is_audio_file(self.tone))
        junk = os.path.join(self.dir, "junk.mp3")
        with open(junk, "wb") as fh:
            fh.write(b"<html>not audio</html>" * 50)
        self.assertFalse(engine.is_audio_file(junk))
        self.assertEqual(engine.probe_duration(junk), 0.0)

    def test_missing_file_ends_without_hanging(self):
        sink = engine.MemorySink()
        eng = engine.Engine(dsp.Chain(), sink_factory=lambda: sink)
        eng.play(os.path.join(self.dir, "nope.wav"))
        eng.join(10)
        self.assertEqual(len(sink.audio()), 0)

    def test_render_offline_writes_a_valid_file(self):
        out = os.path.join(self.dir, "out.flac")
        engine.render_offline(dsp.Chain(fade_in_s=0.0), self.tone, out)
        self.assertTrue(engine.is_audio_file(out))
        self.assertAlmostEqual(engine.probe_duration(out), 2.0, delta=0.1)

    def test_no_portaudio_in_the_process(self):
        self.assertNotIn("sounddevice", sys.modules)


if __name__ == "__main__":
    unittest.main()
