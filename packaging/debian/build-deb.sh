#!/bin/sh
# Build roseblox_<version>_all.deb for Ubuntu 24.04 or newer and Debian 13 or newer.
#
#   packaging/debian/build-deb.sh [output directory]   (default: dist)
#
# The package uses the system's Python, PyGObject, GTK 4 and libadwaita, and does not
# include Rosemary. It needs only dpkg-deb, which every Debian-based system has.

set -eu

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
OUT=$(mkdir -p "${1:-$ROOT/dist}" && cd "${1:-$ROOT/dist}" && pwd)
APP_ID=io.github.roseblox.Roseblox
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/src/roseblox/__init__.py")
[ -n "$VERSION" ] || { echo "Could not read the version from src/roseblox/__init__.py" >&2; exit 1; }

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
umask 022
chmod 755 "$STAGE"

mkdir -p "$STAGE/DEBIAN" "$STAGE/usr/bin" "$STAGE/usr/lib/python3/dist-packages" \
    "$STAGE/usr/share/applications" "$STAGE/usr/share/icons/hicolor/scalable/apps" "$STAGE/usr/share/doc/roseblox"

cp -R "$ROOT/src/roseblox" "$STAGE/usr/lib/python3/dist-packages/roseblox"
find "$STAGE/usr/lib/python3/dist-packages" -name __pycache__ -type d -prune -exec rm -rf {} +

cat > "$STAGE/usr/bin/roseblox" <<'EOF'
#!/usr/bin/python3
import sys

from roseblox.__main__ import main

sys.exit(main())
EOF
chmod 755 "$STAGE/usr/bin/roseblox"

cp "$ROOT/packaging/arch/$APP_ID.desktop" "$STAGE/usr/share/applications/$APP_ID.desktop"
cp "$ROOT/packaging/arch/$APP_ID.svg" "$STAGE/usr/share/icons/hicolor/scalable/apps/$APP_ID.svg"
{
    echo "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/"
    echo "Upstream-Name: Roseblox"
    echo "Source: https://github.com/MadnessMyth/RoseBlox"
    echo
    echo "Files: *"
    echo "License: MIT"
    sed 's/^$/./; s/^/ /' "$ROOT/LICENSE"
} > "$STAGE/usr/share/doc/roseblox/copyright"

SIZE=$(du -sk "$STAGE/usr" | cut -f1)
cat > "$STAGE/DEBIAN/control" <<EOF
Package: roseblox
Version: $VERSION
Architecture: all
Maintainer: Roseblox contributors <https://github.com/MadnessMyth/RoseBlox/issues>
Installed-Size: $SIZE
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1 (>= 1.4), xdg-utils, libarchive-tools
Suggests: gamemode, mangohud, pulseaudio-utils
Section: games
Priority: optional
Homepage: https://github.com/MadnessMyth/RoseBlox
Description: Native GTK launcher and installation manager for Roblox through Rosemary
 Roseblox installs, updates and launches the macOS Roblox Player and Roblox
 Studio on Linux through Rosemary, with Roblox link handling, update
 notifications, performance settings, mods and Discord Rich Presence.
 .
 Rosemary is not included; Roseblox's first-run setup explains how to get it.
EOF

dpkg-deb --root-owner-group --build "$STAGE" "$OUT/roseblox_${VERSION}_all.deb" >/dev/null
echo "$OUT/roseblox_${VERSION}_all.deb"
