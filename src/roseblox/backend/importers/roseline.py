"""Roséline (github.com/buwryme/Roseline): ``~/.config/roseline/settings.toml``.

Format from Roseline's src/settings.rs and src/flags.rs: ``flags`` holds the
preset switches (only the allowlisted preset and override-slider keys are
written to Roblox), ``user_flags`` the manual layer written on top.
"""

from __future__ import annotations

from pathlib import Path

from .model import PLAYER, ROSEBLOX, Importer, ImportResult, home, read_toml, xdg

PRESET_KEYS = frozenset({
    "FIntDebugForceMSAASamples",
    "FIntGrassMovementReducedMotionFactor",
    "FFlagDebugSkyGray",
    "DFIntCSGLevelOfDetailSwitchingDistance",
    "DFIntCSGLevelOfDetailSwitchingDistanceL12",
    "DFIntCSGLevelOfDetailSwitchingDistanceL23",
    "DFIntCSGLevelOfDetailSwitchingDistanceL34",
    "DFIntTextureQualityOverride",
    "DFFlagTextureQualityOverrideEnabled",
})
ROSELINE_DISCORD_APPLICATION = "1561415907023265872"
DISCORD_DEFAULTS = {"show_game": True, "show_avatar": False, "details": "Roblox", "state": "via Roséline"}
ROSELINE_ONLY = "A Roséline setting with no Roseblox equivalent."


def detect(environ: dict[str, str]) -> list[Path]:
    candidates = [
        xdg(environ, "CONFIG") / "roseline" / "settings.toml",
        home(environ) / ".var/app/net.buwryy.Roseline/config/roseline/settings.toml",
    ]
    return [path for path in candidates if path.is_file()]


def locate(path: Path) -> Path:
    if path.is_dir():
        for candidate in (path / "settings.toml", path / "roseline" / "settings.toml"):
            if candidate.is_file():
                return candidate
    return path


def read(path: Path) -> ImportResult:
    path = locate(path)
    document = read_toml(path)
    result = ImportResult("Roséline", path)

    flags = document.get("flags") or {}
    user_flags = document.get("user_flags") or {}
    if not isinstance(flags, dict) or not isinstance(user_flags, dict):
        result.invalid("flags", "Roséline's flags must be names and values.")
        flags, user_flags = {}, {}
    for name, value in flags.items():
        if name in user_flags:
            result.skip(f"flags.{name}", "Replaced by the same flag in user_flags, as Roséline does.")
        elif name not in PRESET_KEYS:
            result.skip(f"flags.{name}", "Roséline does not write this preset key to Roblox.")
        else:
            result.flag(PLAYER, name, value, f"flags.{name}")
    result.flags(PLAYER, user_flags, "user_flags")

    graphics = _table(result, document, "graphics")
    result.gpu(PLAYER, graphics.get("device"), "graphics.device")
    if graphics.get("backend") not in (None, "auto"):
        result.unsupported(
            "graphics.backend",
            "Roseblox has no renderer switch. To force OpenGL, hide the Vulkan drivers from Rosemary in "
            "Performance → Environment.",
        )
    result.leftovers(graphics, {"device", "backend"}, "graphics.", ROSELINE_ONLY)

    environment = _table(result, document, "environment")
    result.environment_lines(PLAYER, True, environment.get("guest"), "environment.guest")
    result.environment_lines(PLAYER, False, environment.get("runner"), "environment.runner")

    behavior = _table(result, document, "behavior")
    result.boolean(PLAYER, "gamemode", behavior.get("gamemode"), "behavior.gamemode")
    result.boolean(ROSEBLOX, "close_on_leave", behavior.get("close_on_leave"), "behavior.close_on_leave")
    result.leftovers(behavior, {"gamemode", "close_on_leave"}, "behavior.", ROSELINE_ONLY)

    discord = _table(result, document, "discord")
    result.boolean(ROSEBLOX, "discord_presence", discord.get("enabled"), "discord.enabled")
    result.boolean(ROSEBLOX, "discord_join_button", discord.get("join_button"), "discord.join_button")
    application = discord.get("app_id")
    if isinstance(application, str) and application.strip() and application.strip() != ROSELINE_DISCORD_APPLICATION:
        if application.strip().isdigit():
            result.item(ROSEBLOX, "discord_application_id", application.strip(), "discord.app_id")
        else:
            result.invalid("discord.app_id", "A Discord application ID is a number.")
    elif application is not None:
        result.skip("discord.app_id", "Roséline's own Discord application; Roseblox uses its own.")
    for key, default in DISCORD_DEFAULTS.items():
        if key in discord and discord[key] != default:
            result.unsupported(f"discord.{key}", "Roseblox's presence text and images are fixed.")

    location = _table(result, document, "server_location")
    result.boolean(ROSEBLOX, "server_location", location.get("enabled"), "server_location.enabled")

    background = _table(result, document, "avatar_background")
    result.boolean(PLAYER, "mods.classic_avatar_background", background.get("old"), "avatar_background.old")

    fonts = _table(result, document, "fonts")
    font = fonts.get("font")
    if isinstance(font, str) and font:
        if Path(font).is_file():
            result.item(PLAYER, "mods.font", font, "fonts.font", "Copied into Roseblox's mods.")
        else:
            result.invalid("fonts.font", f"The font file {font} no longer exists.")
    if fonts.get("mono"):
        result.unsupported("fonts.mono", "Roseblox replaces the text fonts only, not the monospace one.")

    for key in ("runner", "compatibility", "prefix"):
        if key in document:
            result.unsupported(key, "Roséline's own Rosemary build, prefix and VM settings do not carry over.")
    handled = {
        "flags", "user_flags", "graphics", "environment", "behavior", "discord", "server_location",
        "avatar_background", "fonts", "runner", "compatibility", "prefix",
    }
    result.leftovers(document, handled, "", ROSELINE_ONLY)
    return result


def _table(result: ImportResult, document: dict, key: str) -> dict:
    value = document.get(key, {})
    if isinstance(value, dict):
        return value
    result.invalid(key, "Expected a table of settings.")
    return {}


IMPORTER = Importer(
    id="roseline",
    title="Roséline",
    description="Player FastFlags, GPU, GameMode, environment variables, Discord and mods.",
    detect=detect,
    read=read,
    browse_hint="settings.toml in ~/.config/roseline",
)
