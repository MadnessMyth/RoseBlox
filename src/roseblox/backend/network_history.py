"""Roblox server connections the activity watcher has seen, for the Networking tab.

The watcher runs as its own process, so the Networking tab in the main window cannot
listen to it directly. This store is the shared record between them: the watcher
appends to it on every ``GameJoined`` — the log marker that says the Player connected
to a server — and the Networking tab reads it. Only public server addresses are kept,
and nothing about the player: an IP, the place it served, where it is, and the
round-trip latency the watcher measured.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .activity import public_address
from .library import default_data_directory

_MAX_SERVERS = 200
_VERSION = 1


@dataclass
class ConnectionRecord:
    """One public Roblox server IP the watcher has seen the Player connect to."""

    address: str
    region: str | None
    best_ping_ms: int | None
    last_ping_ms: int | None
    connections: int
    last_seen: float
    place_id: int | None
    job_id: str | None


@dataclass
class RegionStat:
    """A region aggregated across every server the watcher has located there."""

    region: str
    best_ping_ms: int | None
    connections: int
    last_seen: float


def default_network_history_path() -> Path:
    return default_data_directory() / "network_history.json"


class NetworkHistoryStore:
    """Public Roblox servers the Player connected to, keyed by address.

    Repeated connections to the same server bump its count and refresh ``last_seen``
    rather than adding a row, so the Networking tab reads one entry per IP.
    """

    def __init__(self, path: Path | None = None, *, clock=time.time) -> None:
        self.path = Path(path) if path is not None else default_network_history_path()
        self._clock = clock

    def load(self) -> list[ConnectionRecord]:
        """All recorded servers, newest connection first."""
        servers = self._read().get("servers")
        records: list[ConnectionRecord] = []
        if isinstance(servers, dict):
            for address, raw in servers.items():
                record = _record_from(address, raw)
                if record is not None:
                    records.append(record)
        records.sort(key=lambda item: item.last_seen, reverse=True)
        return records

    def best_regions(self) -> list[RegionStat]:
        """Regions the watcher has located a server in, ranked by their best ping."""
        grouped: dict[str, RegionStat] = {}
        for record in self.load():
            if not record.region:
                continue
            stat = grouped.get(record.region)
            if stat is None:
                grouped[record.region] = RegionStat(
                    region=record.region,
                    best_ping_ms=record.best_ping_ms,
                    connections=record.connections,
                    last_seen=record.last_seen,
                )
            else:
                stat.connections += record.connections
                stat.last_seen = max(stat.last_seen, record.last_seen)
                if record.best_ping_ms is not None and (
                    stat.best_ping_ms is None or record.best_ping_ms < stat.best_ping_ms
                ):
                    stat.best_ping_ms = record.best_ping_ms
        return sorted(grouped.values(), key=_region_sort_key)

    def record(
        self,
        address: str,
        *,
        place_id: int | None = None,
        job_id: str | None = None,
        region: str | None = None,
        ping_ms: int | None = None,
        timestamp: float | None = None,
    ) -> None:
        """Note a connection to ``address``; private or malformed addresses are ignored."""
        checked = public_address(address)
        if checked is None:
            return
        when = timestamp if timestamp is not None else self._clock()
        with self._file_lock():
            document = self._read()
            servers = document.setdefault("servers", {})
            existing = _record_from(checked, servers.get(checked)) or ConnectionRecord(
                address=checked, region=None, best_ping_ms=None, last_ping_ms=None,
                connections=0, last_seen=0.0, place_id=None, job_id=None,
            )
            existing.connections += 1
            existing.last_seen = when
            if ping_ms is not None:
                existing.last_ping_ms = ping_ms
                if existing.best_ping_ms is None or ping_ms < existing.best_ping_ms:
                    existing.best_ping_ms = ping_ms
            if region:
                existing.region = region
            if place_id is not None:
                existing.place_id = place_id
            if job_id is not None:
                existing.job_id = job_id
            servers[checked] = _record_to_dict(existing)
            _prune(servers)
            self._write(document)

    def update_ping(
        self, address: str, *, region: str | None = None, ping_ms: int | None = None
    ) -> None:
        """Refresh a known server's ping (and region if it was missing) without bumping its count."""
        checked = public_address(address)
        if checked is None:
            return
        with self._file_lock():
            document = self._read()
            servers = document.setdefault("servers", {})
            existing = _record_from(checked, servers.get(checked))
            if existing is None:
                return
            if ping_ms is not None:
                existing.last_ping_ms = ping_ms
                if existing.best_ping_ms is None or ping_ms < existing.best_ping_ms:
                    existing.best_ping_ms = ping_ms
            if region:
                existing.region = region
            servers[checked] = _record_to_dict(existing)
            self._write(document)

    @contextlib.contextmanager
    def _file_lock(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            stream = open(self.path.with_name(self.path.name + ".lock"), "a")
        except OSError:
            # A store that cannot be locked still loads and records; only the read-modify-write
            # is then not guarded against a concurrent writer, which the atomic replace absorbs.
            yield
            return
        with stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": _VERSION, "servers": {}}
        except (OSError, UnicodeError, json.JSONDecodeError):
            # A corrupted history is rebuilt from scratch rather than blocking the watcher.
            return {"version": _VERSION, "servers": {}}

    def _write(self, document: dict) -> None:
        temporary_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, delete=False
            ) as stream:
                temporary_path = Path(stream.name)
                json.dump(document, stream, indent=2, sort_keys=True)
                stream.write("\n")
            os.replace(temporary_path, self.path)
        except OSError:
            if temporary_path is not None:
                with contextlib.suppress(OSError):
                    temporary_path.unlink(missing_ok=True)
            raise


def _record_from(address: str, raw: object) -> ConnectionRecord | None:
    if not isinstance(raw, dict):
        return None
    try:
        return ConnectionRecord(
            address=str(address),
            region=_opt_str(raw.get("region")),
            best_ping_ms=_opt_int(raw.get("best_ping_ms")),
            last_ping_ms=_opt_int(raw.get("last_ping_ms")),
            connections=int(raw.get("connections", 0)),
            last_seen=float(raw.get("last_seen", 0.0)),
            place_id=_opt_int(raw.get("place_id")),
            job_id=_opt_str(raw.get("job_id")),
        )
    except (TypeError, ValueError):
        return None


def _record_to_dict(record: ConnectionRecord) -> dict:
    return {
        "region": record.region,
        "best_ping_ms": record.best_ping_ms,
        "last_ping_ms": record.last_ping_ms,
        "connections": record.connections,
        "last_seen": record.last_seen,
        "place_id": record.place_id,
        "job_id": record.job_id,
    }


def _opt_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _opt_str(value: object) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _region_sort_key(stat: RegionStat):
    # A measured ping sorts the region by it; an unknown ping sits last, then by name.
    return (stat.best_ping_ms if stat.best_ping_ms is not None else 1_000_000, stat.region)


def _prune(servers: dict) -> None:
    if len(servers) <= _MAX_SERVERS:
        return
    aging = []
    for address, raw in servers.items():
        record = _record_from(address, raw)
        if record is not None:
            aging.append((record.last_seen, address))
    aging.sort()
    for _seen, address in aging[: len(aging) - _MAX_SERVERS]:
        del servers[address]
