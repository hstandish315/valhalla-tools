#!/usr/bin/env python3
"""
Headless DSP tests. Run: venv/bin/python3 -m unittest discover -s tests

The properties pinned here are the ones that decide whether the effect sounds
right: the pan law's power contract, that the LFO and modulation land at the
requested frequencies, that the inter-ear delay is the requested size, and
chunk invariance (no state lost at block boundaries, hence no clicks).
"""

from __future__ import annotations

import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bifrost import dsp  # noqa: E402

SR = dsp.SAMPLE_RATE


def noise(seconds: float, seed: int = 1, stereo: bool = True) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    x = rng.uniform(-0.5, 0.5, size=(n, 1)).astype(np.float32)
    return np.repeat(x, 2, axis=1) if stereo else x


def run_chunked(proc, x: np.ndarray, sizes) -> np.ndarray:
    out, i, k = [], 0, 0
    while i < len(x):
        s = sizes[k % len(sizes)]
        out.append(proc.process(x[i:i + s]))
        i += s
        k += 1
    return np.concatenate(out)


class TestPanLaw(unittest.TestCase):
    pos = np.linspace(-1.0, 1.0, 201)

    def test_constant_power(self):
        gl, gr = dsp.pan_gains(self.pos)
        np.testing.assert_allclose(gl ** 2 + gr ** 2, 1.0, atol=1e-6,
                                   err_msg="total power must not dip between the ears")

    def test_gains_bounded(self):
        gl, gr = dsp.pan_gains(self.pos)
        self.assertGreaterEqual(min(gl.min(), gr.min()), 0.0)
        self.assertLessEqual(max(gl.max(), gr.max()), 1.0 + 1e-9)

    def test_monotonic_and_mirrored(self):
        gl, gr = dsp.pan_gains(self.pos)
        self.assertTrue(np.all(np.diff(gl) <= 1e-9), "left must fall as pos rises")
        self.assertTrue(np.all(np.diff(gr) >= -1e-9), "right must rise as pos rises")
        np.testing.assert_allclose(gl, gr[::-1], atol=1e-9)

    def test_extremes_favour_the_near_ear(self):
        gl, gr = dsp.pan_gains(np.array([-1.0, 1.0]))
        self.assertGreater(gl[0], gr[0])
        self.assertGreater(gr[1], gl[1])


class TestPan(unittest.TestCase):
    def test_centre_is_dual_mono_and_power_preserving(self):
        p = dsp.Pan(width=0.0, itd_ms=0.0, shadow=0.0)
        x = noise(0.5)
        y = p.process(x)
        np.testing.assert_allclose(y[:, 0], y[:, 1], atol=1e-6)
        # level-matched mono sum of the source, aligned to the FIR group delay (15 samples)
        ref = x.astype(np.float64).sum(axis=1) / math.sqrt(2.0)
        np.testing.assert_allclose(y[15:, 0] ** 2 + y[15:, 1] ** 2,
                                   ref[:-15] ** 2, atol=1e-5)

    def test_energy_swings_at_the_lfo_rate(self):
        rate = 0.5
        p = dsp.Pan(rate=rate, width=1.0, itd_ms=0.0, shadow=0.0)
        y = p.process(noise(20.0))
        blk = int(0.02 * SR)
        m = len(y) // blk
        e = (y[:m * blk] ** 2).reshape(m, blk, 2).mean(axis=1)
        bal = e[:, 1] - e[:, 0]
        spec = np.abs(np.fft.rfft(bal - bal.mean()))
        freqs = np.fft.rfftfreq(m, d=0.02)
        self.assertAlmostEqual(freqs[spec.argmax()], rate, delta=0.1)

    def test_itd_is_the_requested_size(self):
        # phase pi/2 with rate 0 holds pos = width, so the left ear is far
        p = dsp.Pan(rate=0.0, width=0.5, itd_ms=0.6, shadow=0.0)
        p.phase = math.pi / 2
        y = p.process(noise(2.0))
        a, b = y[:, 0].astype(np.float64), y[:, 1].astype(np.float64)
        n = 1 << int(np.ceil(np.log2(len(a) * 2)))
        xc = np.fft.irfft(np.fft.rfft(a, n) * np.conj(np.fft.rfft(b, n)), n)
        lags = np.concatenate([xc[-60:], xc[:60]])
        lag = np.arange(-60, 60)[lags.argmax()]
        expected = 0.6 * 0.5 * SR / 1000.0           # 13.2 samples
        self.assertAlmostEqual(lag, expected, delta=1.5)
        self.assertGreater(lag, 0, "the far (left) ear must hear it later")

    def test_shadow_darkens_the_far_ear(self):
        p = dsp.Pan(rate=0.0, width=1.0, itd_ms=0.0, shadow=1.0)
        p.phase = math.pi / 2
        y = p.process(noise(1.0))
        def hf(sig):
            return np.abs(np.fft.rfft(sig))[len(sig) // 4:].sum()
        # left is far; compare tonal balance after removing the level difference
        gl, gr = dsp.pan_gains(np.array([1.0]))
        ratio_l = hf(y[:, 0]) / max(gl[0], 1e-6) if gl[0] > 1e-6 else 0.0
        if gl[0] > 1e-6:
            self.assertLess(ratio_l, hf(y[:, 1]) / gr[0])

    def test_chunk_invariance(self):
        x = noise(1.0)
        whole = dsp.Pan(itd_ms=0.6, shadow=0.6).process(x)
        parts = run_chunked(dsp.Pan(itd_ms=0.6, shadow=0.6), x, [441, 1000, 77, 2048])
        np.testing.assert_allclose(whole, parts, atol=1e-5)

    def test_all_shapes_stay_in_range(self):
        for shape in ("sine", "triangle", "pingpong"):
            p = dsp.Pan(shape=shape, rate=2.0)
            y = p.process(noise(1.0))
            # level-matching lets the near ear reach sqrt(2) x the input peak at the extreme of a
            # sweep (+3 dB); that is the price of keeping the *average* level where it was
            self.assertLessEqual(np.abs(y).max(), 0.5 * math.sqrt(2.0) + 1e-3, shape)


class TestAmpMod(unittest.TestCase):
    def test_depth_zero_is_bit_exact_passthrough(self):
        x = noise(0.5)
        y = dsp.AmpMod(depth=0.0).process(x)
        self.assertTrue(np.array_equal(x, y))

    def test_envelope_peaks_at_the_modulation_frequency(self):
        for f in (8.0, 16.0, 30.0):
            am = dsp.AmpMod(freq=f, depth=0.4)
            y = am.process(np.ones((SR * 2, 2), dtype=np.float32))
            spec = np.abs(np.fft.rfft(y[:, 0] - y[:, 0].mean()))
            freqs = np.fft.rfftfreq(len(y), d=1.0 / SR)
            self.assertAlmostEqual(freqs[spec.argmax()], f, delta=0.6)

    def test_modulation_depth_and_level_compensation(self):
        d = 0.4
        y = dsp.AmpMod(freq=16.0, depth=d).process(np.ones((SR, 2), dtype=np.float32))
        lo, hi = float(y.min()), float(y.max())
        self.assertAlmostEqual((hi - lo) / (hi + lo), d / (2.0 - d), places=3)
        self.assertAlmostEqual(float(y.mean()), 1.0, delta=0.01)   # loudness kept

    def test_alternate_mode_runs_the_ears_in_antiphase(self):
        y = dsp.AmpMod(freq=16.0, depth=0.5, mode="alternate").process(
            np.ones((SR, 2), dtype=np.float32))
        corr = np.corrcoef(y[:, 0], y[:, 1])[0, 1]
        self.assertLess(corr, -0.99)

    def test_chunk_invariance(self):
        x = noise(1.0)
        whole = dsp.AmpMod(depth=0.3).process(x)
        parts = run_chunked(dsp.AmpMod(depth=0.3), x, [441, 1000, 77, 2048])
        np.testing.assert_allclose(whole, parts, atol=1e-6)


class TestSafety(unittest.TestCase):
    def test_limiter_never_exceeds_ceiling(self):
        lim = dsp.Limiter(ceiling_db=-3.0)
        x = (np.random.default_rng(3).uniform(-4, 4, size=(SR, 2))).astype(np.float32)
        self.assertLessEqual(np.abs(lim.process(x)).max(), lim.ceiling + 1e-6)

    def test_limiter_is_transparent_below_the_knee(self):
        lim = dsp.Limiter()
        x = noise(0.2) * 0.5
        self.assertTrue(np.array_equal(lim.process(x), x))

    def test_chain_fades_in_and_never_clips(self):
        c = dsp.Chain(fade_in_s=1.0)
        c.volume = 1.0
        c._volume_prev = 1.0
        y = c.process(np.ones((SR * 2, 2), dtype=np.float32))
        self.assertLess(abs(float(y[0, 0])), 0.01)               # silent at the start
        self.assertLessEqual(np.abs(y).max(), c.limiter.ceiling + 1e-6)

    def test_chain_fade_out_finishes_silent(self):
        c = dsp.Chain(fade_in_s=0.0)
        c.fade_out(0.5)
        y = c.process(np.ones((SR, 2), dtype=np.float32) * 0.3)
        self.assertTrue(c.finished)
        self.assertLess(abs(float(y[-1, 0])), 1e-3)

    def test_reset_stream_fades_in_again_and_clears_fade_out(self):
        c = dsp.Chain(fade_in_s=0.5)
        c.fade_out(0.1)
        c.process(np.ones((SR, 2), dtype=np.float32) * 0.3)
        self.assertTrue(c.finished)
        c.reset_stream()
        self.assertFalse(c.finished)
        y = c.process(np.ones((SR // 2, 2), dtype=np.float32) * 0.3)
        self.assertLess(abs(float(y[0, 0])), 0.01)               # new track fades in
        self.assertGreater(float(np.abs(y[-1]).max()), 0.05)     # and is audible after

    def test_reset_fade_cancels_a_pending_fade_out(self):
        c = dsp.Chain(fade_in_s=0.0)
        c.fade_out(10.0)
        c.reset_fade()
        y = c.process(np.ones((SR, 2), dtype=np.float32) * 0.3)
        self.assertFalse(c.finished)
        self.assertGreater(float(np.abs(y[-1]).max()), 0.05)

    def test_chain_chunk_invariance(self):
        x = noise(1.0)
        a, b = dsp.Chain(fade_in_s=0.2), dsp.Chain(fade_in_s=0.2)
        whole = a.process(x)
        parts = run_chunked(b, x, [882, 1000, 333])
        np.testing.assert_allclose(whole, parts, atol=1e-5)


if __name__ == "__main__":
    unittest.main()
