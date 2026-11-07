"""Minimal Discord Rich Presence client over Discord's local IPC socket."""

from __future__ import annotations

import json
import os
import re
import socket
import struct
import uuid
from pathlib import Path
from typing import Mapping

DEFAULT_APPLICATION_ID = ""
_APPLICATION_ID = re.compile(r"^\d{15,22}$")
_HANDSHAKE, _FRAME, _CLOSE = 0, 1, 2
_MAX_PAYLOAD = 64 * 1024
# Discord's desktop app, and its Flatpak and Snap builds, each put the socket in a different place.
_SOCKET_DIRECTORIES = ("", "app/com.discordapp.Discord", "app/com.discordapp.DiscordCanary", "snap.discord", "snap.discord-canary")


class DiscordError(RuntimeError):
    pass


def valid_application_id(value: str) -> bool:
    return bool(_APPLICATION_ID.match(value or ""))


def effective_application_id(saved: str) -> str:
    """The ID set in Settings, or Roseblox's own Discord application."""
    return saved or DEFAULT_APPLICATION_ID


def socket_candidates(env: Mapping[str, str] | None = None) -> list[Path]:
    env = os.environ if env is None else env
    bases = []
    for name in ("XDG_RUNTIME_DIR", "TMPDIR", "TMP", "TEMP"):
        if env.get(name):
            bases.append(Path(env[name]))
    bases.append(Path("/tmp"))
    candidates: list[Path] = []
    for base in dict.fromkeys(bases):
        for directory in _SOCKET_DIRECTORIES:
            for index in range(10):
                candidates.append(base / directory / f"discord-ipc-{index}")
    return candidates


def _limit(text: str, length: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= length else text[: length - 1] + "…"


def build_activity(
    *,
    game_name: str,
    creator: str | None,
    started_at: int,
    icon_url: str | None,
    buttons: list[tuple[str, str]],
) -> dict:
    """An activity payload within Discord's limits (128 characters per text, two buttons)."""
    activity: dict = {
        "details": _limit(game_name or "A Roblox game", 128),
        "timestamps": {"start": int(started_at)},
        "assets": {"large_text": _limit(game_name or "Roblox", 128)},
    }
    if creator:
        activity["state"] = _limit(f"by {creator}", 128)
    if icon_url and icon_url.startswith("https://"):
        activity["assets"]["large_image"] = icon_url
    else:
        activity["assets"]["large_image"] = "roseblox"
    if buttons:
        activity["buttons"] = [
            {"label": _limit(label, 32), "url": url} for label, url in buttons[:2] if url.startswith("https://")
        ]
    return activity


class DiscordPresence:
    """One connection to the Discord app; every method fails quietly by raising ``DiscordError``."""

    def __init__(self, application_id: str, *, env: Mapping[str, str] | None = None, timeout: float = 3.0) -> None:
        if not valid_application_id(application_id):
            raise DiscordError("The Discord application ID must be a number from the Discord Developer Portal.")
        self.application_id = application_id
        self.env = env
        self.timeout = timeout
        self._socket: socket.socket | None = None

    @property
    def connected(self) -> bool:
        return self._socket is not None

    def connect(self) -> None:
        if self._socket is not None:
            return
        last_error: OSError | None = None
        for path in socket_candidates(self.env):
            if not path.exists():
                continue
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(self.timeout)
            try:
                connection.connect(str(path))
                self._socket = connection
                self._send(_HANDSHAKE, {"v": 1, "client_id": self.application_id})
                opcode, reply = self._receive()
            except (OSError, DiscordError) as error:
                connection.close()
                self._socket = None
                last_error = error if isinstance(error, OSError) else OSError(str(error))
                continue
            if opcode == _CLOSE or reply.get("evt") == "ERROR":
                self.close()
                message = reply.get("message") or reply.get("data", {}).get("message") or "Discord refused the connection"
                raise DiscordError(f"Discord refused the application ID: {message}")
            return
        if last_error is not None:
            raise DiscordError(f"Could not connect to Discord: {last_error}")
        raise DiscordError("Discord is not running.")

    def set_activity(self, pid: int, activity: dict | None) -> None:
        if self._socket is None:
            self.connect()
        payload = {"cmd": "SET_ACTIVITY", "args": {"pid": int(pid), "activity": activity}, "nonce": str(uuid.uuid4())}
        try:
            self._send(_FRAME, payload)
            _opcode, reply = self._receive()
        except (OSError, DiscordError) as error:
            self.close()
            raise DiscordError(f"Discord connection lost: {error}") from error
        if reply.get("evt") == "ERROR":
            raise DiscordError(str(reply.get("data", {}).get("message") or "Discord rejected the activity"))

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

    def _send(self, opcode: int, payload: dict) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        assert self._socket is not None
        self._socket.sendall(struct.pack("<II", opcode, len(data)) + data)

    def _receive(self) -> tuple[int, dict]:
        header = self._read_exact(8)
        opcode, length = struct.unpack("<II", header)
        if length > _MAX_PAYLOAD:
            raise DiscordError("Discord sent an oversized reply.")
        try:
            reply = json.loads(self._read_exact(length) or b"{}")
        except (UnicodeError, json.JSONDecodeError) as error:
            raise DiscordError("Discord sent an unreadable reply.") from error
        return opcode, reply if isinstance(reply, dict) else {}

    def _read_exact(self, size: int) -> bytes:
        assert self._socket is not None
        chunks = b""
        while len(chunks) < size:
            chunk = self._socket.recv(size - len(chunks))
            if not chunk:
                raise DiscordError("Discord closed the connection.")
            chunks += chunk
        return chunks
