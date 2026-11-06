"""Persistent onboarding and URL-handler preferences."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path


class SetupSettingsError(RuntimeError):
    """Raised when setup preferences cannot safely be read or written."""


def default_setup_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config_home / "roseblox" / "setup.json"


@dataclass(frozen=True)
class SetupState:
    onboarding_completed: bool = False
    url_handlers_enabled: bool = False
    previous_url_defaults: dict[str, str | None] | None = None


_LOCKS: dict[Path, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _path_lock(path: Path) -> threading.RLock:
    key = path.resolve()
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


class SetupStore:
    """Small atomic JSON store separate from Library and version settings."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_setup_path()
        self._lock = _path_lock(self.path)

    def load(self) -> SetupState:
        with self._lock:
            document = self._read()
        completed = document.get("onboarding_completed", False)
        enabled = document.get("url_handlers_enabled", False)
        previous = document.get("previous_url_defaults", {})
        if not isinstance(completed, bool) or not isinstance(enabled, bool) or not isinstance(previous, dict):
            raise SetupSettingsError("Roseblox setup settings have an invalid format; the file was not changed.")
        if any(
            not isinstance(key, str)
            or (value is not None and not isinstance(value, str))
            for key, value in previous.items()
        ):
            raise SetupSettingsError("Saved URL-handler defaults are invalid; the file was not changed.")
        return SetupState(completed, enabled, dict(previous))

    def complete_onboarding(self) -> SetupState:
        return self._update(onboarding_completed=True)

    def save_url_handlers(
        self,
        enabled: bool,
        previous_defaults: dict[str, str | None] | None = None,
    ) -> SetupState:
        if not isinstance(enabled, bool):
            raise ValueError("URL-handler preference must be enabled or disabled.")
        return self._update(
            url_handlers_enabled=enabled,
            previous_url_defaults=dict(previous_defaults or {}),
        )

    def _update(self, **updates: object) -> SetupState:
        with self._lock:
            document = self._read()
            document.update(updates)
            document["version"] = 1
            self._write(document)
        return self.load()

    def _read(self) -> dict:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {
                "version": 1,
                "onboarding_completed": False,
                "url_handlers_enabled": False,
                "previous_url_defaults": {},
            }
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise SetupSettingsError(f"Could not read Roseblox setup settings: {error}") from error
        if not isinstance(document, dict) or document.get("version") != 1:
            raise SetupSettingsError("Roseblox setup settings have an invalid format; the file was not changed.")
        return document

    def _write(self, document: dict) -> None:
        temporary_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, delete=False
            ) as stream:
                temporary_path = Path(stream.name)
                json.dump(document, stream, indent=2, sort_keys=True)
                stream.write("\n")
            os.replace(temporary_path, self.path)
        except OSError as error:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise SetupSettingsError(f"Could not save Roseblox setup settings: {error}") from error
