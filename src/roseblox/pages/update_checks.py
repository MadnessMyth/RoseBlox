"""The per-app update-check and auto-update switches shown in Settings and first-time setup."""

from __future__ import annotations

import threading

import gi
gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gtk

from ..backend.setup import APP_TYPES, SetupSettingsError
from ..backend.updates import APP_TITLES, UpdateCheckerError, UpdateCheckerStatus


def _checks_updates(window, app_type: str, status: UpdateCheckerStatus | None) -> bool:
    return bool(status is not None and status.enabled and window.setup_state.checks_updates(app_type))


class UpdateCheckRow(Adw.ActionRow):
    """One app's "Notify me" switch, kept in sync by the window, which owns the checker service.

    The background checker runs while either app's switch is on: the first switch turned on
    starts it and the last one turned off stops it.
    """

    def __init__(self, window, app_type: str, *, icon_name: str | None = None) -> None:
        super().__init__()
        self.window = window
        self.app_type = app_type
        self.set_title(f"Notify me about {APP_TITLES[app_type]} updates")
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
        active = _checks_updates(self.window, self.app_type, status)
        self._block = True
        self.switch.set_active(active)
        self.switch.set_state(active)
        self._block = False
        self.switch.set_sensitive(not self.busy)
        if not self.busy:
            if self._message:
                details = self._message
            elif active or not status.enabled:
                details = status.message
                if status.error:
                    details = f"{details} ({status.error})"
            else:
                details = f"Off. {APP_TITLES[self.app_type]} is not checked for updates."
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
        store = self.window.setup_store
        message = None
        try:
            state = store.load()
            others = any(state.checks_updates(app) for app in APP_TYPES if app != self.app_type)
            if enable and not state.update_checks_enabled:
                for app in APP_TYPES:
                    store.save_update_app(app, app == self.app_type)
                status = checker.enable()
            elif enable or others:
                store.save_update_app(self.app_type, enable)
                status = checker.status()
            else:
                status = checker.disable()
        except (UpdateCheckerError, SetupSettingsError, OSError, ValueError) as error:
            message = str(error)[:500]
            status = checker.status()
        GLib.idle_add(self._done, status, message)

    def _done(self, status: UpdateCheckerStatus, message: str | None) -> bool:
        self.busy = False
        try:
            self.window.setup_state = self.window.setup_store.load()
        except SetupSettingsError:
            pass
        self.window.apply_update_checker_status(status, message, source=self)
        return GLib.SOURCE_REMOVE


class AutoUpdateRow(Adw.ActionRow):
    """Install new versions of one app without asking; shown only while that app is checked."""

    def __init__(self, window, app_type: str) -> None:
        super().__init__()
        self.window = window
        self.app_type = app_type
        self.set_title(f"Automatically update {APP_TITLES[app_type]} in the background")
        self._subtitle = "Installs new versions without asking, then a notification says it is done."
        self.set_subtitle(self._subtitle)
        self.switch = Gtk.Switch()
        self.switch.set_valign(Gtk.Align.CENTER)
        self.add_suffix(self.switch)
        self.set_activatable_widget(self.switch)
        self._block = False
        self.switch.connect("notify::active", self._changed)
        window.update_check_rows.append(self)
        self.apply_status(window.update_checker_status)

    def get_active(self) -> bool:
        return self.switch.get_active()

    def set_active(self, active: bool) -> None:
        self.switch.set_active(active)

    def apply_status(self, status: UpdateCheckerStatus | None, message: str | None = None) -> None:
        self.set_visible(_checks_updates(self.window, self.app_type, status))
        self._block = True
        self.set_active(self.window.setup_state.auto_updates.get(self.app_type, False))
        self._block = False

    def _changed(self, _switch, _param) -> None:
        if self._block:
            return
        try:
            self.window.setup_state = self.window.setup_store.save_auto_update(self.app_type, self.get_active())
        except (SetupSettingsError, OSError, ValueError) as error:
            self.set_subtitle(f"Could not save: {str(error)[:300]}")
            self._block = True
            self.set_active(not self.get_active())
            self._block = False
            return
        self.set_subtitle(self._subtitle)
        for row in self.window.update_check_rows:
            if row is not self and isinstance(row, AutoUpdateRow) and row.app_type == self.app_type:
                row.apply_status(self.window.update_checker_status)


def update_rows(window, *, icon_name: str | None = None):
    """Each app's notify switch followed by its auto-update switch, as ``(notify, auto)`` dicts."""
    notify, auto = {}, {}
    for app_type in APP_TYPES:
        notify[app_type] = UpdateCheckRow(window, app_type, icon_name=icon_name)
        auto[app_type] = AutoUpdateRow(window, app_type)
    return notify, auto
