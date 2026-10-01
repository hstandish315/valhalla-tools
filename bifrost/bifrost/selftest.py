"""
End-to-end self test against the real stack: ffmpeg -> DSP -> pw-play.

Run:  ./bifrost-audio selftest [--live]

Checks, in order: required tools exist, a generated tone plays through a real
`pw-play` child, that child actually appears while playing (live process state,
not just "no exception"), PortAudio never loaded, and - with --live - that
Openverse and the Internet Archive both answer a real query.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time

from . import engine
from .dsp import SAMPLE_RATE, Chain

OK, BAD = "\033[32mok\033[0m", "\033[31mFAIL\033[0m"


def check(name: str, passed: bool, detail: str = "") -> bool:
    print(f"  [{OK if passed else BAD}] {name}" + (f"  {detail}" if detail else ""))
    return passed


def _children_named(pid: int, name: str) -> list[int]:
    out = subprocess.run(["pgrep", "-P", str(pid), "-x", name], capture_output=True, text=True)
    return [int(x) for x in out.stdout.split()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bifrost selftest")
    ap.add_argument("--live", action="store_true", help="also query the real Openverse / Archive APIs")
    args = ap.parse_args(argv)
    ok = True
    print("Bifrost selftest")

    for tool in ("ffmpeg", "ffprobe", "pw-play"):
        ok &= check(f"tool: {tool}", shutil.which(tool) is not None)

    tmp = tempfile.mkdtemp(prefix="bifrost-selftest-")
    try:
        tone = os.path.join(tmp, "tone.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"sine=frequency=330:sample_rate={SAMPLE_RATE}:duration=1.5", tone], check=True)
        chain = Chain(fade_in_s=0.2)
        chain.volume = 0.15                      # a selftest should not be loud
        chain._volume_prev = 0.15
        seen_sink: list[engine.PwPlaySink] = []

        def factory():
            s = engine.PwPlaySink()
            seen_sink.append(s)
            return s

        eng = engine.Engine(chain, sink_factory=factory)
        eng.play(tone)
        time.sleep(0.6)
        ok &= check("playback running", eng.state == "playing", f"state={eng.state}")
        ok &= check("pw-play child is alive while playing",
                    bool(seen_sink) and seen_sink[0]._proc.poll() is None,
                    f"pid={seen_sink[0].pid if seen_sink else '?'}")
        eng.join(15)
        ok &= check("track ended cleanly", eng.state == "ended" and eng.error is None,
                    f"state={eng.state} error={eng.error}")
        ok &= check("pw-play exited after the track", bool(seen_sink) and seen_sink[0]._proc.poll() == 0,
                    f"rc={seen_sink[0]._proc.poll() if seen_sink else '?'}")
        ok &= check("PortAudio not loaded", "sounddevice" not in sys.modules)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # Live mode prerequisites, and the CD path. Neither plays anything or writes anything.
    from . import cd, live
    missing = live.missing_tools()
    ok &= check("live mode tools (PipeWire)", not missing, "missing: " + ", ".join(missing) if missing else "")
    drives = cd.find_drives()
    if not drives:
        print("  [skip] optical drive  none attached; the CD path was not exercised")
    else:
        try:
            toc = cd.read_toc(drives[0].device)
            ok &= check("optical drive + disc", True,
                        f"{drives[0].label}: {len(toc.audio_tracks)} audio tracks, id {cd.disc_id(toc)}")
        except cd.CDError as exc:
            print(f"  [skip] optical drive  {drives[0].label}: {exc}")

    if args.live:
        from .sources import archive, openverse
        for name, fn in (("openverse", openverse.search), ("internet archive", archive.search)):
            try:
                tracks, total = fn("ambient")
                ok &= check(f"live: {name}", bool(tracks), f"{len(tracks)} usable / ~{total:,} total")
            except Exception as exc:
                ok &= check(f"live: {name}", False, str(exc))

    print("PASS" if ok else "FAILED")
    return 0 if ok else 1
