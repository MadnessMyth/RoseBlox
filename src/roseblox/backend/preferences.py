"""Persistent preferred Player and Studio installation selections."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from .library import APP_BUNDLE_NAMES, BundleValidationError, InstallationRecord, LibraryStore, validate_bundle


class PreferencesError(RuntimeError):
    """Raised when Roseblox preferences cannot safely be persisted."""


@dataclass(frozen=True)
class PreferredInstallations:
    player_id: str | None = None
    studio_id: str | None = None
    errors: dict[str, str] | None = None

    def for_app(self, app_type: str) -> str | None:
        if app_type == "player":
            return self.player_id
        if app_type == "studio":
            return self.studio_id
        raise ValueError(f"Unsupported application type: {app_type}")


def default_preferences_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config_home / "roseblox" / "preferences.json"


_PREFERENCE_LOCKS: dict[Path, threading.RLock] = {}
_PREFERENCE_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    key = path.resolve()
    with _PREFERENCE_LOCKS_GUARD:
        lock = _PREFERENCE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PREFERENCE_LOCKS[key] = lock
        return lock


def resolve_preferred_installation(
    app_type: str,
    preferences: PreferredInstallations,
    records: list[InstallationRecord],
    validation_by_id: dict[str, str],
) -> str | None:
    """Return the saved choice if usable, otherwise the first valid installation."""
    if app_type not in APP_BUNDLE_NAMES:
        raise ValueError(f"Unsupported application type: {app_type}")
    preferred_id = preferences.for_app(app_type)
    by_id = {record.id: record for record in records}
    preferred = by_id.get(preferred_id) if preferred_id else None
    if (
        preferred is not None
        and preferred.app_type == app_type
        and validation_by_id.get(preferred.id) == "Valid"
    ):
        return preferred.id
    return next(
        (
            record.id
            for record in records
            if record.app_type == app_type and validation_by_id.get(record.id) == "Valid"
        ),
        None,
    )


class PreferencesStore:
    """JSON preferences independent of Library registration records."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_preferences_path()
        self._lock: threading.RLock | None = None

    @property
    def _path_lock(self) -> threading.RLock:
        if self._lock is None:
            self._lock = _lock_for(self.path)
        return self._lock

    def load(self) -> PreferredInstallations:
        """Load valid app choices independently; damaged preferences fall back safely."""
        try:
            document = self._read_document()
        except PreferencesError as error:
            return PreferredInstallations(errors={"player": str(error), "studio": str(error)})

        entries = document["preferred_installations"]
        values: dict[str, str | None] = {}
        errors: dict[str, str] = {}
        for app_type in APP_BUNDLE_NAMES:
            raw = entries.get(app_type)
            if raw is None:
                values[app_type] = None
            elif isinstance(raw, str) and raw.strip():
                values[app_type] = raw
            else:
                values[app_type] = None
                errors[app_type] = "Saved preferred installation ID is invalid."
        return PreferredInstallations(values["player"], values["studio"], errors or None)

    def set_preferred(self, app_type: str, installation_id: str, library: LibraryStore) -> None:
        if app_type not in APP_BUNDLE_NAMES:
            raise ValueError(f"Unsupported application type: {app_type}")
        # This method is called from a worker. Validate against the current library
        # and bundle before recording an ID from a potentially stale UI snapshot.
        with library.mutation_lock:
            records = library.load()
            record = next((item for item in records if item.id == installation_id), None)
            if record is None or record.app_type != app_type:
                raise PreferencesError(f"That installation is no longer registered as Roblox {app_type.title()}.")
            try:
                validate_bundle(record.bundle_path, app_type)
            except (BundleValidationError, OSError) as error:
                raise PreferencesError(f"That installation is not currently valid: {error}") from error
            self._update(app_type, installation_id)

    def clear(self, app_type: str) -> None:
        self._update(app_type, None)

    def clear_if_matches(self, app_type: str, installation_id: str) -> bool:
        if app_type not in APP_BUNDLE_NAMES:
            raise ValueError(f"Unsupported application type: {app_type}")
        with self._path_lock:
            document = self._read_document()
            entries = document["preferred_installations"]
            if entries.get(app_type) != installation_id:
                return False
            entries[app_type] = None
            self._write(document)
            return True

    def _update(self, app_type: str, installation_id: str | None) -> None:
        if app_type not in APP_BUNDLE_NAMES:
            raise ValueError(f"Unsupported application type: {app_type}")
        if installation_id is not None and (not isinstance(installation_id, str) or not installation_id.strip()):
            raise ValueError("Preferred installation ID must be a non-empty string or null.")
        with self._path_lock:
            document = self._read_document()
            document["preferred_installations"][app_type] = installation_id
            self._write(document)

    def _read_document(self) -> dict:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "preferred_installations": {}}
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise PreferencesError(f"Could not read Roseblox preferences: {error}") from error
        if (
            not isinstance(document, dict)
            or document.get("version") != 1
            or not isinstance(document.get("preferred_installations"), dict)
        ):
            raise PreferencesError("Roseblox preferences have an invalid format; the file was not changed.")
        return document

    def _write(self, document: dict) -> None:
        temporary_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, delete=False
            ) as stream:
                temporary_path = Path(stream.name)
                json.dump(document, stream, indent=2)
                stream.write("\n")
            os.replace(temporary_path, self.path)
        except OSError as error:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise PreferencesError(f"Could not save Roseblox preferences: {error}") from error
