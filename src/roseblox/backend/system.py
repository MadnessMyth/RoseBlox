"""Which Linux distribution Roseblox runs on, and how to install a missing tool there."""

from __future__ import annotations

import platform
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

OS_RELEASE_PATHS = (Path("/etc/os-release"), Path("/usr/lib/os-release"))

# Distribution families by the package manager that installs system packages there.
_FAMILIES = (
    ("pacman", ("arch",)),
    ("apt", ("debian", "ubuntu")),
    ("dnf", ("fedora", "rhel", "centos")),
    ("zypper", ("suse", "opensuse")),
)

_INSTALL_PREFIXES = {
    "pacman": "sudo pacman -S --needed",
    "apt": "sudo apt install",
    "dnf": "sudo dnf install",
    "zypper": "sudo zypper install",
}

# Package names per package manager for the commands Roseblox uses but does not depend on.
_PACKAGES = {
    "bsdtar": {"pacman": "libarchive", "apt": "libarchive-tools", "dnf": "bsdtar", "zypper": "bsdtar"},
    "xdg-mime": {"pacman": "xdg-utils", "apt": "xdg-utils", "dnf": "xdg-utils", "zypper": "xdg-utils"},
    "gamemoded": {"pacman": "gamemode", "apt": "gamemode", "dnf": "gamemode", "zypper": "gamemode"},
    "mangohud": {"pacman": "mangohud", "apt": "mangohud", "dnf": "mangohud", "zypper": "mangohud"},
    # The GObject bindings, which bring the library; Debian 13 has them, Ubuntu 24.04 does not.
    "gtk4-layer-shell": {"pacman": "gtk4-layer-shell", "apt": "gir1.2-gtk4layershell-1.0", "dnf": "gtk4-layer-shell"},
    # PyGObject's cairo module; Fedora's python3-gobject has it, python3-gobject-base does not.
    "python-cairo": {
        "pacman": "python-cairo", "apt": "python3-gi-cairo", "dnf": "python3-gobject", "zypper": "python3-gobject-cairo",
    },
}


@dataclass(frozen=True)
class SystemInfo:
    id: str = ""
    id_like: tuple[str, ...] = ()
    name: str = "Linux"
    machine: str = ""

    @property
    def package_manager(self) -> str | None:
        ids = (self.id, *self.id_like)
        for manager, members in _FAMILIES:
            if any(identifier in members for identifier in ids):
                return manager
        return None

    @property
    def arch_based(self) -> bool:
        return self.package_manager == "pacman"


def parse_os_release(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def detect_system(
    paths: tuple[Path, ...] = OS_RELEASE_PATHS,
    lookup: Callable[[str], str | None] = shutil.which,
    machine: str | None = None,
) -> SystemInfo:
    """Read os-release; a system without one still counts as Arch-based when pacman exists."""
    values: dict[str, str] = {}
    for path in paths:
        try:
            values = parse_os_release(path.read_text(encoding="utf-8"))
            break
        except (OSError, UnicodeError):
            continue
    info = SystemInfo(
        id=values.get("ID", "").lower(),
        id_like=tuple(values.get("ID_LIKE", "").lower().split()),
        name=values.get("PRETTY_NAME") or values.get("NAME") or "Linux",
        machine=platform.machine() if machine is None else machine,
    )
    if info.package_manager is None and lookup("pacman"):
        return SystemInfo("arch", info.id_like, info.name, info.machine)
    return info


def install_command(tool: str, system: SystemInfo | None = None) -> str | None:
    """The command that installs ``tool`` on this system, or None when it is not known."""
    system = detect_system() if system is None else system
    manager = system.package_manager
    package = _PACKAGES.get(tool, {}).get(manager or "")
    if manager is None or package is None:
        return None
    return f"{_INSTALL_PREFIXES[manager]} {package}"


def install_hint(tool: str, fallback: str, system: SystemInfo | None = None) -> str:
    """A sentence telling the user how to install ``tool``."""
    command = install_command(tool, system)
    return f"Install it with “{command}”." if command else fallback
