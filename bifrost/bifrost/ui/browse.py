"""Browse view: search Openverse / the Internet Archive and download into the library."""

from __future__ import annotations

import threading

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from .. import downloader  # noqa: E402
from .. import theme as T  # noqa: E402
from ..sources import archive, match, openverse  # noqa: E402
from ..sources.common import Track  # noqa: E402
from .player import fmt_time, label  # noqa: E402
from .widgets import Card  # noqa: E402

SOURCES = ("Openverse", "Internet Archive")
HOSTS = tuple(sorted(set(openverse.HOSTS) | set(archive.HOSTS)))


class ResultRow(Gtk.Box):
    """One search hit with its own download button and progress bar."""

    def __init__(self, view: "BrowseView", hit: match.Hit):
        super().__init__(spacing=12)
        track = hit.track
        self.view, self.track, self.hit = view, track, hit
        self._cancel = threading.Event()
        self._entries: list = []
        self.set_margin_top(6)
        self.set_margin_bottom(6)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.set_hexpand(True)
        text.append(label(track.title, [], ellipsize=True))
        # The artist is what tells two songs with the same title apart, so it gets
        # normal weight and sits on its own line, not in small grey print.
        who = label(track.creator or "unknown artist", ["artist"], ellipsize=True)
        text.append(who)
        text.append(label(track.provider or track.source, ["status", "mute"], ellipsize=True))
        self.msg = label("", ["status"], ellipsize=True)
        self.msg.set_visible(False)
        text.append(self.msg)
        self.bar = Gtk.ProgressBar()
        self.bar.set_visible(False)
        text.append(self.bar)
        self.append(text)

        if hit.note:
            note = label(hit.note.upper(), ["badge", "nd"])
            note.set_tooltip_text("This is not the song and artist you searched for.")
            note.set_valign(Gtk.Align.CENTER)
            self.append(note)

        badge = label(track.license.upper(), ["badge"])
        if not track.allows_modification:
            badge.add_css_class("nd")
            badge.set_tooltip_text("No-derivatives: you can play it, but not export a processed copy.")
        badge.set_valign(Gtk.Align.CENTER)
        self.append(badge)

        dur = label(fmt_time(track.duration) if track.duration else "", ["mono", "dim"], xalign=1.0)
        dur.set_size_request(48, -1)
        dur.set_valign(Gtk.Align.CENTER)
        self.append(dur)

        self.btn = Gtk.Button(label="Download")
        self.btn.set_valign(Gtk.Align.CENTER)
        self.btn.set_size_request(104, -1)
        self.btn.connect("clicked", self._on_click)
        self.append(self.btn)

    def _on_click(self, _btn):
        if self._entries:                       # finished: now it plays
            self.view.ctx.play_entry(self._entries[0])
            return
        if self.btn.get_label() == "Cancel":
            self._cancel.set()
            return
        self._cancel.clear()
        self.btn.set_label("Cancel")
        self.msg.set_visible(False)
        self.bar.set_visible(True)
        self.bar.set_fraction(0.0)
        threading.Thread(target=self._work, daemon=True, name="bifrost-download").start()

    # runs off the UI thread; every UI touch goes through GLib.idle_add
    def _work(self):
        results: list[tuple[Track, str]] = []
        try:
            tracks = [self.track]
            if self.track.source == "archive":
                tracks = archive.resolve(self.track)
                if not tracks:
                    raise downloader.DownloadError("this item has no usable open license or audio files")
            for i, t in enumerate(tracks):
                def prog(done, total, i=i, n=len(tracks)):
                    frac = (done / total) if total else 0.0
                    GLib.idle_add(self.bar.set_fraction, (i + frac) / n)
                path = downloader.download(t, self.view.ctx.library.music_dir, HOSTS,
                                           progress=prog, cancel=self._cancel.is_set)
                results.append((t, path))
        except downloader.DownloadError as exc:
            GLib.idle_add(self._finish, results, str(exc))
            return
        except Exception as exc:                # network libraries raise many things
            GLib.idle_add(self._finish, results, f"download failed: {exc}")
            return
        GLib.idle_add(self._finish, results, "")

    def _finish(self, results, error: str):
        self.bar.set_visible(False)
        self._entries = [self.view.ctx.library.add_download(t, p) for t, p in results]
        if self._entries:
            self.view.ctx.library_changed()
            n = len(self._entries)
            self.btn.set_label("Play" if n == 1 else f"Play ({n})")
            self.btn.add_css_class("primary")
            self.msg.set_label("Saved to your library" + (f" · {error}" if error else ""))
            self.msg.set_visible(True)
        else:
            self.btn.set_label("Retry" if error != "cancelled" else "Download")
            self.msg.set_label(error)
            self.msg.add_css_class("error")
            self.msg.set_visible(bool(error))
        return False


class BrowseView(Gtk.Box):
    def __init__(self, ctx):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.ctx = ctx
        self._page = 1
        self._title = ""
        self._artist = ""
        self._source = 0
        self._with_artist = True        # query variant in use (artist words included?)
        self._hits: list[match.Hit] = []
        self._total = 0
        self._searching = False
        self.set_margin_start(12)
        self.set_margin_end(12)
        self.set_margin_top(12)
        self.set_margin_bottom(6)

        card = Card("The Wells", "free & open music", T.SEA, "fehu")
        card.set_vexpand(True)
        b = card.body

        row = Gtk.Box(spacing=8)
        self.entry = Gtk.SearchEntry()
        self.entry.set_placeholder_text("Song or search — try “ambient”, “lofi”, “piano”")
        self.entry.set_hexpand(True)
        self.entry.connect("activate", lambda *_: self.search())
        row.append(self.entry)
        self.artist = Gtk.Entry()
        self.artist.set_placeholder_text("Artist (optional)")
        self.artist.set_size_request(200, -1)
        self.artist.connect("activate", lambda *_: self.search())
        row.append(self.artist)
        self.dd = Gtk.DropDown.new_from_strings(list(SOURCES))
        self.dd.connect("notify::selected", lambda *_: self.search() if self._title else None)
        row.append(self.dd)
        go = Gtk.Button(label="Search")
        go.add_css_class("primary")
        go.connect("clicked", lambda *_: self.search())
        row.append(go)
        b.append(row)

        opts = Gtk.Box(spacing=12)
        opts.append(label("Creative Commons and public-domain music only — popular commercial songs "
                          "aren't in these libraries (use Live mode or Rip CD for those).",
                          ["status", "mute"], wrap=True))
        opts.get_first_child().set_hexpand(True)
        self.hide = Gtk.CheckButton(label="Hide non-matching")
        self.hide.set_active(True)
        self.hide.set_tooltip_text("Hide results that aren't the song and artist you typed.")
        self.hide.connect("toggled", lambda *_: self._refilter())
        opts.append(self.hide)
        b.append(opts)

        self.list = Gtk.ListBox()
        self.list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list.set_filter_func(self._row_visible)
        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        sw.set_child(self.list)
        b.append(sw)

        foot = Gtk.Box(spacing=10)
        self.status = label("Search to begin.", ["status"], wrap=True)
        self.status.set_hexpand(True)
        foot.append(self.status)
        self.more = Gtk.Button(label="More results")
        self.more.set_visible(False)
        self.more.connect("clicked", lambda *_: self.search(more=True))
        foot.append(self.more)
        b.append(foot)
        self.append(card)

    def focus_search(self):
        self.entry.grab_focus()

    # ----------------------------------------------------------- filtering ---
    def _row_visible(self, row) -> bool:
        child = row.get_child()
        hit = getattr(child, "hit", None)
        return hit is None or hit.exact or not self.hide.get_active()

    def _refilter(self) -> None:
        self.list.invalidate_filter()
        self._describe()

    def _describe(self) -> None:
        if not self._title:
            return
        msg = match.describe(self._hits, self._title, self._artist, self.hide.get_active())
        self.status.set_label(f"{msg}  ·  {SOURCES[self._source]}")
        self.status.remove_css_class("error")

    # -------------------------------------------------------------- search ---
    def search(self, more: bool = False):
        if self._searching:
            return
        if more:
            self._page += 1
        else:
            title = self.entry.get_text().strip()
            if not title:
                return
            self._title, self._artist = title, self.artist.get_text().strip()
            self._page, self._source, self._with_artist = 1, self.dd.get_selected(), True
            self._hits, self._total = [], 0
            self.list.remove_all()
            self.hide.set_active(bool(self._artist))
        self._searching = True
        self.status.set_label(f"Searching {SOURCES[self._source]}…")
        self.status.remove_css_class("error")
        self.more.set_visible(False)
        threading.Thread(target=self._search_work, daemon=True, name="bifrost-search",
                         args=(self._title, self._artist, self._page, self._source, self._with_artist)).start()

    def _fetch(self, title, artist, page, source, with_artist):
        if source == 0:
            q = f"{title} {artist}".strip() if (artist and with_artist) else title
            return openverse.search(q, page=page)
        return archive.search(title, page=page, artist=artist if with_artist else "")

    def _search_work(self, title, artist, page, source, with_artist):
        try:
            tracks, total = self._fetch(title, artist, page, source, with_artist)
            if not tracks and page == 1 and artist and with_artist:
                # Adding the artist can over-constrain the server search; retry on the
                # title alone and let the client-side matcher label what comes back.
                with_artist = False
                tracks, total = self._fetch(title, artist, page, source, with_artist)
        except Exception as exc:
            GLib.idle_add(self._search_done, [], 0, f"Search failed: {exc}", with_artist)
            return
        GLib.idle_add(self._search_done, tracks, total, "", with_artist)

    def _search_done(self, tracks, total, error, with_artist):
        self._searching = False
        if error:
            self.status.set_label(error)
            self.status.add_css_class("error")
            return False
        self._with_artist, self._total = with_artist, total
        new_hits = match.annotate(tracks, self._title, self._artist)
        self._hits += new_hits
        for h in new_hits:
            self.list.append(ResultRow(self, h))
        self.list.invalidate_filter()
        self._describe()
        shown = len(self._hits)
        self.more.set_visible(bool(tracks) and shown < total)
        return False
