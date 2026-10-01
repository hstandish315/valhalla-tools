"""
Presets: named starting points for the sweep and the pulse.

Values stay inside the ranges the UI knobs allow and the DSP is tuned for.
Amplitude modulation sits at 16 Hz for the focus presets, the rate used in the
published attention study; "Calm" drops it to 10 Hz. "Reset" is the slow,
EMDR-style ping-pong with the pulse off, for winding down rather than working.

These are starting points, not prescriptions - every parameter stays live.
"""

from __future__ import annotations

from dataclasses import dataclass

from .dsp import Chain


@dataclass(frozen=True)
class Preset:
    name: str
    blurb: str
    rate: float          # sweep cycles per second
    width: float         # 0 - 1
    shape: str           # sine | triangle | pingpong
    realism: float       # 0 - 1, scales ITD and head shadow together
    am_freq: float       # Hz
    am_depth: float      # 0 - 0.5
    am_mode: str         # inphase | alternate
    pan_on: bool = True
    am_on: bool = True


PRESETS: tuple[Preset, ...] = (
    Preset("Focus", "steady sweep, 16 Hz pulse", 0.25, 0.8, "sine", 0.8, 16.0, 0.25, "inphase"),
    Preset("Deep Work", "slow, narrow, stronger pulse", 0.12, 0.6, "triangle", 0.8, 16.0, 0.35, "inphase"),
    Preset("Calm", "wide drift, gentle 10 Hz pulse", 0.08, 1.0, "sine", 1.0, 10.0, 0.15, "inphase"),
    Preset("Reset", "ping-pong sweep, pulse off", 0.5, 1.0, "pingpong", 0.6, 16.0, 0.0, "inphase",
           pan_on=True, am_on=False),
)

MAX_ITD_MS = 0.7        # a human head; the DSP buffer allows up to 1.0


def apply(chain: Chain, p: Preset) -> None:
    chain.pan.rate, chain.pan.width, chain.pan.shape = p.rate, p.width, p.shape
    set_realism(chain, p.realism)
    chain.am.freq, chain.am.depth, chain.am.mode = p.am_freq, p.am_depth, p.am_mode
    chain.pan_enabled, chain.am_enabled = p.pan_on, p.am_on


def set_realism(chain: Chain, r: float) -> None:
    """One knob for both spatial cues: inter-ear delay and far-ear dulling."""
    r = max(0.0, min(1.0, r))
    chain.pan.itd_ms = MAX_ITD_MS * r
    chain.pan.shadow = 0.7 * r


def realism_of(chain: Chain) -> float:
    return min(1.0, chain.pan.itd_ms / MAX_ITD_MS)
