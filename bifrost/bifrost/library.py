"""
Track library: a JSON index beside the audio files.

The index records where each file came from and under what license, so the UI
can show attribution and refuse to export a processed copy of a no-derivatives
work. Writes are atomic (temp file then rename) so a crash cannot truncate it.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

from .engine import is_audio_file, probe_duration
from .sources.common import Track

DEFAULT_MUSIC_DIR = os.path.expanduser("~/Music/Bifrost")
DEFAULT_DATA_DIR = os.path.join(
    os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "bifrost")


@dataclass
class Entry:
    path: str
    title: str
    creator: str = ""
    album: str = ""
    track: int = 0
    license: str = "Local file"
    license_url: str = ""
    landing_url: str = ""
    attribution: str = ""
    source: str = "local"
    duration: float = 0.0
    added: float = field(default_factory=time.time)

    @property
    def allows_modification(self) -> bool:
        return Track(source=self.source, ident="", title="", license=self.license).allows_modification


class Library:
    def __init__(self, music_dir: str = DEFAULT_MUSIC_DIR, data_dir: str = DEFAULT_DATA_DIR):
        self.music_dir = music_dir
        self.data_dir = data_dir
        self.index_path = os.path.join(data_dir, "library.json")
        self.entries: list[Entry] = []
        self.load()

    def load(self) -> None:
        try:
            with open(self.index_path, encoding="utf-8") as fh:
                raw = json.load(fh)
            self.entries = [Entry(**e) for e in raw.get("tracks", [])]
        except (OSError, ValueError, TypeError):
            self.entries = []
        # An index entry whose file was deleted outside the app is dead weight.
        self.entries = [e for e in self.entries if os.path.exists(e.path)]

    def save(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".library-", dir=self.data_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"tracks": [asdict(e) for e in self.entries]}, fh, indent=1)
            os.replace(tmp, self.index_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _find(self, path: str) -> Optional[Entry]:
        return next((e for e in self.entries if e.path == path), None)

    def add_download(self, track: Track, path: str) -> Entry:
        entry = self._find(path) or Entry(path=path, title=track.title)
        entry.title, entry.creator = track.title, track.creator
        entry.license, entry.license_url = track.license, track.license_url
        entry.landing_url, entry.attribution = track.landing_url, track.attribution
        entry.source = track.source
        entry.duration = track.duration or probe_duration(path)
        if entry not in self.entries:
            self.entries.append(entry)
        self.save()
        return entry

    def add_local(self, path: str, copy: bool = False) -> Optional[Entry]:
        """Import a user's own file. Returns None if it is not audio."""
        if not is_audio_file(path):
            return None
        if copy:
            os.makedirs(self.music_dir, exist_ok=True)
            dest = os.path.join(self.music_dir, os.path.basename(path))
            if not os.path.exists(dest):
                shutil.copy2(path, dest)
            path = dest
        existing = self._find(path)
        if existing:
            return existing
        title = os.path.splitext(os.path.basename(path))[0]
        entry = Entry(path=path, title=title, duration=probe_duration(path))
        self.entries.append(entry)
        self.save()
        return entry

    def add_ripped(self, path: str, title: str, creator: str, album: str, track: int = 0) -> Entry:
        """A track ripped from the user's own CD. Not CC-licensed, so no ND restriction applies."""
        entry = self._find(path) or Entry(path=path, title=title)
        entry.title, entry.creator, entry.album, entry.track = title, creator, album, track
        entry.license, entry.source = "Ripped from own CD", "cd"
        entry.attribution = f'"{title}" by {creator or "unknown"} from "{album}" (ripped from a disc you own)'
        entry.duration = probe_duration(path)
        if entry not in self.entries:
            self.entries.append(entry)
        self.save()
        return entry

    def remove(self, path: str, delete_file: bool = False) -> None:
        self.entries = [e for e in self.entries if e.path != path]
        # Only ever delete files inside our own music dir, never an imported original.
        music = os.path.abspath(self.music_dir)
        if delete_file and os.path.commonpath([os.path.abspath(path), music]) == music:
            try:
                os.unlink(path)
            except OSError:
                pass
        self.save()
