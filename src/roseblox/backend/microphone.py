"""Silence Roblox Player's microphone stream until a hotkey opens it.

Roblox records through its own capture stream (a PulseAudio "source output",
a PipeWire "Stream/Input/Audio" node). Muting that stream feeds Roblox digital
silence while the stream itself, the device and every other app's microphone
keep running, so Roblox's voice chat never notices a disconnect.

Roblox replaces the stream whenever it reopens the microphone, and PipeWire
remembers a stream's mute for the next one, so the gate re-applies its state to
each new stream and opens every stream again when it stops.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable

MODE_TOGGLE = "toggle"
MODE_HOLD = "hold"
MICROPHONE_MODES = (MODE_TOGGLE, MODE_HOLD)
DEFAULT_RELEASE_DELAY_MS = 250
MAX_RELEASE_DELAY_MS = 5000

ROBLOX_APPLICATION_IDS = frozenset({"com.roblox.RobloxPlayer"})
ROBLOX_APPLICATION_NAMES = frozenset({"Roblox", "RobloxPlayer"})
CAPTURE_STREAM_CLASS = "Stream/Input/Audio"
COMMAND_TIMEOUT_SECONDS = 5


class MicrophoneError(RuntimeError):
    """Raised when the sound server cannot be queried or changed."""


@dataclass(frozen=True)
class CaptureStream:
    index: str
    muted: bool
    properties: dict[str, str] = field(default_factory=dict)


def is_roblox_stream(properties: dict[str, str]) -> bool:
    if properties.get("media.class", CAPTURE_STREAM_CLASS) != CAPTURE_STREAM_CLASS:
        return False
    if properties.get("application.id") in ROBLOX_APPLICATION_IDS:
        return True
    return "application.id" not in properties and properties.get("application.name") in ROBLOX_APPLICATION_NAMES


_PROPERTY = re.compile(r'^\s+([A-Za-z0-9_.\-]+) = "(.*)"$')


def parse_source_outputs(text: str) -> list[CaptureStream]:
    """Parse ``pactl list source-outputs``; its labels are English only under LC_ALL=C."""
    streams: list[CaptureStream] = []
    index: str | None = None
    muted = False
    properties: dict[str, str] = {}

    def finish() -> None:
        if index is not None:
            streams.append(CaptureStream(index, muted, dict(properties)))

    for line in text.splitlines():
        if line.startswith("Source Output #"):
            finish()
            index, muted, properties = line.removeprefix("Source Output #").strip(), False, {}
            continue
        if index is None:
            continue
        stripped = line.strip()
        if stripped.startswith("Mute:"):
            muted = stripped.removeprefix("Mute:").strip() == "yes"
            continue
        match = _PROPERTY.match(line)
        if match:
            properties[match.group(1)] = match.group(2).replace('\\"', '"').replace("\\\\", "\\")
    finish()
    return streams


def parse_pw_dump(text: str) -> list[CaptureStream]:
    try:
        objects = json.loads(text or "[]")
    except json.JSONDecodeError as error:
        raise MicrophoneError(f"pw-dump printed something unexpected: {error}") from error
    streams = []
    for item in objects if isinstance(objects, list) else []:
        if not isinstance(item, dict) or item.get("type") != "PipeWire:Interface:Node":
            continue
        info = item.get("info") or {}
        props = info.get("props") or {}
        if props.get("media.class") != CAPTURE_STREAM_CLASS:
            continue
        params = (info.get("params") or {}).get("Props") or []
        muted = any(isinstance(param, dict) and param.get("mute") is True for param in params)
        properties = {str(key): str(value) for key, value in props.items()}
        streams.append(CaptureStream(str(item.get("id")), muted, properties))
    return streams


Runner = Callable[[list[str]], str]


def run_command(arguments: list[str]) -> str:
    try:
        result = subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            env={**os.environ, "LC_ALL": "C"},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise MicrophoneError(f"{arguments[0]} did not run: {error}") from error
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip() or f"exit status {result.returncode}"
        raise MicrophoneError(f"{arguments[0]}: {message}")
    return result.stdout


class PulseBackend:
    """``pactl``: PulseAudio, and PipeWire through pipewire-pulse."""

    name = "pactl"
    monitor_command = ["pactl", "subscribe"]

    def __init__(self, runner: Runner = run_command) -> None:
        self.runner = runner

    def streams(self) -> list[CaptureStream]:
        return parse_source_outputs(self.runner(["pactl", "list", "source-outputs"]))

    def set_muted(self, stream: CaptureStream, muted: bool) -> None:
        self.runner(["pactl", "set-source-output-mute", stream.index, "1" if muted else "0"])

    @staticmethod
    def relevant(line: str) -> bool:
        return "source-output" in line


class PipeWireBackend:
    """PipeWire without pipewire-pulse: ``pw-dump`` to look, ``wpctl`` to mute."""

    name = "wpctl"
    monitor_command = ["pw-dump", "--monitor", "--no-colors"]

    def __init__(self, runner: Runner = run_command) -> None:
        self.runner = runner

    def streams(self) -> list[CaptureStream]:
        return parse_pw_dump(self.runner(["pw-dump"]))

    def set_muted(self, stream: CaptureStream, muted: bool) -> None:
        self.runner(["wpctl", "set-mute", stream.index, "1" if muted else "0"])

    @staticmethod
    def relevant(line: str) -> bool:
        return True


def detect_backend(which: Callable[[str], str | None] = shutil.which, runner: Runner = run_command):
    if which("pactl"):
        return PulseBackend(runner)
    if which("pw-dump") and which("wpctl"):
        return PipeWireBackend(runner)
    return None


NO_SOUND_SERVER = (
    "No PipeWire or PulseAudio sound server is running, so Roseblox cannot mute Roblox's microphone. "
    "Setups with only ALSA or JACK have no separate microphone stream per app."
)
NO_MUTE_TOOLS = (
    "Roseblox needs pactl to mute Roblox's microphone (the libpulse package on Arch, pulseaudio-utils "
    "on Debian, Ubuntu, Fedora and openSUSE), or PipeWire's pw-dump and wpctl."
)


def sound_server_running(environ: dict[str, str] | None = None) -> bool:
    environ = os.environ if environ is None else environ
    if environ.get("PULSE_SERVER") or environ.get("PIPEWIRE_REMOTE"):
        return True
    runtime = environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        return False
    return any(os.path.exists(os.path.join(runtime, name)) for name in ("pulse/native", "pipewire-0"))


def microphone_problem(
    which: Callable[[str], str | None] = shutil.which, environ: dict[str, str] | None = None
) -> str | None:
    """Why the microphone toggle cannot work on this computer, or None."""
    if not sound_server_running(environ):
        return NO_SOUND_SERVER
    if not which("pactl") and not (which("pw-dump") and which("wpctl")):
        return NO_MUTE_TOOLS
    return None


def validate_release_delay(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_RELEASE_DELAY_MS:
        raise ValueError(f"The release delay must be between 0 and {MAX_RELEASE_DELAY_MS} milliseconds.")
    return value


def _validated(mode: object, release_delay_ms: object, inverted: object) -> tuple[str, int, bool]:
    if mode not in MICROPHONE_MODES:
        raise ValueError(f"Unknown microphone mode: {mode!r}")
    if not isinstance(inverted, bool):
        raise ValueError("Inverted must be on or off.")
    return mode, validate_release_delay(release_delay_ms), inverted


class MicrophoneGate:
    """Keeps every Roblox capture stream muted unless the hotkey has opened the microphone.

    ``schedule(milliseconds, callback)`` returns a handle for ``cancel``; the
    service passes GLib timeouts and the tests pass a fake clock.
    """

    def __init__(
        self,
        backend,
        mode: str = MODE_TOGGLE,
        release_delay_ms: int = DEFAULT_RELEASE_DELAY_MS,
        inverted: bool = False,
        *,
        schedule: Callable[[int, Callable[[], None]], object],
        cancel: Callable[[object], None],
        log: Callable[[str], None] = lambda _message: None,
        changed: Callable[[bool], None] = lambda _open: None,
    ) -> None:
        self.backend = backend
        self.mode, self.release_delay_ms, self.inverted = _validated(mode, release_delay_ms, inverted)
        self.schedule = schedule
        self.cancel = cancel
        self.log = log
        self.changed = changed
        self.open = self._resting()
        self.active = True
        self._release_timer = None
        self._held = False

    def configure(self, mode: str, release_delay_ms: int, inverted: bool = False) -> None:
        mode, self.release_delay_ms, inverted = _validated(mode, release_delay_ms, inverted)
        if (mode, inverted) != (self.mode, self.inverted):
            self.mode, self.inverted = mode, inverted
            self._held = False
            self._cancel_release()
            self._set_open(self._resting())

    def _resting(self) -> bool:
        """Hold mode returns here on release: muted, or open when inverted (push to mute)."""
        return self.mode == MODE_HOLD and self.inverted

    def toggle(self) -> None:
        self._cancel_release()
        self._set_open(not self.open)

    def press(self) -> None:
        if self.mode == MODE_TOGGLE:
            self.toggle()
            return
        self._held = True
        self._cancel_release()
        self._set_open(not self._resting())

    def release(self) -> None:
        if self.mode != MODE_HOLD or not self._held:
            return
        self._held = False
        self._cancel_release()
        if self.release_delay_ms <= 0:
            self._set_open(self._resting())
        else:
            self._release_timer = self.schedule(self.release_delay_ms, self._release_elapsed)

    def set_open(self, value: bool) -> None:
        self._held = False
        self._cancel_release()
        self._set_open(bool(value))

    def stop(self) -> None:
        """Open every Roblox stream so PipeWire does not remember a mute for the next session."""
        self._cancel_release()
        self.active = False
        self.sync()

    def sync(self) -> None:
        want_muted = self.active and not self.open
        try:
            streams = [stream for stream in self.backend.streams() if is_roblox_stream(stream.properties)]
        except MicrophoneError as error:
            self.log(str(error))
            return
        for stream in streams:
            if stream.muted == want_muted:
                continue
            try:
                self.backend.set_muted(stream, want_muted)
            except MicrophoneError as error:
                # The stream may have closed between listing and muting.
                self.log(str(error))

    def _release_elapsed(self) -> bool:
        self._release_timer = None
        if not self._held:
            self._set_open(self._resting())
        return False

    def _cancel_release(self) -> None:
        if self._release_timer is not None:
            self.cancel(self._release_timer)
            self._release_timer = None

    def _set_open(self, value: bool) -> None:
        changed = value != self.open
        self.open = value
        self.sync()
        if changed:
            self.log("microphone open" if value else "microphone muted")
            self.changed(value)
