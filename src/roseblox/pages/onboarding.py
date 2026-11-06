"""Compact first-run setup guidance using the existing application services."""

from __future__ import annotations

import threading

import gi
gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gtk

from ..backend.rosemary import RosemaryStatus
from ..backend.setup import SetupSettingsError, SetupStore
from ..backend.url_handlers import URLHandlerError, URLHandlerService, URLHandlerStatus


class OnboardingPage(Gtk.Box):
    def __init__(self, window, setup_store: SetupStore, url_handlers: URLHandlerService) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.window = window
        self.setup_store = setup_store
        self.url_handlers = url_handlers
        self._busy = False
        self._records = []
        self._validation = {}
        self._status = RosemaryStatus(None)
        self._setup_state = None
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_margin_top(28)
        self.set_margin_bottom(28)
        self.set_margin_start(28)
        self.set_margin_end(28)
        self.add_css_class("setup-page")

        title = Gtk.Label(label="Let’s set up Roseblox")
        title.set_xalign(0)
        title.add_css_class("title-1")
        self.append(title)
        introduction = Gtk.Label(
            label="Roseblox uses your existing Rosemary installation and keeps Player and Studio separate. You can finish now and complete any step later."
        )
        introduction.set_xalign(0)
        introduction.set_wrap(True)
        introduction.add_css_class("dim-label")
        self.append(introduction)

        rosemary_group = Adw.PreferencesGroup.new()
        rosemary_group.set_title("1. Rosemary")
        rosemary_group.set_description("Rosemary is an external requirement for launching Roblox on Linux.")
        self.rosemary_status = Gtk.Label(label="Checking Rosemary…")
        self.rosemary_status.set_xalign(0)
        self.rosemary_status.set_wrap(True)
        self.rosemary_status.set_selectable(True)
        row = Adw.ActionRow.new()
        row.set_title("Rosemary status")
        row.add_suffix(self.rosemary_status)
        rosemary_group.add(row)
        rosemary_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.choose_rosemary_button = Gtk.Button.new_with_label("Choose executable…")
        self.choose_rosemary_button.connect("clicked", lambda *_: self.window.choose_rosemary_executable())
        rosemary_actions.append(self.choose_rosemary_button)
        rosemary_link = Gtk.LinkButton.new_with_label(
            "https://github.com/microlift/rosemary", "Rosemary project"
        )
        rosemary_actions.append(rosemary_link)
        rosemary_row = Adw.ActionRow.new()
        rosemary_row.set_title("Configure or learn about Rosemary")
        rosemary_row.add_suffix(rosemary_actions)
        rosemary_group.add(rosemary_row)
        self.append(rosemary_group)

        url_group = Adw.PreferencesGroup.new()
        url_group.set_title("2. Roblox links")
        url_group.set_description("Supported Roblox links can open through Roseblox. This is optional and can be changed in Settings.")
        self.url_status = Gtk.Label(label="Checking desktop registration…")
        self.url_status.set_xalign(0)
        self.url_status.set_wrap(True)
        self.url_status.set_selectable(True)
        url_row = Adw.ActionRow.new()
        url_row.set_title("URL handler status")
        url_row.add_suffix(self.url_status)
        url_group.add(url_row)
        self.url_button = Gtk.Button.new_with_label("Enable URL handling")
        self.url_button.add_css_class("suggested-action")
        self.url_button.connect("clicked", self._toggle_url_handlers)
        url_action_row = Adw.ActionRow.new()
        url_action_row.set_title("Handle Roblox URLs")
        url_action_row.set_subtitle("Open supported Player and Studio links through Roseblox.")
        url_action_row.add_suffix(self.url_button)
        url_group.add(url_action_row)
        self.append(url_group)

        apps_group = Adw.PreferencesGroup.new()
        apps_group.set_title("3. Player and Studio")
        apps_group.set_description("Install either app, both, or keep using installations already in your Library.")
        self.player_status = self._add_app_row(apps_group, "Roblox Player", "player")
        self.studio_status = self._add_app_row(apps_group, "Roblox Studio", "studio")
        self.append(apps_group)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_halign(Gtk.Align.END)
        finish = Gtk.Button.new_with_label("Skip for now")
        finish.connect("clicked", self._finish_clicked)
        self.finish_button = Gtk.Button.new_with_label("Finish setup")
        self.finish_button.add_css_class("suggested-action")
        self.finish_button.connect("clicked", self._finish_clicked)
        footer.append(finish)
        footer.append(self.finish_button)
        self.append(footer)

        self.error_label = Gtk.Label()
        self.error_label.set_xalign(0)
        self.error_label.set_wrap(True)
        self.error_label.add_css_class("status-notice")
        self.error_label.set_visible(False)
        self.append(self.error_label)

    def _add_app_row(self, group, title: str, app_type: str) -> Gtk.Label:
        status = Gtk.Label(label="Checking Library…")
        status.set_xalign(0)
        status.set_wrap(True)
        action = Gtk.Button.new_with_label(f"Set up {title}…")
        action.connect("clicked", self._open_app_page, app_type)
        row = Adw.ActionRow.new()
        row.set_title(title)
        row.add_suffix(status)
        row.add_suffix(action)
        group.add(row)
        if app_type == "player":
            self.player_button = action
        else:
            self.studio_button = action
        return status

    def apply_snapshot(
        self,
        rosemary_status: RosemaryStatus,
        setup_state,
        setup_error: str | None,
        handler_status: URLHandlerStatus,
        records,
        validation: dict[str, str],
    ) -> None:
        self._status = rosemary_status
        self._setup_state = setup_state
        self._records = list(records)
        self._validation = dict(validation)
        if rosemary_status.available:
            self.rosemary_status.set_text(f"Detected · {rosemary_status.executable}")
        elif rosemary_status.error:
            self.rosemary_status.set_text(f"Unable to verify · {rosemary_status.error}")
        else:
            self.rosemary_status.set_text("Not detected · Rosemary must be installed separately.")
        if setup_error:
            self.error_label.set_text(setup_error)
            self.error_label.set_visible(True)
        elif not self._busy:
            self.error_label.set_visible(False)
        if handler_status.error:
            self.url_status.set_text(f"Unable to verify · {handler_status.error}")
        elif handler_status is not None:
            self.url_status.set_text(handler_status.message)
        else:
            self.url_status.set_text("Unable to verify URL-handler status. Open Settings to retry.")
        self.url_button.set_label(
            ("Disable URL handling" if handler_status and handler_status.enabled else "Repair URL handling")
            if setup_state and setup_state.url_handlers_enabled else "Enable URL handling"
        )
        for app_type, label, button, title in (
            ("player", self.player_status, self.player_button, "Player"),
            ("studio", self.studio_status, self.studio_button, "Studio"),
        ):
            records_for_type = [record for record in self._records if record.app_type == app_type]
            valid = [record for record in records_for_type if self._validation.get(record.id) == "Valid"]
            if valid:
                label.set_text(f"Installed · {len(valid)} valid installation{'s' if len(valid) != 1 else ''}")
                button.set_label(f"Manage {title}…")
            elif records_for_type:
                label.set_text("Registered, but no bundle is currently valid")
                button.set_label(f"Review {title}…")
            else:
                label.set_text("Not installed")
                button.set_label(f"Install {title}")

    def _set_busy(self, busy: bool, message: str | None = None) -> None:
        self._busy = busy
        self.url_button.set_sensitive(not busy)
        self.finish_button.set_sensitive(not busy)
        self.choose_rosemary_button.set_sensitive(not busy)
        if message:
            self.error_label.set_text(message)
            self.error_label.set_visible(True)

    def _toggle_url_handlers(self, _button) -> None:
        state = self._setup_state
        enabled = bool(state and state.url_handlers_enabled)
        registered = bool(self.window.url_handler_status and self.window.url_handler_status.registered)
        enable = not enabled or (enabled and not registered)
        self._set_busy(True, "Updating URL handler registration…")
        threading.Thread(target=self._url_worker, args=(enable,), daemon=True).start()

    def _url_worker(self, enable: bool) -> None:
        try:
            status = self.url_handlers.enable() if enable else self.url_handlers.disable()
            error = None
        except (URLHandlerError, SetupSettingsError, OSError) as failure:
            status = None
            error = str(failure)
        GLib.idle_add(self._url_finished, status, error)

    def _url_finished(self, _status, error: str | None) -> bool:
        self._set_busy(False, error)
        self.window.refresh_pages(reload_library=False, reload_preferences=True)
        return GLib.SOURCE_REMOVE

    def _open_app_page(self, _button, app_type: str) -> None:
        controller = self.window.pages.get(app_type)
        self.window.navigate_to(app_type)
        if controller is None:
            return
        if not any(record.app_type == app_type and self._validation.get(record.id) == "Valid" for record in self._records):
            if not any(record.app_type == app_type for record in self._records):
                controller.start_install_from_setup()

    def _finish_clicked(self, _button) -> None:
        self._set_busy(True, "Saving setup status…")
        threading.Thread(target=self._finish_worker, daemon=True).start()

    def _finish_worker(self) -> None:
        try:
            self.setup_store.complete_onboarding()
            error = None
        except SetupSettingsError as failure:
            error = str(failure)
        GLib.idle_add(self._finish_complete, error)

    def _finish_complete(self, error: str | None) -> bool:
        self._set_busy(False, error)
        if error is None:
            self.window.refresh_pages(reload_library=False, reload_preferences=True)
            self.window.navigate_to("home")
        return GLib.SOURCE_REMOVE
