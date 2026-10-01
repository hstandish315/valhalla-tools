"""
Match search results against what the user actually asked for.

The free libraries only hold openly licensed music, and Openverse silently
ignores its `creator=` filter, so a search for a famous song by a famous band
returns *other* songs that merely share the title. Left alone, that looks like
success and downloads the wrong track. This module makes the mismatch visible:
every result is checked against the title and artist the user typed, mismatches
are labelled (and hidden by default when an artist was given), and an honest
explanation replaces an empty or misleading list.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .common import Track

_FILLER = {"the", "a", "an", "feat", "ft", "featuring", "by", "of", "and", "with", "vs"}


def norm(text: str) -> str:
    """Case-fold, strip accents and punctuation, collapse spaces."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^\w]+", " ", text.casefold(), flags=re.UNICODE).strip()


def tokens(text: str, drop_filler: bool = False) -> list[str]:
    toks = norm(text).split()
    return [t for t in toks if t not in _FILLER] if drop_filler else toks


def title_coverage(track: Track, query: str) -> float:
    """Fraction of the query's words that appear in the track title (1.0 = all)."""
    want = tokens(query)
    if not want:
        return 1.0
    have = set(tokens(track.title))
    return sum(1 for t in want if t in have) / len(want)


def artist_matches(track: Track, artist: str) -> bool:
    """True if every meaningful word of `artist` appears in the track's creator."""
    want = tokens(artist, drop_filler=True)
    if not want:
        return True
    have = set(tokens(track.creator))
    return all(t in have for t in want)


def query_coverage(track: Track, query: str) -> float:
    """For a single free-text box: how many query words appear in title + creator."""
    want = tokens(query, drop_filler=True)
    if not want:
        return 1.0
    have = set(tokens(track.title)) | set(tokens(track.creator))
    return sum(1 for t in want if t in have) / len(want)


@dataclass
class Hit:
    track: Track
    exact: bool
    note: str = ""              # "" | "Different artist" | "Partial match"
    coverage: float = 1.0


def annotate(tracks: list[Track], title: str, artist: str = "") -> list[Hit]:
    """
    Label each track and order exact matches first (stable otherwise).

    With an artist given: exact means the title words all match AND the artist
    matches. Without one: exact means every query word is found in the title or
    creator, so "bring me to life evanescence" typed in one box is held to the
    same standard as two separate fields.
    """
    hits: list[Hit] = []
    for t in tracks:
        if artist.strip():
            cov = title_coverage(t, title)
            by_artist = artist_matches(t, artist)
            exact = cov >= 1.0 and by_artist
            note = "" if exact else ("Different artist" if not by_artist else "Partial match")
        else:
            cov = query_coverage(t, title)
            exact = cov >= 1.0
            note = "" if exact else "Partial match"
        hits.append(Hit(t, exact, note, cov))
    hits.sort(key=lambda h: (not h.exact, -h.coverage))        # sort() is stable
    return hits


def describe(hits: list[Hit], title: str, artist: str, hidden: bool) -> str:
    """One honest sentence about what the user is looking at."""
    wanted = f"“{title}”" + (f" by {artist}" if artist.strip() else "")
    exact = sum(1 for h in hits if h.exact)
    if not hits:
        return (f"Nothing found for {wanted}. Free libraries only hold openly licensed music; "
                f"popular commercial songs aren't in them.")
    if exact:
        extra = len(hits) - exact
        tail = f" ({extra} other{'s' if extra != 1 else ''} hidden)" if hidden and extra else ""
        return f"{exact} match{'es' if exact != 1 else ''} for {wanted}{tail}."
    return (f"No openly licensed match for {wanted}. "
            f"{'These are other songs with similar words. ' if not hidden else ''}"
            f"Commercial music isn't in free libraries; use Live mode, Rip CD, or Library → Add files.")
