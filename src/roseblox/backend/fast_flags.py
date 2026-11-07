"""FastFlag validation and the ``ClientAppSettings.json`` file inside a Roblox bundle."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path

FLAG_FILE_RELATIVE_PATH = Path("Contents") / "MacOS" / "ClientSettings" / "ClientAppSettings.json"
_MARKER_NAME = ".roseblox-managed"

_FLAG_NAME = re.compile(r"^(?:[DS]?F)(Flag|Int|String|Log)[A-Za-z0-9_]+$")
_INT32_MIN = -(2**31)
_INT32_MAX = 2**31 - 1
MAX_STRING_LENGTH = 4096
MAX_FLAGS = 500


class FastFlagError(ValueError):
    """Raised when a FastFlag or the bundle's flag file cannot be used safely."""


def flag_kind(name: str) -> str | None:
    """Return ``bool``, ``int`` or ``string`` from a flag's Roblox prefix, or None."""
    match = _FLAG_NAME.match(name) if isinstance(name, str) else None
    if match is None:
        return None
    return {"Flag": "bool", "Int": "int", "Log": "int", "String": "string"}[match.group(1)]


def validate_flag_name(name: object) -> str | None:
    if not isinstance(name, str) or not name:
        return "Flag names cannot be empty."
    if flag_kind(name) is None:
        return (
            f"{name!r} is not a FastFlag name. Names start with FFlag, FInt, FString or FLog, "
            "optionally prefixed with D or S."
        )
    return None


def normalize_flag_value(name: str, value: object) -> str:
    """Return the value in the string form Roblox reads, or raise FastFlagError."""
    problem = validate_flag_name(name)
    if problem:
        raise FastFlagError(problem)
    kind = flag_kind(name)
    if kind == "bool":
        if isinstance(value, bool):
            return "True" if value else "False"
        text = str(value).strip().lower() if isinstance(value, (str, int)) else ""
        if text in {"true", "1"}:
            return "True"
        if text in {"false", "0"}:
            return "False"
        raise FastFlagError(f"{name} needs True or False.")
    if kind == "int":
        if isinstance(value, bool):
            raise FastFlagError(f"{name} needs a whole number.")
        try:
            number = int(str(value).strip()) if not isinstance(value, int) else value
        except ValueError:
            raise FastFlagError(f"{name} needs a whole number.") from None
        if not _INT32_MIN <= number <= _INT32_MAX:
            raise FastFlagError(f"{name} must be between {_INT32_MIN} and {_INT32_MAX}.")
        return str(number)
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise FastFlagError(f"{name} needs a text value.")
    text = str(value)
    if "\0" in text:
        raise FastFlagError(f"{name} cannot contain NUL characters.")
    if len(text) > MAX_STRING_LENGTH:
        raise FastFlagError(f"{name} is longer than {MAX_STRING_LENGTH} characters.")
    return text


def normalize_flags(flags: Mapping[str, object]) -> dict[str, str]:
    if not isinstance(flags, Mapping):
        raise FastFlagError("FastFlags must be a JSON object of names and values.")
    if len(flags) > MAX_FLAGS:
        raise FastFlagError(f"Roseblox keeps at most {MAX_FLAGS} FastFlags per application.")
    return {name: normalize_flag_value(name, value) for name, value in flags.items()}


def parse_flag_document(text: str) -> dict[str, str]:
    """Parse imported or existing ``ClientAppSettings.json`` text into validated flags."""
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise FastFlagError(f"The file is not valid JSON: {error}") from error
    if not isinstance(document, dict):
        raise FastFlagError("The file must contain a JSON object of FastFlag names and values.")
    return normalize_flags(document)


def format_flag_document(flags: Mapping[str, str]) -> str:
    return json.dumps(dict(sorted(flags.items())), indent=2) + "\n"


def flag_file_path(bundle_path: str | Path) -> Path:
    return Path(bundle_path) / FLAG_FILE_RELATIVE_PATH


def read_bundle_flags(bundle_path: str | Path) -> dict[str, str] | None:
    """Return the flags already in a bundle, or None when it has no flag file."""
    path = flag_file_path(bundle_path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as error:
        raise FastFlagError(f"Could not read {path}: {error}") from error
    return parse_flag_document(text)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_bundle_flags(
    bundle_path: str | Path,
    flags: Mapping[str, str],
    backup_directory: Path,
) -> Path | None:
    """Make the bundle's flag file match ``flags``.

    Roseblox records a digest of what it writes. A flag file that does not match it
    was made by someone else, so it is copied to ``backup_directory`` before being
    replaced or left alone; an empty configuration never deletes a file Roseblox did
    not write. Returns the backup path when one was made.
    """
    path = flag_file_path(bundle_path)
    marker = path.with_name(_MARKER_NAME)
    try:
        existing = path.read_bytes()
    except FileNotFoundError:
        existing = None
    except OSError as error:
        raise FastFlagError(f"Could not read {path}: {error}") from error
    try:
        recorded = marker.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, UnicodeError):
        recorded = ""
    except OSError as error:
        raise FastFlagError(f"Could not read {marker}: {error}") from error
    managed = existing is not None and recorded == _digest(existing)

    data = format_flag_document(flags).encode("utf-8") if flags else None
    if data == existing:
        if data is not None and not managed:
            _write_atomic(marker, (_digest(data) + "\n").encode("utf-8"))
        return None
    if data is None and (existing is None or not managed):
        return None

    backup = None
    if existing is not None and not managed:
        backup = _backup_foreign_file(path, backup_directory)
    try:
        if data is None:
            path.unlink(missing_ok=True)
            marker.unlink(missing_ok=True)
        else:
            _write_atomic(path, data)
            _write_atomic(marker, (_digest(data) + "\n").encode("utf-8"))
    except OSError as error:
        raise FastFlagError(f"Could not update {path}: {error}") from error
    return backup


def _backup_foreign_file(path: Path, backup_directory: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    destination = backup_directory / f"ClientAppSettings-{stamp}-{os.urandom(3).hex()}.json"
    try:
        backup_directory.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    except OSError as error:
        raise FastFlagError(f"Could not back up the existing flag file {path}: {error}") from error
    return destination


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary_path = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, path)
    except OSError:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
