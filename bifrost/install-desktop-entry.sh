#!/usr/bin/env bash
#
# Register Bifrost in the GNOME application grid.
#
# Idempotent: re-running rewrites the same two files and nothing else. Touches
# only per-user paths under ~/.local/share -- no root, no system units.
#
# To revert:
#   rm -f ~/.local/share/applications/bifrost-audio.desktop
#   rm -f ~/.local/share/icons/hicolor/256x256/apps/bifrost-audio.png
#   update-desktop-database ~/.local/share/applications 2>/dev/null || true

set -euo pipefail

SRC="$(dirname "$(readlink -f "$0")")"
LAUNCHER="$SRC/bifrost-audio"
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/256x256/apps"
DESKTOP="$APPS/bifrost-audio.desktop"
ICON="$ICONS/bifrost-audio.png"

[ -x "$LAUNCHER" ] || { echo "error: launcher not found or not executable: $LAUNCHER" >&2; exit 1; }
[ -x "$SRC/venv/bin/python3" ] || { echo "error: not set up yet; run $SRC/setup.sh first" >&2; exit 1; }
[ -f "$SRC/bifrost-audio.png" ] || { echo "error: icon missing; run tools/make_icon.py" >&2; exit 1; }

echo "This will create (or overwrite):"
echo "  $DESKTOP"
echo "  $ICON"
echo "pointing at: $LAUNCHER"
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

install -m 0644 "$SRC/bifrost-audio.png" "$ICON"
cat > "$DESKTOP" <<DESK
[Desktop Entry]
Type=Application
Version=1.0
Name=Bifrost
GenericName=Focus Audio
Comment=Bilateral sweep and amplitude-modulated focus audio for any music, with a free-music browser
Exec=$LAUNCHER
Icon=bifrost-audio
Terminal=false
Categories=AudioVideo;Audio;Player;
Keywords=adhd;focus;bilateral;8d;music;binaural;study;
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
