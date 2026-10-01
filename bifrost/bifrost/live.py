"""
Live mode: apply the sweep and pulse to any app's audio (Spotify, a browser,
a video) as it plays, without downloading or saving anything.

    app ──▶ [Bifrost Live] virtual sink ──monitor──▶ pw-record ──▶ Chain ──▶ pw-play ──▶ speakers

It is a real-time effect, in the same family as an equaliser. Nothing is
extracted from the app and nothing is written to disk.

Every PipeWire behaviour this relies on was measured first, on Ubuntu 26.04 with PipeWire 1.6:
a `pw-cli` child can host a null sink and the sink disappears when that child
exits (so a crash cannot strand audio); `pw-metadata <node> target.object <sink>`
moves a running stream onto it and `-d` puts it back on the real output;
`pw-record --raw --target <sink> -P stream.capture.sink=true` captures the sink's
monitor (a 440 Hz tone came back at 443 Hz). `pactl` is not assumed to be
installed, so only PipeWire's own tools are used.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .dsp import SAMPLE_RATE, Chain
from .engine import Engine, PwPlaySink

SINK = "bifrost_live"
REQUIRED_TOOLS = ("pw-cli", "pw-dump", "pw-metadata", "pw-record", "pw-play")
_CREATE_SINK = ('create-node adapter { factory.name=support.null-audio-sink '
                f'node.name={SINK} node.description="Bifrost Live" media.class=Audio/Sink '
                'object.linger=false audio.position=[ FL FR ] monitor.channel-volumes=true }\n')


class LiveError(Exception):
    """A failure whose message is safe to show the user."""


@dataclass
class Stream:
    node_id: int
    app: str
    binary: str = ""
    media: str = ""
    pid: int = 0
    routed: bool = False            # currently sent to Bifrost by us


def missing_tools(which: Callable = shutil.which) -> list[str]:
    return [t for t in REQUIRED_TOOLS if which(t) is None]


# ---------------------------------------------------------- pw-dump parsing ---

def _nodes(dump: list[dict]):
    for o in dump:
        if o.get("type") == "PipeWire:Interface:Node":
            yield o["id"], (o.get("info") or {}).get("props") or {}


def parse_streams(dump: list[dict], exclude_pids=()) -> list[Stream]:
    """Apps currently playing audio (output streams), excluding our own player."""
    out = []
    for node_id, p in _nodes(dump):
        if p.get("media.class") != "Stream/Output/Audio":
            continue
        pid = p.get("application.process.id") or 0
        if pid and pid in exclude_pids:
            continue
        out.append(Stream(node_id, p.get("application.name") or p.get("node.name") or "unknown app",
                          p.get("application.process.binary") or "", p.get("media.name") or "", int(pid)))
    return out


def parse_sinks(dump: list[dict]) -> list[str]:
    return [p["node.name"] for _, p in _nodes(dump)
            if p.get("media.class") == "Audio/Sink" and p.get("node.name")]


def parse_default_sink(dump: list[dict]) -> Optional[str]:
    for o in dump:
        if o.get("type") == "PipeWire:Interface:Metadata" and \
                (o.get("props") or {}).get("metadata.name") == "default":
            for m in o.get("metadata") or []:
                if m.get("key") == "default.audio.sink":
                    value = m.get("value")
                    return value.get("name") if isinstance(value, dict) else None
    return None


# ------------------------------------------------------------------- router ---

class LiveRouter:
    """Owns the virtual sink and the streams we've moved onto it."""

    def __init__(self, runner: Callable = subprocess.run, popen: Callable = subprocess.Popen,
                 sleep: Callable = time.sleep, timeout: float = 4.0):
        self._run, self._popen, self._sleep, self._timeout = runner, popen, sleep, timeout
        self._cli = None
        self.moved: set[int] = set()

    # -- graph access
    def dump(self) -> list[dict]:
        try:
            out = self._run(["pw-dump"], capture_output=True, text=True, timeout=10, check=True).stdout
            return json.loads(out)
        except (subprocess.SubprocessError, OSError, ValueError) as exc:
            raise LiveError(f"Could not read the audio graph: {exc}") from exc

    def sink_exists(self) -> bool:
        return SINK in parse_sinks(self.dump())

    @property
    def active(self) -> bool:
        return self._cli is not None

    # -- the virtual sink
    def create_sink(self) -> None:
        if self._cli is not None:
            return
        if self.sink_exists():
            raise LiveError("A Bifrost Live sink already exists; is Live mode running in another window?")
        self._cli = self._popen(["pw-cli"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, text=True)
        try:
            self._cli.stdin.write(_CREATE_SINK)
            self._cli.stdin.flush()
            waited = 0.0
            while waited < self._timeout:
                self._sleep(0.2)
                waited += 0.2
                if self.sink_exists():
                    return
            raise LiveError("PipeWire didn't create the Bifrost Live sink.")
        except (OSError, LiveError) as exc:
            self._reap()
            raise exc if isinstance(exc, LiveError) else LiveError(f"Could not create the sink: {exc}")

    def _reap(self) -> None:
        cli, self._cli = self._cli, None
        if cli is None:
            return
        try:
            if cli.stdin:
                cli.stdin.close()
        except OSError:
            pass
        if cli.poll() is None:
            cli.terminate()
            try:
                cli.wait(timeout=3)
            except subprocess.TimeoutExpired:
                cli.kill()
                cli.wait()

    def destroy_sink(self) -> None:
        """Return every stream we moved to the real output, then remove the sink."""
        self.restore_all()
        self._reap()

    # -- streams
    def streams(self, exclude_pids=()) -> list[Stream]:
        found = parse_streams(self.dump(), exclude_pids)
        for s in found:
            s.routed = s.node_id in self.moved
        return found

    def move(self, node_id: int) -> None:
        if self._cli is None:
            raise LiveError("Start live mode first.")
        res = self._run(["pw-metadata", str(node_id), "target.object", SINK, "Spa:String"],
                        capture_output=True, text=True, timeout=10)
        if res.returncode != 0:
            raise LiveError("Could not move that app: " + (res.stderr.strip() or "unknown error"))
        self.moved.add(node_id)

    def restore(self, node_id: int) -> None:
        self._run(["pw-metadata", "-d", str(node_id), "target.object"],
                  capture_output=True, text=True, timeout=10)
        self.moved.discard(node_id)

    def restore_all(self) -> None:
        for node_id in list(self.moved):
            try:
                self.restore(node_id)
            except (subprocess.SubprocessError, OSError):
                self.moved.discard(node_id)         # the stream has gone; nothing to put back

    # -- where the processed audio goes
    def output_sink(self) -> str:
        """A real output device. Never our own sink: that would be a feedback loop."""
        dump = self.dump()
        default = parse_default_sink(dump)
        if default and default != SINK:
            return default
        others = [n for n in parse_sinks(dump) if n != SINK]
        if not others:
            raise LiveError("No audio output device found.")
        return others[0]


# ------------------------------------------------------------------- engine ---

class LiveEngine(Engine):
    """The same DSP chain as file playback, fed by capture instead of a decoder."""

    def __init__(self, chain: Chain, router: LiveRouter, on_end=None, sink_factory=None):
        self.router = router
        super().__init__(chain, sink_factory or self._real_sink, on_end)

    def _real_sink(self) -> PwPlaySink:
        # 50 ms keeps the added delay small; the target is explicit so we never feed ourselves.
        return PwPlaySink(latency="50ms", target=self.router.output_sink())

    def _spawn_source(self) -> subprocess.Popen:
        return subprocess.Popen(
            ["pw-record", "--raw", "--target", SINK, "-P", "stream.capture.sink=true",
             "--rate", str(SAMPLE_RATE), "--channels", "2", "--format", "f32",
             "--latency", "20ms", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)

    def start(self) -> None:
        self.play("(live)")

    def seek(self, seconds: float) -> None:        # a live stream has no timeline
        return

    @property
    def own_pids(self) -> tuple[int, ...]:
        pid = getattr(self.sink, "pid", None)
        return (pid,) if pid else ()
