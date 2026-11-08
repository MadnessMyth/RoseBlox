"""The window's side of the microphone hotkey: show the key and open the desktop's dialog to change it."""

from __future__ import annotations

import os
import shlex
import subprocess
from typing import Callable

from gi.repository import GLib

from ..backend.appimage import appimage_path
from ..backend.global_shortcuts import (
    MICROPHONE_DESCRIPTION,
    MICROPHONE_SHORTCUT,
    ShortcutsSession,
    open_connection,
)

APP_ID = "io.github.roseblox.Roseblox"
KDE_SHORTCUTS_COMMAND = ["systemsettings", "kcm_keys"]


def toggle_command() -> str:
    executable = appimage_path()
    return f"{shlex.quote(executable) if executable else 'roseblox'} --mic toggle"


def on_kde(environ=os.environ) -> bool:
    return "KDE" in environ.get("XDG_CURRENT_DESKTOP", "").split(":")


class MicrophoneHotkey:
    """Holds a portal session while the window is open; ``changed(text, can_change)`` updates the row."""

    def __init__(
        self,
        changed: Callable[[str, bool], None],
        *,
        connection_factory=lambda: open_connection(APP_ID),
        session_factory=None,
        spawn=lambda arguments: subprocess.Popen(arguments, start_new_session=True),
        environ=os.environ,
    ) -> None:
        self.changed = changed
        self.connection_factory = connection_factory
        self.session_factory = session_factory or (lambda *args, **kwargs: ShortcutsSession(*args, **kwargs))
        self.spawn = spawn
        self.environ = environ
        self.session: ShortcutsSession | None = None
        self.trigger = ""
        self.unavailable = False
        self._starting = False
        self._configure_when_bound = False

    def start(self) -> None:
        if self.session is not None or self._starting or self.unavailable:
            return
        self._starting = True
        self.changed("Asking the desktop…", False)
        try:
            connection = self.connection_factory()
        except GLib.Error as error:
            self._unavailable(f"No session bus: {error.message}")
            return
        self.session = self.session_factory(connection, on_changed=self._shortcuts_changed)
        self.session.start([(MICROPHONE_SHORTCUT, MICROPHONE_DESCRIPTION)], self._bound)

    def change(self) -> None:
        """Open the desktop's shortcut settings, binding first if needed."""
        if self.session is None or self._starting:
            self._configure_when_bound = True
            self.start()
            return
        if self.session.configure():
            return
        if on_kde(self.environ):
            try:
                self.spawn(KDE_SHORTCUTS_COMMAND)
                return
            except OSError:
                pass
        self.changed(
            f"{self._describe()} Change it in your desktop's shortcut settings, where it is listed under Roseblox.",
            True,
        )

    def close(self) -> None:
        if self.session is not None:
            self.session.close()
            self.session = None

    def _bound(self, shortcuts, error) -> None:
        self._starting = False
        if error:
            self.session = None
            self._unavailable(error)
            return
        self._shortcuts_changed(shortcuts)
        if self._configure_when_bound:
            self._configure_when_bound = False
            if not self.trigger:
                self.change()

    def _shortcuts_changed(self, shortcuts) -> None:
        for shortcut in shortcuts or []:
            if shortcut.id == MICROPHONE_SHORTCUT:
                self.trigger = shortcut.trigger
        self.changed(self._describe(), True)

    def _describe(self) -> str:
        return f"Hotkey: {self.trigger}." if self.trigger else "No key assigned yet."

    def _unavailable(self, reason: str) -> None:
        self._starting = False
        self.unavailable = True
        self.changed(f"{reason} Bind a key to “{toggle_command()}” in your desktop's keyboard settings.", False)
