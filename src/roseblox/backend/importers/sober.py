"""Sober (sober.vinegarhq.org): ``~/.var/app/org.vinegarhq.Sober/config/sober/config.json``.

Keys from Sober's documentation (vinegarhq.org/Sober). Sober runs Roblox's
Android client, so its FastFlags were chosen for a different build of Roblox.
"""

from __future__ import annotations

from pathlib import Path

from .model import PLAYER, ROSEBLOX, Importer, ImportResult, ImportSourceError, home, read_json

ANDROID_FLAGS = "Chosen for Roblox's Android client; the macOS client may ignore it."
SOBER_ONLY = {
    "use_opengl": "Roseblox has no renderer switch.",
    "touch_mode": "Touch controls are an Android client feature.",
    "enable_hidpi": "Rosemary follows the desktop's scaling.",
    "use_libsecret": "A sign-in storage setting; Roseblox does not handle sign-in data.",
    "allow_gamepad_permission": "Rosemary passes controllers through without a permission.",
    "graphics_optimization_mode": "An Android client setting.",
    "enable_mobile_home_screen": "An Android client setting.",
    "use_console_experience": "An Android client setting.",
}


def detect(environ: dict[str, str]) -> list[Path]:
    path = home(environ) / ".var/app/org.vinegarhq.Sober/config/sober/config.json"
    return [path] if path.is_file() else []


def read(path: Path) -> ImportResult:
    if path.is_dir():
        path = next((p for p in (path / "config.json", path / "sober" / "config.json") if p.is_file()), path)
    document = read_json(path)
    if not isinstance(document, dict):
        raise ImportSourceError(f"{path.name} is not a JSON object of settings.")
    result = ImportResult("Sober", path)
    result.flags(PLAYER, document.get("fflags"), "fflags", ANDROID_FLAGS)
    result.boolean(ROSEBLOX, "discord_presence", document.get("discord_rpc_enabled"), "discord_rpc_enabled")
    result.boolean(
        ROSEBLOX, "discord_join_button", document.get("discord_rpc_show_join_button"), "discord_rpc_show_join_button"
    )
    result.boolean(
        ROSEBLOX, "server_location", document.get("server_location_indicator_enabled"),
        "server_location_indicator_enabled",
    )
    result.boolean(ROSEBLOX, "close_on_leave", document.get("close_on_leave"), "close_on_leave")
    result.boolean(PLAYER, "gamemode", document.get("enable_gamemode"), "enable_gamemode")
    result.boolean(PLAYER, "mods.classic_death_sound", document.get("bring_back_oof"), "bring_back_oof")
    handled = {
        "fflags", "discord_rpc_enabled", "discord_rpc_show_join_button", "server_location_indicator_enabled",
        "close_on_leave", "enable_gamemode", "bring_back_oof",
    }
    for key, reason in SOBER_ONLY.items():
        if key in document:
            result.unsupported(key, reason)
    result.leftovers(document, handled | set(SOBER_ONLY), "", "A Sober setting with no Roseblox equivalent.")
    return result


IMPORTER = Importer(
    id="sober",
    title="Sober",
    description="FastFlags (from the Android client), GameMode, Discord, server location and the classic “oof”.",
    detect=detect,
    read=read,
    browse_hint="config.json in ~/.var/app/org.vinegarhq.Sober/config/sober",
)
