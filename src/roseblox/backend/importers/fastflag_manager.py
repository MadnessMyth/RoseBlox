"""Roblox FastFlag Manager (github.com/4anti/Roblox-FastFlag-Manager), inside a Wine prefix.

From src/utils/config.py and src/gui/api.py: ``%USERPROFILE%\\.FFlagManager\\user_flags.json``
is a list of ``{"name", "value", "type", "enabled", ...}``. The manager writes
flags into the running game's memory and may store names without their
prefix, looking the prefix up in Roblox at that moment.
"""

from __future__ import annotations

from pathlib import Path

from ..fast_flags import flag_kind
from .model import PLAYER, Importer, ImportResult, ImportSourceError, read_json
from .wine import find_in_prefixes, resolve_in_prefix

RELATIVE = ".FFlagManager/user_flags.json"
GUESSED_PREFIXES = {"bool": "FFlag", "int": "FInt", "string": "FString"}
LIVE_ONLY = ("bind", "cycle_states")


def detect(environ: dict[str, str]) -> list[Path]:
    return find_in_prefixes(environ, RELATIVE)


def locate(path: Path) -> Path:
    if path.is_file():
        return path
    return resolve_in_prefix(path, RELATIVE) or resolve_in_prefix(path, "user_flags.json") or path


def read(path: Path) -> ImportResult:
    path = locate(path)
    document = read_json(path)
    if not isinstance(document, list):
        raise ImportSourceError(f"{path.name} is not a list of flags.")
    result = ImportResult("Roblox FastFlag Manager", path)
    result.notes.append(
        "FastFlag Manager changes flags in the running game, which can take effect where a "
        "ClientAppSettings.json flag would not."
    )
    for index, entry in enumerate(document):
        key = f"[{index}]"
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not entry["name"]:
            result.invalid(key, "Expected a flag with a name.")
            continue
        name = entry["name"]
        key = name
        if not entry.get("enabled", True):
            result.skip(key, "Turned off in FastFlag Manager.")
            continue
        if any(entry.get(field) for field in LIVE_ONLY):
            result.unsupported(f"{key}.bind", "Hotkeys that change flags while playing are a FastFlag Manager feature.")
        kind = entry.get("type")
        if flag_kind(name) is None:
            prefix = GUESSED_PREFIXES.get(kind)
            if prefix is None:
                result.invalid(key, f"No prefix, and Roseblox cannot tell what kind of flag a {kind!r} is.")
                continue
            result.flag(
                PLAYER, prefix + name, entry.get("value"), key,
                f"Saved without a prefix; Roseblox guessed {prefix}. It may be a D or S flag instead.",
                selected=False,
            )
            continue
        result.flag(PLAYER, name, entry.get("value"), key)
    return result


IMPORTER = Importer(
    id="fastflag_manager",
    title="Roblox FastFlag Manager",
    description="Turned-on flags from user_flags.json in a Wine prefix.",
    detect=detect,
    read=read,
    browse_hint="the Wine prefix, or .FFlagManager/user_flags.json inside it",
    windows=True,
)
