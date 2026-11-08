"""Windowless background process that notifies about new Roblox versions."""

from __future__ import annotations

import os
import sys
import threading

from gi.repository import Gio, GLib

from .backend import appimage
from .backend.library import LibraryStore
from .backend.rosemary import default_log_directory
from .command_line import print_error
from .backend.setup import APP_TYPES, SetupSettingsError, SetupStore
from .backend.updates import (
    APP_TITLES,
    CHECK_INTERVAL_SECONDS,
    FIRST_CHECK_DELAY_SECONDS,
    UPDATER_APP_ID,
    AvailableUpdate,
    NotificationLog,
    _spawn_detached,
    check_for_updates,
)
from .backend.version_settings import VersionSettingsStore
from .backend.versions import VersionCache, VersionClient

SERVICE_USAGE = "Usage: roseblox --update-service [--check-now | --quit]"


def _notification_id(app_type: str) -> str:
    return f"update-{app_type}"


class UpdateNotifierApplication(Gio.Application):
    """Single-instance checker: later invocations forward --check-now or --quit to it."""

    def __init__(
        self,
        *,
        setup_store: SetupStore | None = None,
        library: LibraryStore | None = None,
        version_client: VersionClient | None = None,
        version_settings: VersionSettingsStore | None = None,
        version_cache: VersionCache | None = None,
        notification_log: NotificationLog | None = None,
        executable_lookup=appimage.which,
        spawn=_spawn_detached,
        first_check_delay: float = FIRST_CHECK_DELAY_SECONDS,
        check_interval: float = CHECK_INTERVAL_SECONDS,
    ) -> None:
        super().__init__(application_id=UPDATER_APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.setup_store = setup_store or SetupStore()
        self.library = library or LibraryStore()
        self.version_client = version_client or VersionClient()
        self.version_settings = version_settings or VersionSettingsStore()
        self.version_cache = version_cache or VersionCache()
        self.notification_log = notification_log or NotificationLog()
        self.executable_lookup = executable_lookup
        self.spawn = spawn
        self.first_check_delay = first_check_delay
        self.check_interval = check_interval
        self.last_errors: dict[str, str] = {}
        self._running = False
        self._checking = False
        self._force = False
        self._force_pending = False
        self._timer_id = 0
        self.connect("command-line", self._on_command_line)

    def do_startup(self) -> None:
        Gio.Application.do_startup(self)
        for name, parameter_type, handler in (
            ("update", "s", self._on_update),
            ("skip", "(ss)", self._on_skip),
            ("open", "s", self._on_open),
        ):
            action = Gio.SimpleAction.new(name, GLib.VariantType.new(parameter_type))
            action.connect("activate", handler)
            self.add_action(action)

    def _on_command_line(self, _application, command_line) -> int:
        options = [item for item in command_line.get_arguments()[1:] if item != "--update-service"]
        if options == ["--quit"]:
            self.stop()
            return 0
        if options not in ([], ["--check-now"]):
            print_error(command_line, SERVICE_USAGE + "\n")
            return 2
        if not self._enabled():
            if not self._running:
                print_error(
                    command_line, "Roblox update checks are turned off. Turn them on in Roseblox Settings.\n"
                )
            self.stop()
            return 0
        if not self._running:
            self._running = True
            self.hold()
            if options:
                self._timer_id = GLib.timeout_add(int(self.check_interval * 1000), self._scheduled_check)
                self.check(force=True)
            else:
                self._timer_id = GLib.timeout_add(int(self.first_check_delay * 1000), self._scheduled_check)
        elif options:
            self.check(force=True)
        return 0

    def stop(self) -> None:
        if self._timer_id:
            GLib.source_remove(self._timer_id)
            self._timer_id = 0
        if self._running:
            self._running = False
            self.release()

    def _enabled(self) -> bool:
        try:
            return self.setup_store.load().update_checks_enabled
        except SetupSettingsError:
            return False

    def _scheduled_check(self) -> bool:
        self._timer_id = GLib.timeout_add(int(self.check_interval * 1000), self._scheduled_check)
        self.check()
        return GLib.SOURCE_REMOVE

    def check(self, *, force: bool = False) -> None:
        """Look for updates; ``force`` (Check Now) notifies even if reminded recently."""
        if self._checking:
            self._force_pending = self._force_pending or force
            return
        if not self._enabled():
            self.stop()
            return
        self._checking = True
        self._force = force
        threading.Thread(target=self._check_worker, daemon=True).start()

    def _check_worker(self) -> None:
        try:
            skipped = self.setup_store.load().skipped_versions
            updates, errors = check_for_updates(
                self.library, self.version_client, self.version_settings, self.version_cache, skipped
            )
        except Exception as error:
            updates, errors = [], {"library": str(error)[:500]}
        GLib.idle_add(self._check_finished, updates, errors)

    def _check_finished(self, updates: list[AvailableUpdate], errors: dict[str, str]) -> bool:
        self._checking = False
        force, self._force = self._force, False
        self.last_errors = errors
        for message in errors.values():
            print(f"roseblox update check: {message}", file=sys.stderr)
        offered = {update.app_type for update in updates}
        for app_type in APP_TYPES:
            if app_type not in offered and app_type not in errors:
                self.withdraw_notification(_notification_id(app_type))
        for update in updates:
            if force or self.notification_log.should_notify(update):
                self.send_notification(_notification_id(update.app_type), self.build_notification(update))
                self.notification_log.record(update)
        if self._force_pending:
            self._force_pending = False
            self.check(force=True)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def build_notification(update: AvailableUpdate) -> Gio.Notification:
        title = APP_TITLES[update.app_type]
        notification = Gio.Notification.new(f"{title} update available")
        current = f"You have {update.installed_version}." if update.installed_version else ""
        notification.set_body(f"{update.version} is available. {current}".strip())
        notification.set_icon(Gio.ThemedIcon.new("io.github.roseblox.Roseblox"))
        notification.set_default_action_and_target("app.open", GLib.Variant("s", update.app_type))
        notification.add_button_with_target(
            "Update", "app.update", GLib.Variant("s", update.app_type)
        )
        notification.add_button_with_target(
            "Skip This Version", "app.skip", GLib.Variant("(ss)", (update.app_type, update.version))
        )
        return notification

    def _roseblox(self, *arguments: str) -> None:
        executable = self.executable_lookup("roseblox")
        if not executable:
            print("roseblox update check: the `roseblox` command is not in PATH", file=sys.stderr)
            return
        try:
            process = self.spawn([os.path.abspath(executable), *arguments], default_log_directory() / "roseblox.log")
        except OSError as error:
            print(f"roseblox update check: could not open Roseblox: {error}", file=sys.stderr)
            return
        pid = getattr(process, "pid", None)
        if isinstance(pid, int):
            # The checker outlives the window it opens; reap it so no zombie is left.
            GLib.child_watch_add(GLib.PRIORITY_DEFAULT, pid, lambda *_: None)

    def _on_update(self, _action, parameter) -> None:
        app_type = parameter.get_string()
        if app_type in APP_TYPES:
            self.withdraw_notification(_notification_id(app_type))
            self._roseblox("--install-update", app_type)

    def _on_skip(self, _action, parameter) -> None:
        app_type, version = parameter.unpack()
        if app_type not in APP_TYPES:
            return
        self.withdraw_notification(_notification_id(app_type))
        try:
            self.setup_store.skip_version(app_type, version)
        except (SetupSettingsError, ValueError) as error:
            print(f"roseblox update check: could not skip {version}: {error}", file=sys.stderr)

    def _on_open(self, _action, parameter) -> None:
        self._roseblox("--show", parameter.get_string())


def main(argv: list[str]) -> int:
    GLib.set_application_name("Roseblox")
    return UpdateNotifierApplication().run(argv)
