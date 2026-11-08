"""Get Rosemary: from the AUR through the user's AUR helper in a visible terminal on Arch-based
systems, and elsewhere from an official AppImage the user downloads themselves."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from .system import SystemInfo


ROSEMARY_AUR_PACKAGE = "rosemary"
ROSEMARY_AUR_URL = f"https://aur.archlinux.org/packages/{ROSEMARY_AUR_PACKAGE}"
ROSEMARY_PROJECT_URL = "https://github.com/microlift/rosemary"
ROSEMARY_RELEASES_URL = f"{ROSEMARY_PROJECT_URL}/releases/latest"
ROSEMARY_NIX_COMMAND = "nix profile install github:microlift/rosemary"
# Rosemary publishes its AppImage for x86-64 only.
APPIMAGE_MACHINES = frozenset({"x86_64", "amd64"})
_APPIMAGE_NAME = re.compile(r"^rosemary[-_.].*\.appimage$", re.IGNORECASE)
_VERSION = re.compile(r"(\d+(?:\.\d+)*)")

INSTALL_METHOD_AUR = "aur"
INSTALL_METHOD_APPIMAGE = "appimage"
INSTALL_METHOD_MANUAL = "manual"
# The AUR package lists these as optional, but Rosemary needs SDL 3 for any GUI app and
# Vulkan for Metal, so Roblox cannot start without them. Both come from Arch's repositories.
ROSEMARY_RUNTIME_PACKAGES = ("sdl3", "vulkan-icd-loader")
ROSEMARY_INSTALL_PACKAGES = (ROSEMARY_AUR_PACKAGE, *ROSEMARY_RUNTIME_PACKAGES)
AUR_HELPERS = ("paru", "yay")

# Each entry maps a terminal command to the arguments that run a program in a new window.
# The command after the prefix is passed as separate arguments, never through a shell string.
_TERMINALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("xdg-terminal-exec", ()),
    ("kgx", ("--",)),
    ("ptyxis", ("--new-window", "--")),
    ("gnome-terminal", ("--wait", "--")),
    ("konsole", ("-e",)),
    ("xfce4-terminal", ("-x",)),
    ("alacritty", ("-e",)),
    ("kitty", ()),
    ("foot", ()),
    ("wezterm", ("start", "--")),
    ("ghostty", ("-e",)),
    ("xterm", ("-e",)),
)
_TERMINAL_PREFIXES = dict(_TERMINALS)

# Runs inside the terminal: "$1" is the helper and the remaining arguments are packages.
# After installing, Rosemary's default prefix gets its macOS base system; without a display
# Rosemary cannot show its own download prompt, and "prefix create" is a no-op once the
# prefix exists. The window stays open so the result can be read, and the helper's exit
# status is preserved.
_INSTALL_SCRIPT = (
    'helper=$1; shift; '
    'printf "Roseblox is installing %s with %s.\\n\\n" "$*" "${helper##*/}"; '
    '"$helper" -S --needed "$@"; status=$?; echo; '
    'if [ "$status" -eq 0 ]; then '
    'echo "Setting up the Rosemary prefix (downloads the macOS base system once)."; '
    'if ROSEMARY_PREFIX_SETUP=download rosemary prefix create; then '
    'echo; echo "Rosemary is installed. You can return to Roseblox."; '
    'else echo; echo "Rosemary is installed, but its prefix is not set up yet. Rosemary offers to '
    'download the base system when it first runs, or run: rosemary prefix create"; fi; '
    'else echo "Installation did not finish (exit status $status)."; fi; '
    'printf "Press Enter to close this window. "; read -r _; exit "$status"'
)


class RosemaryInstallError(RuntimeError):
    """Raised when Roseblox cannot start a Rosemary installation."""


@dataclass(frozen=True)
class RosemaryInstallState:
    waiting: bool = False
    message: str | None = None
    error: bool = False


def install_method(system: SystemInfo) -> str:
    """How setup offers Rosemary on this system."""
    if system.arch_based:
        return INSTALL_METHOD_AUR
    if system.machine.lower() in APPIMAGE_MACHINES:
        return INSTALL_METHOD_APPIMAGE
    return INSTALL_METHOD_MANUAL


def appimage_search_directories(environ: Mapping[str, str] | None = None) -> list[Path]:
    """Where a downloaded Rosemary AppImage usually ends up."""
    environ = os.environ if environ is None else environ
    home = Path(environ.get("HOME") or Path.home())
    directories = [_downloads_directory(home, environ), home / "Applications", home / ".local" / "bin", home / "bin"]
    unique: list[Path] = []
    for directory in directories:
        if directory not in unique:
            unique.append(directory)
    return unique


def _downloads_directory(home: Path, environ: Mapping[str, str]) -> Path:
    """XDG_DOWNLOAD_DIR from user-dirs.dirs, which may name a translated folder."""
    config_home = Path(environ.get("XDG_CONFIG_HOME") or home / ".config")
    try:
        text = (config_home / "user-dirs.dirs").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return home / "Downloads"
    for line in text.splitlines():
        key, _, value = line.strip().partition("=")
        if key == "XDG_DOWNLOAD_DIR":
            value = value.strip().strip('"').replace("$HOME", str(home))
            if value and Path(value).is_absolute():
                return Path(value)
    return home / "Downloads"


def _version_key(path: Path) -> tuple[tuple[int, ...], float]:
    match = _VERSION.search(path.name)
    version = tuple(int(part) for part in match.group(1).split(".")) if match else ()
    try:
        modified = path.stat().st_mtime
    except OSError:
        modified = 0.0
    return version, modified


def find_rosemary_appimage(directories: list[Path] | None = None) -> Path | None:
    """The newest ``Rosemary-*.AppImage`` in the usual places, executable or not."""
    found: list[Path] = []
    for directory in directories if directories is not None else appimage_search_directories():
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if _APPIMAGE_NAME.match(entry.name) and entry.is_file():
                found.append(entry)
    return max(found, key=_version_key) if found else None


def prepare_appimage(path: str | Path) -> str:
    """Make a Rosemary AppImage the user chose executable for them, and return its path.

    Downloaded files are not executable; the user picked this one to run Roblox with.
    """
    candidate = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    if not candidate.name.lower().endswith(".appimage") or not candidate.is_file():
        return str(candidate)
    if not os.access(candidate, os.X_OK):
        try:
            mode = candidate.stat().st_mode
            candidate.chmod(mode | stat.S_IXUSR)
        except OSError as error:
            raise RosemaryInstallError(f"Could not make {candidate.name} executable: {error}") from error
    return str(candidate)


@dataclass(frozen=True)
class RosemaryInstallPlan:
    helper: str
    terminal: str
    command: tuple[str, ...]

    @property
    def helper_name(self) -> str:
        return Path(self.helper).name

    @property
    def display_command(self) -> str:
        return _helper_command(self.helper_name)


def _helper_command(helper_name: str) -> str:
    return f"{helper_name} -S --needed {' '.join(ROSEMARY_INSTALL_PACKAGES)}"


def find_aur_helper(lookup: Callable[[str], str | None] = shutil.which) -> str | None:
    for helper in AUR_HELPERS:
        path = lookup(helper)
        if path:
            return os.path.abspath(path)
    return None


def find_terminal(
    lookup: Callable[[str], str | None] = shutil.which,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, tuple[str, ...]] | None:
    """Return a terminal executable and its run-command prefix, preferring $TERMINAL."""
    environ = os.environ if environ is None else environ
    preferred = Path(environ.get("TERMINAL", "").strip()).name
    candidates = [preferred] if preferred in _TERMINAL_PREFIXES else []
    candidates += [name for name, _prefix in _TERMINALS if name != preferred]
    for name in candidates:
        path = lookup(name)
        if path:
            return os.path.abspath(path), _TERMINAL_PREFIXES[name]
    return None


def build_install_command(terminal: str, prefix: tuple[str, ...], helper: str) -> tuple[str, ...]:
    return (
        terminal,
        *prefix,
        "/bin/sh",
        "-c",
        _INSTALL_SCRIPT,
        "roseblox-install-rosemary",
        helper,
        *ROSEMARY_INSTALL_PACKAGES,
    )


def plan_rosemary_install(
    lookup: Callable[[str], str | None] = shutil.which,
    environ: Mapping[str, str] | None = None,
) -> RosemaryInstallPlan:
    """Describe how Rosemary would be installed, or explain what is missing."""
    helper = find_aur_helper(lookup)
    if helper is None:
        if not lookup("pacman"):
            raise RosemaryInstallError(
                "Automatic installation uses the Arch User Repository and is only available on "
                "Arch-based systems. Install Rosemary using its documentation, then choose the executable."
            )
        raise RosemaryInstallError(
            "No AUR helper was found. Install paru or yay, or build the "
            f"“{ROSEMARY_AUR_PACKAGE}” AUR package manually, then check again."
        )
    terminal = find_terminal(lookup, environ)
    if terminal is None:
        raise RosemaryInstallError(
            "No supported terminal emulator was found to run the installation. Run "
            f"“{_helper_command(Path(helper).name)}” in a terminal, then check again."
        )
    terminal_path, prefix = terminal
    return RosemaryInstallPlan(helper, terminal_path, build_install_command(terminal_path, prefix, helper))


def start_rosemary_install(
    plan: RosemaryInstallPlan,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> subprocess.Popen:
    """Open the terminal; the user reviews the build and authenticates there."""
    try:
        return popen(
            list(plan.command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        raise RosemaryInstallError(f"Could not open a terminal for the installation: {error}") from error
