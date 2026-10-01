"""
Command-line CD tools: `bifrost-audio cd info` and `bifrost-audio cd rip`.

`info` is read-only: it reads the table of contents and (unless --no-lookup) asks
MusicBrainz for the track names. It is the safe first thing to run when a drive
is attached. `rip` saves every audio track to a folder. Rip discs you own.
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.error

from . import cd, musicbrainz
from .settings import Settings


def _drive(args) -> cd.Drive:
    drives = cd.find_drives()
    if not drives:
        raise cd.CDError("No optical drive found. Plug in a USB CD/DVD drive.")
    if args.device:
        return next((d for d in drives if d.device == args.device), cd.Drive(args.device))
    return drives[0]


def _album(toc, did, lookup: bool):
    if not lookup:
        return None
    try:
        return musicbrainz.lookup(did)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"  (MusicBrainz lookup failed: {exc})")
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bifrost-audio cd", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("info", "rip", "art"):
        p = sub.add_parser(name)
        p.add_argument("--device", help="e.g. /dev/sr0 (default: the first drive)")
        p.add_argument("--no-lookup", action="store_true", help="don't contact MusicBrainz")
        p.add_argument("--no-cover", action="store_true", help="don't fetch or embed cover art")
    art = sub.choices["art"]
    art.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    art.add_argument("--replace", action="store_true", help="also replace art that is already embedded")
    rip = sub.choices["rip"]
    rip.add_argument("--out", default=Settings()["rip_dir"], help="folder to save into")
    rip.add_argument("--format", choices=list(cd.FORMATS), default="flac")
    rip.add_argument("--tracks", help="e.g. 1,3,5-7 (default: all audio tracks)")
    args = ap.parse_args(argv)

    try:
        drive = _drive(args)
        toc = cd.read_toc(drive.device)
    except cd.CDError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    did = cd.disc_id(toc)
    album = _album(toc, did, not args.no_lookup)
    print(f"Drive : {drive.label}")
    print(f"Disc  : {len(toc.audio_tracks)} audio tracks, {toc.minutes:.1f} min   (MusicBrainz id {did})")
    if album:
        print(f"Match : {album.title} — {album.artist} ({album.year})")
    elif not args.no_lookup:
        print("Match : none (MusicBrainz doesn't know this disc)")
    for t in toc.tracks:
        info = album.track(t.number) if album else None
        name = info.title if info else f"Track {t.number}"
        kind = "  [data]" if t.is_data else ""
        print(f"  {t.number:2d}  {int(t.seconds // 60)}:{int(t.seconds % 60):02d}  {name}{kind}")
    if args.cmd == "info":
        return 0
    if args.cmd == "art":
        return _add_art(args, album)

    wanted = {t.number for t in toc.audio_tracks}
    if args.tracks:
        wanted = set()
        for part in args.tracks.split(","):
            lo, _, hi = part.partition("-")
            wanted |= set(range(int(lo), int(hi or lo) + 1))
        wanted &= {t.number for t in toc.audio_tracks}
    ext = cd.FORMATS[args.format][0]
    cover = None
    if album and album.mbid and not args.no_cover and args.format in cd.COVER_FORMATS:
        try:
            cover = musicbrainz.fetch_cover(album.mbid)
            print("Cover : " + ("found, will be embedded" if cover else "none available for this release"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"Cover : lookup failed ({exc})")
    failures = 0
    for t in toc.audio_tracks:
        if t.number not in wanted:
            continue
        info = album.track(t.number) if album else None
        tags = cd.Tags(title=info.title if info else f"Track {t.number}",
                       artist=(info.artist if info else ""), album=album.title if album else "",
                       album_artist=album.artist if album else "", track=t.number,
                       total=len(toc.audio_tracks), year=album.year if album else "")
        path = cd.track_path(args.out, tags, ext)
        interactive = sys.stdout.isatty()
        print(f"ripping {t.number:2d}  {tags.title} ...", end=" ", flush=True)

        def bar(frac, n=t.number):
            if interactive:                              # a live percentage only where it can overwrite itself
                print(f"\rripping {n:2d}  {tags.title} ... {frac * 100:3.0f}%", end="", flush=True)
        try:
            final = cd.rip_track(drive.device, t, path, args.format, tags, progress=bar, cover=cover)
            print(f"\rripped  {t.number:2d}  {os.path.relpath(final, args.out)}            " if interactive
                  else f"ripped  {t.number:2d}  {os.path.relpath(final, args.out)}")
        except cd.CDError as exc:
            failures += 1
            print(f"\rFAILED  {t.number:2d}  {exc}")
    print(f"Saved to {args.out}" + (f"  ({failures} failed)" if failures else ""))
    return 1 if failures else 0


def _add_art(args, album) -> int:
    """Embed the matched release's cover into files already ripped from this disc."""
    from .library import Library
    from .sources.match import norm
    if album is None or not album.mbid:
        print("error: MusicBrainz doesn't know this disc, so there is no release to take art from.", file=sys.stderr)
        return 1
    try:
        cover = musicbrainz.fetch_cover(album.mbid)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"error: couldn't fetch the cover: {exc}", file=sys.stderr)
        return 1
    if not cover:
        print("No cover art is available for this release.")
        return 1
    want = (norm(album.artist), norm(album.title))
    files = [e for e in Library().entries
             if (norm(e.creator), norm(e.album)) == want and e.path.lower().endswith((".flac", ".mp3"))]
    print(f"\nCover found. {len(files)} file(s) in your library are from {album.title!r} by {album.artist}:")
    done = skipped = failed = 0
    for e in sorted(files, key=lambda x: (x.track, x.path)):
        name = os.path.basename(e.path)
        if cd.has_cover(e.path) and not args.replace:
            print(f"  skip    {name}  (already has art)")
            skipped += 1
        elif args.dry_run:
            print(f"  would add  {name}")
        elif cd.embed_cover(e.path, cover, replace=args.replace):
            print(f"  added   {name}")
            done += 1
        else:
            print(f"  FAILED  {name}  (left untouched: verification did not pass)")
            failed += 1
    print(f"\n{'Dry run: nothing changed.' if args.dry_run else f'Done: {done} updated, {skipped} skipped, {failed} failed.'}")
    return 1 if failed else 0
