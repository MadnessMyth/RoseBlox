"""A color of its own for the PING row inside a user's MangoHud overlay.

MangoHud draws every stat and all custom text in one text color. The line of its media
player is the exception (``media_player_color``), and its text is the track title of an
MPRIS player on the session bus. So while that row is on screen, the activity watcher is
such a player, and the ping is its title.

It answers only what MangoHud reads, the properties of ``org.mpris.MediaPlayer2.Player``,
and says that it cannot be played or controlled. The ``org.mpris.MediaPlayer2`` interface
with a player's name is left out on purpose: KDE Plasma drops a player it cannot read that
from, and GNOME Shell lists only players that can play, so neither offers this one in its
media controls.

Like the microphone gate, it runs on a thread with its own GLib main loop, because the
watcher's own loop is busy with network requests when MangoHud asks.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Callable

from gi.repository import Gio, GLib

BUS_PREFIX = "org.mpris.MediaPlayer2."
OBJECT_PATH = "/org/mpris/MediaPlayer2"
INTERFACE = "org.mpris.MediaPlayer2.Player"
INTERFACE_XML = f"""
<node>
  <interface name="{INTERFACE}">
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="Metadata" type="a{{sv}}" access="read"/>
    <property name="CanControl" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
  </interface>
</node>
"""
TRACK_ID = "/io/github/roseblox/Roseblox/Ping"
DBUS = ("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus")
DO_NOT_QUEUE = 4
OWNER_REPLIES = (1, 4)
CALL_TIMEOUT_MS = 3000
# MangoHud waits two seconds for the title, and who is asking is looked up before the answer.
ASKER_TIMEOUT_MS = 1000
# MangoHud re-reads its config 100 ms after a change, and shows "(paused)" for a player that left before that.
RELEASE_DELAY_MS = 2000


class PingPlayer:
    """On the session bus between ``show`` and ``hide``; ``name`` is what MangoHud's ``media_player_name`` takes."""

    def __init__(
        self,
        *,
        log: Callable[[str], None] = lambda message: print(f"roseblox ping row: {message}", file=sys.stderr, flush=True),
        connection_factory=lambda: Gio.bus_get_sync(Gio.BusType.SESSION, None),
        release_delay_ms: int = RELEASE_DELAY_MS,
        is_game: Callable[[int], bool] = lambda pid: True,
    ) -> None:
        # One watcher runs per Player, and each row shows its own server's ping.
        self.name = f"roseblox.instance{os.getpid()}"
        self.bus_name = BUS_PREFIX + self.name
        self.log = log
        self.connection_factory = connection_factory
        self.release_delay_ms = release_delay_ms
        self.is_game = is_game
        # How often a process that ``is_game`` asked for the title. MangoHud asks whenever it loads
        # the row. A desktop's media controls ask every new player too, and that says nothing
        # about the row.
        self.reads = 0
        self._askers: dict[str, bool] = {}
        self.context: GLib.MainContext | None = None
        self.loop: GLib.MainLoop | None = None
        self.thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._connection: Gio.DBusConnection | None = None
        self._registration = 0
        self._title = ""
        self._owned = False
        self._release_source: GLib.Source | None = None

    # Called from the watcher's thread.

    def show(self, title: str) -> bool:
        """Whether the player is on the bus with this title."""
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="roseblox-ping-row", daemon=True)
            self.thread.start()
            self._ready.wait(CALL_TIMEOUT_MS / 1000)
        return bool(self._call(lambda: self._show(title)))

    def hide(self) -> None:
        self._call(self._hide)

    def stop(self, timeout: float = 5.0) -> None:
        self._call(self._shutdown)
        if self.thread is not None:
            self.thread.join(timeout)

    def _call(self, function: Callable[[], object]):
        """The result of ``function`` run on the service thread, or ``None`` when that thread is not running."""
        if self.loop is None or not self.loop.is_running():
            return None
        done = threading.Event()
        results = []

        def call(*_data) -> bool:
            try:
                results.append(function())
            finally:
                done.set()
            return GLib.SOURCE_REMOVE

        source = GLib.idle_source_new()
        source.set_callback(call)
        source.attach(self.context)
        done.wait(2 * CALL_TIMEOUT_MS / 1000)
        return results[0] if results else None

    # Everything below runs on the service thread.

    def _run(self) -> None:
        self.context = GLib.MainContext.new()
        self.context.push_thread_default()
        loop = GLib.MainLoop.new(self.context, False)
        try:
            try:
                self._connection = self.connection_factory()
                interface = Gio.DBusNodeInfo.new_for_xml(INTERFACE_XML).interfaces[0]
                self._registration = self._connection.register_object(
                    OBJECT_PATH, interface, None, self._get_property, None,
                )
            except GLib.Error as error:
                self.log(f"no session bus: {error.message}")
                return
            self.loop = loop
            # Dispatched by the loop, so calls made once this is set find it running.
            started = GLib.idle_source_new()
            started.set_callback(self._started)
            started.attach(self.context)
            loop.run()
        finally:
            self._ready.set()
            self.context.pop_thread_default()

    def _started(self, *_data) -> bool:
        self._ready.set()
        return GLib.SOURCE_REMOVE

    def _show(self, title: str) -> bool:
        self._cancel_release()
        changed = title != self._title
        self._title = title
        if not self._owned:
            try:
                reply = self._connection.call_sync(
                    *DBUS, "RequestName", GLib.Variant("(su)", (self.bus_name, DO_NOT_QUEUE)),
                    GLib.VariantType.new("(u)"), Gio.DBusCallFlags.NONE, CALL_TIMEOUT_MS, None,
                )
            except GLib.Error as error:
                self.log(f"could not go on the session bus: {error.message}")
                return False
            self._owned = reply.unpack()[0] in OWNER_REPLIES
            if not self._owned:
                self.log(f"{self.bus_name} is taken")
        elif changed:
            self._connection.emit_signal(
                None, OBJECT_PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                GLib.Variant("(sa{sv}as)", (INTERFACE, {"Metadata": self._metadata()}, [])),
            )
        return self._owned

    def _hide(self) -> None:
        if not self._owned or self._release_source is not None:
            return
        self._release_source = GLib.timeout_source_new(self.release_delay_ms)
        self._release_source.set_callback(self._release)
        self._release_source.attach(self.context)

    def _release(self, *_data) -> bool:
        self._release_source = None
        if self._owned:
            self._owned = False
            try:
                self._connection.call_sync(
                    *DBUS, "ReleaseName", GLib.Variant("(s)", (self.bus_name,)),
                    None, Gio.DBusCallFlags.NONE, CALL_TIMEOUT_MS, None,
                )
            except GLib.Error:
                pass
        return GLib.SOURCE_REMOVE

    def _cancel_release(self) -> None:
        if self._release_source is not None:
            self._release_source.destroy()
            self._release_source = None

    def _shutdown(self) -> None:
        self._cancel_release()
        self._release()
        if self._registration:
            self._connection.unregister_object(self._registration)
            self._registration = 0
        self.loop.quit()

    def _metadata(self) -> GLib.Variant:
        return GLib.Variant("a{sv}", {
            "mpris:trackid": GLib.Variant("o", TRACK_ID),
            "xesam:title": GLib.Variant("s", self._title),
        })

    def _get_property(self, _connection, sender, _path, _interface, name):
        if name == "Metadata":
            if self._from_game(sender):
                self.reads += 1
            return self._metadata()
        if name == "PlaybackStatus":
            return GLib.Variant("s", "Playing")
        return GLib.Variant("b", False)

    def _from_game(self, sender: str | None) -> bool:
        """Whether the process behind a bus connection is the game. One that cannot be told is."""
        if sender not in self._askers:
            try:
                reply = self._connection.call_sync(
                    *DBUS, "GetConnectionUnixProcessID", GLib.Variant("(s)", (sender,)),
                    GLib.VariantType.new("(u)"), Gio.DBusCallFlags.NONE, ASKER_TIMEOUT_MS, None,
                )
                self._askers[sender] = bool(self.is_game(reply.unpack()[0]))
            except (GLib.Error, OSError, TypeError):
                return True
        return self._askers[sender]
