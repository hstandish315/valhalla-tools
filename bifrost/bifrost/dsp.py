"""
Bifrost DSP - bilateral panning, amplitude modulation and output safety.

Pure numpy, no GTK and no audio I/O, so every property is unit-testable.

Everything here is *stateful and chunked*: a processor is fed consecutive blocks
of an unbroken stream and carries its phase and filter history between calls.
The invariant that matters is chunk invariance - processing a signal in one
block or in many small ones must give the same samples - because any state lost
at a block boundary is audible as a click at the chunk rate.

Audio convention: float32 arrays shaped (n, 2), values nominally in [-1, 1].
"""

from __future__ import annotations

import math

import numpy as np

SAMPLE_RATE = 44100
TWO_PI = 2.0 * math.pi

# Interaural time difference is at most ~0.7 ms for a human head. The delay
# buffer is sized for the largest value the UI will ever request.
MAX_ITD_MS = 1.0
_FIR_TAPS = 31                      # odd, so the group delay is a whole sample


def pan_gains(pos: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """
    Map a pan position to (left_gain, right_gain).

    `pos` is an array in [-1, 1]: -1 is hard left, 0 centre, +1 hard right.
    The result must satisfy the contract pinned by tests/test_dsp.py:
    gains in [0, 1], monotonic in `pos`, and constant total power at every
    position so the sweep does not pump in loudness.
    """
    # Constant-power (sin/cos) law. A linear law has L^2 + R^2 = 0.5 at the
    # centre - a 3 dB dip that would pulse once per sweep - whereas rotating
    # through a quarter circle keeps L^2 + R^2 = 1 at every position.
    angle = (np.asarray(pos, dtype=np.float64) + 1.0) * (math.pi / 4.0)
    return np.cos(angle), np.sin(angle)


# --------------------------------------------------------------- LFO shapes ---

def lfo(phase: np.ndarray, shape: str) -> np.ndarray:
    """Unit LFO in [-1, 1] for an array of phases (radians)."""
    s = np.sin(phase)
    if shape == "triangle":
        return (2.0 / math.pi) * np.arcsin(s)
    if shape == "pingpong":
        # A squashed square: dwells near each ear, then crosses quickly. This is
        # the classic EMDR-style "ping-pong" without the click of a hard edge.
        k = 3.0
        return np.tanh(k * s) / math.tanh(k)
    return s


def _lowpass_taps(cutoff_hz: float, sr: int, taps: int = _FIR_TAPS) -> np.ndarray:
    """Hamming-windowed sinc, normalised to unity gain at DC."""
    n = np.arange(taps) - (taps - 1) / 2.0
    fc = cutoff_hz / sr
    h = np.sinc(2.0 * fc * n) * np.hamming(taps)
    return h / h.sum()


# ------------------------------------------------------------------ panner ---

class Pan:
    """
    Bilateral panner: sweeps a mono image between the ears.

    Beyond the level change it adds two cues the brain actually localises with:
    an interaural time difference (the far ear hears the sound later) and a head
    shadow (the far ear hears it duller). `itd_ms` and `shadow` can be zero for a
    plain volume pan.
    """

    def __init__(self, sr: int = SAMPLE_RATE, rate: float = 0.25, width: float = 1.0,
                 shape: str = "sine", itd_ms: float = 0.6, shadow: float = 0.5,
                 shadow_hz: float = 3500.0):
        self.sr = sr
        self.rate = rate            # sweeps per second, 0.05 - 2
        self.width = width          # 0 = centre, 1 = full sweep
        self.shape = shape
        self.itd_ms = itd_ms
        self.shadow = shadow        # 0 - 1, how dark the far ear gets
        self.phase = 0.0

        self._h = _lowpass_taps(shadow_hz, sr)
        self._lat = (_FIR_TAPS - 1) // 2
        self._mono_hist = np.zeros(_FIR_TAPS - 1)
        self._dmax = int(math.ceil(MAX_ITD_MS * sr / 1000.0)) + 2
        self._ear_hist = [np.zeros(self._dmax), np.zeros(self._dmax)]
        self.position = 0.0         # last block's final pan position, for the UI

    def process(self, x: np.ndarray) -> np.ndarray:
        n = len(x)
        if n == 0:
            return x
        # (L+R)/sqrt(2), not the plain average: with the constant-power pan law each ear then
        # gets (L+R)/2 at centre, which for a mono source is exactly its original level and for
        # typical wide stereo is within ~0.5 dB. The plain average came out 3.5 dB quieter.
        mono = x.astype(np.float64).sum(axis=1) / math.sqrt(2.0)

        w = TWO_PI * self.rate / self.sr
        phases = self.phase + w * np.arange(n)
        self.phase = (self.phase + w * n) % TWO_PI
        pos = np.clip(self.width * lfo(phases, self.shape), -1.0, 1.0)
        self.position = float(pos[-1])

        # Dry path aligned to the FIR's group delay, so dry and filtered mix
        # without comb filtering.
        buf = np.concatenate([self._mono_hist, mono])
        self._mono_hist = buf[-(_FIR_TAPS - 1):]
        lp = np.convolve(buf, self._h, mode="valid")
        dry = buf[self._lat:self._lat + n]

        gl, gr = pan_gains(pos)
        far_l = np.clip(pos, 0.0, 1.0)        # source on the right -> left ear is far
        far_r = np.clip(-pos, 0.0, 1.0)
        out = np.empty((n, 2))
        for ch, (g, far) in enumerate(((gl, far_l), (gr, far_r))):
            s = self.shadow * far
            ear = g * (dry * (1.0 - s) + lp * s)
            out[:, ch] = self._delay(ch, ear, self.itd_ms * far * self.sr / 1000.0)
        return out.astype(np.float32)

    def _delay(self, ch: int, ear: np.ndarray, delay: np.ndarray) -> np.ndarray:
        """Per-sample fractional delay via linear interpolation of the history."""
        n = len(ear)
        buf = np.concatenate([self._ear_hist[ch], ear])
        self._ear_hist[ch] = buf[-self._dmax:]
        idx = self._dmax + np.arange(n) - np.minimum(delay, self._dmax - 2)
        i0 = np.floor(idx).astype(np.int64)
        fr = idx - i0
        i1 = np.minimum(i0 + 1, len(buf) - 1)
        return buf[i0] * (1.0 - fr) + buf[i1] * fr


# --------------------------------------------------- amplitude modulation ---

class AmpMod:
    """
    Amplitude modulation: the attention-linked effect from the published work.

    g(t) = 1 - d + d * (0.5 + 0.5 sin(2 pi f t)), divided by its mean (1 - d/2)
    so turning the depth up does not also turn the volume down. `alternate` runs
    the right channel half a cycle behind the left, giving a bilateral flutter.
    """

    def __init__(self, sr: int = SAMPLE_RATE, freq: float = 16.0, depth: float = 0.25,
                 mode: str = "inphase"):
        self.sr = sr
        self.freq = freq            # Hz, 8 - 40
        self.depth = depth          # 0 - 0.5
        self.mode = mode            # "inphase" | "alternate"
        self.phase = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        n = len(x)
        if n == 0:
            return x
        d = float(self.depth)
        w = TWO_PI * self.freq / self.sr
        phases = self.phase + w * np.arange(n)
        self.phase = (self.phase + w * n) % TWO_PI
        if d == 0.0:
            return x
        norm = 1.0 - d / 2.0
        left = (1.0 - d + d * (0.5 + 0.5 * np.sin(phases))) / norm
        if self.mode == "alternate":
            right = (1.0 - d + d * (0.5 + 0.5 * np.sin(phases + math.pi))) / norm
        else:
            right = left
        env = np.stack([left, right], axis=1).astype(np.float32)
        return x * env


# ------------------------------------------------------------------ safety ---

class Limiter:
    """
    Memoryless soft limiter. Transparent below the knee, then a tanh shoulder
    that approaches the ceiling asymptotically, so the output never exceeds it.

    The ceiling (-0.5 dBFS) and knee are set so normal music passes untouched: an
    earlier -3 dBFS ceiling squashed the peaks of any loud, modern master (3.8% of
    samples on one test track) even with every effect off.
    """

    def __init__(self, ceiling_db: float = -0.5, knee: float = 0.9):
        self.ceiling = 10.0 ** (ceiling_db / 20.0)
        self.knee = knee

    def process(self, x: np.ndarray) -> np.ndarray:
        c = self.ceiling
        t = c * self.knee
        a = np.abs(x)
        over = a > t
        if not over.any():
            return x
        y = x.copy()
        y[over] = np.sign(x[over]) * (t + (c - t) * np.tanh((a[over] - t) / (c - t)))
        return y


class Chain:
    """
    The full signal path: pan -> amplitude modulation -> volume -> limiter,
    with a fade-in at the start and an optional fade-out for session end.

    Parameters live on the processor objects and can be changed from another
    thread between blocks; volume is ramped across each block to avoid zipper
    noise.
    """

    def __init__(self, sr: int = SAMPLE_RATE, fade_in_s: float = 3.0):
        self.sr = sr
        self.pan = Pan(sr)
        self.am = AmpMod(sr)
        self.limiter = Limiter()
        self.volume = 1.0           # a trim, 0 - 1, unity by default: the system volume is the
                                    # real control, and a built-in cut made Bifrost sound quieter
        self.pan_enabled = True
        self.am_enabled = True
        self.fade_in_s = fade_in_s
        self._volume_prev = self.volume
        self._pos = 0               # samples processed, for the fade-in
        self._fade_out_left: int | None = None
        self._fade_out_total = 1

    def fade_out(self, seconds: float) -> None:
        self._fade_out_total = max(1, int(seconds * self.sr))
        self._fade_out_left = self._fade_out_total

    def reset_fade(self) -> None:
        """Cancel a pending or completed fade-out (session ended early)."""
        self._fade_out_left = None

    def reset_stream(self) -> None:
        """Prepare for a new track: fade in again and clear any fade-out."""
        self._pos = 0
        self._fade_out_left = None
        self._volume_prev = self.volume

    @property
    def finished(self) -> bool:
        return self._fade_out_left == 0

    def process(self, x: np.ndarray) -> np.ndarray:
        n = len(x)
        if n == 0:
            return x
        y = x
        if self.pan_enabled:
            y = self.pan.process(y)
        if self.am_enabled:
            y = self.am.process(y)

        vol = np.linspace(self._volume_prev, self.volume, n, dtype=np.float32)
        self._volume_prev = self.volume
        gain = vol
        if self.fade_in_s > 0 and self._pos < self.fade_in_s * self.sr:
            ramp = (self._pos + np.arange(n)) / (self.fade_in_s * self.sr)
            gain = gain * np.clip(ramp, 0.0, 1.0).astype(np.float32)
        if self._fade_out_left is not None:
            left = self._fade_out_left - np.arange(n)
            gain = gain * np.clip(left / self._fade_out_total, 0.0, 1.0).astype(np.float32)
            self._fade_out_left = max(0, self._fade_out_left - n)
        self._pos += n
        return self.limiter.process(y * gain[:, None])
