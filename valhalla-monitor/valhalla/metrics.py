"""
Telemetry sampling for Valhalla.

Everything here runs on a worker thread and hands finished snapshots to the GTK
main loop via GLib.idle_add, so a slow `nvidia-smi` or a stalled filesystem can
never stutter the animation.

Machine-specific notes:
  * CPU package temperature is k10temp/Tctl (Ryzen 9 9900X); Tccd1/Tccd2 are
    the two core dies. psutil exposes these without lm-sensors installed.
  * GPU telemetry comes from `nvidia-smi` in one batched query (~20 ms). pynvml
    is not installed and is not required.
  * Loop devices are excluded from disk I/O and from the filesystem list, or
    ~30 snap mounts drown out the real storage.
"""

from __future__ import annotations

import glob
import os
import platform
import re
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import psutil

HISTORY = 240          # samples retained per series (~4 min at 1 Hz)
GPU_FIELDS = (
    "utilization.gpu", "utilization.memory", "memory.used", "memory.total",
    "temperature.gpu", "power.draw", "power.limit", "clocks.sm", "clocks.mem",
    "fan.speed",
)
_IGNORED_FS = {"squashfs", "tmpfs", "devtmpfs", "overlay", "ramfs", "fuse.portal",
               "efivarfs", "autofs", "nsfs", "tracefs", "cgroup2"}


def _f(v, default=None):
    """nvidia-smi prints '[N/A]' and '[Not Supported]' for absent fields."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------- block device resolution ---
# A mount's temperature is only meaningful if it comes from the drive that
# actually backs it. A root filesystem is often filesystem -> LVM -> dm-crypt ->
# partition -> disk, so the device chain has to be walked rather than guessed from the name.

_BASE_DISK_CACHE: dict[str, str | None] = {}


def _base_disk(device: str) -> str | None:
    """Resolve /dev/mapper/... or /dev/nvme0n1p2 down to its physical disk."""
    if device in _BASE_DISK_CACHE:
        return _BASE_DISK_CACHE[device]
    name = os.path.basename(os.path.realpath(device))
    seen: set[str] = set()
    result: str | None = None
    while name and name not in seen:
        seen.add(name)
        base = f"/sys/class/block/{name}"
        slaves = f"{base}/slaves"
        if os.path.isdir(slaves):
            entries = sorted(os.listdir(slaves))
            if entries:                       # dm target -> its backing device
                name = entries[0]
                continue
        if os.path.exists(f"{base}/partition"):   # partition -> whole disk
            name = os.path.basename(os.path.dirname(os.path.realpath(base)))
            continue
        result = name if os.path.exists(base) else None
        break
    _BASE_DISK_CACHE[device] = result
    return result


def _disk_temps() -> dict[str, float]:
    """Map physical disk name (nvme0n1, sda) -> current temperature."""
    out: dict[str, float] = {}
    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            with open(f"{hw}/name") as fh:
                kind = fh.read().strip()
            if kind not in ("nvme", "drivetemp"):
                continue
            with open(f"{hw}/temp1_input") as fh:
                temp = int(fh.read().strip()) / 1000.0
        except (OSError, ValueError):
            continue
        devdir = os.path.realpath(f"{hw}/device")
        try:
            for entry in os.listdir(devdir):          # NVMe namespaces
                if re.fullmatch(r"nvme\d+n\d+", entry):
                    out[entry] = temp
            blockdir = os.path.join(devdir, "block")  # SATA via drivetemp
            if os.path.isdir(blockdir):
                for entry in os.listdir(blockdir):
                    out[entry] = temp
        except OSError:
            continue
    return out


@dataclass
class CPU:
    percent: float = 0.0
    per_core: list[float] = field(default_factory=list)
    freq_mhz: float = 0.0
    freq_max: float = 0.0
    temp: float | None = None
    ccd: list[tuple[str, float]] = field(default_factory=list)
    load: tuple[float, float, float] = (0.0, 0.0, 0.0)
    procs: int = 0
    threads: int = 0
    running: int = 0


@dataclass
class GPU:
    present: bool = False
    name: str = "NO CUDA DEVICE"
    util: float = 0.0
    mem_util: float = 0.0
    mem_used: float = 0.0        # MiB
    mem_total: float = 0.0       # MiB
    temp: float | None = None
    power: float | None = None
    power_cap: float | None = None
    clock_sm: float | None = None
    clock_mem: float | None = None
    fan: float | None = None
    driver: str = ""


@dataclass
class RAM:
    percent: float = 0.0
    used: int = 0
    total: int = 0
    available: int = 0
    cached: int = 0
    swap_percent: float = 0.0
    swap_used: int = 0
    swap_total: int = 0


@dataclass
class Mount:
    label: str
    device: str
    percent: float
    used: int
    total: int
    disk: str = ""
    temp: float | None = None


@dataclass
class Storage:
    percent: float = 0.0         # primary volume ("/")
    used: int = 0
    total: int = 0
    mounts: list[Mount] = field(default_factory=list)
    read_bps: float = 0.0
    write_bps: float = 0.0
    read_total: int = 0
    write_total: int = 0


@dataclass
class Proc:
    pid: int
    name: str
    cpu: float
    mem: int


@dataclass
class Snapshot:
    cpu: CPU = field(default_factory=CPU)
    gpu: GPU = field(default_factory=GPU)
    ram: RAM = field(default_factory=RAM)
    disk: Storage = field(default_factory=Storage)
    procs: list[Proc] = field(default_factory=list)
    uptime: float = 0.0
    stamp: float = 0.0


class History:
    """Fixed-length ring per series, for the trend graphs."""

    def __init__(self, interval: float = 1.0):
        self.series: dict[str, deque[float]] = {}
        self.interval = interval

    def span_label(self) -> str:
        """
        How much wall time the graphs actually cover.

        The ring is a fixed number of samples, so the window is HISTORY x the
        sampling interval - it is not a constant, and a hardcoded label would
        be wrong for any interval but the default.
        """
        seconds = HISTORY * self.interval
        if seconds < 90:
            return f"{seconds:.0f} s"
        if seconds < 5400:
            return f"{seconds / 60:.0f} min"
        return f"{seconds / 3600:.1f} h"

    def push(self, key: str, value: float):
        d = self.series.get(key)
        if d is None:
            d = self.series[key] = deque([value] * HISTORY, maxlen=HISTORY)
        d.append(value)

    def get(self, key: str) -> deque[float]:
        d = self.series.get(key)
        if d is None:
            d = self.series[key] = deque([0.0] * HISTORY, maxlen=HISTORY)
        return d


class Sampler:
    """Background poller. `on_sample(Snapshot, History)` is called on the GTK loop."""

    def __init__(self, on_sample, interval: float = 1.0):
        self._on_sample = on_sample
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.history = History(interval)

        self._nvsmi = shutil.which("nvidia-smi")
        self._gpu_static = self._query_gpu_static()
        self._prev_io = None
        self._prev_io_t = 0.0
        self._proc_tick = 0
        self._procs: list[Proc] = []

        psutil.cpu_percent(percpu=True)   # prime the delta counters
        psutil.cpu_percent()

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        self._thread = threading.Thread(target=self._run, name="valhalla-sampler",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _run(self):
        from gi.repository import GLib
        while not self._stop.is_set():
            began = time.monotonic()
            try:
                snap = self.sample()
                GLib.idle_add(self._on_sample, snap, self.history,
                              priority=GLib.PRIORITY_DEFAULT_IDLE)
            except Exception as exc:                      # keep the thread alive
                print(f"[valhalla] sample failed: {exc!r}")
            self._stop.wait(max(0.05, self._interval - (time.monotonic() - began)))

    # -- collection --------------------------------------------------------

    def sample(self) -> Snapshot:
        snap = Snapshot(stamp=time.time(), uptime=time.time() - psutil.boot_time())
        temps = psutil.sensors_temperatures()
        snap.cpu = self._cpu(temps)
        snap.gpu = self._gpu()
        snap.ram = self._ram()
        snap.disk = self._storage()
        snap.procs = self._top_procs()

        h = self.history
        h.push("cpu", snap.cpu.percent)
        h.push("gpu", snap.gpu.util)
        h.push("vram", (snap.gpu.mem_used / snap.gpu.mem_total * 100.0)
               if snap.gpu.mem_total else 0.0)
        h.push("ram", snap.ram.percent)
        h.push("cpu_temp", snap.cpu.temp or 0.0)
        h.push("gpu_temp", snap.gpu.temp or 0.0)
        h.push("gpu_power", snap.gpu.power or 0.0)
        h.push("io", (snap.disk.read_bps + snap.disk.write_bps) / (1 << 20))
        return snap

    def _cpu(self, temps) -> CPU:
        c = CPU()
        c.per_core = psutil.cpu_percent(percpu=True)
        c.percent = sum(c.per_core) / len(c.per_core) if c.per_core else 0.0
        try:
            f = psutil.cpu_freq()
            if f:
                c.freq_mhz, c.freq_max = f.current, f.max or 0.0
        except Exception:
            pass
        for entry in temps.get("k10temp", []):
            if entry.label == "Tctl":
                c.temp = entry.current
            elif entry.label.startswith("Tccd"):
                c.ccd.append((entry.label, entry.current))
        if c.temp is None:                       # non-Ryzen fallback
            for key in ("coretemp", "acpitz"):
                if temps.get(key):
                    c.temp = temps[key][0].current
                    break
        try:
            c.load = os.getloadavg()
        except OSError:
            pass
        c.procs = len(psutil.pids())
        # /proc/loadavg's 4th field is running/total threads. Summing
        # num_threads across process_iter gives the identical number but walks
        # every /proc entry - 8 ms versus 0.02 ms, once a second, forever.
        try:
            with open("/proc/loadavg") as fh:
                running, total = fh.read().split()[3].split("/")
            c.threads = int(total)
            c.running = int(running)
        except (OSError, ValueError, IndexError):
            c.threads = 0
        return c

    def _query_gpu_static(self) -> tuple[str, str]:
        if not self._nvsmi:
            return ("NO CUDA DEVICE", "")
        try:
            out = subprocess.run(
                [self._nvsmi, "--query-gpu=name,driver_version",
                 "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5.0, check=True).stdout
            name, driver = (p.strip() for p in out.splitlines()[0].split(","))
            return (name, driver)
        except Exception:
            return ("NVIDIA GPU", "")

    def _gpu(self) -> GPU:
        g = GPU(name=self._gpu_static[0], driver=self._gpu_static[1])
        if not self._nvsmi:
            return g
        try:
            out = subprocess.run(
                [self._nvsmi, f"--query-gpu={','.join(GPU_FIELDS)}",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=4.0, check=True).stdout
            row = [p.strip() for p in out.splitlines()[0].split(",")]
        except Exception:
            return g
        if len(row) < len(GPU_FIELDS):
            return g
        g.present = True
        g.util       = _f(row[0], 0.0)
        g.mem_util   = _f(row[1], 0.0)
        g.mem_used   = _f(row[2], 0.0)
        g.mem_total  = _f(row[3], 0.0)
        g.temp       = _f(row[4])
        g.power      = _f(row[5])
        g.power_cap  = _f(row[6])
        g.clock_sm   = _f(row[7])
        g.clock_mem  = _f(row[8])
        g.fan        = _f(row[9])
        return g

    def _ram(self) -> RAM:
        v, s = psutil.virtual_memory(), psutil.swap_memory()
        return RAM(percent=v.percent, used=v.total - v.available, total=v.total,
                   available=v.available, cached=getattr(v, "cached", 0),
                   swap_percent=s.percent, swap_used=s.used, swap_total=s.total)

    def _storage(self) -> Storage:
        st = Storage()
        drive_temps = _disk_temps()

        seen: set[str] = set()
        for part in psutil.disk_partitions(all=False):
            if part.fstype in _IGNORED_FS or not part.device.startswith("/dev/"):
                continue
            if part.mountpoint in seen:
                continue
            seen.add(part.mountpoint)
            try:
                u = psutil.disk_usage(part.mountpoint)
            except (PermissionError, OSError):
                continue
            m = Mount(label=part.mountpoint, device=part.device,
                      percent=u.percent, used=u.used, total=u.total)
            disk = _base_disk(part.device)
            if disk:
                m.disk = disk
                m.temp = drive_temps.get(disk)
            st.mounts.append(m)
            if part.mountpoint == "/":
                st.percent, st.used, st.total = u.percent, u.used, u.total

        st.mounts.sort(key=lambda m: (m.label != "/", m.label))

        try:
            per = psutil.disk_io_counters(perdisk=True) or {}
        except Exception:
            per = {}
        reads = sum(v.read_bytes for k, v in per.items() if not k.startswith("loop"))
        writes = sum(v.write_bytes for k, v in per.items() if not k.startswith("loop"))
        st.read_total, st.write_total = reads, writes
        now = time.monotonic()
        if self._prev_io is not None:
            dt = max(1e-3, now - self._prev_io_t)
            st.read_bps = max(0.0, (reads - self._prev_io[0]) / dt)
            st.write_bps = max(0.0, (writes - self._prev_io[1]) / dt)
        self._prev_io = (reads, writes)
        self._prev_io_t = now
        return st

    def _top_procs(self) -> list[Proc]:
        """Refreshed every third tick - walking /proc is the costliest probe."""
        self._proc_tick += 1
        if self._proc_tick % 3 and self._procs:
            return self._procs
        out: list[Proc] = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
            try:
                info = p.info
                cpu = info["cpu_percent"] or 0.0
                if cpu <= 0.0:
                    continue
                mi = info["memory_info"]
                out.append(Proc(info["pid"], info["name"] or "?", cpu,
                                mi.rss if mi else 0))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        out.sort(key=lambda x: x.cpu, reverse=True)
        self._procs = out[:6]
        return self._procs


# ----------------------------------------------------------------- helpers ---

def host_info() -> dict[str, str]:
    try:
        pretty = ""
        with open("/etc/os-release") as fh:
            for line in fh:
                if line.startswith("PRETTY_NAME="):
                    pretty = line.split("=", 1)[1].strip().strip('"')
                    break
    except OSError:
        pretty = platform.system()
    return {"host": socket.gethostname(), "kernel": platform.release(),
            "distro": pretty, "arch": platform.machine()}


def fmt_bytes(n: float, digits: int = 1) -> str:
    n = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024.0 or unit == "T":
            return f"{n:.{0 if unit == 'B' else digits}f}{unit}"
        n /= 1024.0
    return f"{n:.{digits}f}T"


def fmt_ibytes(n: float, digits: int = 1) -> str:
    """Full IEC form with a space: "512 B", "8.0 GiB". Use where width allows."""
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024.0 or unit == "TiB":
            return f"{n:.0f} B" if unit == "B" else f"{n:.{digits}f} {unit}"
        n /= 1024.0
    return f"{n:.{digits}f} TiB"


def fmt_rate(bps: float) -> str:
    return f"{fmt_bytes(bps)}/s"


def fmt_uptime(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    return f"{d}d {h:02d}:{m:02d}" if d else f"{h:02d}:{m:02d}"
