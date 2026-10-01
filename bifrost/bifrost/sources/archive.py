"""
Internet Archive source.

Search is restricted to items whose `licenseurl` is a Creative Commons or
public-domain license, and results are re-checked client-side because the field
is uploader-supplied free text. Search hits are items, and an item is often an
album, so a second call (`resolve`) lists the files inside it and picks the best
audio format per track.

Quality order, highest first: FLAC original, original VBR/high-bitrate MP3,
other MP3, Ogg Vorbis. WAV is skipped by default - it is the same audio as FLAC
at roughly twice the size.
"""

from __future__ import annotations

import urllib.parse
from typing import Optional

from .common import Http, Track, parse_license

SEARCH_URL = "https://archive.org/advancedsearch.php"
META_URL = "https://archive.org/metadata/{ident}"
DOWNLOAD_URL = "https://archive.org/download/{ident}/{name}"
HOSTS = ("archive.org",)

MAX_FILES_PER_ITEM = 25

# (rank, human label); lower rank wins. Matched against the file's `format`.
_FORMAT_RANK = {
    "Flac": 0, "24bit Flac": 0,
    "VBR MP3": 1, "320Kbps MP3": 1, "256Kbps MP3": 1,
    "MP3": 2, "192Kbps MP3": 2, "128Kbps MP3": 3, "64Kbps MP3": 4,
    "Ogg Vorbis": 5,
}
_EXT = {"Flac": "flac", "24bit Flac": "flac", "Ogg Vorbis": "ogg"}


def parse_search(payload: dict) -> tuple[list[Track], int]:
    if "response" not in payload:
        # An error body must not read as "no results" - that hid a bad query once.
        raise ValueError(f"unexpected Internet Archive reply: {str(payload)[:200]}")
    docs = payload.get("response", {}).get("docs", [])
    total = int(payload.get("response", {}).get("numFound") or 0)
    tracks: list[Track] = []
    for d in docs:
        lic = parse_license(d.get("licenseurl") or "")
        if lic is None:
            continue
        short, lic_url = lic
        creator = d.get("creator") or ""
        if isinstance(creator, list):
            creator = ", ".join(creator)
        title = d.get("title") or d["identifier"]
        if isinstance(title, list):
            title = title[0]
        ident = d["identifier"]
        tracks.append(Track(
            source="archive", ident=ident, title=title, creator=creator,
            license=short, license_url=lic_url,
            landing_url=f"https://archive.org/details/{urllib.parse.quote(ident)}",
            attribution=f"\"{title}\" by {creator or 'unknown'} via the Internet Archive, "
                        f"licensed under {short}. {lic_url}",
            provider="archive",
            extra={"downloads": d.get("downloads") or 0},
        ))
    return tracks, total


def search(query: str, page: int = 1, rows: int = 20,
           http: Optional[Http] = None, artist: str = "") -> tuple[list[Track], int]:
    http = http or Http()
    # Quoting the user's text keeps AND/OR/colons in it from reshaping the query.
    safe = query.replace('"', " ").strip()
    # The server-side filter is deliberately coarse: a '/' inside a Lucene
    # wildcard is a syntax error. parse_search re-validates every result with
    # parse_license, which is what actually decides what reaches the UI.
    q = f'mediatype:audio AND ("{safe}") AND licenseurl:*creativecommons.org*'
    who = artist.replace('"', " ").strip()
    if who:
        q += f' AND creator:("{who}")'
    payload = http.get_json(SEARCH_URL, {
        "q": q, "fl[]": ["identifier", "title", "creator", "licenseurl", "downloads"],
        "sort[]": "downloads desc", "rows": rows, "page": page, "output": "json",
    })
    return parse_search(payload)


def pick_files(meta: dict) -> list[tuple[str, str, int]]:
    """
    From an item's metadata choose one file per track: (name, format, size).
    Files are grouped by base name so an album's FLAC and MP3 renditions of the
    same track collapse to the best one. The lowest format rank wins; between
    equal ranks an original beats a derivative.
    """
    best: dict[str, tuple[tuple[int, int], str, str, int]] = {}
    for f in meta.get("files", []):
        fmt = f.get("format", "")
        rank = _FORMAT_RANK.get(fmt)
        name = f.get("name", "")
        if rank is None or not name or "/" in name:
            continue
        base = name.rsplit(".", 1)[0]
        score = (rank, 0 if f.get("source") == "original" else 1)
        cur = best.get(base)
        if cur is None or score < cur[0]:
            best[base] = (score, name, fmt, int(f.get("size") or 0))
    ordered = sorted(best.values(), key=lambda v: v[1].lower())
    return [(name, fmt, size) for _, name, fmt, size in ordered][:MAX_FILES_PER_ITEM]


def resolve(track: Track, http: Optional[Http] = None, meta: Optional[dict] = None) -> list[Track]:
    """Expand an item-level search hit into one Track per downloadable audio file."""
    http = http or Http()
    if meta is None:
        meta = http.get_json(META_URL.format(ident=urllib.parse.quote(track.ident)))
    # Trust the item's own metadata license over the search row if they disagree.
    lic = parse_license((meta.get("metadata", {}).get("licenseurl")) or "")
    if lic is None:
        return []
    short, lic_url = lic
    files = pick_files(meta)
    out: list[Track] = []
    for name, fmt, size in files:
        stem = name.rsplit(".", 1)[0]
        multi = len(files) > 1
        title = f"{track.title} - {stem}" if multi else track.title
        out.append(Track(
            source="archive", ident=f"{track.ident}/{name}", title=title,
            creator=track.creator, license=short, license_url=lic_url,
            landing_url=track.landing_url,
            download_url=DOWNLOAD_URL.format(ident=urllib.parse.quote(track.ident),
                                            name=urllib.parse.quote(name)),
            filetype=_EXT.get(fmt, "mp3"), size=size,
            attribution=track.attribution, provider="archive",
        ))
    return out
