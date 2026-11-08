"""Windowless playtime tracker of one Roblox Player or Studio launch.

It runs as ``roseblox --playtime APP_TYPE PID START_TIME`` so time keeps counting after the Roseblox window closes,
and exits when that Rosemary process does.
"""

from __future__ import annotations

import signal
import sys
import time

from .backend.appimage import appimage_path
from .backend.playtime import APP_TYPES, PlaytimeError, PlaytimeStore
from .backend.rosemary import read_process_snapshot

USAGE = "Usage: roseblox --playtime APP_TYPE PID START_TIME"
POLL_SECONDS = 5.0
# How much playtime a crash or logout can lose.
SAVE_SECONDS = 60.0


def _log(message: str) -> None:
    print(f"roseblox playtime: {message}", file=sys.stderr, flush=True)


class PlaytimeTracker:
    def __init__(
        self,
        app_type: str,
        pid: int,
        start_time: int,
        *,
        store: PlaytimeStore | None = None,
        clock=time.monotonic,
        sleep=time.sleep,
        process_snapshot=read_process_snapshot,
    ) -> None:
        self.app_type = app_type
        self.pid = pid
        self.start_time = start_time
        self.store = store or PlaytimeStore()
        # Monotonic time stops while the computer is suspended, so a sleeping laptop is not counted.
        self.clock = clock
        self.sleep = sleep
        self.process_snapshot = process_snapshot
        self._counted_until: float | None = None

    def alive(self) -> bool:
        snapshot = self.process_snapshot(self.pid)
        return snapshot is not None and snapshot.start_time == self.start_time

    def run(self) -> int:
        if not self.alive():
            return 0
        self._call(self.store.start_session, self.app_type)
        self._counted_until = self.clock()
        try:
            while self.alive():
                self.sleep(POLL_SECONDS)
                if self.clock() - self._counted_until >= SAVE_SECONDS:
                    self.save()
        finally:
            self.save()
        return 0

    def save(self) -> None:
        if self._counted_until is None:
            return
        now = self.clock()
        if self._call(self.store.add_seconds, self.app_type, now - self._counted_until):
            self._counted_until = now

    def _call(self, method, *arguments) -> bool:
        try:
            method(*arguments)
        except PlaytimeError as error:
            _log(str(error))
            return False
        return True


def tracker_arguments(app_type: str, pid: int, start_time: int) -> list[str]:
    executable = appimage_path()
    command = [executable] if executable else [sys.executable, "-m", "roseblox"]
    return [*command, "--playtime", app_type, str(pid), str(start_time)]


def main(argv: list[str]) -> int:
    arguments = [item for item in argv[1:] if item != "--playtime"]
    if (
        len(arguments) != 3
        or arguments[0] not in APP_TYPES
        or not arguments[1].isdigit()
        or not arguments[2].isdigit()
    ):
        print(USAGE, file=sys.stderr)
        return 2
    # Save the time played so far on logout or `kill` too.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    return PlaytimeTracker(arguments[0], int(arguments[1]), int(arguments[2])).run()
