"""Game joins and leaves, read from the Roblox Player's log output.

The Mac Player writes its log to ``~/Library/Logs/Roblox`` in the guest rather
than to its output, so the watcher follows the log file the Player has open and
reads Rosemary's launch log only until that file is found. The markers below
are the ones Roblox has printed for years and that Bloxstrap's activity watcher
also relies on.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .measure import process_tree

_JOIN = re.compile(
    r"! Joining game '(?P<job>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})'"
    r" place (?P<place>\d{1,20}) at (?P<address>[0-9A-Fa-f.:]+)"
)
_SERVER_ADDRESS = re.compile(
    r"(?:UDMUX Address = |Connected to server at |serverId: |Replicator created for player )"
    r"(?P<address>[0-9A-Fa-f.:]+?)(?:[|,: ]|$)"
)
_PRIVATE_REFERRALS = ("RequestPrivateGame", "GameDetailPageJSHybridEvent")
_ROBLOX_LOG = re.compile(r"/Logs/Roblox/[^/]+\.log$")
_LOG_UPTIME = re.compile(r"^\d{4}-\d{2}-\d{2}T[^,]+,(?P<uptime>\d+(?:\.\d+)?),")
_MAX_LINE = 16 * 1024
_MAX_READ = 4 * 1024 * 1024


@dataclass(frozen=True)
class GameJoining:
    """The Player started connecting to a server; ``GameJoined`` follows once the connection is up."""

    place_id: int
    job_id: str


@dataclass(frozen=True)
class GameJoined:
    place_id: int
    job_id: str
    server_address: str | None
    private: bool
    latency_ms: int | None = None

    @property
    def game_url(self) -> str:
        return f"https://www.roblox.com/games/{self.place_id}"

    @property
    def join_url(self) -> str | None:
        """Roblox's own web link that opens this server; private servers cannot be joined this way."""
        if self.private:
            return None
        return f"https://www.roblox.com/games/start?placeId={self.place_id}&gameInstanceId={self.job_id}"


@dataclass(frozen=True)
class GameLeft:
    """``to_home`` is True when the player went back to the home screen rather than being disconnected."""

    to_home: bool


def public_address(text: str) -> str | None:
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return None
    if address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified:
        return None
    if address.is_multicast or address.is_reserved:
        return None
    return str(address)


def _log_uptime(line: str) -> float | None:
    match = _LOG_UPTIME.match(line)
    if not match:
        return None
    try:
        return float(match["uptime"])
    except ValueError:
        return None


class ActivityTracker:
    """Turns log lines into ``GameJoining``, ``GameJoined`` and ``GameLeft`` events.

    A join is reported once the connection is up ("Replicator created:"), so the
    public server address and whether the server is private are known by then.
    """

    def __init__(self) -> None:
        self._pending: dict | None = None
        self._join_uptime: float | None = None
        self.last_join_latency_ms: int | None = None
        self.in_game = False

    def feed(self, line: str) -> list[GameJoining | GameJoined | GameLeft]:
        if "! Joining game '" in line:
            match = _JOIN.search(line)
            if not match:
                return []
            self._pending = {
                "place_id": int(match["place"]),
                "job_id": match["job"].lower(),
                "server_address": public_address(match["address"]),
                "private": False,
            }
            self._join_uptime = _log_uptime(line)
            self.last_join_latency_ms = None
            return [GameJoining(self._pending["place_id"], self._pending["job_id"])]
        if "referral_page:" in line and any(marker in line for marker in _PRIVATE_REFERRALS):
            if self._pending is not None:
                self._pending["private"] = True
            return []
        if "Replicator created:" in line:
            if self._pending is None:
                return []
            uptime = _log_uptime(line)
            if self._join_uptime is not None and uptime is not None and uptime > self._join_uptime:
                # RakNet's connection handshake between UDMUX Address and Replicator created is ~2 RTTs.
                estimate = round((uptime - self._join_uptime) * 1000 / 2)
                if 1 <= estimate <= 2000:
                    self.last_join_latency_ms = estimate
            joined = GameJoined(**self._pending)
            self._pending = None
            self.in_game = True
            return [joined]
        match = _SERVER_ADDRESS.search(line)
        if match:
            address = public_address(match["address"])
            if address and self._pending is not None:
                self._pending["server_address"] = address
                if "UDMUX Address = " in line:
                    uptime = _log_uptime(line)
                    if uptime is not None:
                        self._join_uptime = uptime
            return []
        if "returnToLuaApp: (stage:UGCGame)" in line:
            return self._left(to_home=True)
        if "Time to disconnect replication data:" in line:
            # The previous server's replicator is torn down on another thread, so on a teleport or a join from
            # inside a game this line can come after the next "Joining game". No replicator exists yet for that
            # join, so the line cannot be about it.
            return self._left(to_home=False, keep_joining=True)
        if "leaveUGCGameInternal" in line:
            return self._left(to_home=False)
        return []

    def _left(self, *, to_home: bool, keep_joining: bool = False) -> list[GameLeft]:
        if not keep_joining:
            self._pending = None
            self._join_uptime = None
        if not self.in_game and not to_home:
            return []
        self.in_game = False
        return [GameLeft(to_home)]


def find_roblox_log(
    pid: int,
    directory: Path | None,
    since: float,
    *,
    proc: Path = Path("/proc"),
    tree: Callable[..., list[int]] = process_tree,
) -> Path | None:
    """The log file this Player has open, or failing that the newest one written since it started.

    The open file is reached through ``/proc/<pid>/fd`` so each of several Players
    finds its own log, whatever the guest calls the path.
    """
    for member in tree(pid, proc):
        fd_directory = proc / str(member) / "fd"
        try:
            entries = sorted(fd_directory.iterdir(), key=lambda entry: int(entry.name) if entry.name.isdigit() else 0)
        except OSError:
            continue
        for entry in entries:
            try:
                target = os.readlink(entry)
            except OSError:
                continue
            if _ROBLOX_LOG.search(target) and "CrashHandler" not in target and not target.endswith(" (deleted)"):
                return entry
    if directory is None:
        return None
    newest: tuple[float, Path] | None = None
    try:
        candidates = list(directory.iterdir())
    except OSError:
        return None
    for candidate in candidates:
        # The crash handler's log is named after the Player too, e.g. "..._Player_bc545_CrashHandler_last.log".
        if candidate.suffix != ".log" or "Player" not in candidate.name or "CrashHandler" in candidate.name:
            continue
        try:
            modified = candidate.stat().st_mtime
        except OSError:
            continue
        if modified >= since and (newest is None or modified > newest[0]):
            newest = (modified, candidate)
    return None if newest is None else newest[1]


class LogFollower:
    """Reads lines appended to a log file, tolerating truncation and invalid bytes."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.offset = 0
        self._partial = b""

    def read_lines(self) -> list[str]:
        try:
            with self.path.open("rb") as stream:
                size = stream.seek(0, 2)
                if size < self.offset:
                    self.offset = 0
                    self._partial = b""
                stream.seek(self.offset)
                data = stream.read(_MAX_READ)
        except OSError:
            return []
        self.offset += len(data)
        data = self._partial + data
        *complete, self._partial = data.split(b"\n")
        if len(self._partial) > _MAX_LINE:
            self._partial = b""
        return [line[:_MAX_LINE].decode("utf-8", errors="replace").rstrip("\r") for line in complete]
