"""
MusicBrainz lookup by disc ID.

Only the *exact* endpoint is used (`/discid/<id>`). The TOC-based variant
(`/discid/-?toc=...`) falls back to fuzzy matching and returned 25 unrelated
releases for a bogus TOC while this was being designed - which would label a
disc with someone else's track names. An unknown disc is a 404 here, and the UI
then offers editable "Track NN" names instead.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from .downloader import _CheckedRedirect, host_allowed
from .sources.common import USER_AGENT, Http

API = "https://musicbrainz.org/ws/2/discid/{id}"
INC = "recordings+artist-credits"

COVER_URL = "https://coverartarchive.org/release/{mbid}/front-500"
COVER_HOSTS = ("coverartarchive.org", "archive.org")        # the CAA redirects to archive.org storage
MAX_COVER_BYTES = 5 * 1024 * 1024
_MBID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


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


def image_kind(data: bytes) -> Optional[str]:
    """'jpg' or 'png' judged by the file's own signature, never by what a server claims."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    return None


def fetch_cover(mbid: str, opener: Optional[urllib.request.OpenerDirector] = None) -> Optional[bytes]:
    """
    Front cover for a MusicBrainz release from the Cover Art Archive, or None if there
    isn't one. The release ID must look like a UUID (it goes into a URL), every hop must
    be https on an allowlisted host, the size is capped, and the bytes must really be a
    JPEG or PNG. Other network failures propagate so the caller can say so.
    """
    if not _MBID.match(mbid or ""):
        return None

    def check(url: str) -> None:
        u = urllib.parse.urlparse(url)
        if u.scheme != "https" or not host_allowed(u.hostname or "", COVER_HOSTS):
            raise urllib.error.URLError(f"refusing cover URL {url!r}")

    url = COVER_URL.format(mbid=mbid.lower())
    check(url)
    opener = opener or urllib.request.build_opener(_CheckedRedirect(check))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with opener.open(req, timeout=20) as resp:
            data = resp.read(MAX_COVER_BYTES + 1)
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code in (404, 403):                          # no art for this release
            return None
        raise
    if len(data) > MAX_COVER_BYTES or image_kind(data) is None:
        return None
    return data
