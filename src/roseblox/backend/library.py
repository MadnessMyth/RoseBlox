"""Persistent registry for existing Roblox application bundles."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


APP_BUNDLE_NAMES = {
    "player": "RobloxPlayer.app",
    "studio": "RobloxStudio.app",
}


class BundleValidationError(ValueError):
    """Raised when a selected directory is not a supported Roblox bundle."""


class LibraryDataError(RuntimeError):
    """Raised when the library file cannot be safely loaded or saved."""


@dataclass(frozen=True)
class InstallationRecord:
    id: str
    display_name: str
    app_type: str
    bundle_path: str
    registered_at: str
    validation_state: str


def default_library_path() -> Path:
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return data_home / "roseblox" / "library.json"


def validate_bundle(bundle_path: str | Path, app_type: str) -> Path:
    """Validate the expected .app directory with a practical Contents check."""
    expected_name = APP_BUNDLE_NAMES.get(app_type)
    if expected_name is None:
        raise BundleValidationError(f"Unsupported application type: {app_type}")
    path = Path(bundle_path).expanduser()
    if not path.exists() or not path.is_dir():
        raise BundleValidationError(f"Application bundle directory does not exist: {path}")
    path = path.absolute()
    if path.name != expected_name:
        raise BundleValidationError(f"Select a {expected_name} directory.")
    if not (path / "Contents").is_dir():
        raise BundleValidationError(f"The selected bundle is missing its Contents directory: {path}")
    return path


def _record_from_json(value: Any) -> InstallationRecord:
    if not isinstance(value, dict):
        raise ValueError("installation record must be an object")
    required = ("id", "display_name", "app_type", "bundle_path", "registered_at", "validation_state")
    if any(not isinstance(value.get(key), str) or not value[key] for key in required):
        raise ValueError("installation record is missing a required string field")
    if value["app_type"] not in APP_BUNDLE_NAMES:
        raise ValueError("installation record has an unsupported application type")
    if not Path(value["bundle_path"]).is_absolute():
        raise ValueError("installation bundle path must be absolute")
    return InstallationRecord(**{key: value[key] for key in required})


class LibraryStore:
    """JSON-backed library which never removes registered bundle files."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_library_path()

    def load(self) -> list[InstallationRecord]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise LibraryDataError(f"Could not read the installation library: {error}") from error
        if not isinstance(data, dict) or not isinstance(data.get("installations"), list):
            raise LibraryDataError("The installation library has an invalid format.")
        try:
            return [_record_from_json(item) for item in data["installations"]]
        except (TypeError, ValueError, KeyError) as error:
            raise LibraryDataError(f"The installation library contains an invalid record: {error}") from error

    def add(self, app_type: str, bundle_path: str | Path, display_name: str) -> InstallationRecord:
        bundle = validate_bundle(bundle_path, app_type)
        display_name = display_name.strip()
        if not display_name:
            raise ValueError("Enter a name for this installation.")

        records = self.load()
        if any(record.app_type == app_type and record.bundle_path == str(bundle) for record in records):
            raise ValueError("This application bundle is already registered.")
        record = InstallationRecord(
            id=str(uuid.uuid4()),
            display_name=display_name,
            app_type=app_type,
            bundle_path=str(bundle),
            registered_at=datetime.now(timezone.utc).isoformat(),
            validation_state="valid",
        )
        self._save([*records, record])
        return record

    def remove(self, installation_id: str) -> bool:
        records = self.load()
        remaining = [record for record in records if record.id != installation_id]
        if len(remaining) == len(records):
            return False
        self._save(remaining)
        return True

    def _save(self, records: list[InstallationRecord]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, delete=False
            ) as stream:
                temporary_path = Path(stream.name)
                json.dump(
                    {"version": 1, "installations": [asdict(record) for record in records]},
                    stream,
                    indent=2,
                )
                stream.write("\n")
            os.replace(temporary_path, self.path)
        except OSError as error:
            try:
                temporary_path.unlink(missing_ok=True)
            except (OSError, UnboundLocalError):
                pass
            raise LibraryDataError(f"Could not save the installation library: {error}") from error
