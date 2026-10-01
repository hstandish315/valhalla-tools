"""
Shared pieces for the music sources: the Track record, license parsing and a
small JSON-over-HTTPS client.

Licensing is first-class. A track with no recognisable open license is dropped
at the source layer and never reaches the UI, so nothing downstream has to
remember to check.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

USER_AGENT = "Bifrost/0.1 (local desktop focus-audio app)"
TIMEOUT = 20

_CC_RE = re.compile(r"creativecommons\.org/licenses/([a-z\-]+)/([0-9.]+)", re.I)
_PD_RE = re.compile(r"creativecommons\.org/publicdomain/(zero|mark)/([0-9.]+)", re.I)


@dataclass
class Track:
    source: str                         # "openverse" | "archive"
    ident: str
    title: str
    creator: str = ""
    license: str = ""                   # short form, e.g. "CC BY-NC-SA 4.0"
    license_url: str = ""
    landing_url: str = ""
    download_url: str = ""
    duration: float = 0.0               # seconds, 0 if unknown
    filetype: str = ""
    size: int = 0
    attribution: str = ""
    provider: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def allows_modification(self) -> bool:
        """False for CC '-ND' licenses: playing is fine, exporting a processed copy is not."""
        parts = self.license.upper().split()
        # "CC BY-NC-ND 4.0" -> parts[1] is the clause list "BY-NC-ND"
        if len(parts) >= 2 and parts[0] == "CC":
            return "ND" not in parts[1].split("-")
        return True


def parse_license(url: str) -> Optional[tuple[str, str]]:
    """
    Return (short_name, normalised_url) for a Creative Commons or public-domain
    license URL, or None if the URL is not one we recognise. Archive items carry
    free-text license URLs, so anything unrecognised is treated as unlicensed.
    """
    if not url:
        return None
    m = _CC_RE.search(url)
    if m:
        kind, ver = m.group(1).upper(), m.group(2)
        return f"CC {kind} {ver}", f"https://creativecommons.org/licenses/{m.group(1).lower()}/{ver}/"
    m = _PD_RE.search(url)
    if m:
        if m.group(1).lower() == "zero":
            return f"CC0 {m.group(2)}", f"https://creativecommons.org/publicdomain/zero/{m.group(2)}/"
        return "Public Domain", f"https://creativecommons.org/publicdomain/mark/{m.group(2)}/"
    return None


class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect that would drop to plain http (the first URL is already checked)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            raise urllib.error.URLError(f"refusing a redirect to a non-https URL: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Http:
    """Minimal JSON GET client. Tests substitute an object with the same method."""

    def get_json(self, url: str, params: Optional[dict] = None) -> dict:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
        if not url.lower().startswith("https://"):
            raise ValueError(f"refusing non-https URL: {url}")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "application/json"})
        with urllib.request.build_opener(_HttpsOnlyRedirect()).open(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
