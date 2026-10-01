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

        # An explicit checkbox, not list selection: GTK's multi-select list adds every plain
        # click to the selection (so an old selection lingered, faintly highlighted, and was
        # exported again) and a single click also played the track.
        self.check = Gtk.CheckButton()
        self.check.set_valign(Gtk.Align.CENTER)
        self.check.set_tooltip_text("Tick the tracks to save or export")
        self.check.connect("toggled", lambda *_: view._on_select())
        self.append(self.check)

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
        self.btn_folder.set_tooltip_text("Copy the ticked tracks (or all of them if none are ticked) to a folder.")
        self.btn_folder.connect("clicked", lambda *_: self._export("folder"))
        bar.append(self.btn_folder)
        self.btn_usb = Gtk.Button(label="Export to USB…")
        self.btn_usb.set_tooltip_text("Copy the ticked tracks (or all of them if none are ticked) to a USB drive.")
        self.btn_usb.connect("clicked", lambda *_: self._export("usb"))
        bar.append(self.btn_usb)
        self.btn_remove = Gtk.Button(label="Remove")
        self.btn_remove.connect("clicked", self._on_remove)
        bar.append(self.btn_remove)
        self.btn_all = Gtk.Button(label="Select all")
        self.btn_all.connect("clicked", self._on_toggle_all)
        bar.append(self.btn_all)
        self.count = label("", ["status"], xalign=1.0)
        self.count.set_hexpand(True)
        bar.append(self.count)
        b.append(bar)

        self.list = Gtk.ListBox()
        self.list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list.set_activate_on_single_click(False)        # a double-click or Enter plays; one click doesn't
        self.list.connect("row-activated", lambda lb, row: ctx.play_entry(row.get_child().entry))
        self._bulk = False
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
    def _rows(self):
        row = self.list.get_first_child()
        while row is not None:
            yield row.get_child()
            row = row.get_next_sibling()

    def selected_entries(self) -> list:
        """Exactly the ticked tracks, in list order. Nothing else."""
        return [r.entry for r in self._rows() if r.check.get_active()]

    def selected(self):
        sel = self.selected_entries()
        return sel[0] if sel else None

    def refresh(self) -> None:
        keep = {e.path for e in self.selected_entries()}
        self._bulk = True
        self.list.remove_all()
        for e in self.ctx.library.entries:
            row = LibraryRow(self, e)
            row.check.set_active(e.path in keep)
            self.list.append(row)
        self._bulk = False
        n = len(self.ctx.library.entries)
        self.count.set_label(f"{n} track{'s' if n != 1 else ''}  ·  {self.ctx.library.music_dir}")
        self._on_select()

    def _set_all(self, checked: bool) -> None:
        self._bulk = True
        for r in self._rows():
            r.check.set_active(checked)
        self._bulk = False
        self._on_select()

    def _on_toggle_all(self, _btn) -> None:
        self._set_all(len(self.selected_entries()) < len(self.ctx.library.entries))

    def _on_select(self) -> None:
        if self._bulk:
            return
        sel = self.selected_entries()
        total = len(self.ctx.library.entries)
        scope = f"({len(sel)})" if sel else f"(all {total})"
        self.btn_remove.set_sensitive(bool(sel))
        self.btn_remove.set_label(f"Remove ({len(sel)})" if sel else "Remove")
        self.btn_folder.set_sensitive(total > 0)
        self.btn_usb.set_sensitive(total > 0)
        self.btn_folder.set_label(f"Save copies… {scope}")
        self.btn_usb.set_label(f"Export to USB… {scope}")
        self.btn_all.set_label("Select none" if sel and len(sel) == total else "Select all")
        if len(sel) == 1:
            e = sel[0]
            parts = [e.attribution or f"{e.title} — {e.license}", e.landing_url, e.path]
            self.detail.set_label("\n".join(p for p in parts if p))
        elif len(sel) > 1:
            self.detail.set_label(f"{len(sel)} tracks ticked. Save copies / Export to USB will use exactly these.")
        else:
            self.detail.set_label("Tick the tracks you want. With none ticked, Save copies and Export to USB "
                                  f"use everything ({total}). Double-click a track to play it.")

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
