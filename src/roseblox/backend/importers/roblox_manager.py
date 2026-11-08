"""Roblox Manager (github.com/Paryx-games/roblox-manager), inside a Wine prefix.

From ram_core/src/models.rs (AppConfig) and docs/guides/client-controls.md:
``%APPDATA%\\RM\\config.json`` holds ``roblox_fast_flags`` (name → text).
The rest of that file is about accounts, and ``accounts.dat`` holds the
accounts themselves; Roseblox reads neither.
"""

from __future__ import annotations

from pathlib import Path

from .model import PLAYER, Importer, ImportResult, ImportSourceError, read_json
from .wine import find_in_prefixes, resolve_in_prefix

RELATIVE = "AppData/Roaming/RM/config.json"


def detect(environ: dict[str, str]) -> list[Path]:
    return find_in_prefixes(environ, RELATIVE)


def locate(path: Path) -> Path:
    if path.is_file():
        return path
    return resolve_in_prefix(path, RELATIVE) or resolve_in_prefix(path, "config.json") or path


def read(path: Path) -> ImportResult:
    path = locate(path)
    if path.name == "accounts.dat":
        raise ImportSourceError("accounts.dat holds sign-in data. Choose RM's config.json instead.")
    document = read_json(path)
    if not isinstance(document, dict):
        raise ImportSourceError(f"{path.name} is not a JSON object of settings.")
    result = ImportResult("Roblox Manager", path)
    result.flags(PLAYER, document.get("roblox_fast_flags"), "roblox_fast_flags")
    if document.get("custom_game_args"):
        result.unsupported("custom_game_args", "Arguments for the Windows RobloxPlayerBeta.exe; Rosemary's Roblox differs.")
    if (path.parent / "client-launch-settings.json").is_file():
        result.unsupported(
            "client-launch-settings.json",
            "FPS limit, graphics quality, fullscreen and mute are Roblox's own in-game settings.",
        )
    result.notes.append("Roblox Manager's account, launch and privacy settings are not read.")
    return result


IMPORTER = Importer(
    id="roblox_manager",
    title="Roblox Manager",
    description="FastFlags from RM's config.json in a Wine prefix.",
    detect=detect,
    read=read,
    browse_hint="the Wine prefix, or AppData/Roaming/RM/config.json inside it",
    windows=True,
)
