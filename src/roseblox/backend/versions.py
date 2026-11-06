"""Roblox macOS Player and Studio version discovery and cache."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from .version_settings import VersionSource


VERSION_ENDPOINTS = {
    "player": "https://clientsettingscdn.roblox.com/v2/client-version/MacPlayer",
    "studio": "https://clientsettingscdn.roblox.com/v2/client-version/MacStudio",
}
ARCHIVE_PATTERNS = {
    "player": "https://setup.rbxcdn.com/mac/{version}-RobloxPlayer.zip",
    "studio": "https://setup.rbxcdn.com/mac/{version}-RobloxStudioApp.zip",
}
VERSION_RE = re.compile(r"version-[0-9a-f]{16}\Z", re.IGNORECASE)
MAX_VERSION_RESPONSE_BYTES = 1024 * 1024
_CACHE_LOCK = threading.RLock()


class VersionLookupError(RuntimeError):
    """Raised when an endpoint response does not contain a valid Roblox version."""


class VersionCacheError(RuntimeError):
    """Raised when cached version data cannot safely be read or written."""


@dataclass(frozen=True)
class VersionInfo:
    version: str
    checked_at: str


@dataclass(frozen=True)
class VersionTestResult:
    connection_status: str
    http_status: int | None
    json_result: str
    extracted_version: str | None
    validation_result: str
    error: str | None = None

    @property
    def valid(self) -> bool:
        return self.validation_result == "Valid"


def validate_version(version: object) -> str:
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise VersionLookupError("Roblox returned an invalid clientVersionUpload value.")
    return version


def archive_url(app_type: str, version: str) -> str:
    pattern = ARCHIVE_PATTERNS.get(app_type)
    if pattern is None:
        raise ValueError(f"Unsupported application type: {app_type}")
    return pattern.format(version=validate_version(version))


class VersionClient:
    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout

    def test_configuration(self, source: VersionSource) -> VersionTestResult:
        from .version_settings import (
            parse_json_pointer,
            VersionSettingsStore,
        )

        try:
            source = VersionSettingsStore.validate_source(source)
            request = Request(source.endpoint, headers={"Accept": "application/json"})
        except Exception as error:
            return VersionTestResult(
                "Not connected", None, "Not attempted", None, "Not attempted",
                f"Invalid version source: {_safe_error(error)}",
            )
        http_status = None
        try:
            with urlopen(request, timeout=self.timeout) as response:
                http_status = getattr(response, "status", None) or getattr(response, "getcode", lambda: 200)()
                body = response.read(MAX_VERSION_RESPONSE_BYTES + 1)
                if len(body) > MAX_VERSION_RESPONSE_BYTES:
                    return VersionTestResult(
                        "Connected", http_status, "Rejected: response exceeds 1 MiB", None,
                        "Not attempted", "Version response exceeded the 1 MiB limit.",
                    )
        except HTTPError as error:
            error.close()
            return VersionTestResult(
                "Connected", error.code, "Not attempted", None, "Not attempted",
                f"Version endpoint returned HTTP {error.code}.",
            )
        except (URLError, TimeoutError, OSError) as error:
            return VersionTestResult(
                "Failed", None, "Not attempted", None, "Not attempted",
                f"Could not reach version endpoint: {_safe_error(error)}",
            )
        except Exception as error:
            return VersionTestResult(
                "Failed", None, "Not completed", None, "Not completed",
                f"Unexpected request failure: {_safe_error(error)}",
            )
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception as error:
            return VersionTestResult(
                "Connected", http_status, f"Failed: {_safe_error(error)}", None, "Not attempted",
                f"Endpoint response could not be parsed as UTF-8 JSON: {_safe_error(error)}",
            )

        try:
            raw_version = parse_json_pointer(payload, source.json_pointer)
        except Exception as error:
            return VersionTestResult(
                "Connected", http_status, "Valid JSON", None, "Not attempted",
                f"Could not extract the configured JSON value: {_safe_error(error)}",
            )
        try:
            version = validate_version(raw_version)
        except VersionLookupError as error:
            if isinstance(raw_version, str):
                extracted = repr(raw_version[:160] + ("…" if len(raw_version) > 160 else ""))
            else:
                extracted = f"<{type(raw_version).__name__} value>"
            return VersionTestResult(
                "Connected", http_status, "Valid JSON", extracted,
                "Invalid", _safe_error(error),
            )
        return VersionTestResult("Connected", http_status, "Valid JSON", version, "Valid")

    def fetch(self, app_type: str, source: VersionSource | None = None) -> VersionInfo:
        from .version_settings import default_version_source

        if app_type not in VERSION_ENDPOINTS:
            raise ValueError(f"Unsupported application type: {app_type}")
        selected_source = source or default_version_source(app_type)
        result = self.test_configuration(selected_source)
        if not result.valid or result.extracted_version is None:
            raise VersionLookupError(result.error or "Version source configuration did not produce a valid Roblox version.")
        version = result.extracted_version
        return VersionInfo(version, datetime.now(timezone.utc).isoformat())


def _safe_error(error: BaseException) -> str:
    message = str(error) or type(error).__name__
    return message[:500] + ("…" if len(message) > 500 else "")


def default_version_cache_path() -> Path:
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return data_home / "roseblox" / "versions.json"


def _parse_cache_record(value: Any) -> VersionInfo:
    if not isinstance(value, dict):
        raise ValueError("cached version entry must be an object")
    version = validate_version(value.get("version"))
    checked_at = value.get("checked_at")
    if not isinstance(checked_at, str) or not checked_at:
        raise ValueError("cached version entry has no check timestamp")
    return VersionInfo(version, checked_at)


class VersionCache:
    """Separate XDG data cache for last successful checks, independent of library.json."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_version_cache_path()

    def load(self) -> dict[str, VersionInfo]:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise VersionCacheError(f"Could not read the version cache: {error}") from error
        if not isinstance(document, dict) or not isinstance(document.get("applications"), dict):
            raise VersionCacheError("The version cache has an invalid format.")
        try:
            return {
                app_type: _parse_cache_record(value)
                for app_type, value in document["applications"].items()
                if app_type in VERSION_ENDPOINTS
            }
        except (ValueError, TypeError, KeyError) as error:
            raise VersionCacheError(f"The version cache contains invalid data: {error}") from error

    def update(self, app_type: str, info: VersionInfo) -> None:
        if app_type not in VERSION_ENDPOINTS:
            raise ValueError(f"Unsupported application type: {app_type}")
        validate_version(info.version)
        with _CACHE_LOCK:
            applications = self.load()
            applications[app_type] = info
            self._save(applications)

    def _save(self, applications: dict[str, VersionInfo]) -> None:
        temporary_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, delete=False
            ) as stream:
                temporary_path = Path(stream.name)
                json.dump(
                    {
                        "version": 1,
                        "applications": {
                            app_type: {
                                "version": info.version,
                                "checked_at": info.checked_at,
                            }
                            for app_type, info in applications.items()
                        },
                    },
                    stream,
                    indent=2,
                )
                stream.write("\n")
            os.replace(temporary_path, self.path)
        except OSError as error:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise VersionCacheError(f"Could not save the version cache: {error}") from error
