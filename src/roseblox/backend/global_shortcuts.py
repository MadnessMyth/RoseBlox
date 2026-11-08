"""Desktop-wide hotkeys through the xdg-desktop-portal GlobalShortcuts interface.

KDE Plasma, GNOME 48+ and Hyprland implement it. The desktop owns the key
binding and the settings dialog; the app only names its shortcuts and hears
when one is pressed (``Activated``) and released (``Deactivated``).

Everything runs on the thread-default main context of the caller, so the
window and the background microphone service can each use it.
"""

from __future__ import annotations

import itertools
import os
import secrets
from dataclasses import dataclass
from typing import Callable

from gi.repository import Gio, GLib

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SHORTCUTS_INTERFACE = "org.freedesktop.portal.GlobalShortcuts"
REQUEST_INTERFACE = "org.freedesktop.portal.Request"
SESSION_INTERFACE = "org.freedesktop.portal.Session"
REGISTRY_INTERFACE = "org.freedesktop.host.portal.Registry"
CALL_TIMEOUT_MS = 5000
# Binding can wait on a dialog the user has to answer.
BIND_TIMEOUT_MS = 10 * 60 * 1000

MICROPHONE_SHORTCUT = "microphone"
MICROPHONE_DESCRIPTION = "Roblox microphone (push to talk or toggle)"

_tokens = itertools.count(1)


@dataclass(frozen=True)
class Shortcut:
    id: str
    description: str
    trigger: str


class ShortcutsError(RuntimeError):
    pass


def open_connection(app_id: str) -> Gio.DBusConnection:
    """A private session-bus connection that tells the portal which app is asking.

    Apps outside Flatpak are otherwise identified by their systemd scope, which a
    terminal launch does not have. Register must be the connection's first
    portal call, hence a connection of its own.
    """
    address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
    connection = Gio.DBusConnection.new_for_address_sync(
        address,
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
        None,
        None,
    )
    try:
        connection.call_sync(
            PORTAL_BUS, PORTAL_PATH, REGISTRY_INTERFACE, "Register",
            GLib.Variant("(sa{sv})", (app_id, {})), None, Gio.DBusCallFlags.NONE, CALL_TIMEOUT_MS, None,
        )
    except GLib.Error:
        pass
    return connection


def portal_version(connection: Gio.DBusConnection) -> int:
    """0 when the desktop has no GlobalShortcuts portal."""
    try:
        reply = connection.call_sync(
            PORTAL_BUS, PORTAL_PATH, "org.freedesktop.DBus.Properties", "Get",
            GLib.Variant("(ss)", (SHORTCUTS_INTERFACE, "version")), GLib.VariantType.new("(v)"),
            Gio.DBusCallFlags.NONE, CALL_TIMEOUT_MS, None,
        )
    except GLib.Error:
        return 0
    value = reply.unpack()[0]
    return value if isinstance(value, int) else 0


def _shortcuts(value) -> list[Shortcut]:
    return [
        Shortcut(str(identifier), str(data.get("description", "")), str(data.get("trigger_description", "")))
        for identifier, data in value or []
    ]


class ShortcutsSession:
    """One portal session holding Roseblox's shortcuts until ``close``."""

    def __init__(
        self,
        connection: Gio.DBusConnection,
        *,
        on_activated: Callable[[str], None] = lambda _id: None,
        on_deactivated: Callable[[str], None] = lambda _id: None,
        on_changed: Callable[[list[Shortcut]], None] = lambda _shortcuts: None,
    ) -> None:
        self.connection = connection
        self.on_activated = on_activated
        self.on_deactivated = on_deactivated
        self.on_changed = on_changed
        self.handle: str | None = None
        self.version = 0
        self._subscriptions: list[int] = []
        self._closed = False

    def start(self, shortcuts: list[tuple[str, str]], done: Callable[[list[Shortcut] | None, str | None], None]) -> None:
        """Create the session and bind ``(id, description)`` pairs; ``done(shortcuts, error)``."""
        self.version = portal_version(self.connection)
        if self.version == 0:
            done(None, "This desktop has no global shortcut service (xdg-desktop-portal GlobalShortcuts).")
            return
        for name, handler in (
            ("Activated", self._activated), ("Deactivated", self._deactivated), ("ShortcutsChanged", self._changed),
        ):
            self._subscriptions.append(self.connection.signal_subscribe(
                PORTAL_BUS, SHORTCUTS_INTERFACE, name, PORTAL_PATH, None, Gio.DBusSignalFlags.NONE, handler,
            ))

        def created(response: int, results: dict) -> None:
            if response != 0 or not results.get("session_handle"):
                done(None, "The desktop refused to create a global shortcut session.")
                return
            self.handle = str(results["session_handle"])
            if self._closed:
                self.close()
                return
            self._bind(shortcuts, done)

        token = self._token()
        self._request(
            "CreateSession",
            lambda request_token: GLib.Variant("(a{sv})", ({
                "handle_token": GLib.Variant("s", request_token),
                "session_handle_token": GLib.Variant("s", token),
            },)),
            created,
            done,
        )

    def configure(self, activation_token: str = "") -> bool:
        """Open the desktop's dialog for changing the keys (portal version 2); False if it has none."""
        if self.handle is None or self.version < 2:
            return False
        options = {"activation_token": GLib.Variant("s", activation_token)} if activation_token else {}
        self.connection.call(
            PORTAL_BUS, PORTAL_PATH, SHORTCUTS_INTERFACE, "ConfigureShortcuts",
            GLib.Variant("(osa{sv})", (self.handle, "", options)), None, Gio.DBusCallFlags.NONE,
            CALL_TIMEOUT_MS, None, None,
        )
        return True

    def close(self) -> None:
        self._closed = True
        for subscription in self._subscriptions:
            self.connection.signal_unsubscribe(subscription)
        self._subscriptions = []
        if self.handle is not None:
            self.connection.call(
                PORTAL_BUS, self.handle, SESSION_INTERFACE, "Close", None, None, Gio.DBusCallFlags.NONE,
                CALL_TIMEOUT_MS, None, None,
            )
            self.handle = None

    def _bind(self, shortcuts: list[tuple[str, str]], done) -> None:
        def bound(response: int, results: dict) -> None:
            if response != 0:
                done(None, "The global shortcut was not set up." if response == 1 else
                     "The desktop could not set up the global shortcut.")
                return
            bound_shortcuts = _shortcuts(results.get("shortcuts"))
            done(bound_shortcuts, None)

        self._request(
            "BindShortcuts",
            lambda request_token: GLib.Variant("(oa(sa{sv})sa{sv})", (
                self.handle,
                [(identifier, {"description": GLib.Variant("s", description)}) for identifier, description in shortcuts],
                "",
                {"handle_token": GLib.Variant("s", request_token)},
            )),
            bound,
            done,
            BIND_TIMEOUT_MS,
        )

    def _request(self, method: str, parameters, responded, failed, timeout: int = CALL_TIMEOUT_MS) -> None:
        token = self._token()
        sender = (self.connection.get_unique_name() or "").lstrip(":").replace(".", "_")
        path = f"{PORTAL_PATH}/request/{sender}/{token}"
        subscription = None

        def response(_connection, _sender, _path, _interface, _signal, values) -> None:
            self.connection.signal_unsubscribe(subscription)
            code, results = values.unpack()
            responded(code, results)

        subscription = self.connection.signal_subscribe(
            PORTAL_BUS, REQUEST_INTERFACE, "Response", path, None, Gio.DBusSignalFlags.NONE, response,
        )

        def called(connection, result) -> None:
            try:
                connection.call_finish(result)
            except GLib.Error as error:
                connection.signal_unsubscribe(subscription)
                failed(None, f"Global shortcuts: {error.message}")

        self.connection.call(
            PORTAL_BUS, PORTAL_PATH, SHORTCUTS_INTERFACE, method, parameters(token), GLib.VariantType.new("(o)"),
            Gio.DBusCallFlags.NONE, timeout, None, called,
        )

    @staticmethod
    def _token() -> str:
        return f"roseblox{os.getpid()}_{next(_tokens)}_{secrets.token_hex(4)}"

    def _ours(self, values) -> tuple[bool, str]:
        session, identifier = values[0], values[1]
        return self.handle is not None and session == self.handle, identifier

    def _activated(self, _connection, _sender, _path, _interface, _signal, values) -> None:
        ours, identifier = self._ours(values.unpack())
        if ours:
            self.on_activated(identifier)

    def _deactivated(self, _connection, _sender, _path, _interface, _signal, values) -> None:
        ours, identifier = self._ours(values.unpack())
        if ours:
            self.on_deactivated(identifier)

    def _changed(self, _connection, _sender, _path, _interface, _signal, values) -> None:
        session, shortcuts = values.unpack()
        if self.handle is not None and session == self.handle:
            self.on_changed(_shortcuts(shortcuts))
