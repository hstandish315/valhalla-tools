"""
Live mode: apply the sweep and pulse to any app's audio (Spotify, a browser,
a video) as it plays, without downloading or saving anything.

    app ──▶ [Bifrost Live] virtual sink ──monitor──▶ pw-record ──▶ Chain ──▶ pw-play ──▶ speakers

It is a real-time effect, in the same family as an equaliser. Nothing is
extracted from the app and nothing is written to disk.

Every PipeWire behaviour this relies on was measured first, on Ubuntu 26.04 with
PipeWire 1.6: a `pw-cli` child can host a null sink and the sink disappears when
that child exits; `pw-metadata <node> target.object <sink>` moves a running
stream onto it and `-d` puts it back on the real output; `pw-record --raw
--target <sink> -P stream.capture.sink=true` captures the sink's monitor (a
440 Hz tone came back at 443 Hz). `pactl` is not assumed to be installed, so
only PipeWire's own tools are used.

Two things real use taught us, both handled here:

* **An app is not a stream.** Spotify opens a *new* PipeWire stream for each
  track or restart, and its streams don't even agree on a name (one says
  "Spotify", another "Chromium"; only the binary and the Flatpak ID identify
  it). So "send this app" is remembered by identity tokens, and any later
  stream sharing one is moved the instant it appears.
* **Never capture ourselves.** Our own player's stream has no process ID in the
  graph, so it is recognised by its node name instead; sending it through the
  sink would be a feedback loop.
"""

from __future__ import annotations

import codecs
import json
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional

from .dsp import SAMPLE_RATE, Chain
from .engine import OWN_NODE_NAME, Engine, PwPlaySink

SINK = "bifrost_live"
REQUIRED_TOOLS = ("pw-cli", "pw-dump", "pw-metadata", "pw-record", "pw-play")
CAPTURE_NODE_NAME = "bifrost-capture"
_CREATE_SINK = ('create-node adapter { factory.name=support.null-audio-sink '
                f'node.name={SINK} node.description="Bifrost Live" media.class=Audio/Sink '
                'object.linger=false audio.position=[ FL FR ] monitor.channel-volumes=true }\n')

# Words that say nothing about *which* app a stream belongs to.
_GENERIC = {"chromium", "chrome", "playback", "audio", "stream", "client", "app", "desktop",
            "com", "org", "io", "net", "unknown", "-", ""}


class LiveError(Exception):
    """A failure whose message is safe to show the user."""


@dataclass
class Stream:
    node_id: int
    app: str                                  # what to show the user
    binary: str = ""
    media: str = ""
    pid: int = 0
    tokens: frozenset = field(default_factory=frozenset)   # identity words, see tokens_for()
    routed: bool = False                      # currently sent to Bifrost by us


def missing_tools(which: Callable = shutil.which) -> list[str]:
    return [t for t in REQUIRED_TOOLS if which(t) is None]


# ---------------------------------------------------------- pw-dump parsing ---

def _nodes(dump: list[dict]):
    for o in dump:
        if o.get("type") == "PipeWire:Interface:Node" and o.get("info"):
            yield o["id"], o["info"].get("props") or {}


def is_own(props: dict) -> bool:
    """Bifrost's own streams (never to be captured, or we would hear ourselves)."""
    name = str(props.get("node.name") or "")
    return name.startswith("bifrost") or props.get("application.name") == "Bifrost"


def tokens_for(props: dict) -> frozenset:
    """
    Identity words for a stream: binary, application name, and the parts of a
    Flatpak/portal app ID, minus generic words. Two streams are the same app if
    they share any token: Spotify's streams call themselves "Spotify" and
    "Chromium" but both carry `spotify`.
    """
    raw = [props.get("application.process.binary"), props.get("application.name")]
    raw += str(props.get("pipewire.access.portal.app_id") or "").split(".")
    return frozenset(t for t in (str(x).strip().lower() for x in raw if x) if t not in _GENERIC)


def label_for(props: dict) -> str:
    for cand in (props.get("application.process.binary"), props.get("application.name"),
                 *reversed(str(props.get("pipewire.access.portal.app_id") or "").split("."))):
        if cand and str(cand).strip().lower() not in _GENERIC:
            return str(cand)[:1].upper() + str(cand)[1:]
    return props.get("application.name") or props.get("node.name") or "unknown app"


def parse_streams(dump: list[dict], exclude_pids=()) -> list[Stream]:
    """Apps currently playing audio (output streams), excluding Bifrost's own."""
    out = []
    for node_id, p in _nodes(dump):
        if p.get("media.class") != "Stream/Output/Audio" or is_own(p):
            continue
        pid = p.get("application.process.id") or 0
        if pid and pid in exclude_pids:
            continue
        out.append(Stream(node_id, label_for(p), p.get("application.process.binary") or "",
                          p.get("media.name") or "", int(pid), tokens_for(p)))
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


def iter_json_docs(read: Callable[[], bytes]) -> Iterator:
    """Decode a stream of concatenated JSON documents (what `pw-dump -m` prints)."""
    dec, text, buf = json.JSONDecoder(), codecs.getincrementaldecoder("utf-8")("replace"), ""
    while True:
        chunk = read()
        if not chunk:
            return
        buf += text.decode(chunk)
        while True:
            buf = buf.lstrip()
            if not buf:
                break
            try:
                obj, end = dec.raw_decode(buf)
            except ValueError:
                break                               # incomplete document: wait for more bytes
            yield obj
            buf = buf[end:]


# ------------------------------------------------------------------- router ---

class LiveRouter:
    """Owns the virtual sink, the streams we've moved onto it, and which apps to keep following."""

    def __init__(self, runner: Callable = subprocess.run, popen: Callable = subprocess.Popen,
                 sleep: Callable = time.sleep, timeout: float = 4.0):
        self._run, self._popen, self._sleep, self._timeout = runner, popen, sleep, timeout
        self._cli = None
        self._watcher: Optional[subprocess.Popen] = None
        self._watch_thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self.moved: set[int] = set()
        self.sticky: dict[str, frozenset] = {}            # label -> identity tokens
        self.on_change: Optional[Callable[[], None]] = None   # called (from a worker thread) after an auto-move

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
                    self._start_watcher()
                    return
            raise LiveError("PipeWire didn't create the Bifrost Live sink.")
        except (OSError, LiveError) as exc:
            self._reap()
            raise exc if isinstance(exc, LiveError) else LiveError(f"Could not create the sink: {exc}")

    def _reap(self) -> None:
        self._stop_watcher()
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
        self._stop_watcher()
        self.restore_all()
        self.sticky.clear()
        self._reap()

    # -- streams
    def streams(self, exclude_pids=()) -> list[Stream]:
        found = parse_streams(self.dump(), exclude_pids)
        with self._lock:
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
        with self._lock:
            self.moved.add(node_id)

    def restore(self, node_id: int) -> None:
        self._run(["pw-metadata", "-d", str(node_id), "target.object"],
                  capture_output=True, text=True, timeout=10)
        with self._lock:
            self.moved.discard(node_id)

    def restore_all(self) -> None:
        for node_id in list(self.moved):
            try:
                self.restore(node_id)
            except (subprocess.SubprocessError, OSError):
                with self._lock:
                    self.moved.discard(node_id)       # the stream has gone; nothing to put back

    # -- "keep sending this app" (an app is not a stream)
    def send(self, stream: Stream, streams: Optional[list[Stream]] = None) -> None:
        """Send an app through Bifrost and keep sending its future streams."""
        # Register the app first: a new stream opening between the move and the
        # registration would otherwise be missed by the watcher.
        with self._lock:
            self.sticky[stream.app] = stream.tokens
        try:
            self.move(stream.node_id)
        except LiveError:
            with self._lock:
                self.sticky.pop(stream.app, None)
            raise
        for other in streams or []:                  # other streams of the same app that are playing now
            if other.node_id != stream.node_id and other.tokens & stream.tokens and \
                    other.node_id not in self.moved:
                self.move(other.node_id)

    def release(self, stream: Stream, streams: Optional[list[Stream]] = None) -> None:
        """Return an app to the speakers and stop following it."""
        with self._lock:
            self.sticky.pop(stream.app, None)
        self.restore(stream.node_id)
        for other in streams or []:
            if other.tokens & stream.tokens and other.node_id in self.moved:
                self.restore(other.node_id)

    def forget(self, label: str) -> None:
        with self._lock:
            self.sticky.pop(label, None)

    def _is_followed(self, s: Stream) -> bool:
        with self._lock:
            return any(s.tokens & t for t in self.sticky.values())

    def follow(self, streams: list[Stream]) -> list[int]:
        """Move any playing stream of a followed app that isn't on Bifrost yet. Returns the ids moved."""
        moved = []
        for s in streams:
            with self._lock:
                skip = s.node_id in self.moved
            if not skip and self._is_followed(s):
                try:
                    self.move(s.node_id)
                    moved.append(s.node_id)
                except LiveError:
                    pass
        return moved

    # -- the graph watcher: react the instant a followed app opens a new stream
    def _start_watcher(self) -> None:
        try:
            self._watcher = self._popen(["pw-dump", "-m"], stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, bufsize=0)
        except OSError:
            self._watcher = None                      # polling in the UI still catches new streams
            return
        proc = self._watcher
        self._watch_thread = threading.Thread(target=self._watch, args=(proc,), daemon=True,
                                              name="bifrost-watch")
        self._watch_thread.start()

    def _watch(self, proc) -> None:
        try:
            for doc in iter_json_docs(lambda: proc.stdout.read(65536)):
                self.on_graph_event(doc if isinstance(doc, list) else [doc])
        except (OSError, ValueError):
            return

    def on_graph_event(self, objs: list[dict]) -> None:
        """One batch of `pw-dump -m` updates: follow new streams, forget vanished ones."""
        for o in objs:
            if not isinstance(o, dict):
                continue
            if "info" in o and o["info"] is None:     # object removed
                with self._lock:
                    self.moved.discard(o.get("id"))
                continue
            moved = self.follow(parse_streams([o]))
            if moved and self.on_change:
                self.on_change()

    def _stop_watcher(self) -> None:
        proc, self._watcher = self._watcher, None
        if proc is not None and proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        if proc is not None and proc.stdout:
            try:
                proc.stdout.close()
            except OSError:
                pass
        t, self._watch_thread = self._watch_thread, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2)

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

    OUTPUT_LATENCY = "50ms"        # the player's own buffer; the cushion below does the real work
    PRIME_MS = 100                 # silence pre-filled ahead of the live audio (see Engine.prime_ms)

    def __init__(self, chain: Chain, router: LiveRouter, on_end=None, sink_factory=None):
        self.router = router
        super().__init__(chain, sink_factory or self._real_sink, on_end)
        self.prime_ms = self.PRIME_MS

    def _real_sink(self) -> PwPlaySink:
        # The target is explicit so we never feed our own sink.
        return PwPlaySink(latency=self.OUTPUT_LATENCY, target=self.router.output_sink())

    def _spawn_source(self) -> subprocess.Popen:
        return subprocess.Popen(
            ["pw-record", "--raw", "--target", SINK, "-P",
             f"{{ stream.capture.sink=true node.name={CAPTURE_NODE_NAME} }}",
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
