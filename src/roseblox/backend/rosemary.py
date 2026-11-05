"""Rosemary discovery and Roblox application launching."""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import threading
import time
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


class LaunchIdentityError(RuntimeError):
    """Raised when Roseblox cannot verify which bundle a Rosemary process runs."""


class ProcessIdentityError(RuntimeError):
    """Raised rather than signaling a process that no longer matches its launch."""


@dataclass
class LaunchSession:
    """One Rosemary child, tied to a specific app bundle and Linux process start."""

    process: subprocess.Popen[bytes]
    rosemary_path: str
    bundle_path: str
    start_time: int | None = None
    stdout: bytes = b""
    stderr: bytes = b""
    output_done: threading.Event | None = None


@dataclass(frozen=True)
class ProcessSnapshot:
    executable: str
    arguments: tuple[str, ...]
    start_time: int


def read_process_snapshot(pid: int) -> ProcessSnapshot | None:
    """Read process identity from procfs; return None if it exited or is unreadable."""
    process_dir = Path("/proc") / str(pid)
    try:
        arguments = tuple(
            os.fsdecode(argument)
            for argument in (process_dir / "cmdline").read_bytes().split(b"\0")
            if argument
        )
        stat = (process_dir / "stat").read_text(encoding="ascii")
        # The comm field is parenthesized and may itself contain spaces or ')'.
        stat_fields = stat[stat.rfind(")") + 2 :].split()
        start_time = int(stat_fields[19])  # field 22 in proc_pid_stat(5)
        executable = os.readlink(process_dir / "exe")
    except (OSError, UnicodeError, ValueError, IndexError):
        return None
    return ProcessSnapshot(executable, arguments, start_time)


def _matches_session(session: LaunchSession) -> bool:
    if session.process.poll() is not None:
        return False
    snapshot = read_process_snapshot(session.process.pid)
    if snapshot is None:
        return False

    expected_bundle = os.path.normpath(session.bundle_path)
    if not any(os.path.normpath(argument) == expected_bundle for argument in snapshot.arguments):
        return False
    try:
        if not os.path.samefile(snapshot.executable, session.rosemary_path):
            return False
    except OSError:
        if os.path.realpath(snapshot.executable) != os.path.realpath(session.rosemary_path):
            return False

    if session.start_time is None:
        session.start_time = snapshot.start_time
    return snapshot.start_time == session.start_time


def _collect_output(session: LaunchSession) -> None:
    try:
        session.stdout, session.stderr = session.process.communicate()
    except (OSError, ValueError):
        pass
    finally:
        if session.output_done is not None:
            session.output_done.set()


def wait_for_running(session: LaunchSession, timeout: float = 8.0) -> None:
    """Wait off the GTK thread until this exact Rosemary guest is verifiable."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if session.process.poll() is not None:
            if session.output_done is not None:
                session.output_done.wait(timeout=1.0)
            details = (session.stderr or session.stdout).decode("utf-8", errors="replace").strip()
            message = details or "Rosemary exited before its Roblox application could be verified."
            raise LaunchIdentityError(message)
        if _matches_session(session):
            return
        time.sleep(0.1)
    raise LaunchIdentityError(
        "Rosemary started, but Roseblox could not verify the process for this application bundle."
    )


def stop_application(session: LaunchSession, timeout: float = 5.0) -> None:
    """Gracefully stop only the verified Rosemary child, escalating after timeout."""
    if session.process.poll() is not None:
        return
    if not _matches_session(session):
        raise ProcessIdentityError(
            "The Rosemary process no longer matches this registered application; it was not signaled."
        )

    try:
        session.process.send_signal(signal.SIGTERM)
    except ProcessLookupError:
        return

    try:
        session.process.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass

    if session.process.poll() is not None:
        return
    if not _matches_session(session):
        raise ProcessIdentityError(
            "The process identity changed during shutdown; Roseblox did not send SIGKILL."
        )
    session.process.kill()
    session.process.wait(timeout=max(timeout, 5.0))


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
) -> LaunchSession:
    """Start Rosemary asynchronously and return a bundle-bound process session."""
    app_type = installation.app_type if isinstance(installation, InstallationRecord) else str(installation["app_type"])
    bundle_path = installation.bundle_path if isinstance(installation, InstallationRecord) else str(installation["bundle_path"])
    validate_bundle(bundle_path, app_type)

    status = rosemary_status or detect_rosemary()
    if not status.available or status.executable is None:
        detail = f" {status.error}" if status.error else ""
        raise RosemaryUnavailableError(f"Rosemary is required to launch Roblox.{detail}")

    process = subprocess.Popen(
        build_launch_arguments(status.executable, bundle_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    session = LaunchSession(
        process=process,
        rosemary_path=status.executable,
        bundle_path=os.path.abspath(bundle_path),
        output_done=threading.Event(),
    )
    threading.Thread(target=_collect_output, args=(session,), daemon=True).start()
    return session
