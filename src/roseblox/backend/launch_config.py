"""Per-application launch configuration with versioned backups.

Player and Studio each keep their own Rosemary environment, Roblox (guest)
environment, extra launch arguments, FastFlags and frame pacing. Every save keeps
the previous document as a backup, so any change can be rolled back.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from .fast_flags import FastFlagError, normalize_flags
from .gpu import Gpu, find_gpu

APP_TYPES = ("player", "studio")
PRESENTATION_MODES = ("mailbox", "fifo", "fifo_relaxed", "immediate")
PRESENTATION_MODE_VARIABLE = "ROSEMARY_METAL_PRESENTATION_MODE"
REFRESH_RATE_VARIABLE = "ROSEMARY_SCREEN_REFRESH_RATE"
GUEST_ENVIRONMENT_VARIABLE = "ROSEMARY_ENVIRON"
MIN_REFRESH_RATE = 24
MAX_REFRESH_RATE = 1000
MAX_ENTRIES = 100
MAX_ARGUMENTS = 64
MAX_TEXT_LENGTH = 4096
MAX_BACKUPS = 30

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PCI_SLOT = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")
_CREDENTIAL_NAME = re.compile(
    r"ROBLOSECURITY|TOKEN|PASSWORD|PASSWD|SECRET|COOKIE|CREDENTIAL|API_?KEY|SESSION", re.IGNORECASE
)
_CREDENTIAL_VALUE = re.compile(r"ROBLOSECURITY|_\|WARNING:-DO-NOT-SHARE-THIS", re.IGNORECASE)
_LIBRARY_INJECTION = {"LD_PRELOAD", "LD_AUDIT", "LD_LIBRARY_PATH", "ROSEMARY_DYLD_INSERT_LIBRARIES"}
_ROSEMARY_INTERNAL = {"ROSEMARY_EXE", "ROSEMARY_GUEST_ENVIRON"}
_DEDICATED_CONTROLS = {
    PRESENTATION_MODE_VARIABLE: "the presentation mode setting",
    REFRESH_RATE_VARIABLE: "the refresh rate setting",
    GUEST_ENVIRONMENT_VARIABLE: "the Roblox environment list",
    "ROSEMARY_PLAIN_CTRL_CLICK": "the Ctrl+click setting",
}
_BLOCKED_ARGUMENTS = {
    "ticket",
    "authcode",
    "authcodetype",
    "rbxtransporttoken",
    "protocolstring",
}


class LaunchConfigError(ValueError):
    """Raised when a launch configuration is invalid or cannot be stored."""


def _require_app_type(app_type: str) -> None:
    if app_type not in APP_TYPES:
        raise ValueError(f"Unsupported application type: {app_type}")


def validate_environment_name(name: object, *, guest: bool) -> str | None:
    """Return why a variable name cannot be used, or None when it can."""
    if not isinstance(name, str) or not _NAME.match(name):
        return "Variable names use letters, digits and underscores, and cannot start with a digit."
    upper = name.upper()
    if _CREDENTIAL_NAME.search(name):
        return f"{name} looks like a credential. Roseblox does not store credentials."
    if guest:
        if upper.startswith("DYLD_"):
            return f"{name} loads extra libraries into Roblox, which Roseblox does not allow."
        return None
    if upper in _LIBRARY_INJECTION:
        return f"{name} loads extra libraries into the process, which Roseblox does not allow."
    if upper in _ROSEMARY_INTERNAL or upper.startswith("ROSEMARY_GUEST_ENVIRON_"):
        return f"{name} is set internally by Rosemary."
    if upper in _DEDICATED_CONTROLS:
        return f"Use {_DEDICATED_CONTROLS[upper]} instead of setting {name} directly."
    return None


def validate_environment_value(name: str, value: object, *, guest: bool) -> str | None:
    if not isinstance(value, str):
        return f"{name} needs a text value."
    if "\0" in value:
        return f"{name} cannot contain NUL characters."
    if guest and ("\n" in value or "\r" in value):
        return f"{name} cannot contain line breaks."
    if len(value) > MAX_TEXT_LENGTH:
        return f"{name} is longer than {MAX_TEXT_LENGTH} characters."
    if _CREDENTIAL_VALUE.search(value):
        return f"The value of {name} looks like a Roblox login cookie. Roseblox does not store credentials."
    return None


def validate_argument(argument: object) -> str | None:
    if not isinstance(argument, str) or not argument:
        return "Arguments cannot be empty."
    if "\0" in argument:
        return "Arguments cannot contain NUL characters."
    if len(argument) > MAX_TEXT_LENGTH:
        return f"Arguments are limited to {MAX_TEXT_LENGTH} characters."
    option = argument.lstrip("-").split("=", 1)[0].lower()
    if argument.startswith("-") and option in _BLOCKED_ARGUMENTS:
        return f"{argument.split('=', 1)[0]} carries login or launch data that Roseblox provides itself."
    if _CREDENTIAL_VALUE.search(argument):
        return "That argument looks like a Roblox login cookie. Roseblox does not store credentials."
    return None


@dataclass(frozen=True)
class AppLaunchConfig:
    rosemary_environment: dict[str, str] = field(default_factory=dict)
    guest_environment: dict[str, str] = field(default_factory=dict)
    arguments: tuple[str, ...] = ()
    fast_flags: dict[str, str] = field(default_factory=dict)
    presentation_mode: str | None = None
    refresh_rate: int | None = None
    gpu: str | None = None
    gamemode: bool = False

    def problems(self) -> list[str]:
        found: list[str] = []
        for guest, entries in ((False, self.rosemary_environment), (True, self.guest_environment)):
            if not isinstance(entries, Mapping):
                found.append("Environment variables must be names and values.")
                continue
            if len(entries) > MAX_ENTRIES:
                found.append(f"Roseblox keeps at most {MAX_ENTRIES} variables per list.")
            for name, value in entries.items():
                problem = validate_environment_name(name, guest=guest) or validate_environment_value(
                    name, value, guest=guest
                )
                if problem:
                    found.append(problem)
        if not isinstance(self.arguments, (tuple, list)):
            found.append("Launch arguments must be a list.")
        else:
            if len(self.arguments) > MAX_ARGUMENTS:
                found.append(f"Roseblox keeps at most {MAX_ARGUMENTS} launch arguments.")
            found.extend(problem for problem in map(validate_argument, self.arguments) if problem)
        try:
            normalize_flags(self.fast_flags)
        except FastFlagError as error:
            found.append(str(error))
        if self.presentation_mode is not None and self.presentation_mode not in PRESENTATION_MODES:
            found.append(f"Presentation mode must be one of {', '.join(PRESENTATION_MODES)}.")
        if self.refresh_rate is not None and (
            isinstance(self.refresh_rate, bool)
            or not isinstance(self.refresh_rate, int)
            or not MIN_REFRESH_RATE <= self.refresh_rate <= MAX_REFRESH_RATE
        ):
            found.append(f"Refresh rate must be between {MIN_REFRESH_RATE} and {MAX_REFRESH_RATE} Hz.")
        if self.gpu is not None and (not isinstance(self.gpu, str) or not _PCI_SLOT.match(self.gpu)):
            found.append("The GPU must be a PCI address such as 0000:01:00.0.")
        if not isinstance(self.gamemode, bool):
            found.append("The GameMode setting must be true or false.")
        return found

    def validated(self) -> "AppLaunchConfig":
        found = self.problems()
        if found:
            raise LaunchConfigError("\n".join(found))
        return replace(
            self,
            rosemary_environment=dict(self.rosemary_environment),
            guest_environment=dict(self.guest_environment),
            arguments=tuple(self.arguments),
            fast_flags=normalize_flags(self.fast_flags),
        )

    @property
    def is_default(self) -> bool:
        return self == AppLaunchConfig()

    def to_document(self) -> dict:
        return {
            "rosemary_environment": dict(self.rosemary_environment),
            "guest_environment": dict(self.guest_environment),
            "arguments": list(self.arguments),
            "fast_flags": dict(self.fast_flags),
            "presentation_mode": self.presentation_mode,
            "refresh_rate": self.refresh_rate,
            "gpu": self.gpu,
            "gamemode": self.gamemode,
        }

    @classmethod
    def from_document(cls, document: object) -> "AppLaunchConfig":
        if not isinstance(document, dict):
            raise LaunchConfigError("A saved launch configuration is not a JSON object.")
        arguments = document.get("arguments", [])
        return cls(
            rosemary_environment=document.get("rosemary_environment", {}),
            guest_environment=document.get("guest_environment", {}),
            arguments=tuple(arguments) if isinstance(arguments, list) else arguments,
            fast_flags=document.get("fast_flags", {}),
            presentation_mode=document.get("presentation_mode"),
            refresh_rate=document.get("refresh_rate"),
            gpu=document.get("gpu"),
            gamemode=document.get("gamemode", False),
        ).validated()


def effective_environment(
    config: AppLaunchConfig, base: Mapping[str, str], gpus: list[Gpu] | tuple[Gpu, ...] = ()
) -> dict[str, str]:
    """Return the environment Rosemary starts with for this configuration.

    A chosen GPU that is no longer installed is skipped, so the launch still uses
    whichever card Rosemary picks.
    """
    environment = dict(base)
    gpu = find_gpu(config.gpu, list(gpus))
    if gpu is not None:
        environment.update(gpu.vulkan_environment())
    environment.update(config.rosemary_environment)
    if config.presentation_mode is not None:
        environment[PRESENTATION_MODE_VARIABLE] = config.presentation_mode
    if config.refresh_rate is not None:
        environment[REFRESH_RATE_VARIABLE] = str(config.refresh_rate)
    if config.guest_environment:
        lines: dict[str, str] = {}
        for line in base.get(GUEST_ENVIRONMENT_VARIABLE, "").splitlines():
            if line.strip():
                lines[line.split("=", 1)[0]] = line
        for name, value in config.guest_environment.items():
            lines[name] = f"{name}={value}"
        environment[GUEST_ENVIRONMENT_VARIABLE] = "\n".join(lines.values())
    return environment


def environment_changes(
    config: AppLaunchConfig, base: Mapping[str, str], gpus: list[Gpu] | tuple[Gpu, ...] = ()
) -> dict[str, str]:
    """Return only the variables this configuration adds or changes."""
    environment = effective_environment(config, base, gpus)
    return {name: value for name, value in environment.items() if base.get(name) != value}


def describe_changes(
    old: AppLaunchConfig, new: AppLaunchConfig, gpu_names: Mapping[str, str] | None = None
) -> list[str]:
    """Return one readable line per difference, for review before saving."""
    lines: list[str] = []
    gpu_names = gpu_names or {}

    def setting(label: str, before: object, after: object, unset: str) -> None:
        if before != after:
            lines.append(f"{label}: {unset if before is None else before} → {unset if after is None else after}")

    setting("Presentation mode", old.presentation_mode, new.presentation_mode, "Rosemary default")
    setting(
        "Refresh rate",
        None if old.refresh_rate is None else f"{old.refresh_rate} Hz",
        None if new.refresh_rate is None else f"{new.refresh_rate} Hz",
        "display default",
    )
    setting("GPU", gpu_names.get(old.gpu, old.gpu), gpu_names.get(new.gpu, new.gpu), "chosen by Rosemary")
    setting("GameMode", "on" if old.gamemode else "off", "on" if new.gamemode else "off", "off")
    for label, before, after in (
        ("FastFlag", old.fast_flags, new.fast_flags),
        ("Rosemary variable", old.rosemary_environment, new.rosemary_environment),
        ("Roblox variable", old.guest_environment, new.guest_environment),
    ):
        for name in sorted(set(before) | set(after)):
            if name not in after:
                lines.append(f"Remove {label} {name}")
            elif name not in before:
                lines.append(f"Add {label} {name} = {after[name]}")
            elif before[name] != after[name]:
                lines.append(f"{label} {name}: {before[name]} → {after[name]}")
    if tuple(old.arguments) != tuple(new.arguments):
        lines.append(
            "Launch arguments: "
            f"{' '.join(old.arguments) or '(none)'} → {' '.join(new.arguments) or '(none)'}"
        )
    return lines


@dataclass(frozen=True)
class Backup:
    id: str
    created_at: float
    reason: str
    path: Path


def default_config_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config_home / "roseblox" / "launch-config.json"


_LOCKS: dict[Path, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _path_lock(path: Path) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(path.resolve(), threading.RLock())


class LaunchConfigStore:
    """Atomic JSON storage for both applications' configurations and their backups."""

    def __init__(self, path: Path | None = None, backup_directory: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_config_path()
        self.backup_directory = (
            Path(backup_directory) if backup_directory is not None else self.path.parent / "backups"
        )
        self._lock = _path_lock(self.path)

    def load(self, app_type: str) -> AppLaunchConfig:
        _require_app_type(app_type)
        document = self._read()
        return AppLaunchConfig.from_document(document["apps"].get(app_type, {}))

    def save(self, app_type: str, config: AppLaunchConfig, reason: str) -> AppLaunchConfig:
        _require_app_type(app_type)
        config = config.validated()
        with self._lock, self._file_lock():
            document = self._read()
            if AppLaunchConfig.from_document(document["apps"].get(app_type, {})) == config:
                return config
            self._snapshot(document, reason)
            document["apps"][app_type] = config.to_document()
            self._write(self.path, document)
        return config

    def backups(self) -> list[Backup]:
        entries: list[Backup] = []
        try:
            paths = list(self.backup_directory.glob("launch-config-*.json"))
        except OSError:
            return []
        for path in paths:
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
                entries.append(
                    Backup(
                        id=path.stem,
                        created_at=float(document["created_at"]),
                        reason=str(document.get("reason", "")),
                        path=path,
                    )
                )
            except (OSError, UnicodeError, ValueError, KeyError, TypeError):
                continue
        return sorted(entries, key=lambda entry: (entry.created_at, entry.id), reverse=True)

    def restore(self, backup_id: str) -> None:
        """Replace the configuration with a backup, keeping the current one as a backup first."""
        backup = next((entry for entry in self.backups() if entry.id == backup_id), None)
        if backup is None:
            raise LaunchConfigError("That backup no longer exists.")
        try:
            saved = json.loads(backup.path.read_text(encoding="utf-8"))["document"]
        except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
            raise LaunchConfigError(f"Could not read the backup: {error}") from error
        restored = self._validated_document(saved)
        with self._lock, self._file_lock():
            try:
                current = self._read()
            except LaunchConfigError:
                current = None
            if current is not None:
                self._snapshot(current, f"Before restoring the backup from {_describe_time(backup.created_at)}")
            self._write(self.path, restored)

    def _snapshot(self, document: dict, reason: str) -> None:
        created_at = time.time()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(created_at))
        path = self.backup_directory / f"launch-config-{stamp}-{os.urandom(3).hex()}.json"
        self._write(path, {"version": 1, "created_at": created_at, "reason": reason, "document": document})
        for stale in self.backups()[MAX_BACKUPS:]:
            try:
                stale.path.unlink()
            except OSError:
                pass

    @staticmethod
    def _validated_document(document: object) -> dict:
        if not isinstance(document, dict) or document.get("version") != 1 or not isinstance(document.get("apps"), dict):
            raise LaunchConfigError("The launch configuration has an invalid format.")
        apps = {}
        for app_type, entry in document["apps"].items():
            if app_type not in APP_TYPES:
                raise LaunchConfigError(f"The launch configuration names an unknown application: {app_type}")
            apps[app_type] = AppLaunchConfig.from_document(entry).to_document()
        return {"version": 1, "apps": apps}

    def _read(self) -> dict:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "apps": {}}
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise LaunchConfigError(
                f"Could not read the launch configuration ({error}). Restore a backup in Settings."
            ) from error
        try:
            return self._validated_document(document)
        except LaunchConfigError as error:
            raise LaunchConfigError(f"{error} Restore a backup in Settings.") from error

    @contextlib.contextmanager
    def _file_lock(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            stream = open(self.path.with_name(self.path.name + ".lock"), "a")
        except OSError as error:
            raise LaunchConfigError(f"Could not lock the launch configuration: {error}") from error
        with stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    @staticmethod
    def _write(path: Path, document: dict) -> None:
        temporary_path: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
                temporary_path = Path(stream.name)
                json.dump(document, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
        except OSError as error:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise LaunchConfigError(f"Could not save {path.name}: {error}") from error


def _describe_time(timestamp: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))
