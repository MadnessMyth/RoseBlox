"""Performance presets built from the settings the Performance page already offers.

A preset only sets frame pacing, GameMode and a few rendering flags from Roblox's
allowlist. Every other flag, variable and argument is left as it is, and the
refresh rate stays a property of the display.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .launch_config import AppLaunchConfig

MSAA_FLAG = "FIntDebugForceMSAASamples"
TEXTURE_ENABLED_FLAG = "DFFlagTextureQualityOverrideEnabled"
TEXTURE_LEVEL_FLAG = "DFIntTextureQualityOverride"
QUALITY_FLAG = "DFIntDebugFRMQualityLevelOverride"
GRASS_FLAGS = ("FIntFRMMinGrassDistance", "FIntFRMMaxGrassDistance")
PRESET_FLAGS = (MSAA_FLAG, TEXTURE_ENABLED_FLAG, TEXTURE_LEVEL_FLAG, QUALITY_FLAG, *GRASS_FLAGS)
CUSTOM = "custom"


@dataclass(frozen=True)
class Preset:
    id: str
    title: str
    description: str
    presentation_mode: str | None = None
    gamemode: bool = False
    flags: dict[str, str] = field(default_factory=dict)

    def apply(self, config: AppLaunchConfig, *, gamemode_available: bool = True) -> AppLaunchConfig:
        flags = {name: value for name, value in config.fast_flags.items() if name not in PRESET_FLAGS}
        flags.update(self.flags)
        return replace(
            config,
            presentation_mode=self.presentation_mode,
            gamemode=self.gamemode and gamemode_available,
            fast_flags=flags,
        )

    def matches(self, config: AppLaunchConfig, *, gamemode_available: bool = True) -> bool:
        return self.apply(config, gamemode_available=gamemode_available) == config


PRESETS = (
    Preset(
        "balanced",
        "Balanced",
        "Roblox's and Rosemary's own defaults.",
    ),
    Preset(
        "performance",
        "Performance",
        "Anti-aliasing off, low textures, no grass, and GameMode when it is installed.",
        gamemode=True,
        flags={
            MSAA_FLAG: "1",
            TEXTURE_ENABLED_FLAG: "True",
            TEXTURE_LEVEL_FLAG: "1",
            GRASS_FLAGS[0]: "0",
            GRASS_FLAGS[1]: "0",
        },
    ),
    Preset(
        "visual",
        "Visual Quality",
        "4× anti-aliasing, high textures, the highest quality level, and V-Sync.",
        presentation_mode="fifo",
        flags={
            MSAA_FLAG: "4",
            TEXTURE_ENABLED_FLAG: "True",
            TEXTURE_LEVEL_FLAG: "3",
            QUALITY_FLAG: "21",
        },
    ),
)


def preset(preset_id: str) -> Preset:
    for candidate in PRESETS:
        if candidate.id == preset_id:
            return candidate
    raise ValueError(f"Unknown preset: {preset_id}")


def matching_preset(config: AppLaunchConfig, *, gamemode_available: bool = True) -> str:
    """Return the id of the preset the configuration is set to, or ``custom``."""
    for candidate in PRESETS:
        if candidate.matches(config, gamemode_available=gamemode_available):
            return candidate.id
    return CUSTOM
