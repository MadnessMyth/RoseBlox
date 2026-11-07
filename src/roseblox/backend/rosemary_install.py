"""Install Rosemary from the AUR through the user's AUR helper in a visible terminal."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping


ROSEMARY_AUR_PACKAGE = "rosemary"
ROSEMARY_AUR_URL = f"https://aur.archlinux.org/packages/{ROSEMARY_AUR_PACKAGE}"
ROSEMARY_PROJECT_URL = "https://github.com/microlift/rosemary"
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
