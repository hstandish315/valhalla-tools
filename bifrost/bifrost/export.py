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
ATTRIBUTION_FILE = "ATTRIBUTION.txt"


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


def destination(entry, root: str, layout: str, fat: bool, ext: Optional[str] = None) -> str:
    """Where `entry` goes under `root`. Layouts: flat | artist | album."""
    src_ext = ext or os.path.splitext(entry.path)[1].lstrip(".").lower() or "mp3"
    artist = _component(entry.creator or "Unknown Artist", fat)
    title = _component(entry.title, fat)
    if layout == "album":
        album = _component(getattr(entry, "album", "") or "Singles", fat)
        n = getattr(entry, "track", 0)
        # Players sort by filename, so keep album order with a track-number prefix.
        name = _component(f"{n:02d} - {entry.title}", fat) if n else title
        return os.path.join(root, artist, album, f"{name}.{src_ext}")
    if layout == "artist":
        return os.path.join(root, artist, f"{title}.{src_ext}")
    flat = _component(f"{entry.creator} - {entry.title}" if entry.creator else entry.title, fat)
    return os.path.join(root, f"{flat}.{src_ext}")


# ------------------------------------------------------------------- copy ---

@dataclass
class Result:
    copied: list[str] = field(default_factory=list)
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
                progress: Optional[Callable[[int, int, str], None]] = None,
                cancel: Optional[Callable[[], bool]] = None) -> Result:
    """
    Copy (or, with `transform`, render) each entry into `dest_root`.

    `transform(src, dst)` writes a processed copy to `dst`; entries whose license
    forbids modification are skipped in that mode. Existing identical files are
    skipped; different files with the same name get " (2)" rather than being
    overwritten. Raises ExportError up front if the destination is unusable or
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
    for e in todo:
        if cancel and cancel():
            raise ExportError("cancelled")
        dst = destination(e, dest_root, layout, safe, transform_ext if transform else None)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if (not transform and os.path.exists(dst) and os.path.getsize(dst) == sizes[e.path]):
            result.skipped.append((e.title, "already there"))
            done += sizes[e.path]
            landed.append(e)
            continue
        dst = _unique(dst)
        stem, ext = os.path.splitext(os.path.basename(dst))
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


def processing_transform(chain) -> Callable[[str, str], None]:
    """
    A `transform` for copy_tracks that renders each track through a *private copy*
    of the live chain, so exporting never disturbs playback and every track starts
    with the sweep and pulse at phase zero.
    """
    import copy

    from .engine import render_offline

    def transform(src: str, dst: str) -> None:
        c = copy.deepcopy(chain)
        c.fade_in_s = 0.0
        c.reset_stream()
        c.pan.phase = c.am.phase = 0.0
        render_offline(c, src, dst)

    return transform
