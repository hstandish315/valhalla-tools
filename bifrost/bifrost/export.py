"""
Copy tracks to a folder or a USB drive.

One core, `copy_tracks`, serves both cases. A USB stick is a folder that can
vanish, so the extra care lives here: FAT-safe filenames, free-space and 4 GiB
checks before anything is written, `.part` files fsynced then renamed (so a
yanked stick never leaves a truncated track that looks finished), cooperative
cancel, and an ATTRIBUTION.txt so Creative Commons obligations travel with the
files. Volume discovery and "safely remove" go through `lsblk` and `udisksctl`,
which run as the user (no sudo).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from .downloader import sanitise_filename

FAT_MAX_FILE = 4 * 1024 ** 3 - 1
FAT_TYPES = {"vfat", "fat", "fat32", "msdos"}
WINDOWS_TYPES = {"exfat", "ntfs", "ntfs3", "fuseblk"}
_FAT_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}
FILLER_SECONDS = 10.0               # tracks shorter than this are almost always silence/padding
ATTRIBUTION_FILE = "ATTRIBUTION.txt"
EDITS_FOLDER = "audio_edits"       # where bilateral versions go, apart from the originals


class ExportError(Exception):
    """A failure whose message is safe to show the user."""


# ---------------------------------------------------------------- volumes ---

@dataclass
class Volume:
    device: str                    # partition, e.g. /dev/sdb1
    disk: str                      # parent disk, e.g. /dev/sdb
    label: str = ""
    fstype: str = ""
    size: int = 0
    free: int = 0
    mountpoint: str = ""           # empty if not mounted yet

    @property
    def is_fat(self) -> bool:
        """FAT has the 4 GiB per-file limit."""
        return self.fstype.lower() in FAT_TYPES

    @property
    def windows_names(self) -> bool:
        """Windows-style filesystems reject  < > : " / \\ | ? *  in names (FAT, exFAT, NTFS)."""
        return self.fstype.lower() in FAT_TYPES | WINDOWS_TYPES

    @property
    def title(self) -> str:
        gib = self.size / 1024 ** 3
        return f"{self.label or self.device}  ·  {gib:.1f} GiB  ·  {self.fstype or '?'}"


def removable_volumes(runner: Callable = subprocess.run) -> list[Volume]:
    """USB / removable partitions with a filesystem. Optical drives and internal disks are excluded."""
    try:
        out = runner(["lsblk", "-J", "-b", "-o",
                      "NAME,PATH,TYPE,RM,HOTPLUG,TRAN,FSTYPE,LABEL,MOUNTPOINTS,SIZE,FSAVAIL"],
                     capture_output=True, text=True, timeout=15, check=True).stdout
        tree = json.loads(out).get("blockdevices", [])
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        raise ExportError(f"Could not list drives: {exc}") from exc

    def truthy(v) -> bool:
        return v in (True, 1, "1", "true")

    vols: list[Volume] = []
    for disk in tree:
        if disk.get("type") != "disk":
            continue
        removable = truthy(disk.get("rm")) or truthy(disk.get("hotplug")) or disk.get("tran") == "usb"
        if not removable or str(disk.get("name", "")).startswith("sr"):
            continue
        parts = disk.get("children") or [disk]            # a stick may be formatted without a partition table
        for p in parts:
            if not p.get("fstype"):
                continue
            mounts = [m for m in (p.get("mountpoints") or []) if m]
            vols.append(Volume(p.get("path") or f"/dev/{p['name']}", disk.get("path") or f"/dev/{disk['name']}",
                               p.get("label") or "", p.get("fstype") or "",
                               int(p.get("size") or 0), int(p.get("fsavail") or 0),
                               mounts[0] if mounts else ""))
    return vols


def mount_volume(vol: Volume, runner: Callable = subprocess.run) -> str:
    """Mount via udisks (user-level, no sudo) and return the mountpoint."""
    if vol.mountpoint:
        return vol.mountpoint
    res = runner(["udisksctl", "mount", "-b", vol.device, "--no-user-interaction"],
                 capture_output=True, text=True, timeout=30)
    m = re.search(r" at (.+?)\.?\s*$", res.stdout.strip())
    if res.returncode != 0 or not m:
        raise ExportError("Could not mount the drive: " + (res.stderr.strip() or res.stdout.strip() or "unknown error"))
    vol.mountpoint = m.group(1)
    return vol.mountpoint


def safely_remove(vol: Volume, runner: Callable = subprocess.run) -> None:
    """Flush, unmount and power the stick off so it can be unplugged."""
    os.sync()
    for cmd in (["udisksctl", "unmount", "-b", vol.device, "--no-user-interaction"],
                ["udisksctl", "power-off", "-b", vol.disk, "--no-user-interaction"]):
        res = runner(cmd, capture_output=True, text=True, timeout=30)
        if res.returncode != 0:
            raise ExportError("Could not safely remove the drive: "
                              + (res.stderr.strip() or "it may still be in use"))
    vol.mountpoint = ""


# ------------------------------------------------------------------ names ---

def fat_safe(name: str) -> str:
    """Make one path component legal on FAT/exFAT/NTFS."""
    cleaned = _FAT_BAD.sub("_", name).strip(" .")
    if cleaned.split(".")[0].upper() in _RESERVED:
        cleaned = "_" + cleaned
    return cleaned or "_"


def _component(name: str, fat: bool) -> str:
    name = sanitise_filename(name, limit=100)
    return fat_safe(name) if fat else name


def is_short(entry) -> bool:
    """A track under FILLER_SECONDS (CDs often carry runs of 4-second silent tracks)."""
    return 0 < (getattr(entry, "duration", 0) or 0) < FILLER_SECONDS


def destination(entry, root: str, layout: str, fat: bool, ext: Optional[str] = None,
                disambiguate: bool = False) -> str:
    """
    Where `entry` goes under `root`. Layouts: flat | artist | album. With `disambiguate`
    the track number is added to the title, used when several tracks in one export would
    otherwise get the same name (instead of an anonymous "(2)", "(3)").
    """
    src_ext = ext or os.path.splitext(entry.path)[1].lstrip(".").lower() or "mp3"
    num = getattr(entry, "track", 0)
    shown = f"{entry.title} ({num:02d})" if disambiguate and num else entry.title
    artist = _component(entry.creator or "Unknown Artist", fat)
    title = _component(shown, fat)
    if layout == "album":
        album = _component(getattr(entry, "album", "") or "Singles", fat)
        n = getattr(entry, "track", 0)
        # Players sort by filename, so keep album order with a track-number prefix.
        name = _component(f"{n:02d} - {entry.title}", fat) if n else title
        return os.path.join(root, artist, album, f"{name}.{src_ext}")
    if layout == "artist":
        return os.path.join(root, artist, f"{title}.{src_ext}")
    flat = _component(f"{entry.creator} - {shown}" if entry.creator else shown, fat)
    return os.path.join(root, f"{flat}.{src_ext}")


# ------------------------------------------------------------------- copy ---

@dataclass
class Result:
    copied: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)                  # earlier renders superseded
    skipped: list[tuple[str, str]] = field(default_factory=list)       # (title, reason)
    failed: list[tuple[str, str]] = field(default_factory=list)
    bytes: int = 0


def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _same_content(a: str, b: str) -> bool:
    """True if two files are byte-for-byte identical (size first, then a streamed hash)."""
    import hashlib
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        ha, hb = hashlib.sha256(), hashlib.sha256()
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                ca, cb = fa.read(1 << 20), fb.read(1 << 20)
                if not ca and not cb:
                    return ha.digest() == hb.digest()
                ha.update(ca)
                hb.update(cb)
    except OSError:
        return False


def _unique(path: str) -> str:
    base, ext = os.path.splitext(path)
    n, candidate = 1, path
    while os.path.exists(candidate):
        n += 1
        candidate = f"{base} ({n}){ext}"
    return candidate


def _write_attribution(root: str, entries: Sequence) -> None:
    lines = []
    for e in entries:
        if e.license and e.license.startswith(("CC", "Public")):
            lines.append(e.attribution or f'"{e.title}" by {e.creator or "unknown"}, {e.license}. {e.license_url}')
    if not lines:
        return
    path = os.path.join(root, ATTRIBUTION_FILE)
    existing = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            existing = {ln.strip() for ln in fh if ln.strip()}
    new = [ln for ln in lines if ln not in existing]
    if new:
        with open(path, "a", encoding="utf-8") as fh:
            if not existing:
                fh.write("Attribution for the Creative Commons tracks in this folder\n\n")
            fh.write("\n".join(new) + "\n")


def copy_tracks(entries: Sequence, dest_root: str, *, layout: str = "flat", fat: bool = False,
                safe_names: Optional[bool] = None,
                transform: Optional[Callable[[str, str], None]] = None,
                transform_ext: Optional[str] = None,
                free_bytes: Optional[int] = None,
                replace_existing: bool = False,
                progress: Optional[Callable[[int, int, str], None]] = None,
                cancel: Optional[Callable[[], bool]] = None) -> Result:
    """
    Copy (or, with `transform`, render) each entry into `dest_root`.

    `transform(src, dst)` writes a processed copy to `dst`; entries whose license
    forbids modification are skipped in that mode. Re-exporting is idempotent: a file
    that is already there and identical is skipped. A *different* file with the same
    name gets " (2)" rather than being overwritten, except that with `replace_existing`
    (used for bilateral renders, which live in a folder of their own) a re-render with
    new settings supersedes the earlier one instead of leaving a duplicate. Raises
    ExportError up front if the destination is unusable or
    there is not enough room; per-track problems are collected in `Result`.
    """
    safe = fat if safe_names is None else safe_names          # `fat` also implies the 4 GiB limit
    if not os.path.isdir(dest_root):
        raise ExportError(f"The destination folder doesn't exist: {dest_root}")
    if not os.access(dest_root, os.W_OK):
        raise ExportError(f"You don't have permission to write to {dest_root}")

    todo = []
    result = Result()
    for e in entries:
        if not os.path.exists(e.path):
            result.skipped.append((e.title, "the source file is missing"))
        elif transform and not e.allows_modification:
            result.skipped.append((e.title, "its license forbids modified copies (no-derivatives)"))
        else:
            todo.append(e)

    sizes = {e.path: os.path.getsize(e.path) for e in todo}
    if fat:
        for e in list(todo):
            if sizes[e.path] > FAT_MAX_FILE:
                todo.remove(e)
                result.skipped.append((e.title, "over the 4 GiB limit of a FAT drive"))
    total = sum(sizes[e.path] for e in todo)
    free = shutil.disk_usage(dest_root).free if free_bytes is None else free_bytes
    if total * 1.02 + 1024 * 1024 > free:
        raise ExportError(f"Not enough space: need about {total / 1024 ** 2:.0f} MiB, "
                          f"{free / 1024 ** 2:.0f} MiB free.")

    done = 0
    landed = []                                   # entries now present at the destination
    # Several tracks in this export may map to one name (e.g. a CD's run of "[silence]" tracks
    # under the flat layout). Give those their track numbers instead of anonymous "(2)", "(3)".
    out_ext = transform_ext if transform else None
    planned = {e.path: destination(e, dest_root, layout, safe, out_ext) for e in todo}
    seen: dict[str, int] = {}
    for p in planned.values():
        seen[p.lower()] = seen.get(p.lower(), 0) + 1
    for e in todo:
        if cancel and cancel():
            raise ExportError("cancelled")
        dst = planned[e.path]
        if seen[dst.lower()] > 1 and getattr(e, "track", 0):
            dst = destination(e, dest_root, layout, safe, out_ext, disambiguate=True)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if (not transform and os.path.exists(dst) and os.path.getsize(dst) == sizes[e.path]):
            result.skipped.append((e.title, "already there"))
            done += sizes[e.path]
            landed.append(e)
            continue
        base = dst
        if not transform:
            dst = _unique(base)                 # transforms decide after rendering: see below
        stem, ext = os.path.splitext(os.path.basename(base))
        # Keep the real extension last: ffmpeg picks the container from it when a
        # transform renders straight into the temp file.
        part = os.path.join(os.path.dirname(dst), f".{stem}.part{ext}")
        try:
            if transform:
                transform(e.path, part)
            else:
                with open(e.path, "rb") as src, open(part, "wb") as out:
                    while True:
                        if cancel and cancel():
                            raise ExportError("cancelled")
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total, e.title)
                    out.flush()
                    os.fsync(out.fileno())
            if transform:
                with open(part, "rb") as fh:
                    os.fsync(fh.fileno())
                done += sizes[e.path]
                if os.path.exists(base):
                    if _same_content(part, base):                     # same render as last time
                        result.skipped.append((e.title, "already there"))
                        landed.append(e)
                        if progress:
                            progress(done, total, e.title)
                        continue
                    if replace_existing:
                        dst = base
                        result.replaced.append(base)
                    else:
                        dst = _unique(base)
                else:
                    dst = base
            os.replace(part, dst)
            _fsync_dir(os.path.dirname(dst))
            result.copied.append(dst)
            landed.append(e)
            result.bytes += os.path.getsize(dst)
            if progress:
                progress(done, total, e.title)
        except ExportError:
            raise
        except OSError as exc:
            result.failed.append((e.title, exc.strerror or str(exc)))
        except Exception as exc:                        # a transform (ffmpeg) failing
            result.failed.append((e.title, str(exc)))
        finally:
            if os.path.exists(part):
                os.unlink(part)

    _write_attribution(dest_root, landed)
    return result


def _has_tag(path: str, key: str) -> bool:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", f"format_tags={key}",
                              "-of", "default=nw=1:nk=1", path],
                             capture_output=True, text=True, timeout=15).stdout
        return bool(out.strip())
    except (subprocess.SubprocessError, OSError):
        return False


def tags_for(entry, src: str) -> dict:
    """
    Tags to write on an exported copy. Ripped and downloaded tracks have authoritative
    metadata in the library, so it wins. For a user's own file the file's own tags are
    kept; the library entry (named after the file) only fills in a missing title so the
    track never shows up nameless.
    """
    if entry is None:
        return {}
    tags = {"title": entry.title, "artist": entry.creator, "album": entry.album,
            "album_artist": entry.creator if entry.album else "",
            "track": str(entry.track) if getattr(entry, "track", 0) else ""}
    if entry.source == "local":
        return {} if _has_tag(src, "title") else {"title": entry.title}
    return tags


def processing_transform(chain, entries: Optional[Sequence] = None) -> Callable[[str, str], None]:
    """
    A `transform` for copy_tracks that renders each track through a *private copy* of the
    live chain, so exporting never disturbs playback and every track starts with the sweep
    and pulse at phase zero. Tags and cover art travel with the audio.
    """
    import copy

    from .engine import render_offline

    by_path = {e.path: e for e in entries or []}

    def transform(src: str, dst: str) -> None:
        c = copy.deepcopy(chain)
        c.fade_in_s = 0.0
        c.reset_stream()
        c.pan.phase = c.am.phase = 0.0
        render_offline(c, src, dst, tags=tags_for(by_path.get(src), src))

    return transform


def conversion_transform(entries: Optional[Sequence] = None) -> Callable[[str, str], None]:
    """
    Convert an original to MP3 with no effects (FLAC won't play in many car stereos).
    An MP3 source is copied as-is rather than re-encoded, which would only lose quality.
    """
    by_path = {e.path: e for e in entries or []}

    def transform(src: str, dst: str) -> None:
        if src.lower().endswith(".mp3"):
            shutil.copyfile(src, dst)
            return
        overrides = [a for k, v in tags_for(by_path.get(src), src).items() if v
                     for a in ("-metadata", f"{k}={v}")]
        res = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", src, "-map", "0:a", "-map", "0:v?",
             "-map_metadata", "0", "-c:a", "libmp3lame", "-q:a", "2", "-c:v", "copy",
             "-disposition:v", "attached_pic", "-id3v2_version", "3", "-write_id3v1", "1",
             *overrides, dst], capture_output=True, text=True, timeout=600)
        if res.returncode != 0:
            raise RuntimeError("conversion failed: " + (res.stderr.strip().splitlines() or ["?"])[-1][:200])

    return transform
