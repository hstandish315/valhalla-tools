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
    # ID3v2.3 plus an ID3v1 tag: the combination old car head units read most reliably
    "mp3": ("mp3", ["-c:a", "libmp3lame", "-q:a", "0", "-id3v2_version", "3", "-write_id3v1", "1"]),
    "opus": ("opus", ["-c:a", "libopus", "-b:a", "128k"]),
    "ogg": ("ogg", ["-c:a", "libvorbis", "-q:a", "5"]),
}
COVER_FORMATS = {"flac", "mp3"}                        # ffmpeg can't attach a picture to Ogg/Opus
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
              tolerance: float = 1.0, cover: Optional[bytes] = None) -> str:
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
    cover_tmp = None
    if cover and fmt in COVER_FORMATS:
        kind = "png" if cover[:4] == b"\x89PNG" else "jpg"
        cover_tmp = wav[:-4] + ".cover." + kind
        with open(cover_tmp, "wb") as fh:
            fh.write(cover)
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
        art = (["-i", cover_tmp, "-map", "0:a", "-map", "1:v", "-c:v", "copy",
                "-disposition:v", "attached_pic", "-metadata:s:v", "title=Album cover",
                "-metadata:s:v", "comment=Cover (front)"] if cover_tmp else ["-vn"])
        enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-i", wav, *art, *codec_args,
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
        for p in (wav, enc_tmp, cover_tmp):
            if p and os.path.exists(p):
                os.unlink(p)


# ------------------------------------------------- cover art for existing files ---

def has_cover(path: str) -> bool:
    """True if the file already carries an embedded picture."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
                              "stream=codec_name", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=30).stdout
        return bool(out.strip())
    except (subprocess.SubprocessError, OSError):
        return False


def _audio_md5(path: str) -> str:
    """Checksum of the *decoded audio* only, so tags and pictures don't matter."""
    res = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-map", "0:a", "-f", "md5", "-"],
                         capture_output=True, text=True, timeout=600)
    return res.stdout.strip() if res.returncode == 0 else ""


def _tags(path: str) -> dict:
    import json
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json", path],
                         capture_output=True, text=True, timeout=30).stdout
    try:
        return {k.lower(): v for k, v in json.loads(out).get("format", {}).get("tags", {}).items()
                if k.lower() != "encoder"}
    except ValueError:
        return {}


def embed_cover(path: str, cover: bytes, replace: bool = False) -> bool:
    """
    Add `cover` to an already-ripped FLAC or MP3 without re-encoding the audio.

    The new file is built beside the original and swapped in only if the decoded audio is
    bit-identical, the tags are unchanged and the picture is really there; anything else
    leaves the original untouched. Returns False (and changes nothing) for formats that
    can't carry a picture, for files that already have one (unless `replace`), or on any
    verification failure.
    """
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    if ext not in COVER_FORMATS or not cover:
        return False
    if has_cover(path) and not replace:
        return False
    kind = "png" if cover[:4] == b"\x89PNG" else "jpg"
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".art-", suffix="." + ext, dir=d)
    os.close(fd)
    cover_tmp = tmp + ".cover." + kind
    try:
        with open(cover_tmp, "wb") as fh:
            fh.write(cover)
        extra = ["-id3v2_version", "3", "-write_id3v1", "1"] if ext == "mp3" else []
        res = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", path, "-i", cover_tmp, "-map", "0:a", "-map", "1:v",
             "-map_metadata", "0", "-c", "copy", "-disposition:v", "attached_pic",
             "-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)", *extra, tmp],
            capture_output=True, text=True, timeout=600)
        if res.returncode != 0 or not os.path.getsize(tmp):
            return False
        before = _audio_md5(path)
        if not before or before != _audio_md5(tmp):
            return False                                   # the audio must be byte-for-byte the same
        if _tags(path) != _tags(tmp) or not has_cover(tmp):
            return False
        os.chmod(tmp, os.stat(path).st_mode & 0o7777)
        os.replace(tmp, path)
        return True
    finally:
        for p in (tmp, cover_tmp):
            if os.path.exists(p):
                os.unlink(p)
