"""What every importer produces: settings Roseblox can represent, and everything it cannot."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..fast_flags import FastFlagError, normalize_flag_value
from ..launch_config import (
    COMMAND_KEYS,
    DECORATIONS,
    MAX_REFRESH_RATE,
    MIN_REFRESH_RATE,
    PRESENTATION_MODES,
    _PCI_SLOT,
    validate_environment_name,
    validate_environment_value,
)

PLAYER = "player"
STUDIO = "studio"
ROSEBLOX = "roseblox"
TARGET_TITLES = {PLAYER: "Roblox Player", STUDIO: "Roblox Studio", ROSEBLOX: "Roseblox"}

# Problems: entries that are reported but never imported.
SKIPPED = "skipped"
UNSUPPORTED = "unsupported"
INVALID = "invalid"

MAX_SOURCE_FILE_SIZE = 4 * 1024 * 1024

# Settings an importer may produce, by target. Flags and environment variables
# use "fast_flags.NAME", "rosemary_environment.NAME" and "guest_environment.NAME".
APP_SETTINGS = {
    "gamemode": "GameMode",
    "mangohud": "MangoHud overlay",
    "gpu": "Graphics card",
    "presentation_mode": "Presentation mode",
    "refresh_rate": "Refresh rate",
    "command_key": "Command key",
    "decorations": "Titlebar",
}
ROSEBLOX_SETTINGS = {
    "discord_presence": "Discord Rich Presence",
    "discord_join_button": "Join Server button",
    "discord_application_id": "Discord application ID",
    "server_location": "Server location notification",
    "close_on_leave": "Close Roblox when leaving a game",
    "plain_ctrl_click": "Ctrl + left-click compatibility",
}
MOD_SETTINGS = {
    "mods.classic_death_sound": "Classic death sound",
    "mods.classic_avatar_background": "Classic avatar editor background",
    "mods.font": "Custom font",
}


class ImportSourceError(RuntimeError):
    """The source's files could not be read at all."""


@dataclass(frozen=True)
class ImportItem:
    """One setting Roseblox can represent, already validated and in Roseblox's form."""

    target: str
    key: str
    value: object
    source_key: str
    note: str = ""
    selected: bool = True

    @property
    def label(self) -> str:
        if self.key.startswith("fast_flags."):
            return self.key.removeprefix("fast_flags.")
        for prefix, title in (("rosemary_environment.", "Rosemary variable"), ("guest_environment.", "Roblox variable")):
            if self.key.startswith(prefix):
                return f"{title} {self.key.removeprefix(prefix)}"
        return {**APP_SETTINGS, **ROSEBLOX_SETTINGS, **MOD_SETTINGS}.get(self.key, self.key)

    @property
    def is_flag(self) -> bool:
        return self.key.startswith("fast_flags.")


@dataclass(frozen=True)
class ImportProblem:
    """Something in the source that is not imported, and why."""

    source_key: str
    status: str
    reason: str


@dataclass
class ImportResult:
    source: str
    location: Path
    items: list[ImportItem] = field(default_factory=list)
    problems: list[ImportProblem] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def item(self, target: str, key: str, value: object, source_key: str, note: str = "", selected: bool = True) -> None:
        self.items.append(ImportItem(target, key, value, source_key, note, selected))

    def skip(self, source_key: str, reason: str) -> None:
        self._problem(ImportProblem(source_key, SKIPPED, reason))

    def unsupported(self, source_key: str, reason: str) -> None:
        self._problem(ImportProblem(source_key, UNSUPPORTED, reason))

    def invalid(self, source_key: str, reason: str) -> None:
        self._problem(ImportProblem(source_key, INVALID, reason))

    def _problem(self, problem: ImportProblem) -> None:
        if problem not in self.problems:
            self.problems.append(problem)

    # Validated adders shared by the importers.

    def flag(self, target: str, name: object, value: object, source_key: str, note: str = "", selected: bool = True) -> None:
        if not isinstance(name, str):
            self.invalid(source_key, "A flag name must be text.")
            return
        try:
            normalized = normalize_flag_value(name, value)
        except FastFlagError as error:
            self.invalid(source_key, str(error))
            return
        self.item(target, f"fast_flags.{name}", normalized, source_key, note, selected)

    def flags(self, target: str, document: object, source_key: str, note: str = "", selected: bool = True) -> None:
        if document is None:
            return
        if not isinstance(document, dict):
            self.invalid(source_key, "FastFlags must be names and values.")
            return
        for name, value in document.items():
            self.flag(target, name, value, f"{source_key}.{name}", note, selected)

    def boolean(self, target: str, key: str, value: object, source_key: str, note: str = "") -> None:
        if isinstance(value, bool):
            self.item(target, key, value, source_key, note)
        elif value is not None:
            self.invalid(source_key, "Expected true or false.")

    def environment(self, target: str, guest: bool, name: object, value: object, source_key: str) -> None:
        problem = validate_environment_name(name, guest=guest) or validate_environment_value(name, value, guest=guest)
        if problem:
            self.invalid(source_key, problem)
            return
        key = "guest_environment" if guest else "rosemary_environment"
        self.item(target, f"{key}.{name}", str(value), source_key)

    def environment_lines(self, target: str, guest: bool, lines: object, source_key: str) -> None:
        if lines is None:
            return
        if not isinstance(lines, list):
            self.invalid(source_key, "Expected a list of NAME=VALUE lines.")
            return
        for index, line in enumerate(lines):
            key = f"{source_key}[{index}]"
            if not isinstance(line, str) or not line.strip():
                self.invalid(key, "Expected a NAME=VALUE line.")
                continue
            name, separator, value = line.strip().partition("=")
            if not separator:
                self.unsupported(key, f"{name} copies this computer's value; add it in Performance → Environment.")
                continue
            self.environment(target, guest, name.strip(), value, key)

    def gpu(self, target: str, value: object, source_key: str) -> None:
        if value in (None, "", "auto"):
            return
        text = value.removeprefix("pci:") if isinstance(value, str) else value
        if isinstance(text, str) and _PCI_SLOT.match(text.lower()):
            self.item(target, "gpu", text.lower(), source_key)
        else:
            self.invalid(source_key, "The graphics card is not a PCI address such as 0000:01:00.0.")

    def choice(self, target: str, key: str, value: object, allowed: tuple[str, ...], source_key: str) -> None:
        if value in (None, ""):
            return
        if value in allowed:
            self.item(target, key, value, source_key)
        else:
            self.invalid(source_key, f"Roseblox accepts {', '.join(allowed)}.")

    def refresh_rate(self, target: str, value: object, source_key: str) -> None:
        if value in (None, ""):
            return
        try:
            number = float(value) if not isinstance(value, bool) else float("nan")
        except (TypeError, ValueError):
            number = float("nan")
        if number.is_integer() and MIN_REFRESH_RATE <= number <= MAX_REFRESH_RATE:
            self.item(target, "refresh_rate", int(number), source_key)
        else:
            self.invalid(source_key, f"Roseblox takes a whole number of hertz between {MIN_REFRESH_RATE} and {MAX_REFRESH_RATE}.")

    def presentation_mode(self, target: str, value: object, source_key: str) -> None:
        self.choice(target, "presentation_mode", value, PRESENTATION_MODES, source_key)

    def command_key(self, target: str, value: object, source_key: str) -> None:
        self.choice(target, "command_key", value, COMMAND_KEYS, source_key)

    def decorations(self, target: str, value: object, source_key: str) -> None:
        self.choice(target, "decorations", value, DECORATIONS, source_key)

    def leftovers(self, document: dict, handled: set[str], prefix: str, reason: str) -> None:
        for key in sorted(set(document) - handled):
            self.unsupported(f"{prefix}{key}", reason)


@dataclass(frozen=True)
class Importer:
    """One source application: where it keeps its files and how to read them."""

    id: str
    title: str
    description: str
    detect: Callable[[dict[str, str]], list[Path]]
    read: Callable[[Path], ImportResult]
    browse_hint: str
    windows: bool = False


def read_text(path: Path) -> str:
    try:
        size = path.stat().st_size
        if size > MAX_SOURCE_FILE_SIZE:
            raise ImportSourceError(f"{path.name} is larger than {MAX_SOURCE_FILE_SIZE // (1024 * 1024)} MB.")
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as error:
        raise ImportSourceError(f"Could not read {path}: {error}") from error


def read_json(path: Path) -> object:
    try:
        return json.loads(read_text(path))
    except json.JSONDecodeError as error:
        raise ImportSourceError(f"{path.name} is not valid JSON: {error}") from error


def read_toml(path: Path) -> dict:
    try:
        import tomllib
    except ModuleNotFoundError as error:
        raise ImportSourceError("Reading TOML files needs Python 3.11 or newer.") from error
    try:
        return tomllib.loads(read_text(path))
    except tomllib.TOMLDecodeError as error:
        raise ImportSourceError(f"{path.name} is not valid TOML: {error}") from error


def home(environ: dict[str, str]) -> Path:
    return Path(environ.get("HOME") or os.path.expanduser("~"))


def xdg(environ: dict[str, str], kind: str) -> Path:
    defaults = {"CONFIG": ".config", "DATA": ".local/share"}
    value = environ.get(f"XDG_{kind}_HOME")
    return Path(value) if value else home(environ) / defaults[kind]
