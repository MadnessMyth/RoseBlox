"""Background Roblox update checks: what to offer, reminders, and the autostart entry."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .library import InstallationRecord
from .setup import APP_TYPES, SetupSettingsError, SetupStore
from .url_handlers import _desktop_quote, URLHandlerError

UPDATER_APP_ID = "io.github.roseblox.Roseblox.Updater"
UPDATER_DESKTOP_ID = f"{UPDATER_APP_ID}.desktop"
FIRST_CHECK_DELAY_SECONDS = 60
CHECK_INTERVAL_SECONDS = 3 * 60 * 60
REMIND_AFTER_SECONDS = 24 * 60 * 60
APP_TITLES = {"player": "Roblox Player", "studio": "Roblox Studio"}
_DESKTOP_MARKER = "X-Roseblox-Managed=true"


class UpdateCheckerError(RuntimeError):
    """Raised when background update checks cannot be turned on or off."""


@dataclass(frozen=True)
class AvailableUpdate:
    app_type: str
    version: str
    installed_version: str | None = None


@dataclass(frozen=True)
class UpdateCheckerStatus:
    enabled: bool
    message: str
    error: str | None = None


def find_available_updates(
    records: Iterable[InstallationRecord],
    latest_versions: Mapping[str, str],
    skipped_versions: Mapping[str, str],
) -> list[AvailableUpdate]:
    """Updates worth offering: only for installed apps, never a skipped or present version."""
    records = list(records)
    updates = []
    for app_type in APP_TYPES:
        installed = [record for record in records if record.app_type == app_type]
        latest = latest_versions.get(app_type)
        if not installed or not latest or skipped_versions.get(app_type) == latest:
            continue
        if any(record.version == latest for record in installed):
            continue
        known_versions = [record.version for record in installed if record.version]
        updates.append(AvailableUpdate(app_type, latest, known_versions[-1] if known_versions else None))
    return updates


def check_for_updates(library, version_client, version_settings, version_cache, skipped_versions):
    """Look up the latest version of each installed app.

    Returns ``(updates, errors)`` where errors maps an app type to a message; one app's
    lookup failing does not hide the other's update.
    """
    records = library.load()
    latest: dict[str, str] = {}
    errors: dict[str, str] = {}
    for app_type in APP_TYPES:
        if not any(record.app_type == app_type for record in records):
            continue
        try:
            info = version_client.fetch(app_type, version_settings.load_for(app_type))
        except Exception as error:
            errors[app_type] = str(error)[:500]
            continue
        latest[app_type] = info.version
        try:
            version_cache.update(app_type, info)
        except Exception:
            pass
    return find_available_updates(records, latest, skipped_versions), errors


def default_state_path() -> Path:
    state_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return state_home / "roseblox" / "update-notifications.json"


class NotificationLog:
    """Remembers which version was last announced so reminders stay infrequent.

    This is bookkeeping only: an unreadable file just means notifying again.
    """

    def __init__(self, path: Path | None = None, *, remind_after: float = REMIND_AFTER_SECONDS) -> None:
        self.path = Path(path) if path is not None else default_state_path()
        self.remind_after = remind_after

    def should_notify(self, update: AvailableUpdate, now: float | None = None) -> bool:
        entry = self._read().get(update.app_type)
        if not isinstance(entry, dict) or entry.get("version") != update.version:
            return True
        notified_at = entry.get("notified_at")
        if not isinstance(notified_at, (int, float)):
            return True
        return (time.time() if now is None else now) - notified_at >= self.remind_after

    def record(self, update: AvailableUpdate, now: float | None = None) -> None:
        document = self._read()
        document[update.app_type] = {
            "version": update.version,
            "notified_at": time.time() if now is None else now,
        }
        temporary_path = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, delete=False
            ) as stream:
                temporary_path = Path(stream.name)
                json.dump(document, stream, indent=2, sort_keys=True)
            os.replace(temporary_path, self.path)
        except OSError:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _read(self) -> dict:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        return document if isinstance(document, dict) else {}


def _default_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def _default_data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def _spawn_detached(arguments: list[str]) -> None:
    subprocess.Popen(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


class UpdateCheckerService:
    """Turns the background update checker on or off for this user.

    The checker starts with the desktop session through an XDG autostart entry. A
    matching hidden application entry lets desktops show its notifications under the
    Roseblox name and icon.
    """

    def __init__(
        self,
        setup_store: SetupStore | None = None,
        *,
        config_home: Path | None = None,
        data_home: Path | None = None,
        executable_lookup: Callable[[str], str | None] = shutil.which,
        spawn: Callable[[list[str]], object] = _spawn_detached,
    ) -> None:
        self.setup_store = setup_store or SetupStore()
        self.config_home = Path(config_home) if config_home is not None else _default_config_home()
        self.data_home = Path(data_home) if data_home is not None else _default_data_home()
        self.executable_lookup = executable_lookup
        self.spawn = spawn

    @property
    def autostart_path(self) -> Path:
        return self.config_home / "autostart" / UPDATER_DESKTOP_ID

    @property
    def desktop_path(self) -> Path:
        return self.data_home / "applications" / UPDATER_DESKTOP_ID

    def status(self) -> UpdateCheckerStatus:
        try:
            enabled = self.setup_store.load().update_checks_enabled
        except SetupSettingsError as error:
            return UpdateCheckerStatus(False, "Update-check settings are unavailable.", str(error))
        if not enabled:
            return UpdateCheckerStatus(False, "Off. Turn this on to be notified when a new Roblox version is out.")
        if not self._owned(self.autostart_path):
            return UpdateCheckerStatus(
                True,
                "On, but the login entry is missing. Turn the switch off and on again to repair it.",
                "missing autostart entry",
            )
        return UpdateCheckerStatus(
            True, "On. Roseblox checks every few hours and notifies you about new versions."
        )

    def enable(self) -> UpdateCheckerStatus:
        executable = self._executable()
        written = []
        try:
            for path, autostart in ((self.desktop_path, False), (self.autostart_path, True)):
                self._write_owned(path, self._desktop_contents(executable, autostart))
                written.append(path)
            self.setup_store.save_update_checks(True)
        except (OSError, SetupSettingsError, UpdateCheckerError) as error:
            for path in written:
                self._remove_owned(path)
            raise UpdateCheckerError(f"Update checks were not turned on: {error}") from error
        try:
            self.spawn([executable, "--update-service"])
        except OSError as error:
            return UpdateCheckerStatus(
                True,
                "On. The checker will start the next time you log in.",
                f"Could not start it now: {error}",
            )
        return self.status()

    def disable(self) -> UpdateCheckerStatus:
        try:
            self.setup_store.save_update_checks(False)
        except SetupSettingsError as error:
            raise UpdateCheckerError(f"Update checks were not turned off: {error}") from error
        problems = []
        for path in (self.autostart_path, self.desktop_path):
            try:
                self._remove_owned(path)
            except OSError as error:
                problems.append(str(error))
        executable = self.executable_lookup("roseblox")
        if executable:
            try:
                self.spawn([os.path.abspath(executable), "--update-service", "--quit"])
            except OSError:
                pass  # A running checker also stops at its next check once it sees the setting.
        if problems:
            raise UpdateCheckerError("Update checks are off, but " + "; ".join(problems))
        return self.status()

    def check_now(self) -> None:
        self.spawn([self._executable(), "--update-service", "--check-now"])

    def _executable(self) -> str:
        executable = self.executable_lookup("roseblox")
        if not executable:
            raise UpdateCheckerError(
                "The `roseblox` command is not available in PATH, so it cannot be started at login."
            )
        executable = os.path.abspath(executable)
        if not Path(executable).is_file() or not os.access(executable, os.X_OK):
            raise UpdateCheckerError("The Roseblox launcher found in PATH is not an executable file.")
        return executable

    @staticmethod
    def _desktop_contents(executable: str, autostart: bool) -> str:
        try:
            command = _desktop_quote(executable)
        except URLHandlerError as error:
            raise UpdateCheckerError(str(error)) from error
        lines = [
            "[Desktop Entry]",
            "Type=Application",
            "Name=Roseblox Update Checker",
            "Comment=Notifies you when a new Roblox version is available",
            "Icon=io.github.roseblox.Roseblox",
            f"Exec={command} --update-service",
            "Terminal=false",
            "NoDisplay=true",
        ]
        if autostart:
            lines.append("X-GNOME-Autostart-enabled=true")
        lines.append(_DESKTOP_MARKER)
        return "\n".join(lines) + "\n"

    @staticmethod
    def _owned(path: Path) -> bool:
        try:
            return _DESKTOP_MARKER in path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return False

    def _write_owned(self, path: Path, contents: str) -> None:
        if path.exists() and not self._owned(path):
            raise UpdateCheckerError(f"{path} exists and was not created by Roseblox; it was left unchanged.")
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
        ) as stream:
            stream.write(contents)
            temporary = Path(stream.name)
        os.replace(temporary, path)

    def _remove_owned(self, path: Path) -> None:
        if self._owned(path):
            path.unlink()
