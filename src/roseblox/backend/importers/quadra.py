"""Quadra (github.com/QuadraHQ/Quadra): ``~/.config/quadrahq/settings.json`` and ``fastflags.json``.

Keys from launcher/quadra/core/config.py (DEFAULT_SETTINGS), rosemary/runtime.py,
studio.py and mods.py. Player flags live in fastflags.json beside settings.json;
Studio's extra flags are ``studio_fflags``. Quadra's ``web`` folder holds the
sign-in cookies and is never read.
"""

from __future__ import annotations

from pathlib import Path

from .model import PLAYER, ROSEBLOX, STUDIO, Importer, ImportResult, ImportSourceError, home, read_json, xdg

FOLDER_NAMES = ("quadrahq", "ciderhq", "macoblox")
QUADRA_ONLY = "A Quadra setting with no Roseblox equivalent."


def detect(environ: dict[str, str]) -> list[Path]:
    bases = [xdg(environ, "CONFIG"), home(environ) / ".var/app/org.quadrahq.Quadra/config"]
    found = []
    for base in bases:
        for name in FOLDER_NAMES:
            folder = base / name
            if (folder / "settings.json").is_file() or (folder / "fastflags.json").is_file():
                found.append(folder)
                break
    return found


def read(path: Path) -> ImportResult:
    folder = path.parent if path.is_file() else path
    settings_file = folder / "settings.json"
    flags_file = folder / "fastflags.json"
    if not settings_file.is_file() and not flags_file.is_file():
        raise ImportSourceError(f"{folder} has neither settings.json nor fastflags.json.")
    result = ImportResult("Quadra", folder)

    if flags_file.is_file():
        try:
            result.flags(PLAYER, read_json(flags_file), "fastflags.json")
        except ImportSourceError as error:
            result.invalid("fastflags.json", str(error))
    else:
        result.notes.append("Quadra has no fastflags.json yet, so there are no Player FastFlags to import.")

    if not settings_file.is_file():
        return result
    try:
        settings = read_json(settings_file)
    except ImportSourceError as error:
        result.invalid("settings.json", str(error))
        return result
    if not isinstance(settings, dict):
        result.invalid("settings.json", "Expected a JSON object of settings.")
        return result

    handled = set()

    def take(key: str) -> object:
        handled.add(key)
        return settings.get(key)

    result.flags(STUDIO, take("studio_fflags"), "studio_fflags")
    result.gpu(PLAYER, take("gpu"), "gpu")
    result.gpu(STUDIO, take("studio_gpu"), "studio_gpu")
    result.boolean(STUDIO, "gamemode", take("studio_gamemode"), "studio_gamemode")
    result.boolean(PLAYER, "mangohud", take("mangohud"), "mangohud")
    result.presentation_mode(PLAYER, take("rosemary_presentation_mode"), "rosemary_presentation_mode")
    result.refresh_rate(PLAYER, take("rosemary_refresh_rate"), "rosemary_refresh_rate")
    result.decorations(PLAYER, take("rosemary_decorations"), "rosemary_decorations")
    result.command_key(PLAYER, take("rosemary_command_key"), "rosemary_command_key")
    result.boolean(ROSEBLOX, "plain_ctrl_click", take("rosemary_plain_ctrl_click"), "rosemary_plain_ctrl_click")
    result.boolean(ROSEBLOX, "discord_presence", take("discord_rpc"), "discord_rpc")

    lookup, notify = take("server_location_lookup"), take("server_location_notify")
    if isinstance(lookup, bool):
        if lookup and notify is False:
            result.unsupported(
                "server_location_lookup",
                "Quadra looks the server up without a notification; Roseblox's lookup always notifies.",
            )
        else:
            result.item(ROSEBLOX, "server_location", lookup, "server_location_lookup")
    elif lookup is not None:
        result.invalid("server_location_lookup", "Expected true or false.")

    death_sound = take("mod_death_sound")
    if death_sound in ("default", "classic_oof"):
        result.item(PLAYER, "mods.classic_death_sound", death_sound == "classic_oof", "mod_death_sound")
    elif death_sound == "custom":
        result.unsupported("mod_death_sound", "Roseblox offers the classic “oof”, not a custom sound file.")
    elif death_sound is not None:
        result.invalid("mod_death_sound", "Expected default, classic_oof or custom.")
    handled.add("mod_custom_death_sound")

    font = take("mod_custom_font")
    if isinstance(font, str) and font:
        if Path(font).is_file():
            result.item(PLAYER, "mods.font", font, "mod_custom_font", "Copied into Roseblox's mods.")
        else:
            result.invalid("mod_custom_font", f"The font file {font} no longer exists.")

    if take("rosemary_force_opengl"):
        result.unsupported(
            "rosemary_force_opengl",
            "Roseblox has no renderer switch. To force OpenGL, hide the Vulkan drivers from Rosemary in "
            "Performance → Environment.",
        )
    result.leftovers(settings, handled, "", QUADRA_ONLY)
    return result


IMPORTER = Importer(
    id="quadra",
    title="Quadra",
    description="Player and Studio FastFlags, Rosemary options, GPU, Discord and mods.",
    detect=detect,
    read=read,
    browse_hint="the ~/.config/quadrahq folder",
)
