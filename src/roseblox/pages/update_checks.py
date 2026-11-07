"""The background update-check switch shown in Settings and first-time setup."""

from __future__ import annotations

import threading

import gi
gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gtk

from ..backend.updates import UpdateCheckerError, UpdateCheckerStatus


class UpdateCheckRow(Adw.ActionRow):
    """A switch row kept in sync by the window, which owns the checker service."""

    def __init__(self, window, *, icon_name: str | None = None) -> None:
        super().__init__()
        self.window = window
        self.set_title("Notify me about Roblox updates")
        self.set_subtitle("Checking…")
        if icon_name:
            self.add_prefix(Gtk.Image.new_from_icon_name(icon_name))
        self.switch = Gtk.Switch()
        self.switch.set_valign(Gtk.Align.CENTER)
        self.switch.connect("state-set", self._switch_changed)
        self.add_suffix(self.switch)
        self.set_activatable_widget(self.switch)
        self._block = False
        self._message: str | None = None
        self.busy = False
        self.status: UpdateCheckerStatus | None = None
        window.update_check_rows.append(self)
        if window.update_checker_status is not None:
            self.apply_status(window.update_checker_status)

    def apply_status(self, status: UpdateCheckerStatus, message: str | None = None) -> None:
        """Show ``status``; a failure ``message`` stays visible until the switch is used again."""
        self.status = status
        if message is not None:
            self._message = message
        self._block = True
        self.switch.set_active(status.enabled)
        self.switch.set_state(status.enabled)
        self._block = False
        self.switch.set_sensitive(not self.busy)
        if not self.busy:
            details = self._message or status.message
            if status.error and not self._message:
                details = f"{details} ({status.error})"
            self.set_subtitle(details)

    def _switch_changed(self, _switch, active: bool) -> bool:
        # Returning True leaves the switch's drawn state alone until apply_status() sets it.
        if self._block:
            return False
        if self.busy:
            return True
        self.busy = True
        self._message = None
        self.switch.set_sensitive(False)
        self.set_subtitle("Turning update checks on…" if active else "Turning update checks off…")
        threading.Thread(target=self._worker, args=(bool(active),), daemon=True).start()
        return True

    def _worker(self, enable: bool) -> None:
        checker = self.window.update_checker
        message = None
        try:
            status = checker.enable() if enable else checker.disable()
        except UpdateCheckerError as error:
            message = str(error)[:500]
            status = checker.status()
        GLib.idle_add(self._done, status, message)

    def _done(self, status: UpdateCheckerStatus, message: str | None) -> bool:
        self.busy = False
        self.window.apply_update_checker_status(status, message, source=self)
        return GLib.SOURCE_REMOVE
