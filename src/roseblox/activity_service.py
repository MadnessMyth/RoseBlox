"""Windowless companion of one Roblox Player launch: Discord presence, server location, close on leave,
and the microphone toggle.

It runs as ``roseblox --activity-watch PID START_TIME LOG [ROBLOX_LOG_DIRECTORY] [--overlay CONFIG]`` so it keeps working
after the Roseblox window closes, and exits when that Rosemary process does.
"""

from __future__ import annotations

import os
import signal
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable

from .backend.appimage import appimage_path
from .backend.activity import ActivityTracker, GameJoined, GameJoining, GameLeft, LogFollower, find_roblox_log
from .backend.discord_rpc import (
    DiscordError,
    DiscordPresence,
    build_activity,
    effective_application_id,
    valid_application_id,
)
from .backend.game_info import GameInfoError, fetch_game, locate_server, ping_server
from .backend.game_overlay import BANNER_SECONDS, GameOverlay, ping_text
from .backend.rosemary import read_process_snapshot
from .backend.setup import SetupSettingsError, SetupStore, WhilePlaying

USAGE = "Usage: roseblox --activity-watch PID START_TIME LOG [ROBLOX_LOG_DIRECTORY] [--overlay CONFIG]"
POLL_SECONDS = 1.0
DISCORD_RETRY_SECONDS = 30.0
LOG_SEARCH_SECONDS = 5.0
LOG_MISSING_SECONDS = 60.0
PING_INTERVAL_SECONDS = 30.0
# Polls MangoHud has to ask the yellow ping row's media player for its title, which it does when it loads the row.
PLAYER_ROW_POLLS = 5


def _log(message: str) -> None:
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    print(f"{stamp} roseblox activity: {message}", file=sys.stderr, flush=True)


def _same_file(first: Path, second: Path) -> bool:
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def process_started_at(start_time: int) -> float | None:
    """When a process started, in seconds since the epoch, from its start time in clock ticks after boot."""
    try:
        for line in Path("/proc/stat").read_text(encoding="ascii").splitlines():
            if line.startswith("btime "):
                return int(line.split()[1]) + start_time / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError):
        pass
    return None


def send_notification(summary: str, body: str) -> None:
    from gi.repository import Gio, GLib

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    bus.call_sync(
        "org.freedesktop.Notifications",
        "/org/freedesktop/Notifications",
        "org.freedesktop.Notifications",
        "Notify",
        GLib.Variant(
            "(susssasa{sv}i)",
            (
                "Roseblox", 0, "io.github.roseblox.Roseblox", summary, body, [],
                {"desktop-entry": GLib.Variant("s", "io.github.roseblox.Roseblox")}, 8000,
            ),
        ),
        GLib.VariantType.new("(u)"),
        Gio.DBusCallFlags.NONE,
        3000,
        None,
    )


class ActivityWatcher:
    def __init__(
        self,
        pid: int,
        start_time: int,
        log_path: Path,
        roblox_logs: Path | None = None,
        *,
        settings: Callable[[], WhilePlaying] | None = None,
        find_log=find_roblox_log,
        presence_factory=DiscordPresence,
        game_lookup=fetch_game,
        locate=locate_server,
        ping=ping_server,
        notify=send_notification,
        clock=time.time,
        sleep=time.sleep,
        process_snapshot=read_process_snapshot,
        kill=os.kill,
        microphone_factory=None,
        overlay: GameOverlay | None = None,
        ping_player_factory=None,
        started_at=process_started_at,
    ) -> None:
        self.pid = pid
        self.start_time = start_time
        self.follower = LogFollower(log_path)
        self.roblox_logs = roblox_logs
        self.find_log = find_log
        self.roblox_follower: LogFollower | None = None
        # Only a log the Player has open is certainly its own; one picked by its age may be another Player's.
        self._roblox_log_held = False
        self._log_search_at = 0.0
        self._reported_missing_log = False
        self.tracker = ActivityTracker()
        self._settings = settings or (lambda: SetupStore().load().while_playing)
        self.presence_factory = presence_factory
        self.game_lookup = game_lookup
        self.locate = locate
        self.ping = ping
        self.notify = notify
        self.clock = clock
        self.sleep = sleep
        self.process_snapshot = process_snapshot
        self.kill = kill
        self.presence: DiscordPresence | None = None
        self.activity: dict | None = None
        self._discord_retry_at = 0.0
        self._games: dict[int, object] = {}
        self._started_at = self.clock()
        launched = started_at(start_time)
        # Logs Roblox started before this Player cannot be its own. Without the process's start time, the watcher's
        # own start stands in for it; that comes a few seconds later, once the launch is verified.
        self._logs_since = self._started_at - 5 if launched is None else launched - 2
        self.microphone_factory = microphone_factory or _microphone_service
        self.microphone = None
        self._microphone_settings: tuple | None = None
        self.overlay = overlay
        self._banner_until: float | None = None
        self._server_address: str | None = None
        self._server_location: str | None = None
        self._last_ping_ms: int | None = None
        self._next_ping_at: float | None = None
        self.ping_player_factory = ping_player_factory or _ping_player
        self.ping_player = None
        self._player_row_on = False
        self._player_row_check: tuple[int, int] | None = None
        self._plain_ping_row = False
        self._last_error: str | None = None

    def settings(self) -> WhilePlaying:
        try:
            return self._settings()
        except SetupSettingsError as error:
            _log(str(error))
            return WhilePlaying()

    def alive(self) -> bool:
        snapshot = self.process_snapshot(self.pid)
        return snapshot is not None and snapshot.start_time == self.start_time

    def run(self) -> int:
        _log(f"watching Roblox Player process {self.pid}")
        try:
            while self.alive():
                self._guarded(self.poll)
                self.sleep(POLL_SECONDS)
            self._guarded(self.poll)
            _log(f"Roblox Player process {self.pid} has ended; stopped watching")
        finally:
            self._clear_presence()
            self._stop_microphone()
            self._finish_overlay()
            self._stop_ping_player()
        return 0

    def _guarded(self, step: Callable, *arguments) -> None:
        """Runs one step of the watcher, logging an unexpected error once instead of stopping: a watcher that
        stopped would miss every later join of this launch."""
        try:
            step(*arguments)
        except Exception:  # noqa: BLE001 - see the docstring
            report = traceback.format_exc().rstrip()
            if report != self._last_error:
                self._last_error = report
                _log(f"unexpected error, still watching:\n{report}")

    def update_microphone(self) -> None:
        settings = self.settings()
        if not settings.microphone_toggle or not self.alive():
            self._stop_microphone()
            return
        wanted = (
            settings.microphone_mode, settings.microphone_release_delay_ms, settings.microphone_inverted, settings.sounds,
        )
        if self.microphone is None:
            self.microphone = self.microphone_factory(*wanted)
            self.microphone.start()
        elif wanted != self._microphone_settings:
            self.microphone.configure(*wanted)
        self._microphone_settings = wanted

    def _stop_microphone(self) -> None:
        if self.microphone is not None:
            self.microphone.stop()
            self.microphone = None
            self._microphone_settings = None

    def _source(self) -> LogFollower:
        if self._roblox_log_held or self.clock() < self._log_search_at:
            return self.roblox_follower or self.follower
        self._log_search_at = self.clock() + LOG_SEARCH_SECONDS
        # Once a log is followed, only the one the Player has open replaces it.
        directory = self.roblox_logs if self.roblox_follower is None else None
        found = self.find_log(self.pid, directory, self._logs_since)
        if found is None:
            if (
                self.roblox_follower is None
                and not self._reported_missing_log
                and self.clock() - self._started_at >= LOG_MISSING_SECONDS
            ):
                self._reported_missing_log = True
                where = "" if self.roblox_logs is None else f" or in {self.roblox_logs}"
                _log(
                    f"no Roblox log found yet among the Player's open files{where}; "
                    "reading Rosemary's output until one appears"
                )
            return self.roblox_follower or self.follower
        held = found.is_symlink()
        self._roblox_log_held = held
        if self.roblox_follower is not None and _same_file(found, self.roblox_follower.path):
            return self.roblox_follower
        try:
            name = os.readlink(found) if held else str(found)
        except OSError:
            name = str(found)
        if self.roblox_follower is None:
            how = "the Player has it open" if held else "the newest Player log since the launch"
            _log(f"following Roblox's log {name} ({how})")
        else:
            _log(f"switching to Roblox's log {name}, which the Player has open")
            if self.tracker.in_game:
                self._guarded(self._handle, GameLeft(False))
            self.tracker = ActivityTracker()
        self.roblox_follower = LogFollower(found)
        return self.roblox_follower

    def poll(self) -> None:
        self._guarded(self.update_microphone)
        if self._banner_until is not None and self.clock() >= self._banner_until:
            self._banner_until = None
            settings = self.settings()
            if settings.server_ping and settings.server_ping_constant and self._last_ping_ms is not None:
                self._show_ping_row(self._last_ping_ms, settings)
            else:
                self._hide_overlay()
        source = self._source()
        if source is not self.follower:
            self.follower.read_lines()
        for line in source.read_lines():
            for event in self.tracker.feed(line):
                # The rest of the lines read in this poll would be lost if one event stopped the loop.
                self._guarded(self._handle, event)
        self._poll_constant_ping()
        self._check_player_row()
        if self.activity is not None and self.presence is None and self.clock() >= self._discord_retry_at:
            self._publish(self.activity)

    def _handle(self, event: GameJoining | GameJoined | GameLeft) -> None:
        if isinstance(event, GameJoining):
            _log(f"joining place {event.place_id}")
        elif isinstance(event, GameJoined):
            _log(f"joined place {event.place_id}")
            self.joined(event)
        elif isinstance(event, GameLeft):
            _log("back on the home screen" if event.to_home else "left the game")
            self.left(event)

    def _measure_ping(self, address: str | None, fallback_ms: int | None = None) -> int | None:
        if address:
            measured = self.ping(address)
            if measured is not None:
                return measured
        return fallback_ms

    def _poll_constant_ping(self) -> None:
        if not self.tracker.in_game:
            return
        settings = self.settings()
        if not settings.server_ping:
            if self._next_ping_at is not None:
                self._next_ping_at = None
                if self.overlay is not None and self._banner_until is None:
                    self._hide_overlay()
            return
        fallback = self.tracker.last_join_latency_ms
        if self._last_ping_ms is None and (self._server_address or fallback is not None):
            ping_ms = self._measure_ping(self._server_address, fallback)
            if ping_ms is not None:
                self._last_ping_ms = ping_ms
                if settings.server_ping_constant and self._server_address:
                    self._next_ping_at = self.clock() + PING_INTERVAL_SECONDS
                self.show_location(None, settings, ping_ms=ping_ms)
                return
        if not settings.server_ping_constant:
            if self._next_ping_at is not None:
                self._next_ping_at = None
                if self.overlay is not None and self._banner_until is None:
                    self._hide_overlay()
            return
        if self._next_ping_at is None and self._server_address:
            self._next_ping_at = self.clock() + PING_INTERVAL_SECONDS
            if self.overlay is not None and self._last_ping_ms is not None and self._banner_until is None:
                self._show_ping_row(self._last_ping_ms, settings)
            return
        if self._server_address is None or self._next_ping_at is None or self.clock() < self._next_ping_at:
            return
        self._next_ping_at = self.clock() + PING_INTERVAL_SECONDS
        measured = self.ping(self._server_address)
        if measured is not None:
            self._last_ping_ms = measured
        if self.overlay is not None and self._last_ping_ms is not None and self._banner_until is None:
            self._show_ping_row(self._last_ping_ms, settings)

    def _show_ping_row(self, ping_ms: int, settings: WhilePlaying) -> bool:
        """The PING row; with the yellow row on, as the title of a media player MangoHud colors by itself."""
        player = self._ping_row_player(ping_ms, settings)
        if not self._overlay_call(self.overlay.show_ping, ping_ms, player):
            self._leave_player_row()
            return False
        if player is None:
            self._leave_player_row()
        elif not self._player_row_on:
            self._player_row_on = True
            self._player_row_check = (PLAYER_ROW_POLLS, self.ping_player.reads)
        return True

    def _ping_row_player(self, ping_ms: int, settings: WhilePlaying) -> str | None:
        """Puts the ping on the session bus first, so MangoHud finds the player when it loads the row."""
        if not settings.server_ping_yellow or self._plain_ping_row or not self.overlay.media_row_fits():
            return None
        if self.ping_player is None:
            self.ping_player = self.ping_player_factory(self._is_game)
        if self.ping_player.show(ping_text(ping_ms)):
            return self.ping_player.name
        self._plain_ping_row = True
        return None

    def _is_game(self, pid: int) -> bool:
        """Whether a process belongs to this launch: MangoHud runs inside the game, and only its
        request for the title says that the row was loaded."""
        return self.overlay.configures(pid) or _descends_from(pid, self.pid)

    def _leave_player_row(self) -> None:
        self._player_row_on = False
        self._player_row_check = None
        if self.ping_player is not None:
            self.ping_player.hide()

    def _check_player_row(self) -> None:
        """A MangoHud that never asks for the title cannot draw the row: it was built without D-Bus, or
        has no way to the session bus. The row is then its clock line for the rest of this launch."""
        if self._player_row_check is None:
            return
        polls, reads = self._player_row_check
        if self.ping_player.reads != reads:
            self._player_row_check = None
        elif polls > 1:
            self._player_row_check = (polls - 1, reads)
        else:
            _log("MangoHud did not ask for the yellow ping row; showing the row in the overlay's text color")
            self._plain_ping_row = True
            if self._last_ping_ms is None:
                self._hide_overlay()
            else:
                self._show_ping_row(self._last_ping_ms, self.settings())

    def _hide_overlay(self) -> bool:
        hidden = self._overlay_call(self.overlay.hide)
        self._leave_player_row()
        return hidden

    def _stop_ping_player(self) -> None:
        if self.ping_player is not None:
            self.ping_player.stop()
            self.ping_player = None

    def joined(self, event: GameJoined) -> None:
        settings = self.settings()
        self._server_address = event.server_address
        self._server_location = None
        self._last_ping_ms = None
        self._next_ping_at = None
        location = None
        if settings.server_location and event.server_address:
            try:
                location = self.locate(event.server_address)
            except GameInfoError as error:
                _log(f"server location: {error}")
            else:
                self._server_location = location
        ping_ms = None
        fallback_ms = event.latency_ms if event.latency_ms is not None else self.tracker.last_join_latency_ms
        if settings.server_ping and (event.server_address or fallback_ms is not None):
            ping_ms = self._measure_ping(event.server_address, fallback_ms)
            self._last_ping_ms = ping_ms
            if settings.server_ping_constant and event.server_address:
                self._next_ping_at = self.clock() + PING_INTERVAL_SECONDS
        if location is not None or ping_ms is not None:
            self.show_location(location, settings, ping_ms=ping_ms)
        application_id = effective_application_id(settings.discord_application_id)
        if not settings.discord_presence or not valid_application_id(application_id):
            return
        details = self._games.get(event.place_id)
        if details is None:
            try:
                details = self.game_lookup(event.place_id)
            except GameInfoError as error:
                _log(f"game details: {error}")
            else:
                self._games[event.place_id] = details
        buttons = []
        if settings.discord_join_button and event.join_url:
            buttons.append(("Join Server", event.join_url))
        buttons.append(("View Game", event.game_url))
        self.activity = build_activity(
            game_name=getattr(details, "name", "") or "A Roblox game",
            creator=getattr(details, "creator", None),
            started_at=int(self.clock()),
            icon_url=getattr(details, "icon_url", None),
            buttons=buttons,
        )
        self._publish(self.activity, application_id)

    def left(self, event: GameLeft) -> None:
        self.activity = None
        self._server_address = None
        self._server_location = None
        self._last_ping_ms = None
        self._next_ping_at = None
        if self.overlay is not None:
            self._banner_until = None
            self._hide_overlay()
        self._clear_presence()
        if event.to_home and self.settings().close_on_leave:
            self.close_roblox()

    def close_roblox(self) -> None:
        if not self.alive():
            return
        try:
            self.kill(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as error:
            _log(f"could not close Roblox: {error}")

    def _publish(self, activity: dict, application_id: str | None = None) -> None:
        try:
            if self.presence is None:
                application_id = application_id or effective_application_id(self.settings().discord_application_id)
                self.presence = self.presence_factory(application_id)
            self.presence.set_activity(self.pid, activity)
        except DiscordError as error:
            _log(f"Discord: {error}")
            if self.presence is not None:
                self.presence.close()
            self.presence = None
            self._discord_retry_at = self.clock() + DISCORD_RETRY_SECONDS

    def _clear_presence(self) -> None:
        if self.presence is None:
            return
        try:
            self.presence.set_activity(self.pid, None)
        except DiscordError:
            pass
        self.presence.close()
        self.presence = None

    def show_location(self, location: str | None, settings: WhilePlaying, *, ping_ms: int | None = None) -> None:
        """Inside the game when its banner is ready, else as a desktop notification."""
        ping_suffix = f" ({ping_ms} ms)" if ping_ms is not None else ""
        if location and ping_ms is not None:
            banner_text = f"Server: {location} · {ping_ms} ms"
            notify_body = f"The server is in {location}{ping_suffix}."
        elif location:
            banner_text = f"Server: {location}"
            notify_body = f"The server is in {location}."
        elif ping_ms is not None:
            banner_text = f"Ping: {ping_ms} ms"
            notify_body = f"Server ping is {ping_ms} ms."
        else:
            return
        wants_in_game = (
            (settings.server_location and settings.server_location_in_game)
            or (settings.server_ping and (settings.server_ping_constant or settings.server_location_in_game))
        )
        if self.overlay is not None and wants_in_game:
            if not location and ping_ms is not None and settings.server_ping and settings.server_ping_constant:
                if self._show_ping_row(ping_ms, settings):
                    self._banner_until = None
                    return
            elif self._overlay_call(self.overlay.show, banner_text):
                self._leave_player_row()
                self._banner_until = self.clock() + BANNER_SECONDS
                return
        self._notify("Connected to a Roblox server", notify_body)

    def _overlay_call(self, method, *arguments) -> bool:
        try:
            method(*arguments)
        except OSError as error:
            _log(f"in-game banner: {error}")
            return False
        return True

    def _finish_overlay(self) -> None:
        # MangoHud falls back to its full default HUD if its config file disappears mid-game.
        if self.overlay is None:
            return
        self._banner_until = None
        self._overlay_call(self.overlay.hide if self.alive() else self.overlay.remove)

    def _notify(self, summary: str, body: str) -> None:
        try:
            self.notify(summary, body)
        except Exception as error:  # noqa: BLE001 - a missing notification server must not stop the watcher
            _log(f"notification: {error}")


def _microphone_service(mode: str, release_delay_ms: int, inverted: bool, sounds):
    from .microphone_service import MicrophoneService

    return MicrophoneService(mode, release_delay_ms, inverted, sounds)


def _descends_from(pid: int, ancestor: int) -> bool:
    """Whether process ``pid`` is ``ancestor`` or was started by it, however many processes in between."""
    for _generation in range(64):
        if pid == ancestor:
            return True
        if pid <= 1:
            return False
        try:
            stat = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii", errors="replace")
            # The comm field is parenthesized and may itself contain spaces or ')'.
            pid = int(stat[stat.rfind(")") + 2 :].split()[1])  # field 4 in proc_pid_stat(5)
        except (OSError, ValueError, IndexError):
            return False
    return False


def _ping_player(is_game):
    from .backend.ping_player import PingPlayer

    return PingPlayer(log=lambda message: _log(f"yellow ping row: {message}"), is_game=is_game)


def watcher_arguments(
    pid: int, start_time: int, log_path: Path, roblox_logs: Path | None = None, *, overlay: Path | None = None
) -> list[str]:
    executable = appimage_path()
    command = [executable] if executable else [sys.executable, "-m", "roseblox"]
    arguments = [*command, "--activity-watch", str(pid), str(start_time), str(log_path)]
    if roblox_logs is not None:
        arguments.append(str(roblox_logs))
    return arguments if overlay is None else [*arguments, "--overlay", str(overlay)]


def main(argv: list[str]) -> int:
    arguments = [item for item in argv[1:] if item != "--activity-watch"]
    overlay = None
    if "--overlay" in arguments:
        index = arguments.index("--overlay")
        if index + 1 >= len(arguments):
            print(USAGE, file=sys.stderr)
            return 2
        overlay = GameOverlay(Path(arguments[index + 1]))
        del arguments[index:index + 2]
    if len(arguments) not in (3, 4) or not arguments[0].isdigit() or not arguments[1].isdigit():
        print(USAGE, file=sys.stderr)
        return 2
    pid, start_time, log_path = int(arguments[0]), int(arguments[1]), Path(arguments[2])
    roblox_logs = Path(arguments[3]) if len(arguments) == 4 else None
    # Unwind on logout or `kill` too, so a muted Roblox microphone is opened again.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    return ActivityWatcher(pid, start_time, log_path, roblox_logs, overlay=overlay).run()
