"""First-run setup: Rosemary, Roblox link handling, and Player or Studio installation."""

from __future__ import annotations

import threading
from pathlib import Path

import gi
gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gtk

from ..backend.rosemary import RosemaryStatus
from ..backend.rosemary_install import ROSEMARY_PROJECT_URL, RosemaryInstallState
from ..backend.setup import SetupSettingsError, SetupStore
from ..backend.url_handlers import URLHandlerError, URLHandlerService, URLHandlerStatus
from .update_checks import UpdateCheckRow

_ICON_PATH = Path(__file__).resolve().parent.parent / "resources" / "roseblox-icon.svg"
_BADGE_STATES = ("badge-done", "badge-attention", "badge-optional", "badge-working")
_APP_TITLES = {"player": "Roblox Player", "studio": "Roblox Studio"}


def _status_badge() -> Gtk.Label:
    badge = Gtk.Label()
    badge.add_css_class("setup-badge")
    badge.set_valign(Gtk.Align.CENTER)
    return badge


def _set_badge(badge: Gtk.Label, text: str, state: str) -> None:
    badge.set_text(text)
    for css_class in _BADGE_STATES:
        badge.remove_css_class(css_class)
    badge.add_css_class(f"badge-{state}")


class OnboardingPage(Gtk.Box):
    def __init__(self, window, setup_store: SetupStore, url_handlers: URLHandlerService) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.setup_store = setup_store
        self.url_handlers = url_handlers
        self._busy = False
        self._url_busy = False
        self._url_switch_block = False
        self._records = []
        self._validation = {}
        self._status = RosemaryStatus(None)
        self._setup_state = None
        self._handler_status: URLHandlerStatus | None = None
        self._install_state = RosemaryInstallState()
        self._operations = {}
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.add_css_class("setup-page")

        clamp = Adw.Clamp()
        clamp.set_maximum_size(720)
        clamp.set_tightening_threshold(520)
        self.append(clamp)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        content.set_margin_top(32)
        content.set_margin_bottom(32)
        content.set_margin_start(24)
        content.set_margin_end(24)
        clamp.set_child(content)

        content.append(self._build_hero())
        content.append(self._build_rosemary_step())
        content.append(self._build_links_step())
        content.append(self._build_apps_step())
        content.append(self._build_updates_step())

        self.error_label = Gtk.Label()
        self.error_label.set_xalign(0)
        self.error_label.set_wrap(True)
        self.error_label.add_css_class("status-notice")
        self.error_label.set_visible(False)
        content.append(self.error_label)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.summary_label = Gtk.Label()
        self.summary_label.set_xalign(0)
        self.summary_label.set_hexpand(True)
        self.summary_label.set_wrap(True)
        self.summary_label.add_css_class("dim-label")
        footer.append(self.summary_label)
        self.skip_button = Gtk.Button.new_with_label("Skip for Now")
        self.skip_button.add_css_class("flat")
        self.skip_button.connect("clicked", self._finish_clicked)
        footer.append(self.skip_button)
        self.finish_button = Gtk.Button.new_with_label("Finish Setup")
        self.finish_button.add_css_class("suggested-action")
        self.finish_button.add_css_class("pill")
        self.finish_button.connect("clicked", self._finish_clicked)
        footer.append(self.finish_button)
        content.append(footer)

        for app_type in ("player", "studio"):
            controller = window.pages.get(app_type)
            if controller is not None and hasattr(controller, "add_operation_listener"):
                controller.add_operation_listener(self._operation_changed)
        self._render()

    def _build_hero(self) -> Gtk.Widget:
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        hero.add_css_class("setup-hero")
        if _ICON_PATH.is_file():
            icon = Gtk.Image.new_from_file(str(_ICON_PATH))
            icon.set_pixel_size(80)
            icon.set_margin_bottom(6)
            hero.append(icon)
        title = Gtk.Label(label="Welcome to Roseblox")
        title.add_css_class("title-1")
        hero.append(title)
        introduction = Gtk.Label(
            label="A few quick steps get Roblox running on Linux. Everything here can be changed later in Settings."
        )
        introduction.set_wrap(True)
        introduction.set_justify(Gtk.Justification.CENTER)
        introduction.set_max_width_chars(60)
        introduction.add_css_class("dim-label")
        hero.append(introduction)
        return hero

    def _step_group(self, title: str, description: str) -> tuple[Adw.PreferencesGroup, Gtk.Label]:
        group = Adw.PreferencesGroup.new()
        group.set_title(title)
        group.set_description(description)
        badge = _status_badge()
        group.set_header_suffix(badge)
        return group, badge

    def _build_rosemary_step(self) -> Gtk.Widget:
        group, self.rosemary_badge = self._step_group(
            "1 · Rosemary",
            "Rosemary runs the macOS Roblox apps on Linux. Roseblox needs it to launch Player and Studio.",
        )
        self.rosemary_row = Adw.ActionRow.new()
        self.rosemary_row.set_title("Rosemary")
        self.rosemary_row.set_subtitle("Checking…")
        self.rosemary_row.set_subtitle_selectable(True)
        self.rosemary_icon = Gtk.Image.new_from_icon_name("content-loading-symbolic")
        self.rosemary_row.add_prefix(self.rosemary_icon)
        self.recheck_rosemary_button = Gtk.Button.new_with_label("Check Again")
        self.recheck_rosemary_button.set_valign(Gtk.Align.CENTER)
        self.recheck_rosemary_button.connect("clicked", lambda *_: self.window.recheck_rosemary())
        self.rosemary_row.add_suffix(self.recheck_rosemary_button)
        group.add(self.rosemary_row)

        self.install_rosemary_row = Adw.ActionRow.new()
        self.install_rosemary_row.set_title("Install from the AUR")
        self.install_rosemary_row.add_prefix(Gtk.Image.new_from_icon_name("system-software-install-symbolic"))
        self.rosemary_spinner = Gtk.Spinner()
        self.rosemary_spinner.set_visible(False)
        self.install_rosemary_row.add_suffix(self.rosemary_spinner)
        self.stop_waiting_button = Gtk.Button.new_with_label("Stop Waiting")
        self.stop_waiting_button.set_valign(Gtk.Align.CENTER)
        self.stop_waiting_button.set_visible(False)
        self.stop_waiting_button.connect("clicked", lambda *_: self.window.stop_waiting_for_rosemary())
        self.install_rosemary_row.add_suffix(self.stop_waiting_button)
        self.install_rosemary_button = Gtk.Button.new_with_label("Install Rosemary")
        self.install_rosemary_button.set_valign(Gtk.Align.CENTER)
        self.install_rosemary_button.add_css_class("suggested-action")
        self.install_rosemary_button.connect("clicked", lambda *_: self.window.install_rosemary())
        self.install_rosemary_row.add_suffix(self.install_rosemary_button)
        group.add(self.install_rosemary_row)

        choose_row = Adw.ActionRow.new()
        choose_row.set_title("Use an existing build")
        choose_row.set_subtitle("Select a Rosemary executable or AppImage you already have.")
        choose_row.add_prefix(Gtk.Image.new_from_icon_name("folder-open-symbolic"))
        self.choose_rosemary_button = Gtk.Button.new_with_label("Choose…")
        self.choose_rosemary_button.set_valign(Gtk.Align.CENTER)
        self.choose_rosemary_button.connect("clicked", lambda *_: self.window.choose_rosemary_executable())
        choose_row.add_suffix(self.choose_rosemary_button)
        group.add(choose_row)

        about_row = Adw.ActionRow.new()
        about_row.set_title("About Rosemary")
        about_row.set_subtitle("Project page and other installation methods")
        about_row.add_prefix(Gtk.Image.new_from_icon_name("help-about-symbolic"))
        link = Gtk.LinkButton.new_with_label(ROSEMARY_PROJECT_URL, "Open Project Page")
        link.set_valign(Gtk.Align.CENTER)
        about_row.add_suffix(link)
        group.add(about_row)
        return group

    def _build_links_step(self) -> Gtk.Widget:
        group, self.url_badge = self._step_group(
            "2 · Roblox Links",
            "Let “Play” buttons on the Roblox website and Studio sign-in links open through Roseblox.",
        )
        self.url_row = Adw.ActionRow.new()
        self.url_row.set_title("Open Roblox links with Roseblox")
        self.url_row.set_subtitle("Checking desktop registration…")
        self.url_row.add_prefix(Gtk.Image.new_from_icon_name("insert-link-symbolic"))
        self.url_repair_button = Gtk.Button.new_with_label("Repair")
        self.url_repair_button.set_valign(Gtk.Align.CENTER)
        self.url_repair_button.set_visible(False)
        self.url_repair_button.connect("clicked", lambda *_: self._run_url_change(True))
        self.url_row.add_suffix(self.url_repair_button)
        self.url_switch = Gtk.Switch()
        self.url_switch.set_valign(Gtk.Align.CENTER)
        self.url_switch.connect("state-set", self._url_switch_changed)
        self.url_row.add_suffix(self.url_switch)
        self.url_row.set_activatable_widget(self.url_switch)
        group.add(self.url_row)
        return group

    def _build_apps_step(self) -> Gtk.Widget:
        group, self.apps_badge = self._step_group(
            "3 · Player and Studio",
            "Download the current version of either app, or both. Existing installations are detected automatically.",
        )
        self._app_widgets = {}
        for app_type, icon_name in (("player", "input-gaming-symbolic"), ("studio", "applications-engineering-symbolic")):
            row = Adw.ActionRow.new()
            row.set_title(_APP_TITLES[app_type])
            row.set_subtitle("Checking Library…")
            row.add_prefix(Gtk.Image.new_from_icon_name(icon_name))
            progress = Gtk.ProgressBar()
            progress.set_valign(Gtk.Align.CENTER)
            progress.set_size_request(120, -1)
            progress.set_visible(False)
            row.add_suffix(progress)
            cancel = Gtk.Button.new_with_label("Cancel")
            cancel.set_valign(Gtk.Align.CENTER)
            cancel.set_visible(False)
            cancel.connect("clicked", self._cancel_app_install, app_type)
            row.add_suffix(cancel)
            action = Gtk.Button.new_with_label("Install")
            action.set_valign(Gtk.Align.CENTER)
            action.connect("clicked", self._app_action_clicked, app_type)
            row.add_suffix(action)
            group.add(row)
            self._app_widgets[app_type] = (row, progress, cancel, action)
        return group

    def _build_updates_step(self) -> Gtk.Widget:
        group, self.updates_badge = self._step_group(
            "4 · Updates",
            "Optional. Get a notification when a new Player or Studio version is out, "
            "with a button to install it or skip that version.",
        )
        self.update_row = UpdateCheckRow(self.window, icon_name="software-update-available-symbolic")
        group.add(self.update_row)
        return group

    def _render_updates(self) -> None:
        status = self.update_row.status
        if status is not None and status.enabled and not status.error:
            _set_badge(self.updates_badge, "Enabled", "done")
        elif status is not None and status.enabled:
            _set_badge(self.updates_badge, "Needs attention", "attention")
        else:
            _set_badge(self.updates_badge, "Optional", "optional")

    @property
    def player_button(self) -> Gtk.Button:
        return self._app_widgets["player"][3]

    @property
    def studio_button(self) -> Gtk.Button:
        return self._app_widgets["studio"][3]

    def apply_snapshot(
        self,
        rosemary_status: RosemaryStatus,
        setup_state,
        setup_error: str | None,
        handler_status: URLHandlerStatus | None,
        records,
        validation: dict[str, str],
    ) -> None:
        self._status = rosemary_status
        self._setup_state = setup_state
        self._handler_status = handler_status
        self._records = list(records)
        self._validation = dict(validation)
        if setup_error:
            self.error_label.set_text(setup_error)
            self.error_label.set_visible(True)
        elif not self._busy:
            self.error_label.set_visible(False)
        self._render()

    def apply_rosemary_install_state(self, state: RosemaryInstallState) -> None:
        self._install_state = state
        self._render()

    def _render(self) -> None:
        self._render_rosemary()
        self._render_links()
        self._render_apps()
        self._render_updates()
        completed = self._setup_state is not None and self._setup_state.onboarding_completed
        self.skip_button.set_visible(not completed)
        self.finish_button.set_label("Done" if completed else "Finish Setup")
        ready = [self._status.available, self._has_valid_installation()]
        self.summary_label.set_text(
            "Ready to play." if all(ready) else f"{sum(ready)} of 2 required steps complete."
        )
        self.finish_button.set_sensitive(not self._busy)
        self.skip_button.set_sensitive(not self._busy)

    def _render_rosemary(self) -> None:
        status = self._status
        install = self._install_state
        if status.available:
            _set_badge(self.rosemary_badge, "Ready", "done")
            self.rosemary_icon.set_from_icon_name("emblem-ok-symbolic")
            self.rosemary_row.set_subtitle(status.executable)
        elif status.error:
            _set_badge(self.rosemary_badge, "Needs attention", "attention")
            self.rosemary_icon.set_from_icon_name("dialog-warning-symbolic")
            self.rosemary_row.set_subtitle(status.error)
        else:
            _set_badge(
                self.rosemary_badge,
                "Installing" if install.waiting else "Required",
                "working" if install.waiting else "attention",
            )
            self.rosemary_icon.set_from_icon_name("dialog-warning-symbolic")
            self.rosemary_row.set_subtitle("Not installed")
        self.install_rosemary_row.set_visible(not status.available or install.waiting)
        self.install_rosemary_row.set_subtitle(
            install.message
            or "Builds the “rosemary” package with paru or yay in a terminal, where you can review it and enter your password."
        )
        if install.error:
            self.install_rosemary_row.add_css_class("error-row")
        else:
            self.install_rosemary_row.remove_css_class("error-row")
        self.rosemary_spinner.set_visible(install.waiting)
        self.rosemary_spinner.set_spinning(install.waiting)
        self.stop_waiting_button.set_visible(install.waiting)
        self.install_rosemary_button.set_visible(not install.waiting and not status.available)
        self.install_rosemary_button.set_label("Try Again" if install.error else "Install Rosemary")

    def _render_links(self) -> None:
        state = self._setup_state
        handler = self._handler_status
        enabled = bool(state and state.url_handlers_enabled)
        self._url_switch_block = True
        self.url_switch.set_active(enabled)
        self._url_switch_block = False
        self.url_switch.set_sensitive(not self._url_busy)
        if self._url_busy:
            return
        if handler is None:
            subtitle = "Registration status is unavailable. Open Settings to retry."
        elif handler.error:
            subtitle = handler.error
        else:
            subtitle = handler.message
        self.url_row.set_subtitle(subtitle)
        needs_repair = enabled and handler is not None and not handler.registered
        self.url_repair_button.set_visible(needs_repair)
        if enabled and handler is not None and handler.registered:
            _set_badge(self.url_badge, "Enabled", "done")
        elif enabled:
            _set_badge(self.url_badge, "Needs attention", "attention")
        elif handler is not None and handler.error:
            _set_badge(self.url_badge, "Unavailable", "optional")
        else:
            _set_badge(self.url_badge, "Optional", "optional")

    def _has_valid_installation(self, app_type: str | None = None) -> bool:
        return any(
            (app_type is None or record.app_type == app_type) and self._validation.get(record.id) == "Valid"
            for record in self._records
        )

    def _render_apps(self) -> None:
        any_busy = False
        for app_type, (row, progress, cancel, action) in self._app_widgets.items():
            operation = self._operations.get(app_type)
            records_for_type = [record for record in self._records if record.app_type == app_type]
            valid_count = sum(1 for record in records_for_type if self._validation.get(record.id) == "Valid")
            if operation is not None and operation.busy:
                any_busy = True
                row.set_subtitle(operation.message or "Installing…")
                progress.set_visible(True)
                if operation.fraction is not None:
                    progress.set_fraction(operation.fraction)
                cancel.set_visible(operation.cancellable)
                action.set_visible(False)
                continue
            progress.set_visible(False)
            cancel.set_visible(False)
            action.set_visible(True)
            action.remove_css_class("suggested-action")
            if operation is not None and operation.message and not valid_count:
                row.set_subtitle(operation.message)
            elif valid_count:
                row.set_subtitle(f"Installed · {valid_count} valid installation{'s' if valid_count != 1 else ''}")
            elif records_for_type:
                row.set_subtitle("Registered, but the application bundle is missing or invalid")
            else:
                row.set_subtitle("Not installed")
            if valid_count:
                action.set_label("Manage")
            elif records_for_type:
                action.set_label("Review")
            else:
                action.set_label("Install")
                action.add_css_class("suggested-action")
        if any_busy:
            _set_badge(self.apps_badge, "Installing", "working")
        elif self._has_valid_installation():
            _set_badge(self.apps_badge, "Ready", "done")
        else:
            _set_badge(self.apps_badge, "Required", "attention")

    def _operation_changed(self, app_type: str, state) -> None:
        self._operations[app_type] = state
        self._render()

    def _set_busy(self, busy: bool, message: str | None = None) -> None:
        self._busy = busy
        if message:
            self.error_label.set_text(message)
            self.error_label.set_visible(True)
        elif busy is False:
            self.error_label.set_visible(False)
        self._render()

    def _url_switch_changed(self, _switch, active: bool) -> bool:
        if self._url_switch_block:
            return False
        self._run_url_change(bool(active))
        return True

    def _run_url_change(self, enable: bool) -> None:
        if self._url_busy:
            return
        self._url_busy = True
        self.url_switch.set_sensitive(False)
        self.url_repair_button.set_sensitive(False)
        self.url_row.set_subtitle("Updating desktop registration…")
        threading.Thread(target=self._url_worker, args=(enable,), daemon=True).start()

    def _url_worker(self, enable: bool) -> None:
        try:
            self.url_handlers.enable() if enable else self.url_handlers.disable()
            error = None
        except (URLHandlerError, SetupSettingsError, OSError) as failure:
            error = str(failure)
        GLib.idle_add(self._url_finished, error)

    def _url_finished(self, error: str | None) -> bool:
        self._url_busy = False
        self.url_repair_button.set_sensitive(True)
        if error:
            self.error_label.set_text(f"Roblox link handling was not changed: {error}")
            self.error_label.set_visible(True)
        self._render()
        self.window.refresh_pages(reload_library=False, reload_preferences=True)
        return GLib.SOURCE_REMOVE

    def _app_action_clicked(self, _button, app_type: str) -> None:
        controller = self.window.pages.get(app_type)
        if controller is None:
            return
        if any(record.app_type == app_type for record in self._records):
            self.window.navigate_to(app_type)
            return
        if not hasattr(controller, "add_operation_listener"):
            self.window.navigate_to(app_type)
        controller.start_install_from_setup()

    def _cancel_app_install(self, _button, app_type: str) -> None:
        controller = self.window.pages.get(app_type)
        if controller is not None:
            controller.cancel_operation()

    def _finish_clicked(self, _button) -> None:
        if self._setup_state is not None and self._setup_state.onboarding_completed:
            self.window.navigate_to("home")
            return
        self._set_busy(True, None)
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
