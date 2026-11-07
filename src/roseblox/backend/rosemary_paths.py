"""Where Rosemary keeps a prefix, the guest's home and its caches, and what Roblox saves there."""

from __future__ import annotations

import os
import pwd
import xml.etree.ElementTree as ElementTree
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

OFF_VALUES = frozenset({"", "off", "0"})
ROBLOX_CACHE_PREFIX = "com.roblox."
ROBLOX_SETTINGS_NAME = "GlobalBasicSettings_13.xml"
_MAX_SETTINGS_BYTES = 4 * 1024 * 1024


class CacheError(RuntimeError):
    """Raised when a cache cannot be moved to the trash safely."""


def _home(env: Mapping[str, str]) -> Path:
    return Path(env.get("HOME") or Path.home())


def prefix_directory(env: Mapping[str, str]) -> Path | None:
    """The prefix Rosemary runs guests in, or None when prefixes are turned off."""
    value = env.get("ROSEMARY_PREFIX")
    if value is not None:
        return None if value in OFF_VALUES else Path(value).expanduser()
    data_home = Path(env.get("XDG_DATA_HOME") or _home(env) / ".local" / "share")
    return data_home / "rosemary" / "prefixes" / "default"


def login_name() -> str:
    try:
        name = pwd.getpwuid(os.geteuid()).pw_name
        if name:
            return name
    except KeyError:
        pass
    return os.environ.get("USER") or "user"


def guest_home(env: Mapping[str, str]) -> Path | None:
    """The host directory behind the guest's ``/Users/<name>``."""
    if env.get("ROSEMARY_PREFIX_HOME") == "host":
        return _home(env)
    prefix = prefix_directory(env)
    return None if prefix is None else prefix / "root" / "Users" / login_name()


def shader_cache_directory(env: Mapping[str, str]) -> Path | None:
    value = env.get("ROSEMARY_SHADER_CACHE")
    if value is not None:
        return None if value in OFF_VALUES else Path(value).expanduser()
    cache_home = Path(env.get("XDG_CACHE_HOME") or _home(env) / ".cache")
    return cache_home / "rosemary" / "metal-shaders"


def roblox_settings_path(env: Mapping[str, str]) -> Path | None:
    home = guest_home(env)
    return None if home is None else home / "Library" / "Roblox" / ROBLOX_SETTINGS_NAME


def read_framerate_cap(path: Path | None) -> int | None:
    """Return the ``FramerateCap`` Roblox saved, or None when it has saved none."""
    if path is None:
        return None
    try:
        if path.stat().st_size > _MAX_SETTINGS_BYTES:
            return None
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError):
        return None
    for element in root.iter():
        if element.get("name") == "FramerateCap":
            try:
                return int((element.text or "").strip())
            except ValueError:
                return None
    return None


@dataclass(frozen=True)
class CacheEntry:
    title: str
    description: str
    path: Path
    size: int
    files: int


def directory_size(path: Path) -> tuple[int, int]:
    """Total bytes and file count under ``path``, never following symlinks."""
    total = files = 0
    for directory, _subdirectories, names in os.walk(path, followlinks=False):
        for name in names:
            try:
                status = os.lstat(os.path.join(directory, name))
            except OSError:
                continue
            total += status.st_size
            files += 1
    return total, files


def cache_entries(env: Mapping[str, str]) -> list[CacheEntry]:
    """The caches that are safe to clear: Rosemary's shader cache and Roblox's own caches.

    Only directories that already exist are listed. Login data, cookies and
    preferences live outside ``Library/Caches`` and are never offered.
    """
    entries: list[CacheEntry] = []
    shaders = shader_cache_directory(env)
    if shaders is not None and shaders.is_dir() and not shaders.is_symlink():
        size, files = directory_size(shaders)
        entries.append(
            CacheEntry(
                "Rosemary shader cache",
                "Translated Metal shaders and Vulkan pipelines. Rebuilt on the next launch, "
                "which may stutter briefly while it does.",
                shaders,
                size,
                files,
            )
        )
    home = guest_home(env)
    caches = home / "Library" / "Caches" if home is not None else None
    if caches is not None and caches.is_dir():
        for path in sorted(caches.iterdir()):
            if path.name.startswith(ROBLOX_CACHE_PREFIX) and path.is_dir() and not path.is_symlink():
                size, files = directory_size(path)
                entries.append(
                    CacheEntry(
                        f"Roblox cache ({path.name})",
                        "Downloaded assets and web content that Roblox fetches again when needed.",
                        path,
                        size,
                        files,
                    )
                )
    return entries


def trash_cache(path: Path, env: Mapping[str, str]) -> None:
    """Move one of :func:`cache_entries`' directories to the trash, so it can be restored."""
    allowed = {entry.path for entry in cache_entries(env)}
    if path not in allowed:
        raise CacheError(f"{path} is not one of the caches Roseblox can clear.")
    try:
        Gio.File.new_for_path(str(path)).trash(None)
    except GLib.Error as error:
        raise CacheError(f"Could not move {path} to the trash: {error.message}") from error
