"""Vinegar (github.com/vinegarhq/vinegar): ``~/.config/vinegar/config.toml``.

Keys from internal/config/config.go. Vinegar runs Roblox Studio through Wine,
so only its Studio FastFlags, GameMode and GPU carry over to Roseblox's Studio.
"""

from __future__ import annotations

from pathlib import Path

from .model import STUDIO, Importer, ImportResult, home, read_toml, xdg

WINE_ONLY = "A Wine setting; Roseblox runs Studio with Rosemary."


def detect(environ: dict[str, str]) -> list[Path]:
    candidates = [
        xdg(environ, "CONFIG") / "vinegar" / "config.toml",
        home(environ) / ".var/app/org.vinegarhq.Vinegar/config/vinegar/config.toml",
    ]
    return [path for path in candidates if path.is_file()]


def read(path: Path) -> ImportResult:
    if path.is_dir():
        path = next((p for p in (path / "config.toml", path / "vinegar" / "config.toml") if p.is_file()), path)
    document = read_toml(path)
    result = ImportResult("Vinegar", path)

    studio = document.get("studio", {})
    if not isinstance(studio, dict):
        result.invalid("studio", "Expected a table of settings.")
        studio = {}
    result.flags(STUDIO, studio.get("fflags"), "studio.fflags")
    result.boolean(STUDIO, "gamemode", studio.get("gamemode"), "studio.gamemode")
    result.gpu(STUDIO, studio.get("gpu"), "studio.gpu")
    for key in ("env", "renderer", "webview", "wineroot", "virtual_desktop", "launcher", "dxvk"):
        if key in studio:
            result.unsupported(f"studio.{key}", WINE_ONLY)
    if "discord_rpc" in studio:
        result.unsupported("studio.discord_rpc", "Roseblox shows Discord Rich Presence for the Player only.")
    for key in ("forced_version", "channel"):
        if studio.get(key):
            result.unsupported(f"studio.{key}", "Roseblox installs the current Studio release.")
    handled = {
        "fflags", "gamemode", "gpu", "env", "renderer", "webview", "wineroot", "virtual_desktop", "launcher",
        "dxvk", "discord_rpc", "forced_version", "channel",
    }
    result.leftovers(studio, handled, "studio.", "A Vinegar setting with no Roseblox equivalent.")
    if document.get("env"):
        result.unsupported("env", WINE_ONLY)
    result.leftovers(document, {"studio", "env", "debug"}, "", "A Vinegar setting with no Roseblox equivalent.")
    return result


IMPORTER = Importer(
    id="vinegar",
    title="Vinegar",
    description="Studio FastFlags, GameMode and GPU.",
    detect=detect,
    read=read,
    browse_hint="config.toml in ~/.config/vinegar",
)
