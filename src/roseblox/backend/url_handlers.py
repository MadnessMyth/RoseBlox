"""Per-user Roblox URL handler registration and safe URI routing."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from typing import Callable

from . import appimage
from .setup import SetupSettingsError, SetupStore
from .system import install_hint


SCHEME_TO_APP = {
    "roblox": "player",
    "roblox-player": "player",
    "roblox-studio": "studio",
    "roblox-studio-auth": "studio",
}
SUPPORTED_SCHEMES = tuple(SCHEME_TO_APP)
DESKTOP_ID = "io.github.roseblox.Roseblox-url-handler.desktop"
# The packaged app entry also declares the schemes so "Open with" dialogs list Roseblox,
# but only the managed handler entry above is ever made the default.
APP_DESKTOP_ID = "io.github.roseblox.Roseblox.desktop"
_OWN_DESKTOP_IDS = frozenset({DESKTOP_ID, APP_DESKTOP_ID})
_SCHEME_MIME = {scheme: f"x-scheme-handler/{scheme}" for scheme in SUPPORTED_SCHEMES}
_DESKTOP_MARKER = "X-Roseblox-Managed=true"
_CACHE_REFRESH_TIMEOUT = 60


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


def _default_data_dirs() -> list[Path]:
    value = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    return [Path(item) for item in value.split(":") if item]


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
        executable_lookup: Callable[[str], str | None] = appimage.which,
        command_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        data_dirs: list[Path] | None = None,
    ) -> None:
        self.setup_store = setup_store or SetupStore()
        self.data_home = Path(data_home) if data_home is not None else _default_data_home()
        self.config_home = Path(config_home) if config_home is not None else _default_config_home()
        self.executable_lookup = executable_lookup
        self.command_runner = command_runner
        self.data_dirs = list(data_dirs) if data_dirs is not None else _default_data_dirs()

    def _app_entry_installed(self) -> bool:
        return any(
            (directory / "applications" / APP_DESKTOP_ID).is_file()
            for directory in (self.data_home, *self.data_dirs)
        )

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
            "Name=Roseblox\n"
            "Comment=Open Roblox links with Roseblox\n"
            "Icon=io.github.roseblox.Roseblox\n"
            "NoDisplay=true\n"
            f"Exec={_desktop_quote(executable)} --roblox-url %u\n"
            "Terminal=false\n"
            f"MimeType={mime_types}\n"
            f"{_DESKTOP_MARKER}\n"
        )

    @staticmethod
    def _is_owned_entry(contents: str) -> bool:
        return _DESKTOP_MARKER in contents.splitlines()

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
            if not self._is_owned_entry(existing):
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
                "The `xdg-mime` command is unavailable. "
                f"{install_hint('xdg-mime', 'Install your desktop environment’s XDG utilities.')} Then retry."
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
                if current == DESKTOP_ID:
                    current = (state.previous_url_defaults or {}).get(mime_type)
                old_defaults[mime_type] = None if current in _OWN_DESKTOP_IDS else current
            contents = self._desktop_contents()
            self.setup_store.save_url_handlers(False, old_defaults)
            created = self._write_owned_desktop(contents)
            for scheme in SUPPORTED_SCHEMES:
                self._set_default(DESKTOP_ID, _SCHEME_MIME[scheme])
            if self._app_entry_installed():
                # The handler entry is hidden, so this is what lists Roseblox in "Open with"
                # dialogs; the app entry itself declares no schemes, so it never becomes
                # an implicit default while URL handling is off.
                self._add_app_associations()
            self._refresh_desktop_caches()
            verified = self.check()
            if not verified.registered:
                raise URLHandlerError(verified.error or verified.message)
            self.setup_store.save_url_handlers(True, old_defaults)
            return self.check()
        except Exception as error:
            rollback_errors = self._restore_defaults(old_defaults)
            try:
                self._remove_mime_app_ids(self.mimeapps_path, {APP_DESKTOP_ID})
            except URLHandlerError as cleanup_error:
                rollback_errors.append(str(cleanup_error))
            if "created" in locals() and created:
                self._remove_owned_desktop(contents)
            self._refresh_desktop_caches()
            rollback_errors += self._verify_not_default()
            try:
                self.setup_store.save_url_handlers(False, old_defaults if rollback_errors else {})
            except SetupSettingsError as save_error:
                rollback_errors.append(str(save_error))
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
        self._refresh_desktop_caches()
        if not errors:
            errors = self._verify_not_default()
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
                if old_default and old_default not in _OWN_DESKTOP_IDS:
                    self._set_default(old_default, mime_type)
                else:
                    self._remove_own_default(mime_type)
            except URLHandlerError as error:
                errors.append(str(error))
        return errors

    def _refresh_desktop_caches(self) -> None:
        """Rebuild the caches some desktops read instead of the files themselves.

        KDE resolves handlers and mimeapps.list through KSycoca, and the generic
        xdg-mime lookup trusts mimeinfo.cache, so a stale cache can keep sending links
        to the old handler (or show a chooser without Roseblox). Best effort only.
        """
        commands: list[list[str]] = []
        applications = self.desktop_path.parent
        tool = self.executable_lookup("update-desktop-database")
        if tool and applications.is_dir():
            commands.append([tool, "-q", str(applications)])
        for name in ("kbuildsycoca6", "kbuildsycoca5"):
            tool = self.executable_lookup(name)
            if tool:
                commands.append([tool])
                break
        for command in commands:
            try:
                self.command_runner(
                    command, check=False, capture_output=True, text=True, timeout=_CACHE_REFRESH_TIMEOUT
                )
            except (OSError, subprocess.TimeoutExpired):
                pass

    def _verify_not_default(self) -> list[str]:
        """Confirm Roseblox no longer handles any scheme.

        Run only after the desktop entry is gone: xdg-mime and GIO fall back to any
        installed entry declaring the MIME type, so while it exists the query would
        still report Roseblox even with every explicit association removed.
        """
        errors: list[str] = []
        for mime_type in (_SCHEME_MIME[scheme] for scheme in SUPPORTED_SCHEMES):
            try:
                if self._query_default(mime_type) == DESKTOP_ID:
                    errors.append(f"Could not clear Roseblox as the {mime_type} default.")
            except URLHandlerError as error:
                errors.append(str(error))
        return errors

    def _remove_own_default(self, mime_type: str) -> None:
        self._remove_mime_app_ids(self.mimeapps_path, {DESKTOP_ID}, {mime_type})

    def _remove_own_associations(self) -> None:
        self._remove_mime_app_ids(self.mimeapps_path, _OWN_DESKTOP_IDS)

    def _add_app_associations(self) -> None:
        path = self.mimeapps_path
        try:
            lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        except (OSError, UnicodeError) as error:
            raise URLHandlerError(f"Could not read MIME associations: {error}") from error
        pending = [_SCHEME_MIME[scheme] for scheme in SUPPORTED_SCHEMES]
        result: list[str] = []
        section = ""

        def add_pending() -> None:
            result.extend(f"{mime_type}={APP_DESKTOP_ID};" for mime_type in pending)
            pending.clear()

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                if section == "Added Associations":
                    add_pending()
                section = stripped[1:-1]
            elif section == "Added Associations":
                key, separator, value = line.partition("=")
                if separator and key.strip() in pending:
                    ids = [item for item in value.strip().split(";") if item]
                    if APP_DESKTOP_ID not in ids:
                        ids.append(APP_DESKTOP_ID)
                    line = f"{key.strip()}={';'.join(ids)};"
                    pending.remove(key.strip())
            result.append(line)
        if section == "Added Associations":
            add_pending()
        if pending:
            if result and result[-1].strip():
                result.append("")
            result.append("[Added Associations]")
            add_pending()
        self._write_mimeapps(path, "\n".join(result) + "\n")

    @classmethod
    def _remove_mime_app_ids(cls, path: Path, ids: set[str] | frozenset[str], mime_types: set[str] | None = None) -> None:
        if not path.exists():
            return
        try:
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        except (OSError, UnicodeError) as error:
            raise URLHandlerError(f"Could not read MIME associations: {error}") from error
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
            if match.group(2) not in (mime_types if mime_types is not None else _SCHEME_MIME.values()):
                result.append(line)
                continue
            kept = [item for item in match.group(4).split(";") if item and item not in ids]
            if kept:
                ending = match.group(5) or ""
                result.append(match.group(1) + match.group(2) + match.group(3) + ";".join(kept) + ";" + ending)
        cls._write_mimeapps(path, "".join(result))

    @staticmethod
    def _write_mimeapps(path: Path, text: str) -> None:
        temporary_path: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, delete=False
            ) as stream:
                temporary_path = Path(stream.name)
                stream.write(text)
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
        if contents != expected and not self._is_owned_entry(contents):
            raise URLHandlerError("The URL-handler desktop entry cannot be verified as Roseblox-owned; it was left in place.")
        try:
            self.desktop_path.unlink()
        except OSError as error:
            raise URLHandlerError(f"Could not remove Roseblox's URL-handler entry: {error}") from error
