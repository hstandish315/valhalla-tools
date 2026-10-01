"""
Window assembly and the animation clock.

Two clocks run here and they are deliberately decoupled:

  * the sampler thread polls hardware at `--interval` (1 s by default), which is
    as fast as CPU deltas and nvidia-smi are meaningful;
  * the GTK frame clock eases every on-screen value toward the last sample, so
    the dashboard moves continuously at display refresh rather than stepping
    once a second.
"""

from __future__ import annotations

import argparse
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from . import theme as T  # noqa: E402
from .gauges import RadialGauge  # noqa: E402
from .metrics import Sampler, fmt_bytes, host_info  # noqa: E402
from .panels import CPUPanel, GPUPanel, HeaderBar, MemoryPanel, StoragePanel  # noqa: E402

APP_ID = "dev.valhalla.MjolnirMonitor"

CSS = b"""
window.valhalla, window.valhalla > * { background-color: #04060c; }
headerbar.valhalla {
    background-color: #070c16;
    border-bottom: 1px solid #1b2a45;
    min-height: 34px;
    box-shadow: none;
}
headerbar.valhalla windowcontrols button { color: #7288a8; }
"""


class Dashboard(Gtk.ApplicationWindow):
    def __init__(self, app, interval: float, fps: int, calm: bool = False):
        super().__init__(application=app, title="Valhalla · Mjölnir System Monitor")
        self.add_css_class("valhalla")
        self.set_default_size(1480, 940)
        self.set_size_request(1200, 820)

        bar = Gtk.HeaderBar()
        bar.add_css_class("valhalla")
        bar.set_title_widget(Gtk.Label())     # our own header carries the title
        self.set_titlebar(bar)

        info = host_info()
        self.header = HeaderBar(info)

        cpu_model = self._short_cpu()
        self.g_cpu = RadialGauge("HUGINN", f"CPU · {cpu_model}", T.CYAN, "hagalaz")
        self.g_gpu = RadialGauge("SURTR", "GPU · core utilisation", T.AMBER, "sowilo")
        self.g_ram = RadialGauge("MIMIR", "MEMORY · system RAM", T.VIOLET, "mannaz",
                          core_label="used")
        self.g_dsk = RadialGauge("FAFNIR", "STORAGE · root volume", T.SEA, "fehu",
                          core_label="used")
        self.gauges = (self.g_cpu, self.g_gpu, self.g_ram, self.g_dsk)
        for g in self.gauges:
            g.calm = calm

        self.p_cpu, self.p_gpu = CPUPanel(), GPUPanel()
        self.p_ram, self.p_dsk = MemoryPanel(), StoragePanel()

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.append(self.header)

        gauge_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10,
                            homogeneous=True)
        gauge_row.set_margin_start(12)
        gauge_row.set_margin_end(12)
        gauge_row.set_margin_top(12)
        for g in self.gauges:
            gauge_row.append(g)
        root.append(gauge_row)

        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        bottom.set_margin_start(12)
        bottom.set_margin_end(12)
        bottom.set_margin_top(10)
        bottom.set_margin_bottom(12)
        bottom.set_vexpand(True)
        bottom.append(self.p_cpu)
        bottom.append(self.p_gpu)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        right.append(self.p_ram)
        right.append(self.p_dsk)
        bottom.append(right)
        root.append(bottom)

        self.set_child(root)

        self.animated = [self.header, *self.gauges, self.p_cpu, self.p_gpu,
                         self.p_ram, self.p_dsk]

        # A GTK tick callback would keep the frame clock running continuously
        # even on frames where nothing is queued for redraw, which costs real
        # CPU while the dashboard is idle. A plain timeout at the target rate
        # lets GTK go quiet whenever no widget calls queue_draw(), and it is
        # also immune to the display's refresh rate.
        self._last_step = time.monotonic()
        self._step_id = GLib.timeout_add(max(8, int(1000.0 / max(1, fps))),
                                         self._on_step)

        self.sampler = Sampler(self._on_sample, interval=interval)
        self.sampler.start()
        self.connect("close-request", self._on_close)
        self._install_shortcuts()

    # -- wiring ------------------------------------------------------------

    @staticmethod
    def _short_cpu() -> str:
        try:
            with open("/proc/cpuinfo") as fh:
                for line in fh:
                    if line.startswith("model name"):
                        name = line.split(":", 1)[1].strip()
                        return (name.replace("(R)", "").replace("(TM)", "")
                                    .replace(" Processor", "").strip())
        except OSError:
            pass
        return "processor"

    def _install_shortcuts(self):
        ctl = Gtk.EventControllerKey()

        def on_key(_c, keyval, _code, state):
            ctrl = state & Gdk.ModifierType.CONTROL_MASK
            if keyval in (Gdk.KEY_q, Gdk.KEY_Q) and ctrl:
                self.close()
                return True
            if keyval == Gdk.KEY_F11:
                if self.is_fullscreen():
                    self.unfullscreen()
                else:
                    self.fullscreen()
                return True
            if keyval == Gdk.KEY_Escape and self.is_fullscreen():
                self.unfullscreen()
                return True
            return False

        ctl.connect("key-pressed", on_key)
        self.add_controller(ctl)

    def _on_close(self, *_):
        if self._step_id:
            GLib.source_remove(self._step_id)
            self._step_id = 0
        self.sampler.stop()
        return False

    def _on_sample(self, snap, hist):
        gpu = snap.gpu
        self.header.update(snap, hist)

        self.g_cpu.update(
            snap.cpu.percent,
            [("temp", f"{snap.cpu.temp:.0f}°" if snap.cpu.temp else "--"),
             ("clock", f"{snap.cpu.freq_mhz/1000:.2f}G"),
             ("load", f"{snap.cpu.load[0]:.2f}")],
            corner=f"{len(snap.cpu.per_core)}T")

        vram = f"{gpu.mem_used/1024:.1f}/{gpu.mem_total/1024:.0f}G" if gpu.mem_total else "--"
        self.g_gpu.update(
            gpu.util,
            [("temp", f"{gpu.temp:.0f}°" if gpu.temp else "--"),
             ("power", f"{gpu.power:.0f}W" if gpu.power else "--"),
             ("vram", vram)],
            corner=gpu.name.replace("NVIDIA GeForce ", ""),
            offline=not gpu.present)

        self.g_ram.update(
            snap.ram.percent,
            [("used", f"{fmt_bytes(snap.ram.used)}"),
             ("free", f"{fmt_bytes(snap.ram.available)}"),
             ("swap", f"{snap.ram.swap_percent:.0f}%")],
            corner=f"{fmt_bytes(snap.ram.total)}iB")

        self.g_dsk.update(
            snap.disk.percent,
            [("used", f"{fmt_bytes(snap.disk.used)}"),
             ("free", f"{fmt_bytes(snap.disk.total - snap.disk.used)}"),
             ("i/o", f"{(snap.disk.read_bps+snap.disk.write_bps)/(1<<20):.0f}M")],
            corner=f"{fmt_bytes(snap.disk.total)}iB")

        for p in (self.p_cpu, self.p_gpu, self.p_ram, self.p_dsk):
            p.update(snap, hist)
        return GLib.SOURCE_REMOVE

    def _on_step(self):
        now = time.monotonic()
        dt = min(0.1, now - self._last_step)    # a stalled frame must not jump state
        self._last_step = now
        for widget in self.animated:
            if widget.tick(dt, now):
                widget.queue_draw()
        return GLib.SOURCE_CONTINUE


class Application(Gtk.Application):
    def __init__(self, interval: float, fps: int, calm: bool = False):
        super().__init__(application_id=APP_ID)
        self.interval = interval
        self.fps = fps
        self.calm = calm
        self.window: Dashboard | None = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        display = Gdk.Display.get_default()
        if display:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self):
        if self.window is None:
            self.window = Dashboard(self, self.interval, self.fps, self.calm)
        self.window.present()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="valhalla-monitor",
        description="Valhalla - Mjolnir system monitor (CPU / GPU / RAM / storage)")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="hardware sampling interval in seconds (default: 1.0)")
    ap.add_argument("--fps", type=int, default=30,
                    help="animation redraw rate cap (default: 30). 60 is smoother "
                         "but roughly doubles the CPU the dashboard itself uses")
    ap.add_argument("--calm", action="store_true",
                    help="drop the ambient gauge animation so the window repaints "
                         "only when a reading actually changes (near-zero idle CPU)")
    ap.add_argument("--fullscreen", action="store_true")
    args = ap.parse_args(argv)

    app = Application(max(0.2, args.interval), max(10, min(120, args.fps)),
                      calm=args.calm)
    if args.fullscreen:
        def _fs(a):
            if a.window:
                a.window.fullscreen()
        app.connect("activate", lambda a: GLib.idle_add(_fs, a))
    return app.run([])


if __name__ == "__main__":
    sys.exit(main())
