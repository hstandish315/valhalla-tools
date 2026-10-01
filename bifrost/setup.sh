#!/usr/bin/env bash
# Bifrost setup - create the project venv and install numpy into it.
#
# What it changes: creates ./venv (with --system-site-packages so the system
# PyGObject and pycairo stay importable) and pip-installs numpy there.
# Nothing outside this directory is touched; no sudo.
#
# Idempotent: re-running with a healthy venv only re-verifies.
# Revert:     rm -rf "$(dirname "$0")/venv"
#
# Usage: ./setup.sh [--yes]
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

YES=0
[[ "${1:-}" == "--yes" ]] && YES=1

echo "Bifrost setup will:"
echo "  - create ./venv (system-site-packages) if missing"
echo "  - pip install numpy into it (network: pypi.org)"
if [[ $YES -eq 0 ]]; then
    read -r -p "Proceed? [y/N] " ans
    [[ "$ans" =~ ^[Yy]$ ]] || { echo "aborted"; exit 1; }
fi

if [[ ! -x venv/bin/python3 ]]; then
    python3 -m venv --system-site-packages venv
fi

# A venv with a stale interpreter path (project moved) is worse than none.
venv/bin/python3 -c "import sys" || { echo "venv is broken; remove ./venv and re-run"; exit 1; }

venv/bin/python3 -m pip install --quiet --disable-pip-version-check "numpy>=2.0"

# Verify against live state, not against what we think we installed.
venv/bin/python3 - <<'PY'
import importlib, sys
import numpy
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk
import cairo
assert "sounddevice" not in sys.modules, "PortAudio must never load (it segfaulted in testing; see README)"
print(f"ok: numpy {numpy.__version__}, Gtk {Gtk.get_major_version()}.{Gtk.get_minor_version()}, pycairo {cairo.version}")
PY
for tool in ffmpeg ffprobe pw-play; do
    command -v "$tool" >/dev/null || { echo "missing required tool: $tool"; exit 1; }
done
echo "tools ok: ffmpeg ffprobe pw-play"
