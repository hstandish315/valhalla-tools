#!/usr/bin/env bash
#
# Register Valhalla in the GNOME application grid.
#
# Idempotent: re-running rewrites the same two files and nothing else. Touches
# only per-user paths under ~/.local/share -- no root, no system units.
#
# To revert:
#   rm -f ~/.local/share/applications/valhalla-monitor.desktop
#   rm -f ~/.local/share/icons/hicolor/256x256/apps/valhalla-monitor.png
#   update-desktop-database ~/.local/share/applications 2>/dev/null || true

set -euo pipefail

SRC="$(dirname "$(readlink -f "$0")")"
LAUNCHER="$SRC/valhalla-monitor"
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/256x256/apps"
DESKTOP="$APPS/valhalla-monitor.desktop"
# Frugal profile: no data is lost, and 15 fps still resolves the 7.5 Hz
# lightning flicker. See README for the measured cost of each flag.
FLAGS="--calm --fps 15"
ICON="$ICONS/valhalla-monitor.png"

[ -x "$LAUNCHER" ] || { echo "error: launcher not found or not executable: $LAUNCHER" >&2; exit 1; }
[ -f "$SRC/valhalla-monitor.png" ] || { echo "error: icon missing; run tools/make_icon.py" >&2; exit 1; }

echo "This will create (or overwrite):"
echo "  $DESKTOP"
echo "  $ICON"
echo "pointing at: $LAUNCHER $FLAGS"
echo
read -r -p "Proceed? [y/N] " reply
case "$reply" in
    [yY]|[yY][eE][sS]) ;;
    *) echo "aborted; nothing changed."; exit 0 ;;
esac

mkdir -p "$APPS" "$ICONS"
for f in "$DESKTOP" "$ICON"; do
    [ -e "$f" ] && cp -p "$f" "$f.bak.$(date +%s)"
done

install -m 0644 "$SRC/valhalla-monitor.png" "$ICON"
cat > "$DESKTOP" <<DESK
[Desktop Entry]
Type=Application
Version=1.0
Name=Valhalla
GenericName=System Monitor
Comment=CPU, GPU, memory and storage telemetry in a Norse storm theme
Exec=$LAUNCHER $FLAGS
Icon=valhalla-monitor
Terminal=false
Categories=System;Monitor;
Keywords=system;monitor;cpu;gpu;memory;storage;temperature;
StartupNotify=true
DESK
chmod 0644 "$DESKTOP"

update-desktop-database "$APPS" 2>/dev/null || true
gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true

# Verify against what actually landed on disk, not against what we intended.
echo
if desktop-file-validate "$DESKTOP" 2>/dev/null; then
    echo "validated: $DESKTOP"
else
    echo "note: desktop-file-validate reported issues (or is not installed)"
fi
echo "Exec line now reads: $(grep '^Exec=' "$DESKTOP")"
[ -f "$ICON" ] && echo "icon installed: $ICON ($(stat -c%s "$ICON") bytes)"
echo "Done. It may take a moment to appear in the app grid."
