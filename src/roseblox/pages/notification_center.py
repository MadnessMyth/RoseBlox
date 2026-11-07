"""The bell in the header bar: active downloads, available updates and recent results."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gtk

from ..backend.setup import SetupSettingsError
from ..backend.updates import APP_TITLES, CHECK_INTERVAL_SECONDS, AvailableUpdate, check_for_updates

RECENT_LIMIT = 20


@dataclass(frozen=True)
class RecentEntry:
    app_type: str
    message: str
    outcome: str  # "done", "failed" or "cancelled"
    finished_at: float


class NotificationCenter(Gtk.MenuButton):
    def __init__(self, window, *, checker=None, check_interval: float = CHECK_INTERVAL_SECONDS) -> None:
        super().__init__()
        self.window = window
        self._checker = checker or self._check_with_window
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
        icon_overlay.set_child(Gtk.Image.new_from_icon_name("preferences-system-notifications-symbolic"))
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
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        heading_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        heading = Gtk.Label(label="Notifications")
        heading.add_css_class("title-4")
        heading.set_halign(Gtk.Align.START)
        heading.set_hexpand(True)
        heading_row.append(heading)
        self.check_button = Gtk.Button.new_with_label("Check for Updates")
        self.check_button.add_css_class("flat")
        self.check_button.connect("clicked", lambda _button: self.check_updates())
        heading_row.append(self.check_button)
        content.append(heading_row)

        self.empty_label = Gtk.Label(label="Nothing right now. Downloads and Roblox updates show up here.")
        self.empty_label.add_css_class("dim-label")
        self.empty_label.set_wrap(True)
        self.empty_label.set_max_width_chars(40)
        self.empty_label.set_margin_top(12)
        self.empty_label.set_margin_bottom(12)
        content.append(self.empty_label)

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
        self.status_label.set_max_width_chars(48)
        self.status_label.set_halign(Gtk.Align.START)
        self.status_label.set_xalign(0)
        self.status_label.set_visible(False)
        content.append(self.status_label)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_propagate_natural_height(True)
        scroller.set_max_content_height(560)
        scroller.set_min_content_width(380)
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
        if automatic and not self._automatic_checks_enabled():
            return
        if self._checking:
            self._check_pending = True
            return
        self._checking = True
        self.check_button.set_sensitive(False)
        self.check_button.set_label("Checking…")
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
        self.check_button.set_label("Check for Updates")
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
        return len(self.operations) + len(self.updates) + self._unread

    def _render_badge(self) -> None:
        count = self.attention_count()
        self.badge.set_text(str(count) if count < 10 else "9+")
        self.badge.set_visible(count > 0)
        self.set_tooltip_text(f"Notifications ({count})" if count else "Notifications")

    def _render(self) -> None:
        for listbox in (self.downloads_list, self.updates_list, self.recent_list):
            listbox.remove_all()
        for app_type, state in self.operations.items():
            self.downloads_list.append(self._download_row(app_type, state))
        for update in self.updates:
            self.updates_list.append(self._update_row(update))
        for entry in self.recent:
            self.recent_list.append(self._recent_row(entry))
        self.downloads_box.set_visible(bool(self.operations))
        self.updates_box.set_visible(bool(self.updates))
        self.recent_box.set_visible(bool(self.recent))
        self.empty_label.set_visible(not (self.operations or self.updates or self.recent))
        problems = [message for message in self.update_errors.values() if message]
        self.status_label.set_text(
            f"Could not check for updates: {problems[0]}" if problems else ""
        )
        self.status_label.set_visible(bool(problems))
        self._render_badge()

    def _download_row(self, app_type: str, state) -> Gtk.Widget:
        row = Adw.ActionRow.new()
        row.set_title(f"Installing {APP_TITLES.get(app_type, app_type)}")
        row.set_subtitle(GLib.markup_escape_text(state.message or "Working…"))
        row.set_subtitle_lines(2)
        if state.fraction is not None:
            progress = Gtk.ProgressBar()
            progress.set_fraction(state.fraction)
            progress.set_valign(Gtk.Align.CENTER)
            progress.set_size_request(80, -1)
            row.add_suffix(progress)
        if state.cancellable:
            cancel = Gtk.Button.new_with_label("Cancel")
            cancel.set_valign(Gtk.Align.CENTER)
            cancel.connect("clicked", self._cancel_clicked, app_type)
            row.add_suffix(cancel)
        open_button = Gtk.Button.new_from_icon_name("go-next-symbolic")
        open_button.add_css_class("flat")
        open_button.set_valign(Gtk.Align.CENTER)
        open_button.set_tooltip_text(f"Open the {APP_TITLES.get(app_type, app_type)} page")
        open_button.connect("clicked", self._open_clicked, app_type)
        row.add_suffix(open_button)
        return row

    def _cancel_clicked(self, _button, app_type: str) -> None:
        page = self.window.pages.get(app_type)
        if page is not None:
            page.cancel_operation()

    def _open_clicked(self, _button, app_type: str) -> None:
        self.popover.popdown()
        self.window.navigate_to(app_type)

    def _update_row(self, update: AvailableUpdate) -> Gtk.Widget:
        row = Adw.ActionRow.new()
        row.set_title(f"{APP_TITLES.get(update.app_type, update.app_type)} update available")
        subtitle = f"{update.version} is available."
        if update.installed_version:
            subtitle += f" You have {update.installed_version}."
        row.set_subtitle(GLib.markup_escape_text(subtitle))
        row.set_subtitle_lines(3)
        skip = Gtk.Button.new_with_label("Skip")
        skip.set_valign(Gtk.Align.CENTER)
        skip.set_tooltip_text("Skip this version")
        skip.connect("clicked", self._skip_clicked, update)
        row.add_suffix(skip)
        install = Gtk.Button.new_with_label("Update")
        install.add_css_class("suggested-action")
        install.set_valign(Gtk.Align.CENTER)
        install.set_sensitive(update.app_type not in self.operations)
        install.connect("clicked", self._update_clicked, update.app_type)
        row.add_suffix(install)
        return row

    @staticmethod
    def _recent_row(entry: RecentEntry) -> Gtk.Widget:
        row = Adw.ActionRow.new()
        title = APP_TITLES.get(entry.app_type, entry.app_type)
        when = time.strftime("%H:%M", time.localtime(entry.finished_at))
        row.set_title(f"{title} · {when}")
        row.set_subtitle(GLib.markup_escape_text(entry.message))
        row.set_subtitle_lines(3)
        icon_name, style = {
            "failed": ("dialog-error-symbolic", "error"),
            "cancelled": ("process-stop-symbolic", "dim-label"),
        }.get(entry.outcome, ("emblem-ok-symbolic", "success"))
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.add_css_class(style)
        row.add_prefix(icon)
        return row
