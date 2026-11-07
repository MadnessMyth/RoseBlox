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

# Runs inside the terminal: "$1" is the helper, "$2" the package. The window stays open
# so the result can be read, and the helper's exit status is preserved.
_INSTALL_SCRIPT = (
    'printf "Roseblox is installing the %s package from the AUR with %s.\\n\\n" "$2" "${1##*/}"; '
    '"$1" -S --needed "$2"; status=$?; echo; '
    'if [ "$status" -eq 0 ]; then echo "Rosemary is installed. You can return to Roseblox."; '
    'else echo "Installation did not finish (exit status $status)."; fi; '
    'printf "Press Enter to close this window. "; read -r _; exit "$status"'
)


class RosemaryInstallError(RuntimeError):
    """Raised when Roseblox cannot start a Rosemary installation."""


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
        return f"{self.helper_name} -S --needed {ROSEMARY_AUR_PACKAGE}"


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
        ROSEMARY_AUR_PACKAGE,
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
            f"“{Path(helper).name} -S --needed {ROSEMARY_AUR_PACKAGE}” in a terminal, then check again."
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
