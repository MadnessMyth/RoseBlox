"""A factual report of the system Roblox runs on, for troubleshooting.

Every line is read from the system or from Rosemary; nothing is estimated.
"""

from __future__ import annotations

import os
import platform
import subprocess
from collections.abc import Mapping
from pathlib import Path

from .. import __version__
from . import mangohud
from .gpu import Gpu
from .rosemary_paths import (
    directory_size,
    guest_home,
    prefix_directory,
    read_framerate_cap,
    roblox_settings_path,
    shader_cache_directory,
)
from .transfer_rate import format_size

VULKAN_ICD_DIRECTORIES = (Path("/usr/share/vulkan/icd.d"), Path("/etc/vulkan/icd.d"))


def rosemary_version(executable: str | None) -> str:
    if not executable:
        return "Not found"
    try:
        result = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.SubprocessError) as error:
        return f"Could not run {executable}: {error}"
    lines = [line.strip() for line in (result.stdout or result.stderr).splitlines() if line.strip()]
    return ", ".join(lines[:2]) if lines else f"{executable} printed no version"


PREFIX_INFO_SHOWN_ELSEWHERE = {"prefix", "guest /"}


def prefix_info(executable: str | None, env: Mapping[str, str]) -> list[tuple[str, str]]:
    """What ``rosemary prefix info`` says about the prefix, minus the paths shown separately."""
    if not executable or prefix_directory(env) is None:
        return []
    try:
        result = subprocess.run(
            [executable, "prefix", "info"],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
            env=dict(env),
        )
    except (OSError, subprocess.SubprocessError) as error:
        return [("Prefix info", f"Could not run rosemary prefix info: {error}")]
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip().splitlines()
        return [("Prefix info", message[-1] if message else f"rosemary prefix info exited with {result.returncode}")]
    items = []
    for line in result.stdout.splitlines()[:20]:
        key, separator, value = line.partition(":")
        key = key.strip()
        if separator and key and key not in PREFIX_INFO_SHOWN_ELSEWHERE:
            items.append((f"Prefix {key}", value.strip()[:300]))
    return items


def vulkan_drivers(directories=VULKAN_ICD_DIRECTORIES) -> list[str]:
    names = set()
    for directory in directories:
        try:
            names.update(path.stem for path in directory.glob("*.json"))
        except OSError:
            continue
    return sorted(names)


def _meminfo(path: Path = Path("/proc/meminfo")) -> dict[str, int]:
    values = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            name, _, rest = line.partition(":")
            parts = rest.split()
            if parts and parts[0].isdigit():
                values[name] = int(parts[0]) * 1024
    except OSError:
        pass
    return values


def cpu_model(path: Path = Path("/proc/cpuinfo")) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def session_type(env: Mapping[str, str]) -> str:
    if env.get("WAYLAND_DISPLAY"):
        return "Wayland"
    if env.get("DISPLAY"):
        return "X11"
    return env.get("XDG_SESSION_TYPE") or "Unknown"


def collect(
    *,
    rosemary_executable: str | None,
    env: Mapping[str, str],
    gpus: list[Gpu],
    gamemode_available: bool,
) -> list[tuple[str, str]]:
    """Return ``(label, value)`` pairs; may take a moment, so call it off the main thread."""
    memory = _meminfo()
    prefix = prefix_directory(env)
    home = guest_home(env)
    shaders = shader_cache_directory(env)
    if shaders is None:
        shader_text = "Turned off"
    elif shaders.is_dir():
        size, files = directory_size(shaders)
        shader_text = f"{shaders} ({format_size(size)} in {files} files)"
    else:
        shader_text = f"{shaders} (empty)"
    cap = read_framerate_cap(roblox_settings_path(env))
    if cap is None:
        cap_text = "Roblox has not saved its settings in this prefix yet"
    elif cap <= 0:
        cap_text = "Default"
    else:
        cap_text = f"{cap} FPS"
    total = memory.get("MemTotal")
    available = memory.get("MemAvailable")
    return [
        ("Roseblox", __version__),
        ("Rosemary", rosemary_version(rosemary_executable)),
        ("Rosemary executable", rosemary_executable or "Not found"),
        ("Prefix", "Turned off" if prefix is None else f"{prefix}{'' if prefix.is_dir() else ' (not created yet)'}"),
        *prefix_info(rosemary_executable, env),
        ("Roblox home", str(home) if home is not None else "None"),
        ("Shader cache", shader_text),
        ("Roblox frame rate cap", cap_text),
        ("GPUs", "; ".join(f"{gpu.name} ({gpu.driver or 'no driver'}, {gpu.slot})" for gpu in gpus) or "None found"),
        ("Vulkan drivers", ", ".join(vulkan_drivers()) or "None found"),
        ("GameMode", "Available" if gamemode_available else "Not available"),
        ("MangoHud layer", "Installed" if mangohud.layer_installed(env) else "Not installed"),
        ("Session", session_type(env)),
        ("Kernel", platform.release()),
        ("CPU", f"{cpu_model()} ({os.cpu_count() or '?'} threads)"),
        (
            "Memory",
            f"{format_size(available)} available of {format_size(total)}" if total and available else "Unknown",
        ),
    ]


def format_report(items: list[tuple[str, str]]) -> str:
    return "\n".join(f"{label}: {value}" for label, value in items) + "\n"
