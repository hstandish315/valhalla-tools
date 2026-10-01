"""
Rip view ("The Forge"): rip tracks from an audio CD into a folder you choose.

The disc is read on a worker thread (opening a spinning-up drive can take
seconds), track names come from MusicBrainz by exact disc ID if that's enabled,
and every field stays editable so a disc MusicBrainz doesn't know is still
rippable with names you type. Ripped files are added to the library and can be
exported on to USB.
"""

from __future__ import annotations

import threading
import urllib.error

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from .. import cd, export, musicbrainz  # noqa: E402
from ..sources.match import norm  # noqa: E402
from .. import theme as T  # noqa: E402
from .export_dialog import ExportDialog, open_folder  # noqa: E402
from .player import fmt_time, label  # noqa: E402
from .widgets import Card  # noqa: E402

FORMAT_KEYS = list(cd.FORMATS)


def looks_like_filler(seconds: float, title: str) -> bool:
    """Silent padding tracks: very short, or MusicBrainz itself calls them "silence"."""
    return seconds < export.FILLER_SECONDS or norm(title) == "silence"


class RipRow(Gtk.Box):
    def __init__(self, track: cd.TocTrack, title: str):
        super().__init__(spacing=10)
        self.track = track
        self.filler = False
        self.set_margin_top(4)
        self.set_margin_bottom(4)
        self.check = Gtk.CheckButton()
        self.check.set_active(not track.is_data)
        self.check.set_sensitive(not track.is_data)
        self.append(self.check)
        num = label(f"{track.number:02d}", ["mono", "dim"])
        num.set_size_request(26, -1)
        self.append(num)
        self.title = Gtk.Entry()
        self.title.set_text(title if not track.is_data else "(data track — can't be ripped as audio)")
        self.title.set_sensitive(not track.is_data)
        self.title.set_hexpand(True)
        self.append(self.title)
        dur = label(fmt_time(track.seconds), ["mono", "dim"], xalign=1.0)
        dur.set_size_request(48, -1)
        self.append(dur)
        self.state = label("", ["status"], xalign=0.0)
        self.state.set_size_request(120, -1)
        self.append(self.state)

    def say(self, text: str, error: bool = False) -> None:
        self.state.set_label(text)
        (self.state.add_css_class if error else self.state.remove_css_class)("error")

    def set_filler(self, flag: bool) -> None:
        """Mark a (probably) silent padding track: start it unticked, with a visible reason."""
        self.filler = flag
        if not self.track.is_data:
            self.check.set_active(not flag)
            self.say("silence?" if flag else "")


class RipView(Gtk.Box):
    def __init__(self, ctx):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.ctx = ctx
        self.drives: list[cd.Drive] = []
        self.toc: cd.Toc | None = None
        self.disc = ""
        self.rows: list[RipRow] = []
        self.ripped: list = []
        self.cover: bytes | None = None         # front cover for the matched release, if found
        self._cancel = threading.Event()
        self._ripping = False
        self._gen = 0                       # discards answers from superseded refreshes
        self.set_margin_start(12)
        self.set_margin_end(12)
        self.set_margin_top(12)
        self.set_margin_bottom(6)

        card = Card("The Forge", "rip your own discs", T.EMBER, "thurisaz")
        card.set_vexpand(True)
        b = card.body

        top = Gtk.Box(spacing=8)
        self.dd_drive = Gtk.DropDown.new_from_strings(["(no drive)"])
        self.dd_drive.set_hexpand(True)
        top.append(self.dd_drive)
        refresh = Gtk.Button(label="Refresh")
        refresh.connect("clicked", lambda *_: self.refresh())
        top.append(refresh)
        self.btn_eject = Gtk.Button(label="Eject")
        self.btn_eject.connect("clicked", self._on_eject)
        top.append(self.btn_eject)
        b.append(top)
        self.disc_note = label("", ["status"], wrap=True)
        b.append(self.disc_note)

        meta = Gtk.Box(spacing=8)
        self.e_artist, self.e_album, self.e_year = Gtk.Entry(), Gtk.Entry(), Gtk.Entry()
        for e, ph, w in ((self.e_artist, "Album artist", -1), (self.e_album, "Album", -1),
                         (self.e_year, "Year", 80)):
            e.set_placeholder_text(ph)
            if w > 0:
                e.set_size_request(w, -1)
            else:
                e.set_hexpand(True)
            meta.append(e)
        b.append(meta)

        self.list = Gtk.ListBox()
        self.list.set_selection_mode(Gtk.SelectionMode.NONE)
        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        sw.set_child(self.list)
        b.append(sw)

        opts = Gtk.Box(spacing=10)
        opts.append(label("Format", ["dim"]))
        self.dd_fmt = Gtk.DropDown.new_from_strings([cd.FORMAT_LABELS[k] for k in FORMAT_KEYS])
        fmt = ctx.settings["rip_format"]
        self.dd_fmt.set_selected(FORMAT_KEYS.index(fmt) if fmt in FORMAT_KEYS else 0)
        self.dd_fmt.connect("notify::selected", lambda *_: ctx.settings.set(
            "rip_format", FORMAT_KEYS[self.dd_fmt.get_selected()]))
        opts.append(self.dd_fmt)
        self.folder_label = label(ctx.settings["rip_dir"], ["mono", "dim"], ellipsize=True)
        self.folder_label.set_hexpand(True)
        self.folder_label.set_halign(Gtk.Align.END)
        opts.append(self.folder_label)
        choose = Gtk.Button(label="Save to…")
        choose.set_tooltip_text("Choose the folder the ripped files are saved in.")
        choose.connect("clicked", self._on_choose)
        opts.append(choose)
        b.append(opts)

        opts2 = Gtk.Box(spacing=14)
        self.chk_lookup = Gtk.CheckButton(label="Look up names on MusicBrainz")
        self.chk_lookup.set_tooltip_text("Sends the disc's ID (not your files) to musicbrainz.org.")
        self.chk_lookup.set_active(bool(ctx.settings["rip_lookup"]))
        self.chk_lookup.connect("toggled", lambda w: ctx.settings.set("rip_lookup", w.get_active()))
        opts2.append(self.chk_lookup)
        self.chk_cover = Gtk.CheckButton(label="Embed cover art")
        self.chk_cover.set_tooltip_text("Fetched from the Cover Art Archive for the matched album (sends the "
                                        "release ID). FLAC and MP3 only; Ogg/Opus can't carry a picture.")
        self.chk_cover.set_active(bool(ctx.settings["rip_cover"]))
        self.chk_cover.connect("toggled", lambda w: ctx.settings.set("rip_cover", w.get_active()))
        opts2.append(self.chk_cover)
        self.chk_eject = Gtk.CheckButton(label="Eject when done")
        self.chk_eject.set_active(bool(ctx.settings["rip_eject"]))
        self.chk_eject.connect("toggled", lambda w: ctx.settings.set("rip_eject", w.get_active()))
        opts2.append(self.chk_eject)
        opts2.append(label("Rip discs you own.", ["status", "mute"]))
        b.append(opts2)

        self.bar = Gtk.ProgressBar()
        self.bar.set_visible(False)
        b.append(self.bar)

        foot = Gtk.Box(spacing=8)
        self.status = label("", ["status"], wrap=True)
        self.status.set_hexpand(True)
        foot.append(self.status)
        self.btn_all = Gtk.Button(label="Select all")
        self.btn_all.connect("clicked", self._on_toggle_all)
        foot.append(self.btn_all)
        self.btn_open = Gtk.Button(label="Open folder")
        self.btn_open.connect("clicked", lambda *_: open_folder(ctx.settings["rip_dir"]))
        foot.append(self.btn_open)
        self.btn_usb = Gtk.Button(label="Export to USB…")
        self.btn_usb.set_sensitive(False)
        self.btn_usb.connect("clicked", lambda *_: ExportDialog(ctx, self.ripped, "usb").present())
        foot.append(self.btn_usb)
        self.btn_rip = Gtk.Button(label="Rip selected")
        self.btn_rip.add_css_class("primary")
        self.btn_rip.connect("clicked", self._on_rip)
        foot.append(self.btn_rip)
        b.append(foot)
        self.append(card)
        self._sync_buttons()

    # ----------------------------------------------------------- the disc ---
    def on_show(self) -> None:
        if not self._ripping:
            self.refresh()

    def refresh(self) -> None:
        self._gen += 1
        gen = self._gen
        self.drives = cd.find_drives()
        self.dd_drive.set_model(Gtk.StringList.new([d.label for d in self.drives] or ["(no drive)"]))
        self._clear()
        if not self.drives:
            self._note("No optical drive found. Plug in a USB CD/DVD drive and press Refresh.")
            return
        self._note("Reading the disc…")
        device = self.drives[self.dd_drive.get_selected()].device
        threading.Thread(target=self._read, args=(gen, device), daemon=True, name="bifrost-toc").start()

    def _read(self, gen: int, device: str) -> None:
        try:
            toc = cd.read_toc(device)
        except cd.CDError as exc:
            GLib.idle_add(self._toc_failed, gen, str(exc))
            return
        did = cd.disc_id(toc)
        GLib.idle_add(self._toc_ready, gen, device, toc, did)
        if self.ctx.settings["rip_lookup"]:
            try:
                album = musicbrainz.lookup(did)
                GLib.idle_add(self._album_ready, gen, album, "")
                if album and album.mbid and self.ctx.settings["rip_cover"]:
                    try:
                        GLib.idle_add(self._cover_ready, gen, musicbrainz.fetch_cover(album.mbid))
                    except (urllib.error.URLError, OSError, ValueError):
                        GLib.idle_add(self._cover_ready, gen, None)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                GLib.idle_add(self._album_ready, gen, None, f"Name lookup failed ({exc}); type the names in.")

    def _toc_failed(self, gen, msg):
        if gen == self._gen:
            self._note(msg)
        return False

    def _toc_ready(self, gen, device, toc, did):
        if gen != self._gen:
            return False
        self._clear()                                   # reset first, then store the new disc
        self.toc, self.disc, self.device = toc, did, device
        for t in toc.tracks:
            row = RipRow(t, f"Track {t.number}")
            self.rows.append(row)
            self.list.append(row)
        for row in self.rows:
            row.set_filler(row.track.seconds < export.FILLER_SECONDS)
        n = len(toc.audio_tracks)
        self._note(f"{n} audio track{'s' if n != 1 else ''}, {toc.minutes:.0f} min. "
                   + ("Looking up names…" if self.ctx.settings["rip_lookup"] else "Type the names in.")
                   + self._filler_note())
        self._sync_buttons()
        return False

    def _album_ready(self, gen, album, problem):
        if gen != self._gen or self.toc is None:
            return False
        if album is None:
            self._note(problem or "MusicBrainz doesn't know this disc; type the names in.")
            return False
        self.e_artist.set_text(album.artist)
        self.e_album.set_text(album.title)
        self.e_year.set_text(album.year)
        for row in self.rows:
            info = album.track(row.track.number)
            if info and not row.track.is_data:
                row.title.set_text(info.title)
        for row in self.rows:
            if not row.track.is_data:
                row.set_filler(looks_like_filler(row.track.seconds, row.title.get_text()))
        self._note(f"Matched “{album.title}” by {album.artist} on MusicBrainz. Check the names, then rip."
                   + self._filler_note())
        return False

    def _filler_note(self) -> str:
        n = sum(1 for r in self.rows if r.filler and not r.track.is_data)
        return (f"  {n} very short or silent track{'s are' if n != 1 else ' is'} unticked "
                f"(tick them yourself if you want them).") if n else ""

    def _cover_ready(self, gen, cover):
        if gen == self._gen:
            self.cover = cover
            if cover:
                self._note(self.disc_note.get_label() + "  Cover art found.")
        return False

    def _clear(self) -> None:
        self.cover = None
        self.toc, self.rows = None, []
        self.list.remove_all()
        self._sync_buttons()

    def _note(self, text: str) -> None:
        self.disc_note.set_label(text)

    def _on_eject(self, _btn) -> None:
        if self._ripping or not self.drives:
            return
        cd.eject(self.drives[self.dd_drive.get_selected()].device)
        self._clear()
        self._note("Ejected.")

    # -------------------------------------------------------------- folder ---
    def _on_choose(self, _btn) -> None:
        dlg = Gtk.FileDialog()
        dlg.set_title("Save ripped tracks to…")
        dlg.select_folder(self.ctx.window, None, self._chose)

    def _chose(self, dlg, res) -> None:
        try:
            f = dlg.select_folder_finish(res)
        except GLib.Error:
            return
        self.ctx.settings.set("rip_dir", f.get_path())
        self.folder_label.set_label(f.get_path())

    def _on_toggle_all(self, _btn) -> None:
        audio = [r for r in self.rows if not r.track.is_data]
        want = not all(r.check.get_active() for r in audio)
        for r in audio:
            r.check.set_active(want)
        self.btn_all.set_label("Select none" if want else "Select all")

    # ------------------------------------------------------------- ripping ---
    def _sync_buttons(self) -> None:
        ready = self.toc is not None and not self._ripping
        self.btn_rip.set_sensitive(ready or self._ripping)
        self.btn_rip.set_label("Cancel" if self._ripping else "Rip selected")
        self.btn_eject.set_sensitive(not self._ripping)
        self.btn_usb.set_sensitive(bool(self.ripped) and not self._ripping)

    def _on_rip(self, _btn) -> None:
        if self._ripping:
            self._cancel.set()
            return
        chosen = [r for r in self.rows if r.check.get_active() and not r.track.is_data]
        if not chosen:
            self.status.set_label("Tick at least one track.")
            return
        fmt = FORMAT_KEYS[self.dd_fmt.get_selected()]
        root = self.ctx.settings["rip_dir"]
        total = len(self.toc.audio_tracks)
        jobs = []
        for r in chosen:
            tags = cd.Tags(title=r.title.get_text().strip() or f"Track {r.track.number}",
                           artist=self.e_artist.get_text().strip(),
                           album=self.e_album.get_text().strip(),
                           album_artist=self.e_artist.get_text().strip(),
                           track=r.track.number, total=total, year=self.e_year.get_text().strip())
            jobs.append((r, tags, cd.track_path(root, tags, cd.FORMATS[fmt][0])))
        self._ripping = True
        self._cancel.clear()
        self.ripped = []
        self.bar.set_visible(True)
        self.bar.set_fraction(0.0)
        self.status.set_label("Ripping…")
        self.status.remove_css_class("error")
        for r in self.rows:
            r.say("")
        self._sync_buttons()
        cover = self.cover if self.chk_cover.get_active() and fmt in cd.COVER_FORMATS else None
        threading.Thread(target=self._rip_all, args=(jobs, fmt, self.device, cover), daemon=True,
                         name="bifrost-rip").start()

    def _rip_all(self, jobs, fmt, device, cover=None) -> None:
        done_ok = 0
        for i, (row, tags, path) in enumerate(jobs):
            if self._cancel.is_set():
                break
            GLib.idle_add(row.say, "ripping…")

            def prog(frac, i=i, row=row):
                GLib.idle_add(self._progress, row, (i + frac) / len(jobs), frac)
            try:
                final = cd.rip_track(device, row.track, path, fmt, tags, progress=prog,
                                     cancel=self._cancel.is_set, cover=cover)
            except cd.CDError as exc:
                if str(exc) == "cancelled":
                    GLib.idle_add(row.say, "cancelled", True)
                    break
                GLib.idle_add(row.say, "failed", True)
                GLib.idle_add(self.status.set_label, str(exc))
                GLib.idle_add(self.status.add_css_class, "error")
                continue
            done_ok += 1
            GLib.idle_add(self._ripped_one, row, final, tags)
        GLib.idle_add(self._finished, done_ok, len(jobs))

    def _progress(self, row, overall, frac):
        self.bar.set_fraction(overall)
        row.say(f"{frac * 100:.0f}%")
        return False

    def _ripped_one(self, row, path, tags):
        entry = self.ctx.library.add_ripped(path, tags.title, tags.artist, tags.album, tags.track)
        self.ripped.append(entry)
        self.ctx.library_changed()
        row.say("done ✓")
        return False

    def _finished(self, ok, n):
        self._ripping = False
        self.bar.set_visible(False)
        cancelled = self._cancel.is_set()
        where = self.ctx.settings["rip_dir"]
        self.status.set_label(("Cancelled. " if cancelled else "") +
                              f"Ripped {ok} of {n} track{'s' if n != 1 else ''} to {where}.")
        if ok < n and not cancelled:
            self.status.add_css_class("error")
        self._sync_buttons()
        self.ctx.status(f"Ripped {ok} of {n} tracks")
        if ok and self.chk_eject.get_active() and not cancelled:
            cd.eject(self.device)
        return False
