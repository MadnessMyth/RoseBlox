"""MangoHud's overlay, which the Vulkan loader adds to Rosemary's renderer as an implicit layer."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

ENABLE_VARIABLE = "MANGOHUD"


def layer_directories(env: Mapping[str, str]) -> list[Path]:
    home = Path(env.get("HOME") or Path.home())
    data_home = Path(env.get("XDG_DATA_HOME") or home / ".local" / "share")
    data_dirs = [Path(entry) for entry in (env.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":") if entry]
    directories = [data_home / "vulkan" / "implicit_layer.d", Path("/etc/vulkan/implicit_layer.d")]
    directories += [entry / "vulkan" / "implicit_layer.d" for entry in data_dirs]
    unique: list[Path] = []
    for directory in directories:
        if directory not in unique:
            unique.append(directory)
    return unique


def layer_installed(env: Mapping[str, str] | None = None) -> bool:
    """Whether a MangoHud implicit layer manifest is installed for the Vulkan loader."""
    env = os.environ if env is None else env
    for directory in layer_directories(env):
        try:
            manifests = list(directory.glob("*.json"))
        except OSError:
            continue
        for manifest in manifests:
            if "mangohud" not in manifest.name.lower():
                continue
            try:
                document = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                continue
            layers = document.get("layers") or [document.get("layer")]
            if any(isinstance(layer, dict) and "MANGOHUD" in str(layer.get("name", "")).upper() for layer in layers):
                return True
    return False
