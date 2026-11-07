"""Feral GameMode, asked over D-Bus to tune the system while Roblox runs.

The ``gamemoderun`` wrapper works by preloading a library into the game, which
Roseblox never does; registering the process with the daemon needs nothing in it.
"""

from __future__ import annotations

import os

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

BUS_NAME = "com.feralinteractive.GameMode"
OBJECT_PATH = "/com/feralinteractive/GameMode"
INTERFACE = "com.feralinteractive.GameMode"
TIMEOUT_MS = 2000


class GameModeError(RuntimeError):
    """Raised when GameMode cannot be reached or refuses a request."""


def _bus():
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS") and not (runtime and os.path.exists(os.path.join(runtime, "bus"))):
        # Connecting anyway would make GLib try to autolaunch a bus.
        raise GameModeError("There is no session bus to reach GameMode.")
    try:
        return Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error as error:
        raise GameModeError(f"There is no session bus to reach GameMode: {error.message}") from error


def available(bus=None) -> bool:
    """Whether the GameMode daemon is running or can be started by D-Bus."""
    try:
        bus = bus or _bus()
        for method in ("NameHasOwner", "ListActivatableNames"):
            arguments = GLib.Variant("(s)", (BUS_NAME,)) if method == "NameHasOwner" else None
            reply = bus.call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                method, arguments, None, Gio.DBusCallFlags.NONE, TIMEOUT_MS, None,
            ).unpack()[0]
            if reply is True or (isinstance(reply, list) and BUS_NAME in reply):
                return True
    except (GameModeError, GLib.Error):
        return False
    return False


def register(pid: int, bus=None) -> None:
    _call("RegisterGame", pid, bus)


def unregister(pid: int, bus=None) -> None:
    _call("UnregisterGame", pid, bus)


def _call(method: str, pid: int, bus) -> None:
    bus = bus or _bus()
    try:
        reply = bus.call_sync(
            BUS_NAME, OBJECT_PATH, INTERFACE, method, GLib.Variant("(i)", (pid,)),
            GLib.VariantType.new("(i)"), Gio.DBusCallFlags.NONE, TIMEOUT_MS, None,
        )
    except GLib.Error as error:
        raise GameModeError(f"GameMode is not running or did not answer: {error.message}") from error
    if reply.unpack()[0] < 0:
        raise GameModeError(f"GameMode refused to {'start' if method == 'RegisterGame' else 'stop'} for Roblox.")
