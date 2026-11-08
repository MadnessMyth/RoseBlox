"""Bloxstrap (github.com/bloxstraplabs/bloxstrap), inside a Wine prefix.

Files from Bloxstrap/Paths.cs, JsonManager.cs and FastFlagManager.cs:
``%LOCALAPPDATA%\\Bloxstrap\\Settings.json`` and
``Modifications\\ClientSettings\\ClientAppSettings.json``. The Bootstrapper
copies the Modifications folder into Player and Studio alike, and leaves the
flag file out when ``UseFastFlagManager`` is off.
"""

from __future__ import annotations

from pathlib import Path

from .model import PLAYER, ROSEBLOX, STUDIO, Importer, ImportResult, ImportSourceError, read_json
from .wine import find_in_prefixes, resolve_in_prefix

RELATIVE = "AppData/Local/Bloxstrap"
FLAGS = Path("Modifications/ClientSettings/ClientAppSettings.json")
FONT = Path("Modifications/content/fonts/CustomFont.ttf")
STUDIO_COPY = "Bloxstrap gives Studio the same flags; tick to copy them to Studio too."
FLAG_MANAGER_OFF = "Bloxstrap's FastFlag manager is off, so Roblox does not get this flag."
BLOXSTRAP_ONLY = "A Bloxstrap setting with no Roseblox equivalent."


def detect(environ: dict[str, str]) -> list[Path]:
    return [path for path in find_in_prefixes(environ, RELATIVE) if (path / "Settings.json").is_file() or (path / FLAGS).is_file()]


def locate(path: Path) -> Path:
    if path.is_file():
        if path.name == "ClientAppSettings.json" and path.parent.name == "ClientSettings":
            return path.parents[2]
        return path.parent
    return resolve_in_prefix(path, RELATIVE) or path


def read(path: Path) -> ImportResult:
    folder = locate(path)
    settings_file, flags_file = folder / "Settings.json", folder / FLAGS
    if not settings_file.is_file() and not flags_file.is_file():
        raise ImportSourceError(f"{folder} has neither Settings.json nor {FLAGS}.")
    result = ImportResult("Bloxstrap", folder)

    settings: dict = {}
    if settings_file.is_file():
        try:
            document = read_json(settings_file)
        except ImportSourceError as error:
            result.invalid("Settings.json", str(error))
        else:
            if isinstance(document, dict):
                settings = document
            else:
                result.invalid("Settings.json", "Expected a JSON object of settings.")

    flag_manager = settings.get("UseFastFlagManager", True) is not False
    if flags_file.is_file():
        try:
            flags = read_json(flags_file)
        except ImportSourceError as error:
            result.invalid("ClientAppSettings.json", str(error))
        else:
            note = "" if flag_manager else FLAG_MANAGER_OFF
            result.flags(PLAYER, flags, "ClientAppSettings.json", note, selected=flag_manager)
            result.flags(STUDIO, flags, "ClientAppSettings.json", note or STUDIO_COPY, selected=False)

    if (folder / FONT).is_file():
        result.item(PLAYER, "mods.font", str(folder / FONT), "Modifications/content/fonts", "Copied into Roseblox's mods.")
    if (folder / "Modifications").is_dir():
        result.notes.append(
            "Other files in Bloxstrap's Modifications folder are not imported. "
            "Copy them into Roseblox's mods folder yourself if you want them."
        )

    tracking = settings.get("EnableActivityTracking", True) is not False
    presence = settings.get("UseDiscordRichPresence")
    if isinstance(presence, bool):
        result.item(ROSEBLOX, "discord_presence", presence and tracking, "UseDiscordRichPresence")
    hide_buttons = settings.get("HideRPCButtons")
    if isinstance(hide_buttons, bool):
        result.item(ROSEBLOX, "discord_join_button", not hide_buttons, "HideRPCButtons")
    details = settings.get("ShowServerDetails")
    if isinstance(details, bool):
        result.item(ROSEBLOX, "server_location", details and tracking, "ShowServerDetails")
    if settings.get("ShowAccountOnRichPresence"):
        result.unsupported("ShowAccountOnRichPresence", "Roseblox never shows your account in Discord.")
    if settings.get("CustomIntegrations"):
        result.unsupported("CustomIntegrations", "Roseblox does not start other programs with Roblox.")
    if settings.get("UseDisableAppPatch"):
        result.unsupported("UseDisableAppPatch", "A Windows desktop app patch.")
    handled = {
        "UseFastFlagManager", "EnableActivityTracking", "UseDiscordRichPresence", "HideRPCButtons",
        "ShowServerDetails", "ShowAccountOnRichPresence", "CustomIntegrations", "UseDisableAppPatch",
    }
    result.leftovers(settings, handled, "", BLOXSTRAP_ONLY)
    return result


IMPORTER = Importer(
    id="bloxstrap",
    title="Bloxstrap",
    description="FastFlags, custom font and Discord settings from a Wine prefix.",
    detect=detect,
    read=read,
    browse_hint="the Wine prefix, or AppData/Local/Bloxstrap inside it",
    windows=True,
)
