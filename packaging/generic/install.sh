#!/bin/sh
# Install Roseblox for the current user on any Linux distribution, using the system's
# Python, GTK 4 and libadwaita. On Arch Linux, prefer the packages in packaging/arch-git.
#
#   packaging/generic/install.sh              install or update
#   packaging/generic/install.sh --uninstall  remove (keeps your settings and Roblox apps)
#
# Files go under $PREFIX (default ~/.local): the app in lib/roseblox, the `roseblox`
# command in bin, and the menu entry and icon in share.

set -eu

APP_ID=io.github.roseblox.Roseblox
PREFIX=${PREFIX:-"$HOME/.local"}
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
LIB="$PREFIX/lib/roseblox"
BIN="$PREFIX/bin/roseblox"
DESKTOP="$PREFIX/share/applications/$APP_ID.desktop"
ICON="$PREFIX/share/icons/hicolor/scalable/apps/$APP_ID.svg"
LICENSE_FILE="$PREFIX/share/licenses/roseblox/LICENSE"
MARKER="$LIB/.installed-by-roseblox"

say() { printf '%s\n' "$*"; }
fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }

package_manager() {
    ids=""
    if [ -r /etc/os-release ]; then
        ids=$(. /etc/os-release && printf '%s %s' "${ID:-}" "${ID_LIKE:-}")
    fi
    for id in $ids; do
        case "$id" in
            arch) echo pacman; return ;;
            debian|ubuntu) echo apt; return ;;
            fedora|rhel|centos) echo dnf; return ;;
            suse|opensuse) echo zypper; return ;;
        esac
    done
    echo unknown
}

dependency_command() {
    case "$(package_manager)" in
        apt) echo "sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 libarchive-tools xdg-utils" ;;
        dnf) echo "sudo dnf install python3-gobject gtk4 libadwaita bsdtar xdg-utils" ;;
        zypper) echo "sudo zypper install python3-gobject python3-gobject-Gdk typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 bsdtar xdg-utils" ;;
        pacman) echo "sudo pacman -S --needed python-gobject gtk4 libadwaita libarchive xdg-utils" ;;
        *) echo "" ;;
    esac
}

uninstall() {
    if [ ! -e "$MARKER" ] && [ -e "$LIB" ]; then
        fail "$LIB was not installed by this script; nothing was removed."
    fi
    rm -rf "$LIB"
    for file in "$BIN" "$DESKTOP" "$ICON" "$LICENSE_FILE"; do
        rm -f "$file"
    done
    rmdir "$PREFIX/share/licenses/roseblox" 2>/dev/null || true
    refresh_desktop
    say "Roseblox was removed from $PREFIX."
    say "Your settings and Roblox apps are kept. To remove them too, see \"Uninstall\" in docs/INSTALL.md."
}

refresh_desktop() {
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database -q "$PREFIX/share/applications" 2>/dev/null || true
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1 && [ -e "$PREFIX/share/icons/hicolor/index.theme" ]; then
        gtk-update-icon-cache -q -t "$PREFIX/share/icons/hicolor" 2>/dev/null || true
    fi
}

check_requirements() {
    PYTHON=$(command -v python3 || true)
    [ -n "$PYTHON" ] || fail "python3 was not found. Install Python 3.10 or newer."
    missing=$("$PYTHON" - 2>/dev/null <<'EOF'
import sys
if sys.version_info < (3, 10):
    print(f"Python 3.10 or newer (this is {sys.version.split()[0]})")
    raise SystemExit
try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk
except (ImportError, ValueError) as error:
    print(f"PyGObject with GTK 4 and libadwaita ({error})")
    raise SystemExit
if (Gtk.get_major_version(), Gtk.get_minor_version()) < (4, 10):
    print(f"GTK 4.10 or newer (this is {Gtk.get_major_version()}.{Gtk.get_minor_version()})")
elif (Adw.get_major_version(), Adw.get_minor_version()) < (1, 4):
    print(f"libadwaita 1.4 or newer (this is {Adw.get_major_version()}.{Adw.get_minor_version()})")
EOF
)
    if [ -n "$missing" ]; then
        say "Roseblox needs $missing."
        command=$(dependency_command)
        [ -n "$command" ] && say "Install it with: $command"
        exit 1
    fi
    for tool in bsdtar xdg-mime; do
        if ! command -v "$tool" >/dev/null 2>&1; then
            command=$(dependency_command)
            say "Warning: \`$tool\` was not found. Roseblox needs it to install Roblox and to open Roblox links."
            [ -n "$command" ] && say "Install it with: $command"
        fi
    done
}

install() {
    [ -f "$ROOT/src/roseblox/__main__.py" ] || fail "Run this script from a Roseblox checkout."
    check_requirements
    if [ "$(package_manager)" = pacman ]; then
        say "Note: on Arch-based systems the roseblox-git package is recommended; see docs/INSTALL.md."
    fi
    if [ -e "$LIB" ] && [ ! -e "$MARKER" ]; then
        fail "$LIB exists but was not installed by this script; remove it first."
    fi

    staging="$PREFIX/lib/.roseblox-install.$$"
    rm -rf "$staging"
    mkdir -p "$staging"
    trap 'rm -rf "$staging"' EXIT
    cp -R "$ROOT/src/roseblox" "$staging/roseblox"
    find "$staging" -name __pycache__ -type d -prune -exec rm -rf {} +
    : > "$staging/.installed-by-roseblox"
    rm -rf "$LIB"
    mv "$staging" "$LIB"
    trap - EXIT

    mkdir -p "$(dirname "$BIN")"
    cat > "$BIN.tmp" <<EOF
#!$PYTHON
# Installed by Roseblox's packaging/generic/install.sh.
import os
import sys

LIB = $(printf '%s' "$LIB" | "$PYTHON" -c 'import sys; print(repr(sys.stdin.read()))')
sys.path.insert(0, LIB)
# The background update checker and activity watcher start "python3 -m roseblox".
os.environ["PYTHONPATH"] = os.pathsep.join(filter(None, (LIB, os.environ.get("PYTHONPATH"))))
from roseblox.__main__ import main

sys.exit(main())
EOF
    chmod 755 "$BIN.tmp"
    mv "$BIN.tmp" "$BIN"

    mkdir -p "$(dirname "$DESKTOP")" "$(dirname "$ICON")" "$(dirname "$LICENSE_FILE")"
    sed -e "s|^Exec=.*|Exec=\"$BIN\" %u|" -e "s|^TryExec=.*|TryExec=$BIN|" \
        "$ROOT/packaging/arch/$APP_ID.desktop" > "$DESKTOP"
    cp "$ROOT/packaging/arch/$APP_ID.svg" "$ICON"
    cp "$ROOT/LICENSE" "$LICENSE_FILE"
    refresh_desktop

    version=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$LIB/roseblox/__init__.py")
    say "Roseblox $version is installed. Open it from your application menu, or run: roseblox"
    case ":$PATH:" in
        *":$PREFIX/bin:"*) ;;
        *) say "Note: $PREFIX/bin is not in your PATH. Log out and back in, or add it to PATH, so the \`roseblox\` command and Roblox links work." ;;
    esac
}

if [ "$(id -u)" = 0 ] && [ -z "${ALLOW_ROOT:-}" ]; then
    fail "Run this script as your normal user, not with sudo."
fi

case "${1:-}" in
    "") install ;;
    --uninstall) uninstall ;;
    -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//' ;;
    *) fail "Unknown option: $1 (use --uninstall or --help)" ;;
esac
