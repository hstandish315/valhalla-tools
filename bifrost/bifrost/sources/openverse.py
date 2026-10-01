"""
Openverse source - Creative Commons audio search.

Openverse indexes open-licensed works and links to the host that serves each
file (Jamendo, Freesound, Wikimedia...). It does not host audio itself, so the
download URL belongs to a third party and the downloader's host allowlist is
what keeps that safe. Every result carries a license and a ready-made
attribution string, which we keep with the file.

API: https://api.openverse.org/v1/audio/ (anonymous access, rate limited).
"""

from __future__ import annotations

from typing import Optional

from .common import Http, Track, parse_license

SEARCH_URL = "https://api.openverse.org/v1/audio/"

# Hosts Openverse's audio providers serve files from. Suffix-matched.
HOSTS = ("jamendo.com", "freesound.org", "wikimedia.org", "freemusicarchive.org",
         "archive.org")


def parse_results(payload: dict) -> list[Track]:
    tracks: list[Track] = []
    for r in payload.get("results", []):
        lic = parse_license(r.get("license_url") or "")
        url = r.get("url") or ""
        if lic is None or not url:
            continue                            # no usable license or file: drop
        if r.get("mature"):
            continue
        short, lic_url = lic
        tracks.append(Track(
            source="openverse",
            ident=str(r.get("id", "")),
            title=r.get("title") or "Untitled",
            creator=r.get("creator") or "",
            license=short,
            license_url=lic_url,
            landing_url=r.get("foreign_landing_url") or "",
            download_url=url,
            duration=(r.get("duration") or 0) / 1000.0,
            filetype=r.get("filetype") or "",
            size=r.get("filesize") or 0,
            attribution=r.get("attribution") or "",
            provider=r.get("provider") or "",
            extra={"genres": r.get("genres") or []},
        ))
    return tracks


def search(query: str, page: int = 1, page_size: int = 20,
           http: Optional[Http] = None) -> tuple[list[Track], int]:
    """Return (tracks, total_result_count)."""
    http = http or Http()
    payload = http.get_json(SEARCH_URL, {
        "q": query, "page": page, "page_size": page_size,
        "category": "music", "mature": "false",
    })
    return parse_results(payload), int(payload.get("result_count") or 0)
