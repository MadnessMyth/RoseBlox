"""Roblox's public FastFlag allowlist and the categories Roseblox shows flags in."""

from __future__ import annotations

import base64
import json
import os
import re
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ALLOWLIST_URL = "https://clientsettingscdn.roblox.com/v2/settings/application/MacDesktopClient"
ALLOWLIST_SETTING = "DFStringAllowedPublicFlags"
REFRESH_SECONDS = 6 * 60 * 60
_TIMEOUT_SECONDS = 15
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024

# Roblox's list as of November 2026, used until the live one has been fetched once.
BUILT_IN_ALLOWLIST = frozenset(
    {
        "DFFlagDebugPauseVoxelizer",
        "DFFlagDisableDPIScale",
        "DFFlagTextureQualityOverrideEnabled",
        "DFIntCSGLevelOfDetailSwitchingDistance",
        "DFIntCSGLevelOfDetailSwitchingDistanceL12",
        "DFIntCSGLevelOfDetailSwitchingDistanceL23",
        "DFIntCSGLevelOfDetailSwitchingDistanceL34",
        "DFIntDebugFRMQualityLevelOverride",
        "DFIntTextureQualityOverride",
        "FFlagDebugGraphicsPreferD3D11",
        "FFlagDebugGraphicsPreferOpenGL",
        "FFlagDebugGraphicsPreferVulkan",
        "FFlagDebugSkyGray",
        "FFlagHandleAltEnterFullscreenManually",
        "FIntDebugForceMSAASamples",
        "FIntFRMMaxGrassDistance",
        "FIntFRMMinGrassDistance",
        "FIntGrassMovementReducedMotionFactor",
    }
)

CATEGORIES = ("Rendering", "Textures", "Level of detail", "Display", "Networking", "Physics", "Security", "Other")
# Profiles never touch these, because they change how a game behaves rather than how it looks.
PROFILE_EXCLUDED_CATEGORIES = frozenset({"Networking", "Physics", "Security"})


@dataclass(frozen=True)
class FlagInfo:
    category: str
    title: str
    description: str


KNOWN_FLAGS = {
    "FIntDebugForceMSAASamples": FlagInfo("Rendering", "Anti-aliasing samples", "Forces MSAA: 0, 1, 2, 4 or 8 samples."),
    "DFIntDebugFRMQualityLevelOverride": FlagInfo(
        "Rendering", "Graphics quality level", "Fixes the automatic quality level, from 1 (lowest) to 21 (highest)."
    ),
    "FFlagDebugSkyGray": FlagInfo("Rendering", "Gray sky", "Replaces the skybox with flat gray."),
    "DFFlagDebugPauseVoxelizer": FlagInfo(
        "Rendering", "Pause lighting voxelizer", "Stops lighting from updating as the world changes."
    ),
    "FFlagDebugGraphicsPreferVulkan": FlagInfo(
        "Rendering", "Prefer Vulkan", "Windows renderer choice. The macOS client always renders with Metal."
    ),
    "FFlagDebugGraphicsPreferOpenGL": FlagInfo(
        "Rendering", "Prefer OpenGL", "Windows renderer choice. The macOS client always renders with Metal."
    ),
    "FFlagDebugGraphicsPreferD3D11": FlagInfo(
        "Rendering", "Prefer Direct3D 11", "Windows renderer choice. The macOS client always renders with Metal."
    ),
    "DFFlagTextureQualityOverrideEnabled": FlagInfo(
        "Textures", "Override texture quality", "Lets the texture quality level below take effect."
    ),
    "DFIntTextureQualityOverride": FlagInfo("Textures", "Texture quality level", "0 (lowest) to 3 (highest)."),
    "DFIntCSGLevelOfDetailSwitchingDistance": FlagInfo(
        "Level of detail", "Union detail distance", "Distance where unions start losing detail."
    ),
    "DFIntCSGLevelOfDetailSwitchingDistanceL12": FlagInfo(
        "Level of detail", "Union detail distance, level 1 to 2", "Distance of the second detail step."
    ),
    "DFIntCSGLevelOfDetailSwitchingDistanceL23": FlagInfo(
        "Level of detail", "Union detail distance, level 2 to 3", "Distance of the third detail step."
    ),
    "DFIntCSGLevelOfDetailSwitchingDistanceL34": FlagInfo(
        "Level of detail", "Union detail distance, level 3 to 4", "Distance of the last detail step."
    ),
    "FIntFRMMinGrassDistance": FlagInfo("Rendering", "Grass start distance", "Where grass starts fading out."),
    "FIntFRMMaxGrassDistance": FlagInfo("Rendering", "Grass end distance", "Where grass disappears. 0 hides grass."),
    "FIntGrassMovementReducedMotionFactor": FlagInfo(
        "Rendering", "Grass movement with reduced motion", "How much grass sways when reduced motion is on."
    ),
    "DFFlagDisableDPIScale": FlagInfo("Display", "Disable DPI scaling", "Renders the interface at native pixels."),
    "FFlagHandleAltEnterFullscreenManually": FlagInfo(
        "Display", "Alt+Enter fullscreen", "Lets Roblox handle Alt+Enter itself."
    ),
}

_CATEGORY_PATTERNS = (
    ("Networking", re.compile(r"Network|Net[A-Z]|Http|Replicat|Packet|Bandwidth|Ping|Latency|Socket|Raknet", re.I)),
    ("Physics", re.compile(r"Physics|Simulation|Collision|Humanoid|Aerodynamic|Assembly|Solver", re.I)),
    ("Security", re.compile(r"Security|Anticheat|Hyperion|Auth|Crypt|Signature|Telemetry|Cookie|Sandbox", re.I)),
    ("Textures", re.compile(r"Texture|Mip", re.I)),
    ("Level of detail", re.compile(r"LevelOfDetail|LOD", re.I)),
    ("Display", re.compile(r"DPI|Fullscreen|Window|Display|Resolution", re.I)),
    ("Rendering", re.compile(r"Render|Graphics|Shadow|Light|MSAA|FRM|Grass|Sky|Shader|Voxel|Fog|PostFX|Bloom", re.I)),
)


def flag_info(name: str) -> FlagInfo:
    known = KNOWN_FLAGS.get(name)
    if known is not None:
        return known
    return FlagInfo(categorize(name), name, "")


def categorize(name: str) -> str:
    if name in KNOWN_FLAGS:
        return KNOWN_FLAGS[name].category
    for category, pattern in _CATEGORY_PATTERNS:
        if pattern.search(name):
            return category
    return "Other"


@dataclass(frozen=True)
class Allowlist:
    flags: frozenset[str]
    fetched_at: float | None
    error: str | None = None

    @property
    def built_in(self) -> bool:
        return self.fetched_at is None

    def allows(self, name: str) -> bool:
        return name in self.flags


def default_cache_path() -> Path:
    cache_home = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return cache_home / "roseblox" / "fflag-allowlist.json"


def parse_allowlist_response(data: bytes) -> frozenset[str]:
    """Read the allowlist out of Roblox's client settings response."""
    try:
        settings = json.loads(data)["applicationSettings"]
        encoded = settings[ALLOWLIST_SETTING]
        allowed = json.loads(base64.b64decode(encoded, validate=True))["Allowed"]
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError(f"Roblox's response did not contain the FastFlag allowlist ({error}).") from error
    if not isinstance(allowed, list) or not all(isinstance(name, str) for name in allowed):
        raise ValueError("Roblox's FastFlag allowlist has an unexpected format.")
    return frozenset(allowed)


def load_cached_allowlist(cache_path: Path | None = None) -> Allowlist:
    path = cache_path or default_cache_path()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        flags = document["flags"]
        fetched_at = float(document["fetched_at"])
        if not isinstance(flags, list) or not all(isinstance(name, str) for name in flags):
            raise ValueError("invalid flags")
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        return Allowlist(BUILT_IN_ALLOWLIST, None)
    return Allowlist(frozenset(flags), fetched_at)


def fetch_allowlist(
    cache_path: Path | None = None,
    *,
    force: bool = False,
    opener=urllib.request.urlopen,
    clock=time.time,
) -> Allowlist:
    """Return the cached list, fetching a new one when it is older than six hours."""
    path = cache_path or default_cache_path()
    cached = load_cached_allowlist(path)
    if not force and cached.fetched_at is not None and clock() - cached.fetched_at < REFRESH_SECONDS:
        return cached
    try:
        request = urllib.request.Request(ALLOWLIST_URL, headers={"User-Agent": "Roseblox"})
        with opener(request, timeout=_TIMEOUT_SECONDS) as response:
            data = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(data) > _MAX_RESPONSE_BYTES:
            raise ValueError("Roblox's response was unexpectedly large.")
        flags = parse_allowlist_response(data)
    except (OSError, ValueError) as error:
        return Allowlist(cached.flags, cached.fetched_at, f"Could not update Roblox's FastFlag allowlist: {error}")
    fetched = Allowlist(flags, clock())
    _save_cache(path, fetched)
    return fetched


def _save_cache(path: Path, allowlist: Allowlist) -> None:
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary_path = Path(stream.name)
            json.dump({"fetched_at": allowlist.fetched_at, "flags": sorted(allowlist.flags)}, stream, indent=2)
        os.replace(temporary_path, path)
    except OSError:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
