#!/usr/bin/env python3
"""
Loudness regression. With its default settings Bifrost made music about 6.5 dB
quieter (a 0.7 master cut, a 3.5 dB loss in the mono-sum + pan stage, and a limiter
that squashed loud masters). These tests pin the fix: the defaults must leave the
level of normal music essentially where it was, whatever the stereo width.
"""

from __future__ import annotations

import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bifrost import dsp, presets  # noqa: E402

SR = dsp.SAMPLE_RATE


def db(x) -> float:
    return 20 * math.log10(max(float(np.sqrt(np.mean(np.square(x.astype(np.float64))))), 1e-12))


def music(rho: float, seconds=20.0, rms_db=-20.0, seed=3) -> np.ndarray:
    """Noise with a chosen left/right correlation, at a chosen RMS level."""
    rng = np.random.default_rng(seed)
    n = int(SR * seconds)
    c, u1, u2 = (rng.standard_normal(n) for _ in range(3))
    left = math.sqrt(rho) * c + math.sqrt(1 - rho) * u1
    right = math.sqrt(rho) * c + math.sqrt(1 - rho) * u2
    x = np.stack([left, right], axis=1)
    x *= 10 ** (rms_db / 20) / np.sqrt(np.mean(x ** 2))
    return x.astype(np.float32)


def process(chain, x):
    out = np.empty_like(x)
    for i in range(0, len(x), 882):
        out[i:i + 882] = chain.process(x[i:i + 882])
    return out


def default_chain(preset=0):
    c = dsp.Chain(fade_in_s=0.0)
    presets.apply(c, presets.PRESETS[preset])
    return c


class TestDefaultsKeepTheLevel(unittest.TestCase):
    def change(self, rho, preset=0):
        x = music(rho)
        y = process(default_chain(preset), x)
        return db(y) - db(x)

    def test_a_mono_source_keeps_its_level(self):
        self.assertLess(abs(self.change(1.0)), 1.0)

    def test_typical_stereo_music_keeps_its_level(self):
        for rho in (0.6, 0.8, 0.9):
            self.assertLess(abs(self.change(rho)), 1.5, f"correlation {rho}")

    def test_even_fully_uncorrelated_stereo_is_not_much_quieter(self):
        self.assertGreater(self.change(0.0), -3.6, "worst case is the unavoidable -3 dB of power summing")

    def test_every_preset_keeps_the_level(self):
        for i, p in enumerate(presets.PRESETS):
            self.assertLess(abs(self.change(0.8, i)), 1.5, p.name)

    def test_the_master_volume_default_is_unity(self):
        self.assertEqual(dsp.Chain().volume, 1.0)

    def test_the_master_volume_still_works_as_a_trim(self):
        x = music(0.8)
        c = default_chain()
        c.volume = 0.5
        c._volume_prev = 0.5
        self.assertAlmostEqual(db(process(c, x)) - db(process(default_chain(), x)), -6.0, delta=0.6)

    def test_pulse_alone_does_not_change_loudness(self):
        x = music(0.8)
        c = default_chain()
        c.pan_enabled = False
        self.assertLess(abs(db(process(c, x)) - db(x)), 0.3)

    def test_effects_off_is_a_bit_exact_passthrough_for_normal_music(self):
        x = music(0.8)
        c = default_chain()
        c.pan_enabled = c.am_enabled = False
        self.assertTrue(np.array_equal(process(c, x), x))


class TestLimiterDoesNotSquashLoudMasters(unittest.TestCase):
    def test_normal_loud_music_passes_with_effects_off(self):
        rng = np.random.default_rng(5)
        x = np.clip(rng.standard_normal((SR * 5, 2)) * 0.22, -0.9, 0.9).astype(np.float32)   # loud: peaks near 0.88
        c = dsp.Chain(fade_in_s=0.0)
        c.pan_enabled = c.am_enabled = False
        y = process(c, x)
        self.assertLess(float(np.mean(x != y)), 0.005, "under 0.5% of samples may be touched")
        self.assertGreater(float(np.abs(y).max()), 0.85, "loud peaks must not be flattened")
        self.assertLess(abs(db(y) - db(x)), 0.05, "and the level must not move")

    def test_the_limiter_still_stops_genuine_overs(self):
        lim = dsp.Limiter()
        x = (np.random.default_rng(1).uniform(-3, 3, size=(SR, 2))).astype(np.float32)
        self.assertLessEqual(float(np.abs(lim.process(x)).max()), lim.ceiling + 1e-6)

    def test_ceiling_is_close_to_full_scale(self):
        self.assertGreater(dsp.Limiter().ceiling, 0.93)


if __name__ == "__main__":
    unittest.main()
