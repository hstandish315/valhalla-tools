"""Library view: everything downloaded, ripped or imported, with license and export."""

from __future__ import annotations

import os

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

from .. import theme as T  # noqa: E402
from .export_dialog import ExportDialog  # noqa: E402
from .player import fmt_time, label  # noqa: E402
from .widgets import Card  # noqa: E402


class LibraryRow(Gtk.Box):
    def __init__(self, view: "LibraryView", entry):
        super().__init__(spacing=12)
        self.entry = entry
        self.set_margin_top(6)
        self.set_margin_bottom(6)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.set_hexpand(True)
        text.append(label(entry.title, [], ellipsize=True))
        sub = entry.creator or "Local file"
        if entry.album:
            sub += f"  ·  {entry.album}"
        text.append(label(sub, ["status"], ellipsize=True))
        self.append(text)

        badge = label(entry.license.upper(), ["badge"])
        if not entry.allows_modification:
            badge.add_css_class("nd")
        badge.set_valign(Gtk.Align.CENTER)
        self.append(badge)

        dur = label(fmt_time(entry.duration) if entry.duration else "", ["mono", "dim"], xalign=1.0)
        dur.set_size_request(48, -1)
        dur.set_valign(Gtk.Align.CENTER)
        self.append(dur)

        play = Gtk.Button(label="Play")
        play.add_css_class("primary")
        play.set_valign(Gtk.Align.CENTER)
        play.connect("clicked", lambda *_: view.ctx.play_entry(entry))
        self.append(play)


class LibraryView(Gtk.Box):
    def __init__(self, ctx):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.ctx = ctx
        self.set_margin_start(12)
        self.set_margin_end(12)
        self.set_margin_top(12)
        self.set_margin_bottom(6)

        card = Card("The Hoard", "your tracks", T.VIOLET, "mannaz")
        card.set_vexpand(True)
        b = card.body

        bar = Gtk.Box(spacing=8)
        add = Gtk.Button(label="Add files…")
        add.connect("clicked", self._on_add)
        bar.append(add)
        self.btn_folder = Gtk.Button(label="Save copies…")
        self.btn_folder.set_tooltip_text("Copy the selected tracks (or all of them) to a folder.")
        self.btn_folder.connect("clicked", lambda *_: self._export("folder"))
        bar.append(self.btn_folder)
        self.btn_usb = Gtk.Button(label="Export to USB…")
        self.btn_usb.set_tooltip_text("Copy the selected tracks (or all of them) to a USB drive.")
        self.btn_usb.connect("clicked", lambda *_: self._export("usb"))
        bar.append(self.btn_usb)
        self.btn_remove = Gtk.Button(label="Remove")
        self.btn_remove.connect("clicked", self._on_remove)
        bar.append(self.btn_remove)
        self.count = label("", ["status"], xalign=1.0)
        self.count.set_hexpand(True)
        bar.append(self.count)
        b.append(bar)

        self.list = Gtk.ListBox()
        self.list.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        self.list.connect("selected-rows-changed", lambda *_: self._on_select())
        self.list.connect("row-activated", lambda lb, row: ctx.play_entry(row.get_child().entry))
        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        sw.set_child(self.list)
        b.append(sw)

        self.detail = label("", ["status", "mute"], wrap=True)
        self.detail.set_selectable(True)
        b.append(self.detail)
        self.append(card)
        self.refresh()

    # ------------------------------------------------------------- state ---
    def selected_entries(self) -> list:
        return [row.get_child().entry for row in self.list.get_selected_rows()]

    def selected(self):
        sel = self.selected_entries()
        return sel[0] if sel else None

    def refresh(self) -> None:
        keep = {e.path for e in self.selected_entries()}
        self.list.remove_all()
        for e in self.ctx.library.entries:
            self.list.append(LibraryRow(self, e))
        n = len(self.ctx.library.entries)
        self.count.set_label(f"{n} track{'s' if n != 1 else ''}  ·  {self.ctx.library.music_dir}")
        row = self.list.get_first_child()
        while row is not None:
            if row.get_child().entry.path in keep:
                self.list.select_row(row)
            row = row.get_next_sibling()
        self._on_select()

    def _on_select(self) -> None:
        sel = self.selected_entries()
        has_any = bool(self.ctx.library.entries)
        self.btn_remove.set_sensitive(bool(sel))
        self.btn_folder.set_sensitive(has_any)
        self.btn_usb.set_sensitive(has_any)
        if len(sel) == 1:
            e = sel[0]
            parts = [e.attribution or f"{e.title} — {e.license}", e.landing_url, e.path]
            self.detail.set_label("\n".join(p for p in parts if p))
        elif len(sel) > 1:
            self.detail.set_label(f"{len(sel)} tracks selected. Save copies / Export to USB will use these.")
        else:
            self.detail.set_label("Select tracks (Ctrl/Shift-click for several), or leave none "
                                  "selected to export everything.")

    # ----------------------------------------------------------- actions ---
    def _on_add(self, _btn):
        dialog = Gtk.FileDialog()
        dialog.set_title("Add music")
        flt = Gtk.FileFilter()
        flt.set_name("Audio files")
        for pat in ("*.mp3", "*.flac", "*.wav", "*.ogg", "*.oga", "*.opus", "*.m4a"):
            flt.add_pattern(pat)
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(flt)
        dialog.set_filters(store)
        dialog.open_multiple(self.ctx.window, None, self._add_done)

    def _add_done(self, dialog, result):
        try:
            files = dialog.open_multiple_finish(result)
        except GLib.Error:
            return                              # cancelled
        added = skipped = 0
        for i in range(files.get_n_items()):
            path = files.get_item(i).get_path()
            if path and self.ctx.library.add_local(path):
                added += 1
            else:
                skipped += 1
        self.ctx.library_changed()
        msg = f"Added {added} file{'s' if added != 1 else ''}"
        self.ctx.status(msg + (f"; {skipped} skipped (not audio)" if skipped else ""),
                        error=bool(skipped and not added))

    def _on_remove(self, _btn):
        for e in self.selected_entries():
            # Only re-downloadable files are ever deleted from disk. A ripped track
            # costs a 20-minute disc read to recreate and an imported file is the
            # user's own, so those are only taken off the list.
            self.ctx.library.remove(e.path, delete_file=e.source in ("openverse", "archive"))
        self.ctx.library_changed()

    def _export(self, destination: str) -> None:
        entries = self.selected_entries() or list(self.ctx.library.entries)
        if not entries:
            self.ctx.status("The library is empty.", error=True)
            return
        ExportDialog(self.ctx, entries, destination).present()
