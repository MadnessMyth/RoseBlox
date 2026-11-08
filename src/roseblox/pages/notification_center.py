"""The bell in the header bar: active downloads, available updates and recent results."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk, Pango

from .. import __version__
from ..backend import self_update
from ..backend.self_update import Release, SelfUpdateError
from ..backend.setup import SetupSettingsError
from ..backend.updates import APP_TITLES, CHECK_INTERVAL_SECONDS, AvailableUpdate, check_for_updates

RECENT_LIMIT = 20
# Narrow enough that the popover stays centered under the bell even when the window
# is maximized and the bell is close to the screen edge.
POPOVER_WIDTH = 240
_TEXT_WIDTH_CHARS = 40  # Upper bound; the popover width decides where text wraps.


ROSEBLOX_UPDATE_HINTS = {
    self_update.INSTALL_DEB: "Download the new .deb from the release page and open it to update.",
    self_update.INSTALL_SCRIPT: "Update with: cd ~/RoseBlox && git pull && packaging/generic/install.sh",
    self_update.INSTALL_PACKAGE: "Update it the same way you installed it.",
    self_update.INSTALL_SOURCE: "Run git pull in your Roseblox folder.",
    self_update.INSTALL_APPIMAGE: "Download the new AppImage from the release page.",
}


@dataclass(frozen=True)
class RecentEntry:
    app_type: str
    message: str
    outcome: str  # "done", "failed" or "cancelled"
    finished_at: float


class NotificationCenter(Gtk.MenuButton):
    def __init__(
        self, window, *, checker=None, release_checker=None, install_kind: str | None = None,
        check_interval: float = CHECK_INTERVAL_SECONDS,
    ) -> None:
        super().__init__()
        self.window = window
        self._checker = checker or self._check_with_window
        self._release_checker = release_checker
        self.install_kind = install_kind or self_update.install_kind()
        self.roseblox_release: Release | None = None
        self.roseblox_error: str | None = None
        self.roseblox_progress: float | None = None
        self.roseblox_updated = False
        self._roseblox_checking = False
        self._roseblox_checked_at = 0.0
        self._roseblox_dismissed: str | None = None
        self.check_interval = check_interval
        self.operations: dict[str, object] = {}
        self.updates: list[AvailableUpdate] = []
        self.update_errors: dict[str, str] = {}
        self.recent: list[RecentEntry] = []
        self._unread = 0
        self._checking = False
        self._check_pending = False
        self._checked_once = False
        self._timer_id = 0
        self.add_css_class("flat")
        self.add_css_class("notification-center-button")
        self.set_tooltip_text("Notifications")
        self.update_property([Gtk.AccessibleProperty.LABEL], ["Notifications"])

        icon_overlay = Gtk.Overlay()
        bell = Gtk.Image.new_from_icon_name("preferences-system-notifications-symbolic")
        # Equal side margins keep the bell centered in the button; the badge sits in the corner.
        bell.set_margin_start(9)
        bell.set_margin_end(9)
        bell.set_margin_top(5)
        bell.set_margin_bottom(5)
        icon_overlay.set_child(bell)
        self.badge = Gtk.Label()
        self.badge.add_css_class("notification-badge")
        self.badge.set_halign(Gtk.Align.END)
        self.badge.set_valign(Gtk.Align.START)
        self.badge.set_can_target(False)
        self.badge.set_visible(False)
        icon_overlay.add_overlay(self.badge)
        self.set_child(icon_overlay)

        self.popover = Gtk.Popover()
        self.popover.add_css_class("notification-center")
        self.popover.connect("notify::visible", self._popover_visibility_changed)
        self.set_popover(self.popover)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        content.set_size_request(POPOVER_WIDTH, -1)
        content.set_margin_top(10)
        content.set_margin_bottom(10)
        content.set_margin_start(8)
        content.set_margin_end(8)
        heading_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        heading = Gtk.Label(label="Notifications")
        heading.add_css_class("title-4")
        heading.set_halign(Gtk.Align.START)
        heading.set_hexpand(True)
        heading_row.append(heading)
        self.check_button = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self.check_button.add_css_class("flat")
        self.check_button.set_tooltip_text("Check for Updates")
        self.check_button.update_property([Gtk.AccessibleProperty.LABEL], ["Check for Updates"])
        self.check_button.connect("clicked", lambda _button: self.check_updates())
        heading_row.append(self.check_button)
        content.append(heading_row)

        self.empty_label = Gtk.Label(label="Nothing right now. Downloads and Roblox updates show up here.")
        self.empty_label.add_css_class("dim-label")
        self.empty_label.set_wrap(True)
        self.empty_label.set_justify(Gtk.Justification.CENTER)
        self.empty_label.set_halign(Gtk.Align.CENTER)
        self.empty_label.set_max_width_chars(_TEXT_WIDTH_CHARS)
        self.empty_label.set_margin_top(12)
        self.empty_label.set_margin_bottom(12)
        content.append(self.empty_label)

        self.roseblox_box, self.roseblox_list = self._group("Roseblox")
        content.append(self.roseblox_box)
        self.downloads_box, self.downloads_list = self._group("Downloads")
        content.append(self.downloads_box)
        self.updates_box, self.updates_list = self._group("Updates")
        content.append(self.updates_box)
        self.recent_box, self.recent_list = self._group("Recent")
        self.clear_button = Gtk.Button.new_with_label("Clear")
        self.clear_button.add_css_class("flat")
        self.clear_button.set_halign(Gtk.Align.END)
        self.clear_button.connect("clicked", self._clear_recent)
        self.recent_box.get_first_child().append(self.clear_button)
        content.append(self.recent_box)

        self.status_label = Gtk.Label()
        self.status_label.add_css_class("dim-label")
        self.status_label.add_css_class("caption")
        self.status_label.set_wrap(True)
        self.status_label.set_max_width_chars(_TEXT_WIDTH_CHARS)
        self.status_label.set_halign(Gtk.Align.START)
        self.status_label.set_xalign(0)
        self.status_label.set_visible(False)
        content.append(self.status_label)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_propagate_natural_height(True)
        scroller.set_max_content_height(560)
        scroller.set_child(content)
        self.popover.set_child(scroller)
        self.connect("destroy", lambda *_: self._stop_timer())
        self._render()

    @staticmethod
    def _group(title: str) -> tuple[Gtk.Box, Gtk.ListBox]:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        label = Gtk.Label(label=title)
        label.add_css_class("heading")
        label.set_halign(Gtk.Align.START)
        label.set_hexpand(True)
        header.append(label)
        box.append(header)
        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        box.append(listbox)
        return box, listbox

    # Downloads

    def operation_changed(self, app_type: str | None, state) -> None:
        if not app_type:
            return
        was_busy = app_type in self.operations
        if state.busy:
            self.operations[app_type] = state
        elif was_busy:
            del self.operations[app_type]
            message = state.message or "Finished."
            if message.startswith("Installation failed"):
                outcome = "failed"
            elif "cancelled" in message.lower():
                outcome = "cancelled"
            else:
                outcome = "done"
                self.check_updates(automatic=True)
            self._add_recent(RecentEntry(app_type, message, outcome, time.time()))
        else:
            return
        self._render()

    def notify(self, app_type: str, message: str, outcome: str = "done") -> None:
        self._add_recent(RecentEntry(app_type, message, outcome, time.time()))
        self._render()

    def _add_recent(self, entry: RecentEntry) -> None:
        self.recent.insert(0, entry)
        del self.recent[RECENT_LIMIT:]
        if not self.popover.get_visible():
            self._unread += 1

    def _clear_recent(self, _button) -> None:
        self.recent.clear()
        self._unread = 0
        self._render()

    def _popover_visibility_changed(self, popover, _pspec) -> None:
        if popover.get_visible() and self._unread:
            self._unread = 0
            self._render_badge()

    # Updates

    def library_ready(self) -> None:
        """Start automatic checks once the window knows what is installed."""
        if self._checked_once:
            return
        self._checked_once = True
        self.check_updates(automatic=True)
        if self._timer_id == 0 and self.check_interval > 0:
            self._timer_id = GLib.timeout_add(int(self.check_interval * 1000), self._scheduled_check)

    def _scheduled_check(self) -> bool:
        self.check_updates(automatic=True)
        return GLib.SOURCE_CONTINUE

    def _stop_timer(self) -> None:
        if self._timer_id:
            GLib.source_remove(self._timer_id)
            self._timer_id = 0

    def _automatic_checks_enabled(self) -> bool:
        state = getattr(self.window, "setup_state", None)
        return bool(getattr(state, "update_checks_enabled", False))

    def check_updates(self, *, automatic: bool = False) -> None:
        """Look for new Player and Studio versions in the background.

        Automatic checks follow the "Notify me about Roblox updates" setting; the
        Check for Updates button always works.
        """
        self.check_roseblox(automatic=automatic)
        if automatic and not self._automatic_checks_enabled():
            return
        if self._checking:
            self._check_pending = True
            return
        self._checking = True
        self.check_button.set_sensitive(False)
        self.check_button.set_tooltip_text("Checking for updates…")
        skipped = dict(getattr(getattr(self.window, "setup_state", None), "skipped_versions", {}) or {})
        threading.Thread(target=self._check_worker, args=(skipped,), daemon=True).start()

    def _check_with_window(self, skipped: dict[str, str]):
        installer = self.window.installer
        return check_for_updates(
            self.window.library, installer.version_client, self.window.version_settings,
            installer.version_cache, skipped,
        )

    def _check_worker(self, skipped: dict[str, str]) -> None:
        try:
            updates, errors = self._checker(skipped)
        except Exception as error:
            updates, errors = [], {"library": str(error)[:500]}
        GLib.idle_add(self._check_finished, updates, errors)

    def _check_finished(self, updates: list[AvailableUpdate], errors: dict[str, str]) -> bool:
        self._checking = False
        self.check_button.set_sensitive(True)
        self.check_button.set_tooltip_text("Check for Updates")
        offered = {update.app_type for update in updates}
        # A failed lookup keeps the update already shown for that app.
        kept = [update for update in self.updates if update.app_type in errors and update.app_type not in offered]
        self.updates = kept + list(updates)
        self.update_errors = errors
        self._render()
        if self._check_pending:
            self._check_pending = False
            self.check_updates()
        return GLib.SOURCE_REMOVE

    # Roseblox itself

    def _roseblox_checks_enabled(self) -> bool:
        state = getattr(self.window, "setup_state", None)
        return bool(getattr(state, "roseblox_update_checks", True))

    def check_roseblox(self, *, automatic: bool = False) -> None:
        """Look for a newer Roseblox release; automatic checks run at most once a day."""
        if self._roseblox_checking or self.roseblox_progress is not None or self.roseblox_updated:
            return
        if automatic:
            checked_recently = (
                self._roseblox_checked_at
                and time.monotonic() - self._roseblox_checked_at < self_update.CHECK_INTERVAL_SECONDS
            )
            if checked_recently or not self._roseblox_checks_enabled():
                return
        self._roseblox_checking = True
        self._roseblox_checked_at = time.monotonic()
        threading.Thread(target=self._roseblox_check_worker, args=(automatic,), daemon=True).start()

    def _roseblox_check_worker(self, automatic: bool) -> None:
        try:
            release, error = (self._release_checker or self_update.fetch_latest_release)(), None
        except SelfUpdateError as error_:
            release, error = None, str(error_)[:300]
        except Exception as error_:  # noqa: BLE001 - a broken check must not reach the main loop
            release, error = None, str(error_)[:300]
        GLib.idle_add(self._roseblox_checked, release, None if automatic else error)

    def _roseblox_checked(self, release: Release | None, error: str | None) -> bool:
        self._roseblox_checking = False
        self.roseblox_error = error
        if release is not None:
            newer = self_update.is_newer(release.version)
            self.roseblox_release = release if newer and release.version != self._roseblox_dismissed else None
        self._render()
        return GLib.SOURCE_REMOVE

    def _roseblox_dismiss_clicked(self, _button) -> None:
        if self.roseblox_release is not None:
            self._roseblox_dismissed = self.roseblox_release.version
        self.roseblox_release = None
        self._render()

    def _roseblox_open_clicked(self, _button, url: str) -> None:
        self.popover.popdown()
        Gtk.UriLauncher.new(url).launch(self.window, None, None, None)

    def _roseblox_update_clicked(self, _button) -> None:
        release = self.roseblox_release
        target = self_update.appimage_path()
        if release is None or target is None:
            return
        self.roseblox_progress = 0.0
        self.roseblox_error = None
        self._render()
        threading.Thread(target=self._roseblox_update_worker, args=(release, target), daemon=True).start()

    def _roseblox_update_worker(self, release: Release, target: str) -> None:
        def progress(received: int, total: int | None) -> None:
            if total:
                GLib.idle_add(self._roseblox_progressed, min(received / total, 1.0))

        try:
            self_update.update_appimage(release, target, progress)
            error = None
        except Exception as error_:  # noqa: BLE001 - the card must leave its progress state
            error = str(error_)[:300]
        GLib.idle_add(self._roseblox_update_finished, error)

    def _roseblox_progressed(self, fraction: float) -> bool:
        if self.roseblox_progress is not None:
            self.roseblox_progress = fraction
            bar = getattr(self, "_roseblox_bar", None)
            if bar is not None:
                bar.set_fraction(fraction)
                bar.set_text(f"{fraction:.0%}")
        return GLib.SOURCE_REMOVE

    def _roseblox_update_finished(self, error: str | None) -> bool:
        self.roseblox_progress = None
        self.roseblox_error = error
        self.roseblox_updated = error is None
        self._render()
        return GLib.SOURCE_REMOVE

    def _roseblox_restart_clicked(self, _button) -> None:
        application = self.window.get_application()
        if application is not None and hasattr(application, "restart"):
            application.restart()

    def forget_skipped(self) -> None:
        """Skipped versions were cleared in Settings, so look again."""
        self.check_updates()

    def _update_clicked(self, _button, app_type: str) -> None:
        self.popover.popdown()
        self.window.install_update(app_type)

    def _skip_clicked(self, _button, update: AvailableUpdate) -> None:
        try:
            state = self.window.setup_store.skip_version(update.app_type, update.version)
        except (SetupSettingsError, ValueError) as error:
            self.status_label.set_text(f"Could not skip {update.version}: {str(error)[:300]}")
            self.status_label.set_visible(True)
            return
        self.window.setup_state = state
        settings = getattr(self.window, "settings_page", None)
        if settings is not None and hasattr(settings, "_show_skipped_versions"):
            settings._show_skipped_versions(state.skipped_versions)
        self.updates = [item for item in self.updates if item != update]
        self._render()

    # Drawing

    def attention_count(self) -> int:
        roseblox = int(self.roseblox_release is not None)
        return roseblox + len(self.operations) + len(self.updates) + self._unread

    def _render_badge(self) -> None:
        count = self.attention_count()
        self.badge.set_text(str(count) if count < 10 else "9+")
        self.badge.set_visible(count > 0)
        self.set_tooltip_text(f"Notifications ({count})" if count else "Notifications")

    def _render(self) -> None:
        for listbox in (self.roseblox_list, self.downloads_list, self.updates_list, self.recent_list):
            listbox.remove_all()
        self._roseblox_bar = None
        if self.roseblox_release is not None:
            self.roseblox_list.append(self._roseblox_row(self.roseblox_release))
        self.roseblox_box.set_visible(self.roseblox_release is not None)
        for app_type, state in self.operations.items():
            self.downloads_list.append(self._download_row(app_type, state))
        for update in self.updates:
            self.updates_list.append(self._update_row(update))
        for entry in self.recent:
            self.recent_list.append(self._recent_row(entry))
        self.downloads_box.set_visible(bool(self.operations))
        self.updates_box.set_visible(bool(self.updates))
        self.recent_box.set_visible(bool(self.recent))
        self.empty_label.set_visible(
            not (self.operations or self.updates or self.recent or self.roseblox_release)
        )
        problems = [message for message in self.update_errors.values() if message]
        if problems:
            status = f"Could not check for updates: {problems[0]}"
        elif self.roseblox_error and self.roseblox_release is None:
            status = f"Could not check for a new Roseblox: {self.roseblox_error}"
        else:
            status = ""
        self.status_label.set_text(status)
        problems = problems or ([status] if status else [])
        self.status_label.set_visible(bool(problems))
        self._render_badge()

    @staticmethod
    def _label(text: str, *css_classes: str) -> Gtk.Label:
        label = Gtk.Label(label=text)
        for css_class in css_classes:
            label.add_css_class(css_class)
        label.set_halign(Gtk.Align.START)
        label.set_xalign(0)
        label.set_wrap(True)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_max_width_chars(_TEXT_WIDTH_CHARS)
        return label

    def _card_row(self, title: str, subtitle: str) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(10)
        box.set_margin_end(10)
        row.set_child(box)
        row.content = box
        row.title_label = self._label(title)
        box.append(row.title_label)
        row.message_label = self._label(subtitle, "dim-label", "caption")
        box.append(row.message_label)
        return row

    @staticmethod
    def _button_row(*buttons: Gtk.Widget) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_halign(Gtk.Align.END)
        for button in buttons:
            box.append(button)
        return box

    def _download_row(self, app_type: str, state) -> Gtk.Widget:
        title = APP_TITLES.get(app_type, app_type)
        row = self._card_row(f"Installing {title}", state.message or "Working…")
        row.add_css_class("notification-download")
        row.progress_bar = Gtk.ProgressBar()
        if state.fraction is None:
            row.progress_bar.pulse()
        else:
            row.progress_bar.set_fraction(state.fraction)
            row.progress_bar.set_text(state.progress_text or f"{state.fraction:.0%}")
            row.progress_bar.set_show_text(True)
        row.content.append(row.progress_bar)

        buttons = []
        open_button = Gtk.Button.new_with_label("Show")
        open_button.add_css_class("flat")
        open_button.set_tooltip_text(f"Open the {title} page")
        open_button.connect("clicked", self._open_clicked, app_type)
        buttons.append(open_button)
        if state.cancellable:
            cancel = Gtk.Button.new_with_label("Cancel")
            cancel.connect("clicked", self._cancel_clicked, app_type)
            buttons.append(cancel)
        row.content.append(self._button_row(*buttons))
        return row

    def _cancel_clicked(self, _button, app_type: str) -> None:
        page = self.window.pages.get(app_type)
        if page is not None:
            page.cancel_operation()

    def _open_clicked(self, _button, app_type: str) -> None:
        self.popover.popdown()
        self.window.navigate_to(app_type)

    def _update_row(self, update: AvailableUpdate) -> Gtk.Widget:
        subtitle = f"New: {update.version}"
        if update.installed_version:
            subtitle += f"\nYou have: {update.installed_version}"
        row = self._card_row(f"{APP_TITLES.get(update.app_type, update.app_type)} update", subtitle)
        skip = Gtk.Button.new_with_label("Skip")
        skip.set_tooltip_text("Skip this version")
        skip.connect("clicked", self._skip_clicked, update)
        install = Gtk.Button.new_with_label("Update")
        install.add_css_class("suggested-action")
        install.set_sensitive(update.app_type not in self.operations)
        install.connect("clicked", self._update_clicked, update.app_type)
        row.content.append(self._button_row(skip, install))
        return row

    def _roseblox_row(self, release: Release) -> Gtk.Widget:
        appimage = self.install_kind == self_update.INSTALL_APPIMAGE and release.appimage_url
        if self.roseblox_updated:
            row = self._card_row(
                f"Roseblox {release.version} is ready",
                "Restart Roseblox to use the new version.",
            )
            restart = Gtk.Button.new_with_label("Restart")
            restart.add_css_class("suggested-action")
            restart.connect("clicked", self._roseblox_restart_clicked)
            row.content.append(self._button_row(restart))
            return row
        subtitle = f"You have {__version__}."
        hint = ROSEBLOX_UPDATE_HINTS.get(self.install_kind) if not appimage else None
        if hint:
            subtitle += f" {hint}"
        if self.roseblox_error:
            subtitle += f"\nThe update failed: {self.roseblox_error}"
        row = self._card_row(f"Roseblox {release.version} is available", subtitle)
        if self.roseblox_progress is not None:
            bar = Gtk.ProgressBar()
            bar.set_fraction(self.roseblox_progress)
            bar.set_text(f"{self.roseblox_progress:.0%}")
            bar.set_show_text(True)
            row.content.append(bar)
            self._roseblox_bar = bar
            return row
        later = Gtk.Button.new_with_label("Not Now")
        later.set_tooltip_text("Hide this until Roseblox restarts")
        later.connect("clicked", self._roseblox_dismiss_clicked)
        if appimage:
            action = Gtk.Button.new_with_label("Update")
            action.set_tooltip_text("Download the new AppImage and replace this one")
            action.connect("clicked", self._roseblox_update_clicked)
        elif self.install_kind == self_update.INSTALL_SCRIPT:
            action = Gtk.Button.new_with_label("How to Update")
            action.connect("clicked", self._roseblox_open_clicked, self_update.UPDATE_GUIDE_URL)
        else:
            action = Gtk.Button.new_with_label("Open Release")
            action.connect("clicked", self._roseblox_open_clicked, release.page_url)
        action.add_css_class("suggested-action")
        row.content.append(self._button_row(later, action))
        return row

    def _recent_row(self, entry: RecentEntry) -> Gtk.Widget:
        title = APP_TITLES.get(entry.app_type, entry.app_type)
        when = time.strftime("%H:%M", time.localtime(entry.finished_at))
        row = self._card_row(f"{title} · {when}", entry.message)
        icon_name, style = {
            "failed": ("dialog-error-symbolic", "error"),
            "cancelled": ("process-stop-symbolic", "dim-label"),
        }.get(entry.outcome, ("object-select-symbolic", "success"))
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.add_css_class(style)
        icon.set_valign(Gtk.Align.START)
        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row.content.remove(row.title_label)
        title_row.append(icon)
        title_row.append(row.title_label)
        row.content.prepend(title_row)
        return row
