"""Rosemary discovery and Roblox application launching."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .library import InstallationRecord, validate_bundle


@dataclass(frozen=True)
class RosemaryStatus:
    executable: str | None
    configured: bool = False
    error: str | None = None

    @property
    def available(self) -> bool:
        return self.executable is not None


class RosemaryUnavailableError(RuntimeError):
    """Raised when an application cannot be launched through Rosemary."""


def default_config_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "roseblox" / "settings.json"


def detect_rosemary(config_path: Path | None = None) -> RosemaryStatus:
    """Use a configured executable when present, otherwise search PATH."""
    config_path = config_path or default_config_path()
    if config_path.exists():
        try:
            settings = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            return RosemaryStatus(None, error=f"Could not read Roseblox settings: {error}")
        if not isinstance(settings, dict):
            return RosemaryStatus(None, error="Roseblox settings must contain a JSON object.")
        configured = settings.get("rosemary_executable")
        if configured:
            executable = os.path.abspath(os.path.expanduser(str(configured)))
            if Path(executable).is_file() and os.access(executable, os.X_OK):
                return RosemaryStatus(executable, configured=True)
            return RosemaryStatus(
                None,
                configured=True,
                error=f"Configured Rosemary executable is not executable: {executable}",
            )

    executable = shutil.which("rosemary")
    return RosemaryStatus(os.path.abspath(executable) if executable else None)


def build_launch_arguments(rosemary_path: str, bundle_path: str | Path) -> list[str]:
    """Build the exact Rosemary invocation without invoking a shell."""
    absolute_bundle = os.path.abspath(os.fspath(bundle_path))
    if not absolute_bundle.endswith(os.sep):
        absolute_bundle += os.sep
    return [rosemary_path, absolute_bundle]


def launch_application(
    installation: InstallationRecord | Mapping[str, object],
    rosemary_status: RosemaryStatus | None = None,
) -> subprocess.Popen[bytes]:
    """Launch a registered bundle asynchronously through Rosemary."""
    app_type = installation.app_type if isinstance(installation, InstallationRecord) else str(installation["app_type"])
    bundle_path = installation.bundle_path if isinstance(installation, InstallationRecord) else str(installation["bundle_path"])
    validate_bundle(bundle_path, app_type)

    status = rosemary_status or detect_rosemary()
    if not status.available or status.executable is None:
        detail = f" {status.error}" if status.error else ""
        raise RosemaryUnavailableError(f"Rosemary is required to launch Roblox.{detail}")

    return subprocess.Popen(
        build_launch_arguments(status.executable, bundle_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
