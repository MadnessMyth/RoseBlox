"""Validated, per-application version endpoint settings."""

from __future__ import annotations

import json
import ipaddress
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .versions import VERSION_ENDPOINTS

_SETTINGS_LOCK = threading.RLock()


class VersionSettingsError(RuntimeError):
    """Raised when version source settings cannot safely be read or written."""


@dataclass(frozen=True)
class VersionSource:
    endpoint: str
    json_pointer: str = "/clientVersionUpload"


def default_version_settings_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config_home / "roseblox" / "version_sources.json"


def default_version_source(app_type: str) -> VersionSource:
    endpoint = VERSION_ENDPOINTS.get(app_type)
    if endpoint is None:
        raise ValueError(f"Unsupported application type: {app_type}")
    return VersionSource(endpoint)


def validate_endpoint(endpoint: object) -> str:
    if not isinstance(endpoint, str) or not endpoint:
        raise VersionSettingsError("Endpoint must be a non-empty HTTPS URL.")
    if any(character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F for character in endpoint):
        raise VersionSettingsError("Endpoint URL cannot contain whitespace or control characters; percent-encode path or query values.")
    if "\\" in endpoint:
        raise VersionSettingsError("Endpoint URL cannot contain backslashes.")
    if re.search(r"%(?![0-9A-Fa-f]{2})", endpoint):
        raise VersionSettingsError("Endpoint URL contains an invalid percent escape.")
    try:
        parsed = urlsplit(endpoint)
        if parsed.scheme.lower() != "https":
            raise VersionSettingsError("Endpoint must use HTTPS.")
        if not parsed.netloc or not parsed.hostname:
            raise VersionSettingsError("Endpoint must include a valid hostname.")
        if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
            raise VersionSettingsError("Endpoint cannot contain URL credentials.")
        if parsed.fragment or "#" in endpoint:
            raise VersionSettingsError("Endpoint cannot contain a fragment.")
        if parsed.port not in (None, 443):
            raise VersionSettingsError("Endpoint must use the default HTTPS port (443).")
        hostname = parsed.hostname
        authority = parsed.netloc
        if ":" in hostname:
            if not authority.startswith("[") or "]" not in authority:
                raise VersionSettingsError("IPv6 endpoint hosts must be enclosed in brackets.")
            try:
                ipaddress.IPv6Address(hostname)
            except ValueError as error:
                raise VersionSettingsError("Endpoint contains an invalid IPv6 address.") from error
            suffix = authority[authority.find("]") + 1 :]
            if suffix:
                if not suffix.startswith(":") or not suffix[1:].isdigit() or int(suffix[1:]) != 443:
                    raise VersionSettingsError("Endpoint has a malformed or unsupported port.")
        else:
            if authority.count(":") > 1:
                raise VersionSettingsError("IPv6 endpoint hosts must be enclosed in brackets.")
            host_text, separator, port_text = authority.partition(":")
            if separator and (not port_text or not port_text.isdigit() or int(port_text) != 443):
                raise VersionSettingsError("Endpoint has a malformed or unsupported port.")
            if host_text.casefold() != hostname.casefold():
                raise VersionSettingsError("Endpoint contains a malformed hostname authority.")
            _validate_dns_or_ipv4_hostname(hostname)
        # urllib accepts some malformed characters and may fail later in its worker.
        # Require URI path/query syntax; IDNs in the authority are checked via IDNA above.
        if not re.fullmatch(r"[A-Za-z0-9._~!$&'()*+,;=:@%/-]*", parsed.path):
            raise VersionSettingsError("Endpoint path contains characters that must be percent-encoded.")
        if not re.fullmatch(r"[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*", parsed.query):
            raise VersionSettingsError("Endpoint query contains characters that must be percent-encoded.")
        if any(ord(character) > 0x7E for character in parsed.path + parsed.query):
            raise VersionSettingsError("Non-ASCII path or query characters must be percent-encoded.")
    except VersionSettingsError:
        raise
    except (ValueError, UnicodeError) as error:
        raise VersionSettingsError("Endpoint must be a syntactically valid HTTPS URL.") from error
    return endpoint


def _validate_dns_or_ipv4_hostname(hostname: str) -> None:
    if not hostname or hostname.startswith(".") or ".." in hostname:
        raise VersionSettingsError("Endpoint must include a valid hostname.")
    candidate = hostname[:-1] if hostname.endswith(".") else hostname
    if not candidate:
        raise VersionSettingsError("Endpoint must include a valid hostname.")
    try:
        ipaddress.IPv4Address(candidate)
        return
    except ValueError:
        pass
    if re.fullmatch(r"[0-9.]+", candidate):
        raise VersionSettingsError("Endpoint contains an invalid IPv4 address.")
    try:
        ascii_hostname = candidate.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise VersionSettingsError("Endpoint contains an invalid hostname.") from error
    if len(ascii_hostname) > 253:
        raise VersionSettingsError("Endpoint hostname is too long.")
    labels = ascii_hostname.split(".")
    if any(
        not label
        or len(label) > 63
        or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
        for label in labels
    ):
        raise VersionSettingsError("Endpoint contains an invalid hostname.")


def validate_json_pointer(pointer: object) -> str:
    if not isinstance(pointer, str):
        raise VersionSettingsError("JSON field path must be a string.")
    if pointer == "":
        return pointer
    if not pointer.startswith("/"):
        raise VersionSettingsError("JSON field path must be a JSON Pointer (for example /data/0/version).")
    for token in pointer.split("/")[1:]:
        index = 0
        while index < len(token):
            if token[index] == "~":
                if index + 1 >= len(token) or token[index + 1] not in "01":
                    raise VersionSettingsError("JSON Pointer contains an invalid escape; use ~0 or ~1.")
                index += 2
            else:
                index += 1
    return pointer


def parse_json_pointer(document: object, pointer: str) -> object:
    """Resolve an RFC 6901 JSON Pointer, including numeric array indexes."""
    validate_json_pointer(pointer)
    if pointer == "":
        return document
    current = document
    for raw_token in pointer.split("/")[1:]:
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                raise VersionSettingsError(f"JSON field path was not found at '{token}'.")
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
                raise VersionSettingsError(f"'{token}' is not a valid JSON array index.")
            index = int(token)
            if index >= len(current):
                raise VersionSettingsError(f"JSON array index {index} is out of range.")
            current = current[index]
        else:
            raise VersionSettingsError(f"Cannot continue JSON field path through a {type(current).__name__} value.")
    return current


class VersionSettingsStore:
    """Persist per-app version sources separately from Rosemary settings and cache."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_version_settings_path()

    def load(self) -> dict[str, VersionSource]:
        settings, errors = self.load_independent()
        if errors:
            app_type, message = next(iter(errors.items()))
            raise VersionSettingsError(f"The {app_type} version source is invalid: {message}")
        return settings

    def load_independent(self) -> tuple[dict[str, VersionSource], dict[str, str]]:
        """Load each application independently, retaining errors for damaged app records."""
        document = self._read_document()
        settings: dict[str, VersionSource] = {}
        errors: dict[str, str] = {}
        applications = document["applications"]
        for app_type in VERSION_ENDPOINTS:
            if app_type not in applications:
                settings[app_type] = default_version_source(app_type)
                continue
            source_value = applications[app_type]
            if not isinstance(source_value, dict):
                errors[app_type] = "saved record must be a JSON object"
                continue
            try:
                settings[app_type] = self.validate_source(
                    VersionSource(source_value.get("endpoint"), source_value.get("json_pointer"))
                )
            except (VersionSettingsError, TypeError, ValueError) as error:
                errors[app_type] = str(error)
        return settings, errors

    def load_for(self, app_type: str) -> VersionSource:
        if app_type not in VERSION_ENDPOINTS:
            raise ValueError(f"Unsupported application type: {app_type}")
        settings, errors = self.load_independent()
        if app_type in errors:
            raise VersionSettingsError(f"The {app_type} version source is invalid: {errors[app_type]}")
        return settings[app_type]

    def _read_document(self) -> dict:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "applications": {}}
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise VersionSettingsError(f"Could not read version source settings: {error}") from error
        if not isinstance(document, dict) or document.get("version") != 1 or not isinstance(document.get("applications"), dict):
            raise VersionSettingsError("Version source settings have an invalid format; the file was not changed.")
        return document

    @staticmethod
    def validate_source(source: VersionSource) -> VersionSource:
        return VersionSource(
            validate_endpoint(source.endpoint),
            validate_json_pointer(source.json_pointer),
        )

    def save(self, app_type: str, source: VersionSource) -> None:
        if app_type not in VERSION_ENDPOINTS:
            raise ValueError(f"Unsupported application type: {app_type}")
        validated = self.validate_source(source)
        with _SETTINGS_LOCK:
            document = self._read_document()
            applications = document["applications"]
            applications[app_type] = {
                "endpoint": validated.endpoint,
                "json_pointer": validated.json_pointer,
            }
            self._save_document(document)

    def reset(self, app_type: str) -> None:
        self.save(app_type, default_version_source(app_type))

    def _save_document(self, document: dict) -> None:
        temporary_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as stream:
                temporary_path = Path(stream.name)
                json.dump(
                    document,
                    stream,
                    indent=2,
                )
                stream.write("\n")
            os.replace(temporary_path, self.path)
        except OSError as error:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise VersionSettingsError(f"Could not save version source settings: {error}") from error
