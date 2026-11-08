"""Short sounds when the microphone toggle opens or mutes Roblox's microphone.

Roseblox's own sounds are generated, so no audio files ship with it. A chosen
file is copied, like the mods' files, so the original may move.
"""

from __future__ import annotations

import io
import math
import os
import shutil
import struct
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ON = "on"
OFF = "off"
KINDS = (ON, OFF)
DEFAULT_VOLUME = 60
MAX_VOLUME = 100
MAX_SOUND_SIZE = 2 * 1024 * 1024
SOUND_SUFFIXES = (".wav", ".ogg", ".oga", ".flac")
_SAMPLE_RATE = 48000
# Two short notes: rising when the microphone opens, falling when it mutes.
_NOTES = {ON: (660.0, 990.0), OFF: (990.0, 660.0)}


class MicrophoneSoundError(ValueError):
    pass


@dataclass(frozen=True)
class MicrophoneSounds:
    enabled: bool = False
    volume: int = DEFAULT_VOLUME
    on: str = ""
    off: str = ""

    def path(self, kind: str) -> str:
        return self.on if kind == ON else self.off


def validate_volume(volume: object) -> int:
    if isinstance(volume, bool) or not isinstance(volume, int) or not 0 <= volume <= MAX_VOLUME:
        raise MicrophoneSoundError(f"The sound volume must be between 0 and {MAX_VOLUME}%.")
    return volume


def validate_sound_path(value: object) -> str:
    if not isinstance(value, str) or (value and not os.path.isabs(value)) or "\0" in value:
        raise MicrophoneSoundError("A microphone sound must be a full file path, or empty for Roseblox's.")
    return value


def check_sound_file(path: Path) -> str:
    """The file's suffix, from its signature; raises when Roseblox cannot play it."""
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            header = stream.read(12)
    except OSError as error:
        raise MicrophoneSoundError(f"Could not read the sound: {error}") from error
    if size > MAX_SOUND_SIZE:
        raise MicrophoneSoundError("Choose a sound smaller than 2 MiB; a short click or beep is best.")
    if header[:4] == b"RIFF" and header[8:12] == b"WAVE":
        return ".wav"
    if header[:4] == b"OggS":
        return ".ogg"
    if header[:4] == b"fLaC":
        return ".flac"
    raise MicrophoneSoundError("Choose a WAV, Ogg or FLAC sound.")


def default_directory(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    data = env.get("XDG_DATA_HOME") or str(Path(env.get("HOME") or Path.home()) / ".local" / "share")
    return Path(data) / "roseblox" / "sounds"


def store_sound(kind: str, source: Path, directory: Path | None = None) -> Path:
    """Copy a chosen sound, replacing the previous choice for this kind."""
    if kind not in KINDS:
        raise MicrophoneSoundError(f"Unknown microphone sound {kind!r}.")
    suffix = check_sound_file(source)
    directory = directory or default_directory()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"microphone-{kind}{suffix}"
    temporary = target.with_name(target.name + ".tmp")
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise MicrophoneSoundError(f"Could not copy the sound: {error}") from error
    for old in directory.glob(f"microphone-{kind}.*"):
        if old != target:
            old.unlink(missing_ok=True)
    return target


def tone(kind: str) -> bytes:
    """Roseblox's own sound as a WAV file: two 70 ms notes with soft edges."""
    samples = bytearray()
    length = int(_SAMPLE_RATE * 0.07)
    fade = int(_SAMPLE_RATE * 0.008)
    for frequency in _NOTES[kind]:
        for index in range(length):
            envelope = min(1.0, index / fade, (length - index) / fade)
            value = 0.5 * envelope * math.sin(2 * math.pi * frequency * index / _SAMPLE_RATE)
            samples += struct.pack("<h", int(value * 32767))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(_SAMPLE_RATE)
        output.writeframes(bytes(samples))
    return buffer.getvalue()


def default_sound(kind: str, directory: Path | None = None) -> Path:
    directory = directory or default_directory()
    path = directory / f"roseblox-microphone-{kind}.wav"
    data = tone(kind)
    try:
        if not path.is_file() or path.read_bytes() != data:
            directory.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    except OSError as error:
        raise MicrophoneSoundError(f"Could not write Roseblox's sound: {error}") from error
    return path


def play_command(path: Path, volume: int, which: Callable[[str], str | None] = shutil.which) -> list[str] | None:
    if which("pw-play"):
        return ["pw-play", f"--volume={volume / 100:.2f}", str(path)]
    if which("paplay"):
        return ["paplay", f"--volume={round(65536 * volume / 100)}", str(path)]
    return None


class SoundPlayer:
    """Plays a sound without waiting for it; a sound that fails to play is only logged."""

    def __init__(
        self,
        *,
        directory: Path | None = None,
        which: Callable[[str], str | None] = shutil.which,
        spawn: Callable[[list[str]], object] | None = None,
        log: Callable[[str], None] = lambda _message: None,
    ) -> None:
        self.directory = directory
        self.which = which
        self.spawn = spawn or self._spawn
        self.log = log
        self._running: list[subprocess.Popen] = []

    def play(self, sounds: MicrophoneSounds, kind: str, *, force: bool = False) -> bool:
        if not (sounds.enabled or force) or sounds.volume == 0:
            return False
        try:
            chosen = sounds.path(kind)
            if chosen and Path(chosen).is_file():
                path = Path(chosen)
            else:
                if chosen:
                    self.log(f"the {kind} sound {chosen} is missing; using Roseblox's")
                path = default_sound(kind, self.directory)
        except MicrophoneSoundError as error:
            self.log(str(error))
            return False
        command = play_command(path, sounds.volume, self.which)
        if command is None:
            self.log("neither pw-play nor paplay was found; microphone sounds cannot play")
            return False
        try:
            self.spawn(command)
        except OSError as error:
            self.log(f"could not play the {kind} sound: {error}")
            return False
        return True

    def _spawn(self, command: list[str]) -> None:
        self._running = [process for process in self._running if process.poll() is None]
        self._running.append(
            subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        )
