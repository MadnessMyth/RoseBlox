"""Persistent onboarding, URL-handler, launch, and update preferences."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .microphone import DEFAULT_RELEASE_DELAY_MS, MICROPHONE_MODES, MODE_TOGGLE, validate_release_delay
from .microphone_sounds import DEFAULT_VOLUME, MicrophoneSounds, validate_sound_path, validate_volume

APP_TYPES = ("player", "studio")


class SetupSettingsError(RuntimeError):
    """Raised when setup preferences cannot safely be read or written."""


def default_setup_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config_home / "roseblox" / "setup.json"


@dataclass(frozen=True)
class ClosePreference:
    """Whether Roseblox closes itself once an application has started."""

    enabled: bool = False
    only_from_link: bool = False

    def applies(self, from_link: bool) -> bool:
        return self.enabled and (from_link or not self.only_from_link)


COLOR_THEMES = ("roseblox", "system")
COLOR_STYLES = ("dark", "light", "system")
ACCENT_CHOICES = (
    "roseblox", "system", "blue", "teal", "green", "yellow", "orange", "red", "pink", "purple", "slate",
)


@dataclass(frozen=True)
class Appearance:
    """``colors`` picks Roseblox's palette or the desktop's; ``style`` is dark, light or system."""

    colors: str = "roseblox"
    style: str = "dark"
    accent: str = "roseblox"

    def validate(self) -> "Appearance":
        if self.colors not in COLOR_THEMES or self.style not in COLOR_STYLES or self.accent not in ACCENT_CHOICES:
            raise ValueError("Unknown appearance setting.")
        return self


@dataclass(frozen=True)
class WhilePlaying:
    """Roblox Player activity features; all off until the user turns them on."""

    discord_presence: bool = False
    discord_join_button: bool = True
    discord_application_id: str = ""
    server_location: bool = False
    close_on_leave: bool = False
    microphone_toggle: bool = False
    microphone_mode: str = MODE_TOGGLE
    microphone_release_delay_ms: int = DEFAULT_RELEASE_DELAY_MS
    microphone_inverted: bool = False
    microphone_sounds: bool = False
    microphone_sound_volume: int = DEFAULT_VOLUME
    microphone_sound_on: str = ""
    microphone_sound_off: str = ""

    @property
    def any_enabled(self) -> bool:
        return self.discord_presence or self.server_location or self.close_on_leave or self.microphone_toggle

    @property
    def sounds(self) -> MicrophoneSounds:
        return MicrophoneSounds(
            self.microphone_sounds, self.microphone_sound_volume, self.microphone_sound_on, self.microphone_sound_off
        )

    def validate(self) -> "WhilePlaying":
        for value in (
            self.discord_presence, self.discord_join_button, self.server_location, self.close_on_leave,
            self.microphone_toggle, self.microphone_inverted, self.microphone_sounds,
        ):
            if not isinstance(value, bool):
                raise ValueError("While Playing switches must be on or off.")
        if self.microphone_mode not in MICROPHONE_MODES:
            raise ValueError("The microphone mode must be Toggle or Hold.")
        validate_release_delay(self.microphone_release_delay_ms)
        validate_volume(self.microphone_sound_volume)
        validate_sound_path(self.microphone_sound_on)
        validate_sound_path(self.microphone_sound_off)
        application_id = self.discord_application_id
        if not isinstance(application_id, str) or (application_id and not application_id.isdigit()) or len(application_id) > 22:
            raise ValueError("The Discord application ID must be a number.")
        return self


@dataclass(frozen=True)
class SetupState:
    onboarding_completed: bool = False
    url_handlers_enabled: bool = False
    previous_url_defaults: dict[str, str | None] | None = None
    close_after_launch: dict[str, ClosePreference] = field(default_factory=dict)
    update_checks_enabled: bool = False
    roseblox_update_checks: bool = True
    skipped_versions: dict[str, str] = field(default_factory=dict)
    appearance: Appearance = field(default_factory=Appearance)
    plain_ctrl_click: bool = True
    multiple_players: bool = False
    expanded_sections: dict[str, bool] = field(default_factory=dict)
    while_playing: WhilePlaying = field(default_factory=WhilePlaying)

    def close_preference(self, app_type: str) -> ClosePreference:
        return self.close_after_launch.get(app_type, ClosePreference())


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
        return SetupState(
            completed,
            enabled,
            dict(previous),
            close_after_launch=self._close_preferences(document.get("close_after_launch", {})),
            update_checks_enabled=self._boolean(document, "update_checks_enabled"),
            roseblox_update_checks=self._boolean(document, "roseblox_update_checks", True),
            skipped_versions=self._skipped_versions(document.get("skipped_versions", {})),
            appearance=self._appearance(document.get("appearance", {})),
            plain_ctrl_click=self._boolean(document, "plain_ctrl_click", True),
            multiple_players=self._boolean(document, "multiple_players"),
            expanded_sections=self._expanded_sections(document.get("expanded_sections", {})),
            while_playing=self._while_playing(document.get("while_playing", {})),
        )

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

    def save_close_after_launch(self, app_type: str, preference: ClosePreference) -> SetupState:
        self._require_app_type(app_type)
        if not isinstance(preference.enabled, bool) or not isinstance(preference.only_from_link, bool):
            raise ValueError("Close-after-launch preferences must be enabled or disabled.")

        def change(document: dict) -> None:
            preferences = dict(document.get("close_after_launch") or {})
            preferences[app_type] = {
                "enabled": preference.enabled,
                "only_from_link": preference.only_from_link,
            }
            document["close_after_launch"] = preferences

        return self._modify(change)

    def save_update_checks(self, enabled: bool) -> SetupState:
        if not isinstance(enabled, bool):
            raise ValueError("The update-check preference must be enabled or disabled.")
        return self._update(update_checks_enabled=enabled)

    def save_roseblox_update_checks(self, enabled: bool) -> SetupState:
        if not isinstance(enabled, bool):
            raise ValueError("The Roseblox update-check preference must be enabled or disabled.")
        return self._update(roseblox_update_checks=enabled)

    def skip_version(self, app_type: str, version: str) -> SetupState:
        self._require_app_type(app_type)
        if not isinstance(version, str) or not version:
            raise ValueError("A Roblox version is required.")

        def change(document: dict) -> None:
            skipped = dict(document.get("skipped_versions") or {})
            skipped[app_type] = version
            document["skipped_versions"] = skipped

        return self._modify(change)

    def clear_skipped_versions(self) -> SetupState:
        return self._update(skipped_versions={})

    @staticmethod
    def _require_app_type(app_type: str) -> None:
        if app_type not in APP_TYPES:
            raise ValueError(f"Unknown Roblox application type: {app_type!r}")

    def save_appearance(self, appearance: Appearance) -> SetupState:
        appearance.validate()
        return self._update(appearance={
            "colors": appearance.colors, "style": appearance.style, "accent": appearance.accent,
        })

    def save_plain_ctrl_click(self, enabled: bool) -> SetupState:
        if not isinstance(enabled, bool):
            raise ValueError("The Ctrl + left-click setting must be on or off.")
        return self._update(plain_ctrl_click=enabled)

    def save_multiple_players(self, enabled: bool) -> SetupState:
        if not isinstance(enabled, bool):
            raise ValueError("The multiple Player setting must be on or off.")
        return self._update(multiple_players=enabled)

    def save_while_playing(self, settings: WhilePlaying) -> SetupState:
        settings.validate()
        return self._update(while_playing={
            "discord_presence": settings.discord_presence,
            "discord_join_button": settings.discord_join_button,
            "discord_application_id": settings.discord_application_id,
            "server_location": settings.server_location,
            "close_on_leave": settings.close_on_leave,
            "microphone_toggle": settings.microphone_toggle,
            "microphone_mode": settings.microphone_mode,
            "microphone_release_delay_ms": settings.microphone_release_delay_ms,
            "microphone_inverted": settings.microphone_inverted,
            "microphone_sounds": settings.microphone_sounds,
            "microphone_sound_volume": settings.microphone_sound_volume,
            "microphone_sound_on": settings.microphone_sound_on,
            "microphone_sound_off": settings.microphone_sound_off,
        })

    def save_section_expanded(self, section: str, expanded: bool) -> SetupState:
        if not isinstance(section, str) or not section or not isinstance(expanded, bool):
            raise ValueError("Invalid Settings section state.")

        def change(document: dict) -> None:
            sections = dict(document.get("expanded_sections") or {})
            sections[section] = expanded
            document["expanded_sections"] = sections

        return self._modify(change)

    @staticmethod
    def _appearance(value: object) -> Appearance:
        invalid = SetupSettingsError("Saved appearance settings are invalid; the file was not changed.")
        if not isinstance(value, dict):
            raise invalid
        defaults = Appearance()
        try:
            return Appearance(
                value.get("colors", defaults.colors),
                value.get("style", defaults.style),
                value.get("accent", defaults.accent),
            ).validate()
        except ValueError as error:
            raise invalid from error

    @staticmethod
    def _while_playing(value: object) -> WhilePlaying:
        invalid = SetupSettingsError("Saved While Playing settings are invalid; the file was not changed.")
        if not isinstance(value, dict):
            raise invalid
        defaults = WhilePlaying()
        try:
            return WhilePlaying(**{
                name: value.get(name, getattr(defaults, name))
                for name in (
                    "discord_presence", "discord_join_button", "discord_application_id",
                    "server_location", "close_on_leave", "microphone_toggle", "microphone_mode",
                    "microphone_release_delay_ms", "microphone_inverted", "microphone_sounds",
                    "microphone_sound_volume", "microphone_sound_on", "microphone_sound_off",
                )
            }).validate()
        except ValueError as error:
            raise invalid from error

    @staticmethod
    def _expanded_sections(value: object) -> dict[str, bool]:
        if not isinstance(value, dict) or any(
            not isinstance(key, str) or not isinstance(state, bool) for key, state in value.items()
        ):
            raise SetupSettingsError("Saved Settings layout is invalid; the file was not changed.")
        return dict(value)

    @staticmethod
    def _boolean(document: dict, key: str, default: bool = False) -> bool:
        value = document.get(key, default)
        if not isinstance(value, bool):
            raise SetupSettingsError("Roseblox setup settings have an invalid format; the file was not changed.")
        return value

    @staticmethod
    def _close_preferences(value: object) -> dict[str, ClosePreference]:
        invalid = SetupSettingsError("Saved close-after-launch settings are invalid; the file was not changed.")
        if not isinstance(value, dict):
            raise invalid
        preferences = {}
        for app_type, entry in value.items():
            if app_type not in APP_TYPES or not isinstance(entry, dict):
                raise invalid
            enabled = entry.get("enabled", False)
            only_from_link = entry.get("only_from_link", False)
            if not isinstance(enabled, bool) or not isinstance(only_from_link, bool):
                raise invalid
            preferences[app_type] = ClosePreference(enabled, only_from_link)
        return preferences

    @staticmethod
    def _skipped_versions(value: object) -> dict[str, str]:
        if not isinstance(value, dict) or any(
            key not in APP_TYPES or not isinstance(version, str) for key, version in value.items()
        ):
            raise SetupSettingsError("Saved skipped Roblox versions are invalid; the file was not changed.")
        return dict(value)

    def _update(self, **updates: object) -> SetupState:
        return self._modify(lambda document: document.update(updates))

    def _modify(self, change) -> SetupState:
        # The update checker runs as a separate process and writes skipped versions,
        # so read-modify-write cycles are serialized across processes too.
        with self._lock, self._file_lock():
            document = self._read()
            change(document)
            document["version"] = 1
            self._write(document)
        return self.load()

    @contextlib.contextmanager
    def _file_lock(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            stream = open(self.path.with_name(self.path.name + ".lock"), "a")
        except OSError as error:
            raise SetupSettingsError(f"Could not lock Roseblox setup settings: {error}") from error
        with stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

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
