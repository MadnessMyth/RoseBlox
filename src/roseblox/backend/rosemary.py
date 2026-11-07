"""Rosemary discovery and Roblox application launching."""

from __future__ import annotations

import json
import os
import re
import signal
import shutil
import subprocess
import tempfile
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
    plain_ctrl_click_supported: bool = False


_MAX_CAPTURED_OUTPUT = 64 * 1024
PLAIN_CTRL_CLICK_VARIABLE = "ROSEMARY_PLAIN_CTRL_CLICK"
_ROSEMARY_SETTINGS_LOCK = threading.RLock()
_INPUT_SUPPORT_CACHE: dict[tuple[str, int, int], bool] = {}


def _drain_output(stream) -> bytes:
    """Drain a pipe without allowing a noisy child to consume unbounded memory."""
    half_limit = _MAX_CAPTURED_OUTPUT // 2
    beginning = bytearray()
    ending = bytearray()
    truncated = False
    read = getattr(stream, "read1", stream.read)
    while True:
        chunk = read(8192)
        if not chunk:
            break
        prefix_space = half_limit - len(beginning)
        if prefix_space > 0:
            beginning.extend(chunk[:prefix_space])
        remainder = chunk[max(prefix_space, 0) :]
        if remainder:
            ending.extend(remainder)
            if len(ending) > half_limit:
                del ending[: len(ending) - half_limit]
            truncated = True
    if truncated:
        return bytes(beginning) + b"\n[Roseblox omitted middle process output.]\n" + bytes(ending)
    return bytes(beginning + ending)


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


_ELF_LOADER_PATTERN = re.compile(r"^ld-(linux[\w.-]*|musl-[\w.-]+)\.so(\.\d+)*$")


def _same_file(first: str, second: str) -> bool:
    try:
        return os.path.samefile(first, second)
    except OSError:
        return os.path.realpath(first) == os.path.realpath(second)


def _launched_rosemary_binary(snapshot: ProcessSnapshot, bundle_index: int) -> str | None:
    """Find libexec/rosemary run by an ELF loader, as Rosemary's release launcher does.

    Release builds (AppImage, extracted AppImage, Butter) start a static launcher which
    re-executes ``<root>/libexec/rosemary`` through a bundled or host ``ld-linux`` loader,
    so ``/proc/<pid>/exe`` names the loader instead of the selected executable.
    """
    if not _ELF_LOADER_PATTERN.match(os.path.basename(snapshot.executable)):
        return None
    for argument in snapshot.arguments[1:bundle_index]:
        path = Path(argument)
        if path.name == "rosemary" and path.parent.name == "libexec" and path.is_absolute():
            return argument
    return None


def _matches_session(session: LaunchSession) -> bool:
    if session.process.poll() is not None:
        return False
    snapshot = read_process_snapshot(session.process.pid)
    if snapshot is None:
        return False

    expected_bundle = os.path.normpath(session.bundle_path)
    bundle_index = next(
        (
            index
            for index, argument in enumerate(snapshot.arguments)
            if os.path.normpath(argument) == expected_bundle
        ),
        None,
    )
    if bundle_index is None:
        return False
    if not _same_file(snapshot.executable, session.rosemary_path):
        if _launched_rosemary_binary(snapshot, bundle_index) is None:
            return False

    if session.start_time is None:
        session.start_time = snapshot.start_time
    return snapshot.start_time == session.start_time


def _collect_output(session: LaunchSession) -> None:
    readers: list[threading.Thread] = []
    try:
        def read_into(stream, attribute: str) -> None:
            if stream is None:
                return
            try:
                setattr(session, attribute, _drain_output(stream))
            except Exception:
                setattr(session, attribute, b"")

        for stream, attribute in (
            (session.process.stdout, "stdout"),
            (session.process.stderr, "stderr"),
        ):
            reader = threading.Thread(target=read_into, args=(stream, attribute), daemon=True)
            readers.append(reader)
            reader.start()
        for reader in readers:
            reader.join()
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


def build_launch_arguments(
    rosemary_path: str,
    bundle_path: str | Path,
    guest_arguments: tuple[str, ...] | list[str] = (),
) -> list[str]:
    """Build the Rosemary invocation without invoking a shell."""
    absolute_bundle = os.path.abspath(os.fspath(bundle_path))
    if not absolute_bundle.endswith(os.sep):
        absolute_bundle += os.sep
    if any(not isinstance(argument, str) or "\0" in argument for argument in guest_arguments):
        raise ValueError("Rosemary arguments must be valid strings without NUL characters.")
    return [rosemary_path, absolute_bundle, *guest_arguments]


def validate_rosemary_executable(path: str | Path) -> str:
    """Return a validated absolute path to an executable Rosemary binary."""
    candidate = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise ValueError(f"Selected Rosemary path is not an executable file: {candidate}")
    return str(candidate.resolve())


def save_rosemary_executable(
    path: str | Path | None,
    config_path: Path | None = None,
) -> None:
    """Update only Roseblox's Rosemary executable setting, atomically."""
    config_path = config_path or default_config_path()
    with _ROSEMARY_SETTINGS_LOCK:
        document: dict[str, object] = {}
        if config_path.exists():
            try:
                document = json.loads(config_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise ValueError(f"Could not read Roseblox settings; the file was not changed: {error}") from error
            if not isinstance(document, dict):
                raise ValueError("Roseblox settings must contain a JSON object; the file was not changed.")
        if path is None:
            document.pop("rosemary_executable", None)
        else:
            document["rosemary_executable"] = validate_rosemary_executable(path)
        temporary_path: Path | None = None
        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=config_path.parent, delete=False
            ) as stream:
                temporary_path = Path(stream.name)
                json.dump(document, stream, indent=2, sort_keys=True)
                stream.write("\n")
            os.replace(temporary_path, config_path)
        except OSError as error:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise ValueError(f"Could not save Roseblox settings: {error}") from error


def rosemary_supports_plain_ctrl_click(rosemary_path: str | Path) -> bool:
    """Detect the documented capability in this Rosemary executable.

    A release launcher (``bin/rosemary``, or ``AppRun`` linking to it) carries no
    options itself; the runtime it starts lives beside it in ``libexec/rosemary``.
    """
    executable = Path(rosemary_path).resolve()
    if _file_contains_marker(executable):
        return True
    runtime = executable.parent.parent / "libexec" / "rosemary"
    return executable.parent.name == "bin" and runtime.is_file() and _file_contains_marker(runtime)


def _file_contains_marker(executable: Path) -> bool:
    try:
        stat = executable.stat()
    except OSError:
        return False
    key = (str(executable), stat.st_mtime_ns, stat.st_size)
    cached = _INPUT_SUPPORT_CACHE.get(key)
    if cached is not None:
        return cached

    marker = PLAIN_CTRL_CLICK_VARIABLE.encode("ascii")
    carry = b""
    supported = False
    try:
        with executable.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                combined = carry + chunk
                if marker in combined:
                    supported = True
                    break
                carry = combined[-(len(marker) - 1) :]
    except OSError:
        supported = False
    _INPUT_SUPPORT_CACHE[key] = supported
    return supported


def launch_application(
    installation: InstallationRecord | Mapping[str, object],
    rosemary_status: RosemaryStatus | None = None,
    *,
    guest_arguments: tuple[str, ...] | list[str] = (),
) -> LaunchSession:
    """Start Rosemary asynchronously and return a bundle-bound process session."""
    app_type = installation.app_type if isinstance(installation, InstallationRecord) else str(installation["app_type"])
    bundle_path = installation.bundle_path if isinstance(installation, InstallationRecord) else str(installation["bundle_path"])
    validate_bundle(bundle_path, app_type)

    status = rosemary_status or detect_rosemary()
    if not status.available or status.executable is None:
        detail = f" {status.error}" if status.error else ""
        raise RosemaryUnavailableError(f"Rosemary is required to launch Roblox.{detail}")

    supports_plain_ctrl_click = rosemary_supports_plain_ctrl_click(status.executable)
    environment = os.environ.copy()
    if supports_plain_ctrl_click:
        environment[PLAIN_CTRL_CLICK_VARIABLE] = "1"
    process = subprocess.Popen(
        build_launch_arguments(status.executable, bundle_path, guest_arguments),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    session = LaunchSession(
        process=process,
        rosemary_path=status.executable,
        bundle_path=os.path.abspath(bundle_path),
        output_done=threading.Event(),
        plain_ctrl_click_supported=supports_plain_ctrl_click,
    )
    threading.Thread(target=_collect_output, args=(session,), daemon=True).start()
    return session
