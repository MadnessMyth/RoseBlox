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
from pathlib import Path
from typing import Callable

from .backend.appimage import appimage_path
from .backend.activity import ActivityTracker, GameJoined, GameLeft, LogFollower, find_roblox_log
from .backend.discord_rpc import (
    DiscordError,
    DiscordPresence,
    build_activity,
    effective_application_id,
    valid_application_id,
)
from .backend.game_info import GameInfoError, fetch_game, locate_server
from .backend.game_overlay import BANNER_SECONDS, GameOverlay
from .backend.rosemary import read_process_snapshot
from .backend.setup import SetupSettingsError, SetupStore, WhilePlaying

USAGE = "Usage: roseblox --activity-watch PID START_TIME LOG [ROBLOX_LOG_DIRECTORY] [--overlay CONFIG]"
POLL_SECONDS = 1.0
DISCORD_RETRY_SECONDS = 30.0
LOG_SEARCH_SECONDS = 5.0


def _log(message: str) -> None:
    print(f"roseblox activity: {message}", file=sys.stderr, flush=True)


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
        notify=send_notification,
        clock=time.time,
        sleep=time.sleep,
        process_snapshot=read_process_snapshot,
        kill=os.kill,
        microphone_factory=None,
        overlay: GameOverlay | None = None,
    ) -> None:
        self.pid = pid
        self.start_time = start_time
        self.follower = LogFollower(log_path)
        self.roblox_logs = roblox_logs
        self.find_log = find_log
        self.roblox_follower: LogFollower | None = None
        self._log_search_at = 0.0
        self.tracker = ActivityTracker()
        self._settings = settings or (lambda: SetupStore().load().while_playing)
        self.presence_factory = presence_factory
        self.game_lookup = game_lookup
        self.locate = locate
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
        self.microphone_factory = microphone_factory or _microphone_service
        self.microphone = None
        self._microphone_settings: tuple | None = None
        self.overlay = overlay
        self._banner_until: float | None = None

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
                self.poll()
                self.sleep(POLL_SECONDS)
            self.poll()
        finally:
            self._clear_presence()
            self._stop_microphone()
            self._finish_overlay()
        return 0

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
        if self.roblox_follower is None and self.clock() >= self._log_search_at:
            self._log_search_at = self.clock() + LOG_SEARCH_SECONDS
            found = self.find_log(self.pid, self.roblox_logs, self._started_at - 5)
            if found is not None:
                try:
                    name = os.readlink(found) if found.is_symlink() else str(found)
                except OSError:
                    name = str(found)
                _log(f"following Roblox's log {name}")
                self.roblox_follower = LogFollower(found)
        return self.roblox_follower or self.follower

    def poll(self) -> None:
        self.update_microphone()
        if self._banner_until is not None and self.clock() >= self._banner_until:
            self._banner_until = None
            self._overlay_call(self.overlay.hide)
        source = self._source()
        if source is not self.follower:
            self.follower.read_lines()
        for line in source.read_lines():
            for event in self.tracker.feed(line):
                if isinstance(event, GameJoined):
                    _log(f"joined place {event.place_id}")
                    self.joined(event)
                elif isinstance(event, GameLeft):
                    self.left(event)
        if self.activity is not None and self.presence is None and self.clock() >= self._discord_retry_at:
            self._publish(self.activity)

    def joined(self, event: GameJoined) -> None:
        settings = self.settings()
        if settings.server_location and event.server_address:
            try:
                location = self.locate(event.server_address)
            except GameInfoError as error:
                _log(f"server location: {error}")
            else:
                self.show_location(location, settings)
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

    def show_location(self, location: str, settings: WhilePlaying) -> None:
        """Inside the game when its banner is ready, else as a desktop notification."""
        if (
            self.overlay is not None and settings.server_location_in_game
            and self._overlay_call(self.overlay.show, f"Server: {location}")
        ):
            self._banner_until = self.clock() + BANNER_SECONDS
            return
        self._notify("Connected to a Roblox server", f"The server is in {location}.")

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
