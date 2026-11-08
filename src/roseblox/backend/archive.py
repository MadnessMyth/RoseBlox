"""ZIP inspection and symlink-preserving, path-safe bsdtar extraction."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .appimage import bundled_tool
from .library import APP_BUNDLE_NAMES, BundleValidationError, validate_bundle
from .system import install_hint


class ArchiveValidationError(ValueError):
    """Raised when a Roblox archive is malformed or unsafe to extract."""


class ArchiveExtractionError(RuntimeError):
    """Raised when bsdtar is unavailable or extraction fails."""


class ArchiveTypeUndetermined(ArchiveValidationError):
    """Raised when archive contents do not identify one app type unambiguously."""


@dataclass(frozen=True)
class ArchiveInspection:
    app_types: frozenset[str]

    @property
    def detected_type(self) -> str | None:
        return next(iter(self.app_types)) if len(self.app_types) == 1 else None


def _member_path(name: str) -> PurePosixPath:
    if not name or "\x00" in name or "\\" in name:
        raise ArchiveValidationError(f"Archive contains an invalid path: {name!r}")
    if name.startswith("/") or name.startswith("//"):
        raise ArchiveValidationError(f"Archive contains an absolute path: {name!r}")
    path = PurePosixPath(name)
    if any(part in ("..", "") for part in path.parts) or (path.parts and ":" in path.parts[0]):
        raise ArchiveValidationError(f"Archive path escapes the extraction directory: {name!r}")
    if not path.parts:
        raise ArchiveValidationError(f"Archive contains an empty path: {name!r}")
    return path


def _validate_link_target(link_path: PurePosixPath, target: str) -> None:
    if not target or "\x00" in target or "\\" in target:
        raise ArchiveValidationError(f"Archive contains an invalid symbolic link at {link_path}.")
    target_path = PurePosixPath(target)
    if target_path.is_absolute():
        raise ArchiveValidationError(f"Symbolic link {link_path} has an absolute target.")
    resolved: list[str] = list(link_path.parent.parts)
    for part in target_path.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not resolved:
                raise ArchiveValidationError(f"Symbolic link {link_path} escapes the extraction directory.")
            resolved.pop()
        else:
            resolved.append(part)


def inspect_archive(archive_path: str | Path) -> ArchiveInspection:
    """Preflight ZIP member names, Unix entry types, and relative symlink targets."""
    archive_path = Path(archive_path)
    if not archive_path.is_file() or not zipfile.is_zipfile(archive_path):
        raise ArchiveValidationError(f"Not a readable ZIP archive: {archive_path}")

    member_paths: dict[str, PurePosixPath] = {}
    symlink_paths: set[str] = set()
    types: set[str] = set()
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for info in archive.infolist():
                path = _member_path(info.filename)
                normalized = path.as_posix().rstrip("/")
                if normalized in member_paths:
                    raise ArchiveValidationError(f"Archive contains a duplicate path: {info.filename!r}")
                member_paths[normalized] = path

                mode = (info.external_attr >> 16) & 0xFFFF
                entry_type = stat.S_IFMT(mode)
                if entry_type not in (0, stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK):
                    raise ArchiveValidationError(f"Archive contains an unsupported special entry: {info.filename!r}")
                if entry_type == stat.S_IFLNK:
                    symlink_paths.add(normalized)
                    with archive.open(info) as link_stream:
                        target_bytes = link_stream.read(4097)
                    if len(target_bytes) > 4096:
                        raise ArchiveValidationError(f"Symbolic link target is too long: {info.filename!r}")
                    _validate_link_target(path, os.fsdecode(target_bytes))

                for app_type, bundle_name in APP_BUNDLE_NAMES.items():
                    parts = path.parts
                    if any(
                        part == bundle_name and index + 1 < len(parts) and parts[index + 1] == "Contents"
                        for index, part in enumerate(parts)
                    ):
                        types.add(app_type)
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        if isinstance(error, ArchiveValidationError):
            raise
        raise ArchiveValidationError(f"Could not inspect ZIP archive: {error}") from error

    # Do not allow bsdtar to write a member through an archive-created symlink.
    for normalized, path in member_paths.items():
        for index in range(1, len(path.parts)):
            parent = PurePosixPath(*path.parts[:index]).as_posix()
            if parent in symlink_paths:
                raise ArchiveValidationError(
                    f"Archive entry {normalized!r} is nested under symbolic link {parent!r}."
                )

    return ArchiveInspection(frozenset(types))


def validate_extracted_symlinks(staging_directory: str | Path) -> None:
    """Ensure resolved links remain inside staging, including links that chain."""
    root = Path(staging_directory).resolve(strict=True)
    for path in root.rglob("*"):
        if not path.is_symlink():
            continue
        try:
            target = path.resolve(strict=False)
            target.relative_to(root)
        except (OSError, RuntimeError, ValueError) as error:
            raise ArchiveValidationError(f"Extracted symbolic link escapes staging: {path}") from error


def find_bundle(staging_directory: str | Path, app_type: str) -> Path:
    expected = APP_BUNDLE_NAMES.get(app_type)
    if expected is None:
        raise ArchiveValidationError(f"Unsupported application type: {app_type}")
    root = Path(staging_directory)
    candidates = [
        path
        for path in root.rglob(expected)
        if path.is_dir() and not path.is_symlink() and (path / "Contents").is_dir()
    ]
    if not candidates:
        raise ArchiveValidationError(f"Archive did not contain a valid {expected} bundle.")
    # Studio archives contain a nested executable bundle and an installer helper
    # below the real outer application. Ignore candidates enclosed by another
    # valid expected bundle, but continue to reject independent roots.
    candidate_paths = {path.absolute() for path in candidates}
    outermost = [
        path
        for path in candidates
        if not any(parent in candidate_paths for parent in path.parents)
    ]
    if len(outermost) != 1:
        raise ArchiveValidationError(f"Archive contains multiple {expected} bundles; refusing to guess.")
    try:
        return validate_bundle(outermost[0], app_type)
    except BundleValidationError as error:
        raise ArchiveValidationError(str(error)) from error


def extract_archive(
    archive_path: str | Path,
    staging_directory: str | Path,
    app_type: str,
    *,
    bsdtar_path: str | None = None,
) -> Path:
    """Extract a preflighted ZIP with bsdtar and return its validated .app path."""
    archive_path = Path(archive_path).absolute()
    staging_directory = Path(staging_directory).absolute()
    inspection = inspect_archive(archive_path)
    if inspection.app_types and app_type not in inspection.app_types:
        raise ArchiveValidationError(
            f"Archive contents do not include the expected {APP_BUNDLE_NAMES[app_type]} bundle."
        )

    executable = bsdtar_path or shutil.which("bsdtar") or bundled_tool("bsdtar")
    if not executable:
        raise ArchiveExtractionError(
            "bsdtar is required to extract Roblox archives while preserving symbolic links. "
            f"{install_hint('bsdtar', 'Install bsdtar using your system package manager.')} Then retry."
        )
    if staging_directory.exists() and any(staging_directory.iterdir()):
        raise ArchiveExtractionError(f"Extraction destination is not empty: {staging_directory}")
    staging_directory.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        [executable, "-xf", str(archive_path), "-C", str(staging_directory)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or f"bsdtar exited with status {result.returncode}."
        raise ArchiveExtractionError(f"Could not extract archive: {detail}")
    validate_extracted_symlinks(staging_directory)
    return find_bundle(staging_directory, app_type)
