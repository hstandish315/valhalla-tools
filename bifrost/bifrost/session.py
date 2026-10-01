"""
Focus session timer.

A session is a fixed length, with a slow fade-out over its last seconds so it
ends gently instead of cutting off. The clock is injectable so the logic can be
tested without waiting.
"""

from __future__ import annotations

import time
from typing import Callable


class Session:
    def __init__(self, minutes: float = 25.0, fade_s: float = 20.0,
                 clock: Callable[[], float] = time.monotonic):
        self.minutes = minutes
        self.fade_s = fade_s
        self._clock = clock
        self._t0: float | None = None
        self._fading = False
        self._done = False

    @property
    def total(self) -> float:
        return self.minutes * 60.0

    @property
    def running(self) -> bool:
        return self._t0 is not None and not self._done

    def start(self) -> None:
        self._t0 = self._clock()
        self._fading = self._done = False

    def stop(self) -> None:
        self._t0 = None
        self._fading = self._done = False

    def elapsed(self) -> float:
        return 0.0 if self._t0 is None else min(self.total, self._clock() - self._t0)

    def remaining(self) -> float:
        return self.total if self._t0 is None else max(0.0, self.total - self.elapsed())

    def progress(self) -> float:
        return 0.0 if self.total <= 0 else self.elapsed() / self.total

    def poll(self) -> str:
        """
        Call regularly. Returns "fade" once when the fade-out window opens and
        "done" once when time is up; otherwise an empty string.
        """
        if not self.running:
            return ""
        rem = self.remaining()
        if rem <= 0:
            self._done = True
            return "done"
        if not self._fading and rem <= min(self.fade_s, self.total):
            self._fading = True
            return "fade"
        return ""

    def label(self) -> str:
        s = int(round(self.remaining()))
        return f"{s // 60:02d}:{s % 60:02d}"
