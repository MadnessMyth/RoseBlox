"""Persistent registry for existing Roblox application bundles."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


APP_BUNDLE_NAMES = {
    "player": "RobloxPlayer.app",
    "studio": "RobloxStudio.app",
}

# Updater bundles that sit beside the primary bundle in a macOS Roblox install.
# Launching the updater runs the Roblox installer, which rebuilds the latest
# packages before booting; the primary RobloxPlayer.app instead runs the game
# directly without checking for updates. Studio ships no separate updater bundle.
APP_UPDATER_BUNDLE_NAMES = {
    "player": "Roblox.app",
}

# Human-readable labels for the launch variants offered to the user when more
# than one Roblox bundle is installed in the same folder.
LAUNCH_VARIANT_LABELS = {
    "player": "Roblox Player",
    "updater": "Roblox Updater",
}


def allowed_bundle_names(app_type: str) -> list[str]:
    """Bundle directory names accepted for an application type.

    The primary bundle is always returned; the updater bundle is included so
    callers can describe what is allowed, but ``validate_bundle`` only accepts
    it when the caller explicitly opts in via ``allow_updater``. Registration
    and management therefore stay strict, while the launch path can start the
    updater variant the user picks from the variant prompt.
    """
    names: list[str] = []
    primary = APP_BUNDLE_NAMES.get(app_type)
    if primary:
        names.append(primary)
    updater = APP_UPDATER_BUNDLE_NAMES.get(app_type)
    if updater and updater not in names:
        names.append(updater)
    return names


@dataclass(frozen=True)
class LaunchVariant:
    """A launchable Roblox bundle discovered beside a registered installation."""

    role: str
    bundle_path: Path


def discover_launch_variants(bundle_path: str | Path, app_type: str) -> list[LaunchVariant]:
    """Return the launch variants present beside a registered bundle.

    For the player application type, an installation directory may hold both the
    direct ``RobloxPlayer.app`` (runs the game without checking for updates) and
    the ``Roblox.app`` updater (rebuilds the latest packages before booting).
    Each present, ``Contents``-valid bundle becomes a launch variant, ordered
    with the direct player first so callers can default to it. A single variant
    means no choice prompt is needed; the caller may launch it directly.
    """
    bundle = Path(bundle_path).expanduser().absolute()
    parent = bundle.parent
    primary = APP_BUNDLE_NAMES.get(app_type)
    updater = APP_UPDATER_BUNDLE_NAMES.get(app_type)
    candidates: list[tuple[str, str]] = []
    if primary:
        candidates.append(("player", primary))
    if updater and updater != primary:
        candidates.append(("updater", updater))
    variants: list[LaunchVariant] = []
    seen: set[Path] = set()
    for role, name in candidates:
        candidate = (parent / name).absolute()
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.is_dir() and (candidate / "Contents").is_dir():
            variants.append(LaunchVariant(role=role, bundle_path=candidate))
    return variants


_MUTATION_LOCKS: dict[Path, threading.RLock] = {}
_MUTATION_LOCKS_GUARD = threading.Lock()


def _mutation_lock_for(path: Path) -> threading.RLock:
    key = path.resolve()
    with _MUTATION_LOCKS_GUARD:
        lock = _MUTATION_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _MUTATION_LOCKS[key] = lock
        return lock


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
    version: str | None = None
    managed: bool = False
    source: str = "existing_bundle"
    archive_path: str | None = None


def default_data_directory() -> Path:
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return data_home / "roseblox"


def managed_install_root() -> Path:
    return default_data_directory() / "installations"


def default_library_path() -> Path:
    return default_data_directory() / "library.json"


def validate_bundle(bundle_path: str | Path, app_type: str, *, allow_updater: bool = False) -> Path:
    """Validate the expected .app directory with a practical Contents check.

    By default only the primary bundle (``RobloxPlayer.app`` / ``RobloxStudio.app``)
    is accepted, which keeps registration and management strict. When
    ``allow_updater`` is set, the updater bundle (``Roblox.app`` for the player)
    is also accepted, so the launch path can start the updater variant chosen by
    the user from the variant prompt.
    """
    expected_name = APP_BUNDLE_NAMES.get(app_type)
    if expected_name is None:
        raise BundleValidationError(f"Unsupported application type: {app_type}")
    allowed = [expected_name]
    if allow_updater:
        updater_name = APP_UPDATER_BUNDLE_NAMES.get(app_type)
        if updater_name and updater_name not in allowed:
            allowed.append(updater_name)
    path = Path(bundle_path).expanduser()
    if not path.exists() or not path.is_dir():
        raise BundleValidationError(f"Application bundle directory does not exist: {path}")
    path = path.absolute()
    if path.name not in allowed:
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
    version = value.get("version")
    if version is not None and (not isinstance(version, str) or not version):
        raise ValueError("installation version must be a non-empty string or null")
    managed = value.get("managed", False)
    if not isinstance(managed, bool):
        raise ValueError("installation managed flag must be a boolean")
    source = value.get("source", "existing_bundle")
    if not isinstance(source, str) or not source:
        raise ValueError("installation source must be a non-empty string")
    archive_path = value.get("archive_path")
    if archive_path is not None:
        if not isinstance(archive_path, str) or not Path(archive_path).is_absolute():
            raise ValueError("installation archive path must be absolute or null")
    return InstallationRecord(
        **{key: value[key] for key in required},
        version=version,
        managed=managed,
        source=source,
        archive_path=archive_path,
    )


class LibraryStore:
    """JSON-backed library which never removes registered bundle files."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_library_path()
        self._mutation_lock: threading.RLock | None = None

    @property
    def mutation_lock(self) -> threading.RLock:
        """Shared process-local lock for compound Library and preference changes."""
        if self._mutation_lock is None:
            self._mutation_lock = _mutation_lock_for(self.path)
        return self._mutation_lock

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

    def add(
        self,
        app_type: str,
        bundle_path: str | Path,
        display_name: str,
        *,
        version: str | None = None,
        managed: bool = False,
        source: str = "existing_bundle",
        archive_path: str | Path | None = None,
    ) -> InstallationRecord:
        bundle = validate_bundle(bundle_path, app_type)
        display_name = display_name.strip()
        if not display_name:
            raise ValueError("Enter a name for this installation.")
        if version is not None and (not isinstance(version, str) or not version):
            raise ValueError("Version must be a non-empty string or unknown.")
        if not isinstance(managed, bool):
            raise ValueError("Managed flag must be a boolean.")
        if not isinstance(source, str) or not source:
            raise ValueError("Installation source must be provided.")
        stored_archive_path = None if archive_path is None else str(Path(archive_path).absolute())

        with self.mutation_lock:
            # Revalidate after acquiring the lock: a concurrent deletion may have
            # removed this bundle after the initial user selection.
            bundle = validate_bundle(bundle, app_type)
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
                version=version,
                managed=managed,
                source=source,
                archive_path=stored_archive_path,
            )
            self._save([*records, record])
            return record

    def remove(self, installation_id: str, *, delete_managed_files: bool = False) -> bool:
        with self.mutation_lock:
            records = self.load()
            record = next((item for item in records if item.id == installation_id), None)
            if record is None:
                return False
            if delete_managed_files and not record.managed:
                raise LibraryDataError("External application bundles can never be deleted by Roseblox.")

            tombstone: Path | None = None
            if delete_managed_files:
                bundle = self._validated_managed_bundle(record)
                tombstone = bundle.with_name(f".roseblox-delete-{uuid.uuid4().hex}")
                try:
                    os.replace(bundle, tombstone)
                except OSError as error:
                    raise LibraryDataError(f"Could not prepare managed bundle deletion: {error}") from error

            remaining = [item for item in records if item.id != installation_id]
            try:
                self._save(remaining)
            except LibraryDataError:
                if tombstone is not None:
                    self._restore_bundle(tombstone, record.bundle_path)
                raise
            if tombstone is not None:
                try:
                    shutil.rmtree(tombstone)
                except OSError as error:
                    # If cleanup fails before deleting the tombstone, put both the
                    # bundle and its record back. Partial cleanup cannot be made
                    # perfectly transactional, so report any failed rollback.
                    try:
                        self._restore_bundle(tombstone, record.bundle_path)
                    except (OSError, LibraryDataError) as restore_error:
                        raise LibraryDataError(
                            f"Managed deletion failed ({error}); bundle rollback failed ({restore_error}). "
                            f"Inspect {record.bundle_path} and {tombstone}."
                        ) from restore_error
                    try:
                        validate_bundle(record.bundle_path, record.app_type)
                    except (BundleValidationError, OSError) as restore_error:
                        raise LibraryDataError(
                            f"Managed deletion removed part of the bundle before failing. The incomplete bundle "
                            f"was restored at {record.bundle_path}; its Library registration remains removed."
                        ) from restore_error
                    try:
                        self._save(records)
                    except LibraryDataError as restore_error:
                        raise LibraryDataError(
                            f"Managed deletion failed ({error}); the bundle was restored at {record.bundle_path}, "
                            f"but its Library registration could not be restored: {restore_error}"
                        ) from restore_error
                    raise LibraryDataError(
                        f"Managed deletion failed; the bundle and Library registration were restored: {error}"
                    ) from error
            return True

    @staticmethod
    def _restore_bundle(tombstone: Path, original: str | Path) -> None:
        destination = Path(original)
        if destination.exists() or destination.is_symlink():
            raise LibraryDataError(f"Cannot restore managed bundle because its original path is occupied: {destination}")
        try:
            os.rename(tombstone, destination)
        except OSError as error:
            raise LibraryDataError(f"Could not restore managed bundle to {destination}: {error}") from error

    @staticmethod
    def _validated_managed_bundle(record: InstallationRecord) -> Path:
        if not record.managed:
            raise LibraryDataError("Only managed installations can be deleted.")
        bundle = Path(record.bundle_path)
        expected = APP_BUNDLE_NAMES.get(record.app_type)
        if expected is None or bundle.name != expected or bundle.is_symlink() or not bundle.is_dir():
            raise LibraryDataError("Managed application path is missing or unsafe; files were not deleted.")
        try:
            root = managed_install_root().resolve(strict=True)
            resolved = bundle.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as error:
            raise LibraryDataError("Managed application path is outside Roseblox's installation directory.") from error
        return bundle

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
