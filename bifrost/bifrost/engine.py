"""
Bifrost audio engine: ffmpeg -> DSP chain -> pw-play.

Why subprocesses on both ends rather than a Python audio library: PortAudio
segfaulted in testing on PipeWire's ALSA shim, and a subprocess
that owns the device has no callback to race. ffmpeg decodes anything it can
read to raw float32 PCM on a pipe; the DSP chain runs on ~20 ms blocks in one
worker thread; the result goes to `pw-play --raw` on stdin. Back-pressure from
the pipe paces the whole loop, so there is no timer to drift.

`import sounddevice` must never appear in this package.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from typing import Callable, Optional, Protocol

import numpy as np

from .dsp import SAMPLE_RATE, Chain

CHANNELS = 2
BLOCK_FRAMES = 882                      # 20 ms at 44.1 kHz
BYTES_PER_FRAME = CHANNELS * 4          # float32
OWN_NODE_NAME = "bifrost-output"        # our player's PipeWire node; Live mode must never capture it
STALL_MS = 60.0                         # a gap this long between blocks risks an audible glitch


class Sink(Protocol):
    def write(self, data: bytes) -> None: ...
    def close(self) -> None: ...


class PwPlaySink:
    """Raw float32 stereo into a `pw-play` child's stdin."""

    def __init__(self, latency: str = "100ms", target: Optional[str] = None):
        target_args = ["--target", target] if target else []
        self._proc = subprocess.Popen(
            ["pw-play", "--raw", "--rate", str(SAMPLE_RATE), "--channels", "2",
             "--format", "f32", "--latency", latency, *target_args,
             "--media-role", "Music", "-P", f"{{ node.name={OWN_NODE_NAME} application.name=Bifrost }}", "-"],
            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

    @property
    def pid(self) -> int:
        return self._proc.pid

    def write(self, data: bytes) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(data)
        self._proc.stdin.flush()

    def close(self) -> None:
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            self._proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()


class MemorySink:
    """Collects output; used by tests and by offline export."""

    def __init__(self):
        self.chunks: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.chunks.append(data)

    def close(self) -> None:
        pass

    def audio(self) -> np.ndarray:
        raw = b"".join(self.chunks)
        return np.frombuffer(raw, dtype=np.float32).reshape(-1, CHANNELS)


def probe_duration(path: str) -> float:
    """Duration in seconds via ffprobe, or 0.0 if it cannot be determined."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", path],
            capture_output=True, text=True, timeout=15, check=True).stdout
        return float(json.loads(out)["format"]["duration"])
    except (subprocess.SubprocessError, KeyError, ValueError, OSError):
        return 0.0


def is_audio_file(path: str) -> bool:
    """True if ffprobe finds an audio stream. Used to vet downloads."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_type", "-of", "json", path],
            capture_output=True, text=True, timeout=15, check=True).stdout
        return bool(json.loads(out).get("streams"))
    except (subprocess.SubprocessError, ValueError, OSError):
        return False


def _spawn_decoder(path: str, start: float) -> subprocess.Popen:
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", path, "-vn", "-f", "f32le", "-ac", str(CHANNELS),
            "-ar", str(SAMPLE_RATE), "-"]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            bufsize=0)


def _read_block(stream, nbytes: int) -> bytes:
    """
    Fill one block from an unbuffered pipe.

    With bufsize=0 the stream is a raw FileIO and read(n) is a single read(2)
    that may return fewer bytes than asked (a classic short-read bug). Loop until
    the block is full; only an empty read means end of stream.
    """
    buf = bytearray()
    while len(buf) < nbytes:
        piece = stream.read(nbytes - len(buf))
        if not piece:
            break
        buf += piece
    return bytes(buf)


class Engine:
    """
    One playback session at a time. Thread model: a single worker thread owns
    the decoder, the chain and the sink; the UI thread only flips flags and
    parameters, which the worker reads between blocks.
    """

    def __init__(self, chain: Optional[Chain] = None,
                 sink_factory: Callable[[], Sink] = PwPlaySink,
                 on_end: Optional[Callable[[], None]] = None):
        self.chain = chain or Chain()
        self._sink_factory = sink_factory
        self.on_end = on_end
        self.state = "stopped"          # stopped | playing | paused | ended
        self.levels = (0.0, 0.0)        # last block peak L/R, for the UI meters
        self.error: Optional[str] = None
        self._path: Optional[str] = None
        self._start = 0.0
        self._frames = 0
        self._run = threading.Event()   # set = unpaused
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._decoder: Optional[subprocess.Popen] = None
        self.sink: Optional[Sink] = None
        # Silence written before the first real block. A real-time source (live capture)
        # delivers each block just as it is needed, so the player has zero slack and any
        # tiny delay starves it. Measured: with no pre-fill PipeWire counted 578 underruns
        # in 25 s; with 60 ms or more, none. File playback is paced by the player itself
        # (pipe back-pressure) and needs no cushion.
        self.prime_ms = 0
        self.stats = {"blocks": 0, "stalls": 0, "max_stall_ms": 0.0}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ control ---

    def play(self, path: str, start: float = 0.0) -> None:
        self.stop()
        self._path = path
        self._start = max(0.0, start)
        self._frames = 0
        self.error = None
        self._stop.clear()
        self._run.set()
        self.state = "playing"
        self._thread = threading.Thread(target=self._worker, name="bifrost-engine",
                                        daemon=True)
        self._thread.start()

    def pause(self) -> None:
        if self.state == "playing":
            self._run.clear()
            self.state = "paused"

    def resume(self) -> None:
        if self.state == "paused":
            self._run.set()
            self.state = "playing"

    def seek(self, seconds: float) -> None:
        if self._path and self.state in ("playing", "paused"):
            was_paused = self.state == "paused"
            self.play(self._path, seconds)
            if was_paused:
                self.pause()

    def stop(self) -> None:
        self._stop.set()
        self._run.set()                 # release a paused worker so it can exit
        self._kill_decoder()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=3.0)
        self._thread = None
        self.state = "stopped"
        self.levels = (0.0, 0.0)

    def join(self, timeout: Optional[float] = None) -> None:
        """Block until the current track ends (for tests and offline use)."""
        t = self._thread
        if t is not None:
            t.join(timeout)

    @property
    def position(self) -> float:
        return self._start + self._frames / SAMPLE_RATE

    # ------------------------------------------------------------- worker ---

    def _spawn_source(self) -> subprocess.Popen:
        """Start whatever produces raw f32 stereo PCM. File playback decodes with ffmpeg."""
        return _spawn_decoder(self._path, self._start)

    def _kill_decoder(self) -> None:
        with self._lock:
            d = self._decoder
        # kill, not terminate: a decoder blocked writing into a full pipe does
        # not act on SIGTERM until the pipe drains, and nothing it still holds
        # is worth flushing once we are discarding the stream.
        if d is not None and d.poll() is None:
            d.kill()

    def _worker(self) -> None:
        sink = None
        try:
            sink = self._sink_factory()
            self.sink = sink
            if self.prime_ms:
                sink.write(b"\0" * (int(SAMPLE_RATE * self.prime_ms / 1000) * BYTES_PER_FRAME))
            dec = self._spawn_source()
            with self._lock:
                self._decoder = dec
            assert dec.stdout is not None
            nbytes = BLOCK_FRAMES * BYTES_PER_FRAME
            ended_naturally = False
            last = None
            self.stats = {"blocks": 0, "stalls": 0, "max_stall_ms": 0.0}
            while not self._stop.is_set():
                if not self._run.is_set():
                    last = None                      # a pause is not a stall
                self._run.wait()
                if self._stop.is_set():
                    break
                raw = _read_block(dec.stdout, nbytes)
                if not raw:
                    ended_naturally = True
                    break
                now = time.monotonic()
                if last is not None:
                    gap = (now - last) * 1000.0
                    if gap > STALL_MS:
                        self.stats["stalls"] += 1
                    self.stats["max_stall_ms"] = max(self.stats["max_stall_ms"], gap)
                last = now
                self.stats["blocks"] += 1
                usable = len(raw) - len(raw) % BYTES_PER_FRAME
                block = np.frombuffer(raw[:usable], dtype=np.float32).reshape(-1, CHANNELS)
                out = self.chain.process(block)
                self.levels = (float(np.abs(out[:, 0]).max()),
                               float(np.abs(out[:, 1]).max()))
                sink.write(np.ascontiguousarray(out, dtype=np.float32).tobytes())
                self._frames += len(block)
                if self.chain.finished:
                    ended_naturally = True
                    break
            if dec.poll() is None:
                dec.kill()
            dec.wait(timeout=2.0)
            dec.stdout.close()
            if ended_naturally and not self._stop.is_set():
                self.state = "ended"
                if self.on_end:
                    self.on_end()
        except (BrokenPipeError, OSError) as exc:
            self.error = f"audio output failed: {exc}"
            self.state = "stopped"
        finally:
            with self._lock:
                self._decoder = None
            if sink is not None:
                sink.close()


def render_offline(chain: Chain, path: str, out_path: str, seconds: Optional[float] = None) -> None:
    """
    Run a file through the chain with no playback and write it with ffmpeg.
    The container is chosen from the extension (flac, wav, mp3, ogg...).
    """
    dec = _spawn_decoder(path, 0.0)
    codec = ["-c:a", "libmp3lame", "-q:a", "2"] if out_path.lower().endswith(".mp3") else []
    enc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(SAMPLE_RATE),
         "-ac", str(CHANNELS), "-i", "-", *codec, out_path],
        stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert dec.stdout is not None and enc.stdin is not None
    limit = None if seconds is None else int(seconds * SAMPLE_RATE)
    done = 0
    nbytes = BLOCK_FRAMES * BYTES_PER_FRAME
    try:
        while limit is None or done < limit:
            raw = _read_block(dec.stdout, nbytes)
            if not raw:
                break
            usable = len(raw) - len(raw) % BYTES_PER_FRAME
            block = np.frombuffer(raw[:usable], dtype=np.float32).reshape(-1, CHANNELS)
            enc.stdin.write(np.ascontiguousarray(chain.process(block)).tobytes())
            done += len(block)
    finally:
        enc.stdin.close()
        enc.wait(timeout=30)
        if dec.poll() is None:
            dec.kill()
        dec.wait(timeout=5)
        dec.stdout.close()
