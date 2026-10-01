"""
Synthetic identity for published figures.

The documentation images are renders of the real widgets, which would otherwise
print the author's hostname, exact kernel and running process names. Figures are
generated through this module so the published images carry none of that.
"""
from __future__ import annotations

from valhalla.metrics import Proc

HOST = {"host": "workstation", "kernel": "6.14.0-generic",
        "distro": "Ubuntu 24.04 LTS", "arch": "x86_64"}

_NAMES = ("firefox", "python3", "gnome-shell", "code", "pipewire")


def anonymise(snap):
    """Replace real process names with generic ones; keep the shape of the data."""
    snap.procs = [Proc(1000 + i, _NAMES[i % len(_NAMES)], p.cpu, p.mem)
                  for i, p in enumerate(snap.procs)]
    return snap
