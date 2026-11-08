"""How long Roblox Player and Roblox Studio have run from Roseblox."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

APP_TYPES = ("player", "studio")
VERSION = 1


class PlaytimeError(Exception):
    pass


@dataclass(frozen=True)
class Playtime:
    seconds: int = 0
    sessions: int = 0
    last_played: datetime | None = None


def default_playtime_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    data = Path(env.get("XDG_DATA_HOME") or Path(env.get("HOME") or Path.home()) / ".local" / "share")
    return data / "roseblox" / "playtime.json"


class PlaytimeStore:
    """Totals per application. Background trackers of separate launches write to it at the same time."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_playtime_path()

    def load(self) -> dict[str, Playtime]:
        return _parse(self._read())

    def get(self, app_type: str) -> Playtime:
        return self.load()[_checked(app_type)]

    def start_session(self, app_type: str, now: datetime | None = None) -> None:
        app_type = _checked(app_type)
        with self._locked() as document:
            entry = document.setdefault(app_type, {})
            entry["sessions"] = _count(entry.get("sessions")) + 1
            entry["last_played"] = _timestamp(now)

    def add_seconds(self, app_type: str, seconds: float, now: datetime | None = None) -> None:
        app_type = _checked(app_type)
        whole = int(seconds)
        if whole <= 0:
            return
        with self._locked() as document:
            entry = document.setdefault(app_type, {})
            entry["seconds"] = _count(entry.get("seconds")) + whole
            entry["last_played"] = _timestamp(now)

    def reset(self, app_type: str) -> None:
        app_type = _checked(app_type)
        with self._locked() as document:
            document.pop(app_type, None)

    def _read(self) -> dict:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as error:
            raise PlaytimeError(f"Could not read playtime: {error}") from error
        try:
            document = json.loads(text)
        except json.JSONDecodeError as error:
            raise PlaytimeError(f"The playtime file is not valid JSON: {error}") from error
        if not isinstance(document, dict):
            raise PlaytimeError("The playtime file is not a JSON object.")
        return document

    @contextmanager
    def _locked(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock = open(self.path.with_name(self.path.name + ".lock"), "a")
        except OSError as error:
            raise PlaytimeError(f"Could not save playtime: {error}") from error
        with lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                document = self._read()
            except PlaytimeError:
                # A damaged file would otherwise stop playtime from ever being saved again.
                document = {}
            document = {key: dict(value) for key, value in document.items() if key in APP_TYPES and isinstance(value, dict)}
            yield document
            document["version"] = VERSION
            try:
                _write_json(self.path, document)
            except OSError as error:
                raise PlaytimeError(f"Could not save playtime: {error}") from error


def format_duration(seconds: int) -> str:
    minutes = max(0, int(seconds)) // 60
    if minutes == 0:
        return "less than a minute"
    hours, minutes = divmod(minutes, 60)
    if hours == 0:
        return f"{minutes} min"
    return f"{hours} h" if minutes == 0 else f"{hours} h {minutes} min"


def describe(playtime: Playtime, app_title: str, now: datetime | None = None) -> str:
    if playtime.sessions == 0 and playtime.seconds == 0:
        return f"You have not played {app_title} from Roseblox yet."
    sessions = "1 session" if playtime.sessions == 1 else f"{playtime.sessions} sessions"
    text = f"Played for {format_duration(playtime.seconds)} in {sessions}."
    if playtime.last_played is not None:
        text += f" Last played {_relative_day(playtime.last_played, now)}."
    return text


def _relative_day(moment: datetime, now: datetime | None) -> str:
    local = moment.astimezone()
    today = (now or datetime.now(timezone.utc)).astimezone().date()
    days = (today - local.date()).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    return f"on {local.strftime('%d %b %Y').lstrip('0')}"


def _parse(document: dict) -> dict[str, Playtime]:
    result = {}
    for app_type in APP_TYPES:
        entry = document.get(app_type)
        if not isinstance(entry, dict):
            result[app_type] = Playtime()
            continue
        result[app_type] = Playtime(
            _count(entry.get("seconds")), _count(entry.get("sessions")), _parse_timestamp(entry.get("last_played"))
        )
    return result


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _parse_timestamp(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _timestamp(now: datetime | None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


def _checked(app_type: str) -> str:
    if app_type not in APP_TYPES:
        raise ValueError(f"Unknown application type: {app_type!r}")
    return app_type


def _write_json(path: Path, document: dict) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        json.dump(document, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(stream.name, path)
