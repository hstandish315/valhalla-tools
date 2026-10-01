"""
Export dialog: send tracks to a folder or a USB drive, as the original files or
as bilateral-processed copies. One dialog serves both entry points ("Save to
folder", "Export to USB"); only the starting destination differs.
"""

from __future__ import annotations

import os
import threading
from typing import Sequence

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

from .. import export  # noqa: E402
from .player import label  # noqa: E402

LAYOUTS = (("flat", "Artist - Title   (one folder; best for car stereos)"),
           ("artist", "Artist / Title"),
           ("album", "Artist / Album / Title"))
FORMATS = (("flac", "FLAC (lossless)"), ("mp3", "MP3 (high quality)"))
USB_FOLDER = "Bifrost"           # originals; bilateral versions go to export.EDITS_FOLDER (audio_edits)


def open_folder(path: str) -> None:
    Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(path).get_uri(), None)


class ExportDialog(Gtk.Window):
    def __init__(self, ctx, entries: Sequence, destination: str = "folder"):
        super().__init__(title="Export tracks", transient_for=ctx.window, modal=True)
        self.add_css_class("bifrost")
        self.set_default_size(560, -1)
        self.set_resizable(False)
        self.ctx, self.entries = ctx, list(entries)
        self.volumes: list[export.Volume] = []
        self.folder = ctx.settings["export_dir"]
        self._cancel = threading.Event()
        self._busy = False
        self._last_volume: export.Volume | None = None

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("start", "end", "top", "bottom"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)

        total = sum(os.path.getsize(e.path) for e in self.entries if os.path.exists(e.path))
        n = len(self.entries)
        box.append(label(f"{n} track{'s' if n != 1 else ''}  ·  {total / 1024 ** 2:.0f} MiB", ["title-big"]))
        # Say exactly what will be exported, so a stray selection can't go unnoticed.
        names = [e.title for e in self.entries]
        box.append(label("Exporting: " + ", ".join(names[:4]) + (f" … and {n - 4} more" if n > 4 else ""),
                         ["status"], wrap=True))

        # destination ---------------------------------------------------------
        row = Gtk.Box(spacing=8)
        self.btn_folder = Gtk.ToggleButton(label="Folder")
        self.btn_usb = Gtk.ToggleButton(label="USB drive")
        self.btn_usb.set_group(self.btn_folder)
        for b in (self.btn_folder, self.btn_usb):
            b.add_css_class("preset")
            row.append(b)
        box.append(row)

        self.stack = Gtk.Stack()
        fbox = Gtk.Box(spacing=8)
        self.folder_label = label(self.folder, ["mono", "dim"], ellipsize=True)
        self.folder_label.set_hexpand(True)
        fbox.append(self.folder_label)
        choose = Gtk.Button(label="Choose folder…")
        choose.connect("clicked", self._on_choose)
        fbox.append(choose)
        self.stack.add_named(fbox, "folder")
        ubox = Gtk.Box(spacing=8)
        self.dd_usb = Gtk.DropDown.new_from_strings(["(none)"])
        self.dd_usb.set_hexpand(True)
        ubox.append(self.dd_usb)
        rescan = Gtk.Button(label="Refresh")
        rescan.connect("clicked", lambda *_: self.scan_usb())
        ubox.append(rescan)
        self.stack.add_named(ubox, "usb")
        box.append(self.stack)
        self.usb_note = label("", ["status", "mute"], wrap=True)
        box.append(self.usb_note)

        # what to write -------------------------------------------------------
        mrow = Gtk.Box(spacing=8)
        self.btn_orig = Gtk.ToggleButton(label="Original files")
        self.btn_conv = Gtk.ToggleButton(label="Original → MP3")
        self.btn_proc = Gtk.ToggleButton(label="Bilateral version")
        self.btn_conv.set_group(self.btn_orig)
        self.btn_proc.set_group(self.btn_orig)
        self.btn_conv.set_tooltip_text("The unchanged music as MP3, for players that can't read FLAC (most cars).")
        self.btn_proc.set_tooltip_text("Renders each track through your current sweep and pulse settings, "
                                       "into a separate folder called audio_edits.")
        for b in (self.btn_orig, self.btn_conv, self.btn_proc):
            b.add_css_class("preset")
            mrow.append(b)
        self.dd_fmt = Gtk.DropDown.new_from_strings([t for _, t in FORMATS])
        self.dd_fmt.set_visible(False)
        mrow.append(self.dd_fmt)
        box.append(mrow)
        self.mode_note = label("", ["status", "mute"], wrap=True)
        box.append(self.mode_note)

        lrow = Gtk.Box(spacing=8)
        lrow.append(label("Names", ["dim"]))
        self.dd_layout = Gtk.DropDown.new_from_strings([t for _, t in LAYOUTS])
        keys = [k for k, _ in LAYOUTS]
        self.dd_layout.set_selected(keys.index(ctx.settings["export_layout"])
                                    if ctx.settings["export_layout"] in keys else 0)
        lrow.append(self.dd_layout)
        box.append(lrow)

        self.bar = Gtk.ProgressBar()
        self.bar.set_visible(False)
        box.append(self.bar)
        self.status = label("", ["status"], wrap=True)
        box.append(self.status)

        actions = Gtk.Box(spacing=8)
        actions.set_halign(Gtk.Align.END)
        self.btn_remove = Gtk.Button(label="Safely remove drive")
        self.btn_remove.set_visible(False)
        self.btn_remove.connect("clicked", self._on_remove)
        self.btn_open = Gtk.Button(label="Open folder")
        self.btn_open.set_visible(False)
        self.btn_cancel = Gtk.Button(label="Close")
        self.btn_cancel.connect("clicked", self._on_close_clicked)
        self.btn_go = Gtk.Button(label="Export")
        self.btn_go.add_css_class("primary")
        self.btn_go.connect("clicked", self._on_go)
        for b in (self.btn_remove, self.btn_open, self.btn_cancel, self.btn_go):
            actions.append(b)
        box.append(actions)

        self.btn_folder.connect("toggled", lambda *_: self._sync())
        self.btn_proc.connect("toggled", lambda *_: self._sync())
        self.btn_conv.connect("toggled", lambda *_: self._sync())
        self.connect("close-request", self._on_close_request)
        (self.btn_usb if destination == "usb" else self.btn_folder).set_active(True)
        self.btn_orig.set_active(True)
        if destination == "usb":
            self.dd_fmt.set_selected(1)               # MP3: it plays in cars; FLAC often won't
        self._sync()
        if destination == "usb":
            self.scan_usb()

    # ------------------------------------------------------------- state ---
    @property
    def use_usb(self) -> bool:
        return self.btn_usb.get_active()

    @property
    def processed(self) -> bool:
        return self.btn_proc.get_active()

    @property
    def converted(self) -> bool:
        return self.btn_conv.get_active()

    @property
    def mode(self) -> str:
        return "bilateral" if self.processed else "mp3" if self.converted else "original"

    def _sync(self) -> None:
        self.stack.set_visible_child_name("usb" if self.use_usb else "folder")
        self.dd_fmt.set_visible(self.processed)
        nd = [e for e in self.entries if not e.allows_modification]
        if self.mode != "original" and nd:
            self.mode_note.set_label(f"{len(nd)} track{'s are' if len(nd) != 1 else ' is'} licensed "
                                     f"no-derivatives and will be skipped in this mode.")
        elif self.processed:
            self.mode_note.set_label(f"Each track is rendered through your current sweep and pulse settings "
                                     f"and saved, with its tags and cover art, in a separate folder called "
                                     f"{export.EDITS_FOLDER}.")
        elif self.converted:
            self.mode_note.set_label("The unchanged music as MP3 (tags and cover art kept), for players that "
                                     "can't read FLAC. MP3 files are copied as they are.")
        else:
            self.mode_note.set_label("Exact copies of the files in your library.")
        if self.use_usb and not self.volumes:
            self.scan_usb()

    def scan_usb(self) -> None:
        try:
            self.volumes = export.removable_volumes()
        except export.ExportError as exc:
            self.volumes = []
            self.usb_note.set_label(str(exc))
        self.dd_usb.set_model(Gtk.StringList.new([v.title for v in self.volumes] or ["(none)"]))
        self.usb_note.set_label("" if self.volumes else
                                "No USB drive found. Plug one in, then press Refresh.")

    def _on_choose(self, _btn) -> None:
        dlg = Gtk.FileDialog()
        dlg.set_title("Choose a folder")
        dlg.select_folder(self, None, self._chose)

    def _chose(self, dlg, res) -> None:
        try:
            f = dlg.select_folder_finish(res)
        except GLib.Error:
            return
        self.folder = f.get_path()
        self.folder_label.set_label(self.folder)
        self.ctx.settings.set("export_dir", self.folder)

    # ---------------------------------------------------------- exporting ---
    def _on_go(self, _btn) -> None:
        if self._busy:
            return
        layout = LAYOUTS[self.dd_layout.get_selected()][0]
        self.ctx.settings.set("export_layout", layout)
        vol = None
        if self.use_usb:
            if not self.volumes:
                self._say("Plug in a USB drive first.", error=True)
                return
            vol = self.volumes[self.dd_usb.get_selected()]
        fmt = FORMATS[self.dd_fmt.get_selected()][0]
        self._busy = True
        self._cancel.clear()
        self.btn_go.set_sensitive(False)
        self.btn_cancel.set_label("Cancel")
        self.btn_remove.set_visible(False)
        self.btn_open.set_visible(False)
        self.bar.set_visible(True)
        self.bar.set_fraction(0.0)
        self._say("Starting…")
        threading.Thread(target=self._work, daemon=True, name="bifrost-export",
                         args=(vol, layout, fmt, self.mode, self.folder)).start()

    def _work(self, vol, layout, fmt, mode, folder) -> None:
        try:
            # Bilateral versions get a folder of their own, apart from the originals.
            if vol is not None:
                root = export.mount_volume(vol)
                dest = os.path.join(root, export.EDITS_FOLDER if mode == "bilateral" else USB_FOLDER)
                fat, safe = vol.is_fat, vol.windows_names
            else:
                dest = os.path.join(folder, export.EDITS_FOLDER) if mode == "bilateral" else folder
                fat, safe = False, False
            os.makedirs(dest, exist_ok=True)
            transform = (export.processing_transform(self.ctx.chain, self.entries) if mode == "bilateral"
                         else export.conversion_transform(self.entries) if mode == "mp3" else None)
            out_ext = fmt if mode == "bilateral" else "mp3" if mode == "mp3" else None

            def prog(done, total, name):
                GLib.idle_add(self._progress, done, total, name)

            res = export.copy_tracks(self.entries, dest, layout=layout, fat=fat, safe_names=safe, transform=transform,
                                     transform_ext=out_ext, replace_existing=(mode == "bilateral"),
                                     progress=prog,
                                     cancel=self._cancel.is_set)
            GLib.idle_add(self._done, res, dest, vol)
        except export.ExportError as exc:
            GLib.idle_add(self._failed, str(exc))
        except Exception as exc:                                # never leave the dialog stuck busy
            GLib.idle_add(self._failed, f"Export failed: {exc}")

    def _progress(self, done, total, name):
        self.bar.set_fraction(done / total if total else 0.0)
        self._say(f"Copying {name}…")
        return False

    def _failed(self, msg: str):
        self._busy = False
        self.btn_go.set_sensitive(True)
        self.btn_cancel.set_label("Close")
        self.bar.set_visible(False)
        self._say(msg, error=msg != "cancelled")
        if msg == "cancelled":
            self._say("Cancelled. Partly copied tracks were removed.")
        return False

    def _done(self, res: export.Result, dest: str, vol):
        self._busy = False
        self.btn_go.set_sensitive(True)
        self.btn_cancel.set_label("Close")
        self.bar.set_fraction(1.0)
        parts = [f"Copied {len(res.copied)} track{'s' if len(res.copied) != 1 else ''} "
                 f"({res.bytes / 1024 ** 2:.0f} MiB) to {dest}."]
        if res.replaced:
            parts.append(f"{len(res.replaced)} earlier version{'s were' if len(res.replaced) != 1 else ' was'} "
                         f"replaced with the new render.")
        if res.skipped:
            parts.append("Skipped: " + "; ".join(f"{t} ({r})" for t, r in res.skipped[:3])
                         + (" …" if len(res.skipped) > 3 else ""))
        if res.failed:
            parts.append("Failed: " + "; ".join(f"{t} ({r})" for t, r in res.failed[:3]))
        self._say("  ".join(parts), error=bool(res.failed and not res.copied))
        self._dest = dest
        self.btn_open.set_visible(True)
        try:
            self.btn_open.disconnect_by_func(self._open)
        except TypeError:
            pass
        self.btn_open.connect("clicked", self._open)
        if vol is not None:
            self._last_volume = vol
            self.btn_remove.set_visible(True)
        self.ctx.status(f"Exported {len(res.copied)} tracks to {dest}")
        return False

    def _open(self, *_):
        open_folder(self._dest)

    def _on_remove(self, _btn) -> None:
        if self._last_volume is None:
            return
        try:
            export.safely_remove(self._last_volume)
        except export.ExportError as exc:
            self._say(str(exc), error=True)
            return
        self.btn_remove.set_visible(False)
        self._say("It's safe to unplug the drive now.")

    # ------------------------------------------------------------ closing ---
    def _on_close_clicked(self, _btn) -> None:
        if self._busy:
            self._cancel.set()
        else:
            self.close()

    def _on_close_request(self, *_) -> bool:
        self._cancel.set()
        return False

    def _say(self, text: str, error: bool = False) -> None:
        self.status.set_label(text)
        (self.status.add_css_class if error else self.status.remove_css_class)("error")
