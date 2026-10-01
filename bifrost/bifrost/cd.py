"""
Audio CD support: drive discovery, table of contents, disc ID, and ripping.

Reading uses GStreamer's `cdparanoiasrc` (the error-correcting reader Sound
Juicer uses) into a temporary WAV, then ffmpeg encodes and tags it. Both tools
were already on this system, so nothing needs installing.

The kernel and the tools are reached through small injectable seams (`ioctl`,
`opener`, the source command) so the logic is tested without a drive; the
hardware path itself was measured against a real disc while building this:
TOC read, exact MusicBrainz match, a rip whose length equalled the TOC's
sector count, and a FLAC that decodes bit-identical to the ripped WAV.
"""

from __future__ import annotations

import base64
import fcntl
import glob
import hashlib
import os
import struct
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from .downloader import sanitise_filename
from .engine import probe_duration

SECTORS_PER_SECOND = 75
PREGAP = 150                                   # TOC offsets include the 2 s lead-in
PCM_BYTES_PER_SECOND = 44100 * 2 * 2

# Linux <linux/cdrom.h>
CDROMREADTOCHDR = 0x5305
CDROMREADTOCENTRY = 0x5306
CDROM_DRIVE_STATUS = 0x5326
CDROM_LBA = 0x01
LEADOUT_TRACK = 0xAA
_TOC_ENTRY = "=BBBxiB3x"                       # struct cdrom_tocentry, 12 bytes

DISC_STATUS_TEXT = {
    1: "No disc in the drive.",
    2: "The drive tray is open.",
    3: "The drive isn't ready yet; give it a few seconds and refresh.",
}

FORMATS: dict[str, tuple[str, list[str]]] = {
    # key: (file extension, ffmpeg codec arguments)
    "flac": ("flac", ["-c:a", "flac", "-compression_level", "8"]),
    "mp3": ("mp3", ["-c:a", "libmp3lame", "-q:a", "0", "-id3v2_version", "3"]),
    "opus": ("opus", ["-c:a", "libopus", "-b:a", "128k"]),
    "ogg": ("ogg", ["-c:a", "libvorbis", "-q:a", "5"]),
}
FORMAT_LABELS = {"flac": "FLAC (lossless)", "mp3": "MP3 (V0, high quality)",
                 "opus": "Opus (128 kbps)", "ogg": "Ogg Vorbis (q5)"}


class CDError(Exception):
    """A failure whose message is safe to show the user."""


@dataclass
class Drive:
    device: str
    vendor: str = ""
    model: str = ""

    @property
    def label(self) -> str:
        name = f"{self.vendor} {self.model}".strip()
        return f"{name} ({self.device})" if name else self.device


@dataclass
class TocTrack:
    number: int
    lba: int                       # sector where the track starts (no lead-in)
    sectors: int                   # length in sectors
    is_data: bool = False

    @property
    def seconds(self) -> float:
        return self.sectors / SECTORS_PER_SECOND


@dataclass
class Toc:
    tracks: list[TocTrack]
    leadout_lba: int

    @property
    def audio_tracks(self) -> list[TocTrack]:
        return [t for t in self.tracks if not t.is_data]

    @property
    def minutes(self) -> float:
        return self.leadout_lba / SECTORS_PER_SECOND / 60.0


# -------------------------------------------------------------- discovery ---

def find_drives(sys_block: str = "/sys/block") -> list[Drive]:
    drives = []
    for path in sorted(glob.glob(os.path.join(sys_block, "sr*"))):
        def read(name: str) -> str:
            try:
                with open(os.path.join(path, "device", name)) as fh:
                    return " ".join(fh.read().split())
            except OSError:
                return ""
        drives.append(Drive(f"/dev/{os.path.basename(path)}", read("vendor"), read("model")))
    return drives


def read_toc(device: str, ioctl: Callable = fcntl.ioctl, opener: Callable = os.open,
             closer: Callable = os.close) -> Toc:
    """
    Read the disc's table of contents through the kernel's CD-ROM ioctls.
    O_NONBLOCK lets the open succeed on an empty drive so we can say why we
    failed instead of hanging.
    """
    try:
        fd = opener(device, os.O_RDONLY | os.O_NONBLOCK)
    except PermissionError as exc:
        raise CDError(f"No permission to read {device}; your account needs to be in the "
                      f"'cdrom' group.") from exc
    except OSError as exc:
        raise CDError(f"Cannot open {device}: {exc.strerror or exc}") from exc
    try:
        status = ioctl(fd, CDROM_DRIVE_STATUS, 0)
        if status != 4:                                      # CDS_DISC_OK
            raise CDError(DISC_STATUS_TEXT.get(status, "No readable disc in the drive."))
        try:
            first, last = struct.unpack("BB", ioctl(fd, CDROMREADTOCHDR, b"\0\0"))
        except OSError as exc:
            raise CDError("The disc's table of contents can't be read (is it blank or "
                          "damaged?).") from exc

        def entry(track: int) -> tuple[int, bool]:
            buf = struct.pack(_TOC_ENTRY, track, 0, CDROM_LBA, 0, 0)
            _, adrctrl, _, lba, _ = struct.unpack(_TOC_ENTRY, ioctl(fd, CDROMREADTOCENTRY, buf))
            return lba, bool((adrctrl >> 4) & 4)            # control bit 2 = data track

        raw = [(n, *entry(n)) for n in range(first, last + 1)]
        leadout, _ = entry(LEADOUT_TRACK)
    except OSError as exc:
        raise CDError(f"Reading the disc failed: {exc.strerror or exc}") from exc
    finally:
        closer(fd)

    tracks = []
    for i, (n, lba, is_data) in enumerate(raw):
        end = raw[i + 1][1] if i + 1 < len(raw) else leadout
        tracks.append(TocTrack(n, lba, end - lba, is_data))
    if not any(not t.is_data for t in tracks):
        raise CDError("This disc has no audio tracks (it looks like a data disc).")
    return Toc(tracks, leadout)


def disc_id(toc: Toc) -> str:
    """
    MusicBrainz disc ID: SHA-1 over the hex-formatted TOC, base64 with the
    '._-' alphabet. Verified against real MusicBrainz TOC -> ID pairs.
    Data tracks are not part of the ID's audio range, so only audio tracks
    count (a trailing data session shortens the "last" track).
    """
    audio = toc.audio_tracks
    first, last = audio[0].number, audio[-1].number
    leadout = toc.leadout_lba + PREGAP
    if toc.tracks[-1].is_data:                               # mixed-mode: data track is last
        leadout = toc.tracks[-1].lba + PREGAP - 11400
    offsets = [t.lba + PREGAP for t in audio]
    text = "%02X%02X%08X" % (first, last, leadout)
    text += "".join("%08X" % o for o in offsets + [0] * (99 - len(offsets)))
    digest = base64.b64encode(hashlib.sha1(text.encode("ascii")).digest()).decode()
    return digest.replace("+", ".").replace("/", "_").replace("=", "-")


def eject(device: str, runner: Callable = subprocess.run) -> None:
    runner(["eject", device], check=False, capture_output=True, timeout=20)


# ---------------------------------------------------------------- ripping ---

@dataclass
class Tags:
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    track: int = 0
    total: int = 0
    year: str = ""

    def ffmpeg_args(self) -> list[str]:
        def clean(s: str) -> str:
            return "".join(c for c in s if c.isprintable()).strip()
        pairs = [("title", self.title), ("artist", self.artist), ("album", self.album),
                 ("album_artist", self.album_artist), ("date", self.year)]
        if self.track:
            pairs.append(("track", f"{self.track}/{self.total}" if self.total else str(self.track)))
        args: list[str] = []
        for key, value in pairs:
            if clean(value):
                args += ["-metadata", f"{key}={clean(value)}"]
        return args


def default_source_cmd(device: str, track: int, wav_path: str) -> list[str]:
    """GStreamer: error-corrected read of one track to 16-bit 44.1 kHz stereo WAV."""
    return ["gst-launch-1.0", "-q", "cdparanoiasrc", f"device={device}", f"track={track}",
            "paranoia-mode=full", "!", "audioconvert", "!",
            "audio/x-raw,format=S16LE,rate=44100,channels=2", "!", "wavenc", "!",
            "filesink", f"location={wav_path}"]


def track_path(root: str, tags: Tags, ext: str) -> str:
    artist = sanitise_filename(tags.album_artist or tags.artist or "Unknown Artist")
    album = sanitise_filename(tags.album or "Unknown Album")
    name = sanitise_filename(f"{tags.track:02d} - {tags.title or f'Track {tags.track}'}")
    return os.path.join(root, artist, album, f"{name}.{ext}")


def _wait(proc: subprocess.Popen, cancel: Optional[Callable[[], bool]],
          tick: Optional[Callable[[], None]] = None) -> int:
    """Poll a child so cancel is honoured within ~0.2 s instead of at exit."""
    while proc.poll() is None:
        if cancel and cancel():
            proc.kill()
            proc.wait()
            raise CDError("cancelled")
        if tick:
            tick()
        time.sleep(0.2)
    return proc.returncode


def rip_track(device: str, track: TocTrack, out_path: str, fmt: str, tags: Tags,
              progress: Optional[Callable[[float], None]] = None,
              cancel: Optional[Callable[[], bool]] = None,
              source_cmd: Callable[[str, int, str], list[str]] = default_source_cmd,
              tolerance: float = 1.0) -> str:
    """
    Rip one track to `out_path` (extension chosen from `fmt`) and return the
    final path. Raises CDError with a user-safe message on any failure; partial
    files never survive. The decoded length must match the TOC within
    `tolerance` seconds, which catches truncated or stalled reads.
    """
    if fmt not in FORMATS:
        raise CDError(f"Unknown format {fmt!r}.")
    ext, codec_args = FORMATS[fmt]
    final = os.path.splitext(out_path)[0] + "." + ext
    os.makedirs(os.path.dirname(final), exist_ok=True)
    fd, wav = tempfile.mkstemp(prefix=".rip-", suffix=".wav", dir=os.path.dirname(final))
    os.close(fd)
    enc_tmp = wav[:-4] + "." + ext
    expected_bytes = max(1, int(track.seconds * PCM_BYTES_PER_SECOND))
    reader = enc = None
    try:
        reader = subprocess.Popen(source_cmd(device, track.number, wav),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        def tick():
            if progress:
                progress(min(0.9, 0.9 * os.path.getsize(wav) / expected_bytes))

        rc = _wait(reader, cancel, tick)
        if rc != 0:
            err = (reader.stderr.read().decode(errors="replace").strip().splitlines() or ["?"])[-1]
            raise CDError(f"Reading track {track.number} failed: {err[:160]}")

        got = probe_duration(wav)
        if abs(got - track.seconds) > tolerance:
            raise CDError(f"Track {track.number} came out {got:.1f}s long but the disc says "
                          f"{track.seconds:.1f}s; the read was incomplete.")
        if progress:
            progress(0.92)
        enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-i", wav, "-vn", *codec_args,
                                *tags.ffmpeg_args(), enc_tmp],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if _wait(enc, cancel) != 0:
            raise CDError("Encoding failed: " + enc.stderr.read().decode(errors="replace")[-160:])
        if abs(probe_duration(enc_tmp) - track.seconds) > tolerance + 0.5:
            raise CDError(f"Encoded track {track.number} has the wrong length.")
        os.chmod(enc_tmp, 0o644)
        os.replace(enc_tmp, final)
        if progress:
            progress(1.0)
        return final
    finally:
        for proc in (reader, enc):                  # never leak a pipe, whichever path we left by
            if proc is not None:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                if proc.stderr:
                    proc.stderr.close()
        for p in (wav, enc_tmp):
            if os.path.exists(p):
                os.unlink(p)
