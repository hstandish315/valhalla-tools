"""
Hardened downloader for music from the free sources.

The URL comes from a third-party API response, so it is treated as untrusted:

  * https only, and the host must match an allowlist (suffix match, so
    `prod-1.storage.jamendo.com` passes for `jamendo.com` but
    `jamendo.com.evil.example` does not);
  * every redirect hop is re-checked against the same rules, because an
    allowed host can still bounce you somewhere else;
  * the declared type must be audio, or a known audio extension;
  * a size cap is enforced both from Content-Length and while streaming, since
    the header can lie or be absent;
  * the file lands in `*.part`, is validated by ffprobe as having an audio
    stream, and only then renamed into place atomically.

Nothing downloaded is ever executed, and the on-disk name is derived from
sanitised metadata, never taken from the server.
"""

from __future__ import annotations

import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Iterable, Optional

from .engine import is_audio_file
from .sources.common import USER_AGENT, Track

MAX_BYTES = 200 * 1024 * 1024
CHUNK = 64 * 1024
ALLOWED_EXT = {"mp3", "ogg", "oga", "flac", "wav", "opus", "m4a"}
_CT_EXT = {
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/ogg": "ogg",
    "application/ogg": "ogg", "audio/flac": "flac", "audio/x-flac": "flac",
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav",
    "audio/opus": "opus", "audio/mp4": "m4a", "audio/x-m4a": "m4a",
    "audio/aac": "m4a",
}


class DownloadError(Exception):
    """Raised for any refusal or failure; the message is safe to show the user."""


def host_allowed(host: str, allowed: Iterable[str]) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == a or host.endswith("." + a) for a in allowed)


def sanitise_filename(text: str, limit: int = 80) -> str:
    """Letters, digits, space, dot, dash, underscore only; no path separators."""
    cleaned = re.sub(r"[^\w .\-]", "", text, flags=re.UNICODE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-_")
    return (cleaned or "track")[:limit].rstrip(" .-_") or "track"


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, check: Callable[[str], None]):
        self._check = check

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self._check(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _extension(url: str, content_type: str) -> Optional[str]:
    ct = content_type.split(";")[0].strip().lower()
    if ct in _CT_EXT:
        return _CT_EXT[ct]
    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lstrip(".").lower()
    return ext if ext in ALLOWED_EXT else None


def download(track: Track, dest_dir: str, allowed_hosts: Iterable[str],
             max_bytes: int = MAX_BYTES, schemes: tuple[str, ...] = ("https",),
             progress: Optional[Callable[[int, int], None]] = None,
             cancel: Optional[Callable[[], bool]] = None) -> str:
    """
    Download `track` into `dest_dir` and return the final path.
    `progress(done, total)` is called per chunk (total is 0 if unknown);
    `cancel()` returning True aborts and removes the partial file.
    """
    allowed = tuple(allowed_hosts)

    def check(url: str) -> None:
        u = urllib.parse.urlparse(url)
        if u.scheme.lower() not in schemes:
            raise DownloadError(f"blocked: {u.scheme or 'unknown'} URLs are not allowed")
        if not host_allowed(u.hostname or "", allowed):
            raise DownloadError(f"blocked: host {u.hostname!r} is not on the allowlist")

    check(track.download_url)
    opener = urllib.request.build_opener(_CheckedRedirect(check))
    req = urllib.request.Request(track.download_url, headers={"User-Agent": USER_AGENT})

    os.makedirs(dest_dir, exist_ok=True)
    fd, part = tempfile.mkstemp(prefix=".dl-", suffix=".part", dir=dest_dir)
    try:
        with os.fdopen(fd, "wb") as out, opener.open(req, timeout=30) as resp:
            ctype = resp.headers.get("Content-Type", "")
            ext = _extension(resp.geturl(), ctype) or _extension(track.download_url, ctype)
            if ext is None:
                raise DownloadError(f"blocked: not an audio file (type {ctype or 'unknown'!r})")
            total = int(resp.headers.get("Content-Length") or 0)
            if total > max_bytes:
                raise DownloadError(f"blocked: file is {total // (1 << 20)} MB, "
                                    f"over the {max_bytes // (1 << 20)} MB limit")
            done = 0
            while True:
                if cancel and cancel():
                    raise DownloadError("cancelled")
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                done += len(chunk)
                if done > max_bytes:
                    raise DownloadError("blocked: download exceeded the size limit")
                out.write(chunk)
                if progress:
                    progress(done, total)
        if not is_audio_file(part):
            raise DownloadError("blocked: the file is not decodable audio")

        stem = sanitise_filename(f"{track.creator} - {track.title}" if track.creator else track.title)
        final = os.path.join(dest_dir, f"{stem}.{ext}")
        n = 1
        while os.path.exists(final):
            n += 1
            final = os.path.join(dest_dir, f"{stem} ({n}).{ext}")
        os.chmod(part, 0o644)              # mkstemp creates 0600; music should be ordinary
        os.replace(part, final)
        return final
    except DownloadError:
        raise
    except (urllib.error.URLError, OSError, ValueError) as exc:
        if isinstance(exc, urllib.error.HTTPError):
            exc.close()                  # release the response socket
        raise DownloadError(f"download failed: {exc}") from exc
    finally:
        if os.path.exists(part):
            os.unlink(part)
