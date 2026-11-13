"""Runs the microphone gate next to a Roblox Player and answers ``roseblox --mic``.

The activity watcher starts it on a thread with its own GLib main loop. Each
watcher queues for one session-bus name, so with several Players open exactly
one gate is in charge and the next one takes over when its Player closes.
"""

from __future__ import annotations

import sys
import threading
from typing import Callable

from gi.repository import Gio, GLib

from .backend.global_shortcuts import (
    MICROPHONE_DESCRIPTION,
    MICROPHONE_SHORTCUT,
    ShortcutsSession,
    open_connection,
)
from .backend.microphone import MicrophoneGate, detect_backend, microphone_problem
from .backend.microphone_sounds import OFF, ON, MicrophoneSounds, SoundPlayer

APP_ID = "io.github.roseblox.Roseblox"
BUS_NAME = "io.github.roseblox.Roseblox.Microphone"
OBJECT_PATH = "/io/github/roseblox/Roseblox/Microphone"
INTERFACE = "io.github.roseblox.Roseblox.Microphone"
INTERFACE_XML = f"""
<node>
  <interface name="{INTERFACE}">
    <method name="Toggle"/>
    <method name="Press"/>
    <method name="Release"/>
    <method name="SetOpen"><arg name="open" type="b" direction="in"/></method>
    <property name="Open" type="b" access="read"/>
    <property name="Mode" type="s" access="read"/>
    <property name="Inverted" type="b" access="read"/>
    <property name="Hotkey" type="s" access="read"/>
  </interface>
</node>
"""
SYNC_DELAY_MS = 15
MONITOR_RESTART_MS = 2000
COMMANDS = ("toggle", "press", "release", "on", "off", "status")
MIC_USAGE = "Usage: roseblox --mic toggle|press|release|on|off|status"
NOT_RUNNING = (
    "The microphone toggle is not running. It runs while Roblox Player is open with "
    "Settings > While Playing > Microphone toggle turned on."
)


class MicrophoneService:
    def __init__(
        self,
        mode: str,
        release_delay_ms: int,
        inverted: bool = False,
        sounds: MicrophoneSounds | None = None,
        *,
        log: Callable[[str], None] = lambda message: print(f"roseblox microphone: {message}", file=sys.stderr, flush=True),
        backend_factory=detect_backend,
        connection_factory=lambda: open_connection(APP_ID),
        shortcuts_factory=ShortcutsSession,
        player: SoundPlayer | None = None,
    ) -> None:
        self.mode = mode
        self.release_delay_ms = release_delay_ms
        self.inverted = inverted
        self.sounds = sounds or MicrophoneSounds()
        self.player = player or SoundPlayer(log=log)
        self.log = log
        self.backend_factory = backend_factory
        self.connection_factory = connection_factory
        self.shortcuts_factory = shortcuts_factory
        self.context: GLib.MainContext | None = None
        self.loop: GLib.MainLoop | None = None
        self.thread: threading.Thread | None = None
        self.gate: MicrophoneGate | None = None
        self.hotkey = ""
        self._ready = threading.Event()
        self._connection: Gio.DBusConnection | None = None
        self._owner_id = 0
        self._registration = 0
        self._shortcuts: ShortcutsSession | None = None
        self._monitor: Gio.Subprocess | None = None
        self._monitor_cancel: Gio.Cancellable | None = None
        self._sync_source: GLib.Source | None = None
        self._restart_source: GLib.Source | None = None

    # Called from the watcher's thread.

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name="roseblox-microphone", daemon=True)
        self.thread.start()
        self._ready.wait(5)

    def configure(
        self, mode: str, release_delay_ms: int, inverted: bool = False, sounds: MicrophoneSounds | None = None
    ) -> None:
        self._invoke(lambda: self._configure(mode, release_delay_ms, inverted, sounds or MicrophoneSounds()))

    def stop(self, timeout: float = 5.0) -> None:
        self._invoke(self._shutdown)
        if self.thread is not None:
            self.thread.join(timeout)

    def _invoke(self, function: Callable[[], None]) -> None:
        if self.context is None:
            return

        def call(*_data) -> bool:
            function()
            return GLib.SOURCE_REMOVE

        # GDBus dispatches method calls at PRIORITY_DEFAULT, and sources of one priority run in the
        # order they were attached, so a `roseblox --mic` call made after this sees its effect.
        source = GLib.idle_source_new()
        source.set_priority(GLib.PRIORITY_DEFAULT)
        source.set_callback(call)
        source.attach(self.context)

    # Everything below runs on the service thread.

    def _run(self) -> None:
        self.context = GLib.MainContext.new()
        self.context.push_thread_default()
        self.loop = GLib.MainLoop.new(self.context, False)
        try:
            try:
                self._connection = self.connection_factory()
            except GLib.Error as error:
                self.log(f"no session bus: {error.message}")
                return
            node = Gio.DBusNodeInfo.new_for_xml(INTERFACE_XML)
            self._interface = node.interfaces[0]
            self._owner_id = Gio.bus_own_name_on_connection(
                self._connection, BUS_NAME, Gio.BusNameOwnerFlags.NONE, self._acquired, self._lost,
            )
            self._ready.set()
            self.loop.run()
        finally:
            self._ready.set()
            self.context.pop_thread_default()

    def _acquired(self, connection, _name) -> None:
        backend = self.backend_factory()
        if backend is None:
            self.log("neither pactl nor pw-dump and wpctl were found; the microphone cannot be muted")
            return
        self.log(f"in charge of Roblox's microphone ({backend.name}, {self.mode} mode)")
        self.gate = MicrophoneGate(
            backend, self.mode, self.release_delay_ms, self.inverted,
            schedule=self._schedule, cancel=self._cancel, log=self.log, changed=self._open_changed,
        )
        self._registration = connection.register_object(
            OBJECT_PATH, self._interface, self._method_call, self._get_property, None,
        )
        self._start_monitor()
        self.gate.sync()
        self._shortcuts = self.shortcuts_factory(
            connection, on_activated=self._activated, on_deactivated=self._deactivated, on_changed=self._hotkey_changed,
        )
        self._shortcuts.start([(MICROPHONE_SHORTCUT, MICROPHONE_DESCRIPTION)], self._shortcuts_bound)

    def _lost(self, _connection, _name) -> None:
        # Called with no connection when the bus goes away, and while queued behind another gate.
        if self.gate is not None:
            self._release_control()

    def _release_control(self) -> None:
        if self._shortcuts is not None:
            self._shortcuts.close()
            self._shortcuts = None
        self._stop_monitor()
        if self._registration and self._connection is not None:
            self._connection.unregister_object(self._registration)
            self._registration = 0
        if self.gate is not None:
            self.gate.stop()
            self.gate = None

    def _shutdown(self) -> None:
        self._release_control()
        if self._owner_id:
            Gio.bus_unown_name(self._owner_id)
            self._owner_id = 0
        if self._connection is not None:
            try:
                self._connection.flush_sync(None)
            except GLib.Error:
                pass
        if self.loop is not None:
            self.loop.quit()

    def _configure(self, mode: str, release_delay_ms: int, inverted: bool, sounds: MicrophoneSounds) -> None:
        self.mode, self.release_delay_ms, self.inverted, self.sounds = mode, release_delay_ms, inverted, sounds
        if self.gate is not None:
            try:
                self.gate.configure(mode, release_delay_ms, inverted)
            except ValueError as error:
                self.log(str(error))

    def _shortcuts_bound(self, shortcuts, error) -> None:
        if error:
            self.log(f"{error} Bind a key to `roseblox --mic toggle` in your desktop's keyboard settings instead.")
            return
        self._hotkey_changed(shortcuts)

    def _hotkey_changed(self, shortcuts) -> None:
        for shortcut in shortcuts or []:
            if shortcut.id == MICROPHONE_SHORTCUT:
                self.hotkey = shortcut.trigger
                self.log(f"hotkey: {shortcut.trigger or 'not set yet'}")

    def _activated(self, identifier: str) -> None:
        if identifier == MICROPHONE_SHORTCUT and self.gate is not None:
            self.gate.press()

    def _deactivated(self, identifier: str) -> None:
        if identifier == MICROPHONE_SHORTCUT and self.gate is not None:
            self.gate.release()

    def _open_changed(self, open_: bool) -> None:
        self.player.play(self.sounds, ON if open_ else OFF)
        if self._connection is None or not self._registration:
            return
        self._connection.emit_signal(
            None, OBJECT_PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
            GLib.Variant("(sa{sv}as)", (INTERFACE, {"Open": GLib.Variant("b", open_)}, [])),
        )

    def _method_call(self, _connection, _sender, _path, _interface, method, parameters, invocation) -> None:
        gate = self.gate
        if gate is None:
            invocation.return_dbus_error(f"{INTERFACE}.Error.NotReady", "The microphone toggle is not ready.")
            return
        if method == "Toggle":
            gate.toggle()
        elif method == "Press":
            gate.press()
        elif method == "Release":
            gate.release()
        elif method == "SetOpen":
            gate.set_open(parameters.unpack()[0])
        invocation.return_value(None)

    def _get_property(self, _connection, _sender, _path, _interface, name):
        if name == "Open":
            return GLib.Variant("b", bool(self.gate and self.gate.open))
        if name == "Mode":
            return GLib.Variant("s", self.mode)
        if name == "Inverted":
            return GLib.Variant("b", self.inverted)
        return GLib.Variant("s", self.hotkey)

    def _schedule(self, milliseconds: int, callback: Callable[[], None]) -> GLib.Source:
        source = GLib.timeout_source_new(milliseconds)

        def fire(*_data) -> bool:
            callback()
            return GLib.SOURCE_REMOVE

        source.set_callback(fire)
        source.attach(self.context)
        return source

    @staticmethod
    def _cancel(source: GLib.Source) -> None:
        if not source.is_destroyed():
            source.destroy()

    def _start_monitor(self) -> None:
        self._restart_source = None
        if self.gate is None:
            return
        try:
            self._monitor = Gio.Subprocess.new(
                self.gate.backend.monitor_command,
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE,
            )
        except GLib.Error as error:
            self.log(f"cannot watch for new streams: {error.message}")
            self._restart_source = self._schedule(MONITOR_RESTART_MS, self._start_monitor)
            return
        self._monitor_cancel = Gio.Cancellable()
        stream = Gio.DataInputStream.new(self._monitor.get_stdout_pipe())
        stream.read_line_async(GLib.PRIORITY_DEFAULT, self._monitor_cancel, self._monitor_line, stream)

    def _monitor_line(self, stream, result, _data=None) -> None:
        try:
            line, _length = stream.read_line_finish_utf8(result)
        except GLib.Error:
            return
        if self.gate is None:
            return
        if line is None:
            # The sound server restarted or the tool exited; streams may have changed meanwhile.
            self.log("stopped receiving sound server events; reconnecting")
            self._monitor = None
            self._sync_soon()
            self._restart_source = self._schedule(MONITOR_RESTART_MS, self._start_monitor)
            return
        if self.gate.backend.relevant(line):
            self._sync_soon()
        stream.read_line_async(GLib.PRIORITY_DEFAULT, self._monitor_cancel, self._monitor_line, stream)

    def _sync_soon(self) -> None:
        if self._sync_source is not None:
            return

        def sync() -> None:
            self._sync_source = None
            if self.gate is not None:
                self.gate.sync()

        self._sync_source = self._schedule(SYNC_DELAY_MS, sync)

    def _stop_monitor(self) -> None:
        for name in ("_sync_source", "_restart_source"):
            source = getattr(self, name)
            if source is not None:
                self._cancel(source)
                setattr(self, name, None)
        if self._monitor_cancel is not None:
            self._monitor_cancel.cancel()
            self._monitor_cancel = None
        if self._monitor is not None:
            self._monitor.force_exit()
            self._monitor = None


def command_main(argv: list[str], bus: Gio.DBusConnection | None = None) -> int:
    """``roseblox --mic COMMAND`` for desktops without global shortcuts, or for scripts."""
    arguments = [item for item in argv[1:] if item != "--mic"]
    if len(arguments) != 1 or arguments[0] not in COMMANDS:
        print(MIC_USAGE, file=sys.stderr)
        return 2
    command = arguments[0]
    try:
        bus = bus or Gio.bus_get_sync(Gio.BusType.SESSION, None)
        if command == "status":
            reply = bus.call_sync(
                BUS_NAME, OBJECT_PATH, "org.freedesktop.DBus.Properties", "GetAll", GLib.Variant("(s)", (INTERFACE,)),
                GLib.VariantType.new("(a{sv})"), Gio.DBusCallFlags.NO_AUTO_START, 3000, None,
            )
            properties = reply.unpack()[0]
            state = "open" if properties.get("Open") else "muted"
            hotkey = properties.get("Hotkey") or "none"
            mode = properties.get("Mode", "toggle")
            if mode == "hold" and properties.get("Inverted"):
                mode = "hold to mute"
            print(f"{state} ({mode} mode, hotkey: {hotkey})")
            return 0
        method, parameters = {
            "toggle": ("Toggle", None),
            "press": ("Press", None),
            "release": ("Release", None),
            "on": ("SetOpen", GLib.Variant("(b)", (True,))),
            "off": ("SetOpen", GLib.Variant("(b)", (False,))),
        }[command]
        bus.call_sync(BUS_NAME, OBJECT_PATH, INTERFACE, method, parameters, None,
                      Gio.DBusCallFlags.NO_AUTO_START, 3000, None)
    except GLib.Error as error:
        if Gio.DBusError.is_remote_error(error) and Gio.DBusError.get_remote_error(error) in (
            "org.freedesktop.DBus.Error.ServiceUnknown", "org.freedesktop.DBus.Error.NameHasNoOwner",
        ):
            problem = microphone_problem()
            print(NOT_RUNNING if problem is None else f"{NOT_RUNNING}\n{problem}", file=sys.stderr)
        else:
            print(f"roseblox --mic: {error.message}", file=sys.stderr)
        return 1
    return 0
