"""
Small persistent settings (chosen folders, formats). A JSON file beside the
library index, written atomically; a missing or corrupt file just means defaults.
"""

from __future__ import annotations

import json
import os
import tempfile

from .library import DEFAULT_DATA_DIR, DEFAULT_MUSIC_DIR

DEFAULTS = {
    "rip_dir": os.path.join(DEFAULT_MUSIC_DIR, "Rips"),
    "rip_format": "flac",
    "rip_lookup": True,            # ask MusicBrainz for track names (sends the disc ID)
    "rip_cover": True,             # embed cover art from the Cover Art Archive (sends the release ID)
    "rip_eject": False,
    "export_layout": "album",       # Artist/Album/NN - Title keeps albums in order on a car stereo
    "export_dir": os.path.expanduser("~/Music"),
}


class Settings:
    def __init__(self, data_dir: str = DEFAULT_DATA_DIR):
        self.path = os.path.join(data_dir, "settings.json")
        self.values = dict(DEFAULTS)
        try:
            with open(self.path, encoding="utf-8") as fh:
                saved = json.load(fh)
            self.values.update({k: v for k, v in saved.items() if k in DEFAULTS})
        except (OSError, ValueError, AttributeError):
            pass

    def __getitem__(self, key: str):
        return self.values[key]

    def set(self, key: str, value) -> None:
        if key not in DEFAULTS:
            raise KeyError(key)
        self.values[key] = value
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".settings-", dir=os.path.dirname(self.path))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.values, fh, indent=1)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
