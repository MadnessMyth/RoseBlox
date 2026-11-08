"""Rosemary discovery and Roblox application launching."""

from __future__ import annotations

import fcntl
import json
import os
import plistlib
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

from . import gamemode
from .fast_flags import FastFlagError, write_bundle_flags
from .gpu import list_gpus
from .launch_config import AppLaunchConfig, effective_arguments, effective_environment
from .library import InstallationRecord, validate_bundle
from .rosemary_paths import roblox_log_directory


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
    # What Rosemary was given: the bundle, or its executable for a direct launch.
    launch_target: str | None = None
    stdout: bytes = b""
    stderr: bytes = b""
    output_done: threading.Event | None = None
    plain_ctrl_click_active: bool = False
    log_path: Path | None = None
    gamemode_error: str | None = None
    mods_message: str | None = None
    roblox_log_directory: Path | None = None

    def output_text(self) -> str:
        """Rosemary's output so far: the end of its log file, or captured pipe output."""
        if self.log_path is not None:
            return _read_log_tail(self.log_path)
        return (self.stderr or self.stdout).decode("utf-8", errors="replace").strip()


_MAX_CAPTURED_OUTPUT = 64 * 1024
PLAIN_CTRL_CLICK_VARIABLE = "ROSEMARY_PLAIN_CTRL_CLICK"
_ROSEMARY_SETTINGS_LOCK = threading.RLock()
_INPUT_SUPPORT_CACHE: dict[tuple[str, int, int], bool] = {}


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

    expected_bundle = os.path.normpath(session.launch_target or session.bundle_path)
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


def wait_for_running(session: LaunchSession, timeout: float = 8.0) -> None:
    """Wait off the GTK thread until this exact Rosemary guest is verifiable."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if session.process.poll() is not None:
            if session.output_done is not None:
                session.output_done.wait(timeout=1.0)
            details = session.output_text()
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


def bundle_executable(bundle_path: str | Path) -> Path:
    """The program inside an .app bundle, as its Info.plist names it."""
    bundle = Path(os.path.abspath(os.fspath(bundle_path)))
    name = bundle.name.removesuffix(".app")
    try:
        with open(bundle / "Contents" / "Info.plist", "rb") as stream:
            declared = plistlib.load(stream).get("CFBundleExecutable")
    except (OSError, plistlib.InvalidFileException, ValueError, AttributeError):
        declared = None
    if isinstance(declared, str) and declared and "/" not in declared and declared not in {".", ".."}:
        name = declared
    executable = bundle / "Contents" / "MacOS" / name
    if not executable.is_file():
        raise ValueError(f"The bundle has no executable at {executable}.")
    return executable


def build_launch_arguments(
    rosemary_path: str,
    bundle_path: str | Path,
    guest_arguments: tuple[str, ...] | list[str] = (),
    *,
    direct: bool = False,
) -> list[str]:
    """Build the Rosemary invocation without invoking a shell.

    Rosemary reads its own flags (such as ``-n``) anywhere after the bundle, so
    guest arguments always follow ``--`` and reach Roblox unchanged.

    Rosemary follows the Mac rule that an app whose Info.plist sets
    ``LSMultipleInstancesProhibited``, as Roblox Player does, runs once per bundle
    identifier: opening any copy of the bundle hands the launch to the running one,
    even with ``--new-instance``. Running the bundle's executable (``direct``) is
    never refused, which is how more than one Player runs at a time.
    """
    if direct:
        target = os.fspath(bundle_executable(bundle_path))
    else:
        target = os.path.abspath(os.fspath(bundle_path))
        if not target.endswith(os.sep):
            target += os.sep
    if any(not isinstance(argument, str) or "\0" in argument for argument in guest_arguments):
        raise ValueError("Rosemary arguments must be valid strings without NUL characters.")
    return [rosemary_path, target, *(("--", *guest_arguments) if guest_arguments else ())]


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


def default_log_directory() -> Path:
    state_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return state_home / "roseblox" / "logs"


def default_flag_backup_directory() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config_home / "roseblox" / "backups"


def _read_log_tail(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(size - _MAX_CAPTURED_OUTPUT, 0))
            data = stream.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    if size > _MAX_CAPTURED_OUTPUT:
        text = "[Roseblox omitted earlier process output.]\n" + text.split("\n", 1)[-1]
    return text.strip()


_LAUNCH_LOG_GUARD = threading.Lock()
_MAX_LAUNCH_LOGS = 32


def _log_in_use(path: Path) -> bool:
    try:
        stream = path.open("rb")
    except FileNotFoundError:
        return False
    with stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    return False


def _open_launch_log(log_directory: Path, app_type: str):
    """Open a fresh log for this launch, keeping the previous run as ``.1``.

    Rosemary inherits the log with its lock, so the lock lasts as long as that
    launch, even across Roseblox restarts. While ``player.log`` is held, another
    Player writes ``player-2.log`` instead of rotating a log that is still in use.
    """
    log_directory.mkdir(parents=True, exist_ok=True)
    with _LAUNCH_LOG_GUARD:
        for number in range(1, _MAX_LAUNCH_LOGS + 1):
            path = log_directory / (f"{app_type}.log" if number == 1 else f"{app_type}-{number}.log")
            if not _log_in_use(path):
                break
        else:
            raise OSError(f"{_MAX_LAUNCH_LOGS} {app_type} logs are already in use.")
        if path.exists():
            os.replace(path, path.with_name(path.name + ".1"))
        stream = path.open("wb")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            raise
        return path, stream


def launch_application(
    installation: InstallationRecord | Mapping[str, object],
    rosemary_status: RosemaryStatus | None = None,
    *,
    guest_arguments: tuple[str, ...] | list[str] = (),
    log_directory: Path | None = None,
    plain_ctrl_click: bool = True,
    launch_config: AppLaunchConfig | None = None,
    flag_backup_directory: Path | None = None,
    direct: bool = False,
) -> LaunchSession:
    """Start Rosemary asynchronously and return a bundle-bound process session.

    Rosemary writes to a log file in its own session rather than to pipes, so Roblox
    keeps running (and logging) after Roseblox exits. With ``launch_config`` the
    bundle's FastFlags are written first, and its environment and arguments apply.
    """
    app_type = installation.app_type if isinstance(installation, InstallationRecord) else str(installation["app_type"])
    bundle_path = installation.bundle_path if isinstance(installation, InstallationRecord) else str(installation["bundle_path"])
    validate_bundle(bundle_path, app_type)

    status = rosemary_status or detect_rosemary()
    if not status.available or status.executable is None:
        detail = f" {status.error}" if status.error else ""
        raise RosemaryUnavailableError(f"Rosemary is required to launch Roblox.{detail}")

    plain_ctrl_click_active = plain_ctrl_click and rosemary_supports_plain_ctrl_click(status.executable)
    environment = os.environ.copy()
    if plain_ctrl_click_active:
        environment[PLAIN_CTRL_CLICK_VARIABLE] = "1"
    else:
        # Turned off in Settings means Rosemary's default, even if the variable is set globally.
        environment.pop(PLAIN_CTRL_CLICK_VARIABLE, None)
    if launch_config is not None:
        gpus = list_gpus() if launch_config.gpu else []
        environment = effective_environment(launch_config, environment, gpus)
        guest_arguments = (*guest_arguments, *effective_arguments(launch_config, app_type))
        try:
            write_bundle_flags(
                bundle_path,
                launch_config.fast_flags,
                flag_backup_directory or default_flag_backup_directory(),
            )
        except FastFlagError as error:
            raise RosemaryUnavailableError(f"Could not apply FastFlags: {error}") from error
    try:
        arguments = build_launch_arguments(status.executable, bundle_path, guest_arguments, direct=direct)
    except ValueError as error:
        raise RosemaryUnavailableError(str(error)) from error
    try:
        log_path, log_stream = _open_launch_log(log_directory or default_log_directory(), app_type)
    except OSError as error:
        raise RosemaryUnavailableError(f"Could not create the Rosemary log file: {error}") from error
    with log_stream:
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            env=environment,
            start_new_session=True,
        )
    done = threading.Event()
    done.set()
    gamemode_error = None
    if launch_config is not None and launch_config.gamemode:
        try:
            gamemode.register(process.pid)
        except gamemode.GameModeError as error:
            gamemode_error = str(error)
    return LaunchSession(
        process=process,
        rosemary_path=status.executable,
        bundle_path=os.path.abspath(bundle_path),
        launch_target=arguments[1],
        output_done=done,
        plain_ctrl_click_active=plain_ctrl_click_active,
        log_path=log_path,
        gamemode_error=gamemode_error,
        roblox_log_directory=roblox_log_directory(environment) if app_type == "player" else None,
    )
