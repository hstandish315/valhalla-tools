"""
MusicBrainz lookup by disc ID.

Only the *exact* endpoint is used (`/discid/<id>`). The TOC-based variant
(`/discid/-?toc=...`) falls back to fuzzy matching and returned 25 unrelated
releases for a bogus TOC while this was being designed - which would label a
disc with someone else's track names. An unknown disc is a 404 here, and the UI
then offers editable "Track NN" names instead.
"""

from __future__ import annotations

import urllib.error
from dataclasses import dataclass, field
from typing import Optional

from .sources.common import Http

API = "https://musicbrainz.org/ws/2/discid/{id}"
INC = "recordings+artist-credits"


@dataclass
class TrackInfo:
    number: int
    title: str
    artist: str
    length_ms: int = 0


@dataclass
class Album:
    title: str
    artist: str
    year: str = ""
    mbid: str = ""
    tracks: list[TrackInfo] = field(default_factory=list)

    def track(self, number: int) -> Optional[TrackInfo]:
        return next((t for t in self.tracks if t.number == number), None)


def _credit(credits: list[dict]) -> str:
    return "".join(c.get("name", "") + c.get("joinphrase", "") for c in credits).strip()


def parse(payload: dict, disc: str) -> Optional[Album]:
    """Pick the release whose medium actually carries this disc ID."""
    for rel in payload.get("releases", []):
        for medium in rel.get("media", []):
            if not any(d.get("id") == disc for d in medium.get("discs", [])):
                continue
            album_artist = _credit(rel.get("artist-credit", []))
            tracks = []
            for t in medium.get("tracks", []):
                try:
                    n = int(t.get("number") or t.get("position"))
                except (TypeError, ValueError):
                    continue
                tracks.append(TrackInfo(n, t.get("title", f"Track {n}"),
                                        _credit(t.get("artist-credit", [])) or album_artist,
                                        int(t.get("length") or 0)))
            return Album(rel.get("title", ""), album_artist, (rel.get("date") or "")[:4],
                         rel.get("id", ""), tracks)
    return None


def lookup(disc: str, http: Optional[Http] = None) -> Optional[Album]:
    """Return the Album, None if MusicBrainz doesn't know the disc; other errors propagate."""
    http = http or Http()
    try:
        payload = http.get_json(API.format(id=disc), {"inc": INC, "fmt": "json"})
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            exc.close()
            return None
        raise
    return parse(payload, disc)
