"""Per-user Roblox URL handler registration and safe URI routing."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from typing import Callable

from .setup import SetupSettingsError, SetupStore


SCHEME_TO_APP = {
    "roblox": "player",
    "roblox-player": "player",
    "roblox-studio": "studio",
    "roblox-studio-auth": "studio",
}
SUPPORTED_SCHEMES = tuple(SCHEME_TO_APP)
DESKTOP_ID = "io.github.roseblox.Roseblox-url-handler.desktop"
_SCHEME_MIME = {scheme: f"x-scheme-handler/{scheme}" for scheme in SUPPORTED_SCHEMES}
_DESKTOP_MARKER = "X-Roseblox-Managed=true"


class URLHandlerError(RuntimeError):
    """Raised when URL handler installation or removal is incomplete."""


@dataclass(frozen=True)
class ParsedRobloxURL:
    app_type: str
    scheme: str
    uri: str


@dataclass(frozen=True)
class URLHandlerStatus:
    enabled: bool
    registered: bool
    message: str
    error: str | None = None


def parse_roblox_url(uri: str) -> ParsedRobloxURL:
    if not isinstance(uri, str) or not uri or len(uri) > 8192:
        raise ValueError("Roblox link is empty or too long.")
    if any(ord(character) < 0x20 or ord(character) == 0x7f for character in uri):
        raise ValueError("Roblox link contains control characters.")
    if any(character.isspace() for character in uri):
        raise ValueError("Roblox link contains whitespace.")
    if ":" not in uri:
        raise ValueError("Roblox link is malformed.")
    try:
        scheme = urlsplit(uri).scheme.lower()
    except ValueError as error:
        raise ValueError("Roblox link is malformed.") from error
    app_type = SCHEME_TO_APP.get(scheme)
    if app_type is None:
        raise ValueError("This Roblox link type is not supported by Roseblox.")
    return ParsedRobloxURL(app_type, scheme, uri)


def _default_data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def _default_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def _desktop_quote(value: str) -> str:
    if any(ord(character) < 0x20 or ord(character) == 0x7f for character in value):
        raise URLHandlerError("Roseblox executable path contains unsupported characters.")
    escaped = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    if any(character.isspace() for character in escaped) or any(character in escaped for character in '"\\'):
        return f'"{escaped}"'
    return escaped


class URLHandlerService:
    """Manages only its own desktop file and MIME associations in user data."""

    def __init__(
        self,
        setup_store: SetupStore | None = None,
        *,
        data_home: Path | None = None,
        config_home: Path | None = None,
        executable_lookup: Callable[[str], str | None] = shutil.which,
        command_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self.setup_store = setup_store or SetupStore()
        self.data_home = Path(data_home) if data_home is not None else _default_data_home()
        self.config_home = Path(config_home) if config_home is not None else _default_config_home()
        self.executable_lookup = executable_lookup
        self.command_runner = command_runner

    @property
    def desktop_path(self) -> Path:
        return self.data_home / "applications" / DESKTOP_ID

    @property
    def mimeapps_path(self) -> Path:
        return self.config_home / "mimeapps.list"

    def _desktop_contents(self) -> str:
        executable = self.executable_lookup("roseblox")
        if not executable:
            raise URLHandlerError(
                "The `roseblox` command is not available in PATH. Install Roseblox or make its launcher available before enabling URL handling."
            )
        executable = os.path.abspath(executable)
        if not Path(executable).is_file() or not os.access(executable, os.X_OK):
            raise URLHandlerError("The Roseblox launcher found in PATH is not an executable file.")
        mime_types = ";".join(_SCHEME_MIME[scheme] for scheme in SUPPORTED_SCHEMES) + ";"
        return (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Roseblox Roblox URL Handler\n"
            "NoDisplay=true\n"
            f"Exec={_desktop_quote(executable)} --roblox-url %u\n"
            "Terminal=false\n"
            f"MimeType={mime_types}\n"
            f"{_DESKTOP_MARKER}\n"
        )

    def _write_owned_desktop(self, contents: str) -> bool:
        path = self.desktop_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            try:
                existing = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as error:
                raise URLHandlerError(f"Could not inspect existing URL-handler entry: {error}") from error
            if existing == contents:
                return False
            raise URLHandlerError(
                f"A different desktop entry already uses {DESKTOP_ID}; it was not overwritten."
            )
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, delete=False
            ) as stream:
                temporary_path = Path(stream.name)
                stream.write(contents)
            os.replace(temporary_path, path)
        except OSError as error:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise URLHandlerError(f"Could not create the URL-handler desktop entry: {error}") from error
        return True

    def _query_default(self, mime_type: str) -> str | None:
        command = self.executable_lookup("xdg-mime")
        if not command:
            raise URLHandlerError(
                "The `xdg-mime` command is unavailable. Install your desktop environment's XDG utilities and retry."
            )
        try:
            result = self.command_runner(
                [command, "query", "default", mime_type],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise URLHandlerError(f"Could not query the desktop URL-handler setting: {error}") from error
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "xdg-mime query failed").strip()
            raise URLHandlerError(message[:500])
        value = result.stdout.strip()
        return value or None

    def _set_default(self, desktop_id: str, mime_type: str) -> None:
        command = self.executable_lookup("xdg-mime")
        if not command:
            raise URLHandlerError("The `xdg-mime` command is unavailable.")
        try:
            result = self.command_runner(
                [command, "default", desktop_id, mime_type],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise URLHandlerError(f"Could not register {mime_type}: {error}") from error
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "xdg-mime could not register the handler").strip()
            raise URLHandlerError(f"Could not register {mime_type}: {message[:500]}")
        if self._query_default(mime_type) != desktop_id:
            raise URLHandlerError(f"The desktop did not confirm Roseblox as the {mime_type} handler.")

    def check(self) -> URLHandlerStatus:
        try:
            state = self.setup_store.load()
            expected = self._desktop_contents()
            actual = self.desktop_path.read_text(encoding="utf-8")
            desktop_matches = actual == expected
            all_defaults = all(
                self._query_default(_SCHEME_MIME[scheme]) == DESKTOP_ID
                for scheme in SUPPORTED_SCHEMES
            )
        except FileNotFoundError:
            state = self.setup_store.load()
            desktop_matches = False
            all_defaults = False
        except (OSError, UnicodeError, SetupSettingsError, URLHandlerError) as error:
            return URLHandlerStatus(False, False, "URL-handler status could not be verified.", str(error))

        registered = desktop_matches and all_defaults
        if registered and state.url_handlers_enabled:
            return URLHandlerStatus(True, True, "Roblox links are set to open through Roseblox.")
        if state.url_handlers_enabled:
            return URLHandlerStatus(
                False,
                registered,
                "URL handling is enabled in Roseblox, but one or more desktop registrations are missing.",
            )
        if registered:
            return URLHandlerStatus(
                False,
                True,
                "A Roseblox URL-handler registration exists while the saved setting is off.",
            )
        return URLHandlerStatus(False, False, "Roblox links are not registered to open through Roseblox.")

    def enable(self) -> URLHandlerStatus:
        state = self.setup_store.load()
        old_defaults: dict[str, str | None] = {}
        try:
            for scheme in SUPPORTED_SCHEMES:
                mime_type = _SCHEME_MIME[scheme]
                current = self._query_default(mime_type)
                if current == DESKTOP_ID and state.previous_url_defaults is not None:
                    old_defaults[mime_type] = state.previous_url_defaults.get(mime_type)
                else:
                    old_defaults[mime_type] = current
            contents = self._desktop_contents()
            self.setup_store.save_url_handlers(False, old_defaults)
            created = self._write_owned_desktop(contents)
            for scheme in SUPPORTED_SCHEMES:
                self._set_default(DESKTOP_ID, _SCHEME_MIME[scheme])
            verified = self.check()
            if not verified.registered:
                raise URLHandlerError(verified.error or verified.message)
            self.setup_store.save_url_handlers(True, old_defaults)
            return self.check()
        except Exception as error:
            rollback_errors = self._restore_defaults(old_defaults)
            try:
                self.setup_store.save_url_handlers(False, old_defaults if rollback_errors else {})
            except SetupSettingsError as save_error:
                rollback_errors.append(str(save_error))
            if "created" in locals() and created:
                self._remove_owned_desktop(contents)
            detail = str(error)
            if rollback_errors:
                detail += " Rollback was incomplete: " + "; ".join(rollback_errors)
            raise URLHandlerError(detail) from error

    def disable(self) -> URLHandlerStatus:
        state = self.setup_store.load()
        previous = state.previous_url_defaults or {}
        errors = self._restore_defaults(previous)
        try:
            self._remove_own_associations()
        except (OSError, URLHandlerError) as error:
            errors.append(str(error))
        try:
            try:
                expected = self._desktop_contents()
            except URLHandlerError:
                expected = None
            self._remove_owned_desktop(expected)
        except (OSError, URLHandlerError) as error:
            errors.append(str(error))
        if errors:
            raise URLHandlerError("Could not fully disable URL handling: " + "; ".join(errors))
        self.setup_store.save_url_handlers(False, {})
        return self.check()

    def _restore_defaults(self, previous: dict[str, str | None]) -> list[str]:
        errors: list[str] = []
        for mime_type in (_SCHEME_MIME[scheme] for scheme in SUPPORTED_SCHEMES):
            try:
                if self._query_default(mime_type) != DESKTOP_ID:
                    continue
                old_default = previous.get(mime_type)
                if old_default:
                    self._set_default(old_default, mime_type)
                else:
                    self._remove_own_default(mime_type)
                    if self._query_default(mime_type) == DESKTOP_ID:
                        raise URLHandlerError(f"Could not clear Roseblox as the {mime_type} default.")
            except URLHandlerError as error:
                errors.append(str(error))
        return errors

    def _remove_own_default(self, mime_type: str) -> None:
        self._remove_mime_app_id(self.mimeapps_path, mime_type)

    def _remove_own_associations(self) -> None:
        for scheme in SUPPORTED_SCHEMES:
            self._remove_mime_app_id(self.mimeapps_path, _SCHEME_MIME[scheme])

    @staticmethod
    def _remove_mime_app_id(path: Path, mime_type: str) -> None:
        if not path.exists():
            return
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        section = ""
        result: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped[1:-1]
                result.append(line)
                continue
            match = re.match(r"^(\s*)([^=#\s]+)(\s*=\s*)(.*?)(\r?\n)?$", line)
            if section not in {"Default Applications", "Added Associations"} or not match:
                result.append(line)
                continue
            if match.group(2) != mime_type:
                result.append(line)
                continue
            ids = [item for item in match.group(4).split(";") if item and item != DESKTOP_ID]
            if ids:
                ending = match.group(5) or ""
                result.append(match.group(1) + match.group(2) + match.group(3) + ";".join(ids) + ";" + ending)
        temporary_path: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, delete=False
            ) as stream:
                temporary_path = Path(stream.name)
                stream.writelines(result)
            os.replace(temporary_path, path)
        except OSError as error:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise URLHandlerError(f"Could not update MIME associations: {error}") from error

    def _remove_owned_desktop(self, expected: str | None) -> None:
        try:
            contents = self.desktop_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except (OSError, UnicodeError) as error:
            raise URLHandlerError(f"Could not inspect Roseblox URL-handler file: {error}") from error
        if expected is not None and contents != expected:
            raise URLHandlerError("The URL-handler desktop entry was changed outside Roseblox; it was left in place.")
        if expected is None and (_DESKTOP_MARKER not in contents or "Name=Roseblox Roblox URL Handler" not in contents):
            raise URLHandlerError("The URL-handler desktop entry cannot be verified as Roseblox-owned; it was left in place.")
        try:
            self.desktop_path.unlink()
        except OSError as error:
            raise URLHandlerError(f"Could not remove Roseblox's URL-handler entry: {error}") from error
