"""GTK pages for registered bundles and Rosemary status."""

from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass, replace
from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gio, GLib, Gtk

from ..backend.archive import ArchiveTypeUndetermined, inspect_archive
from ..backend.downloader import DownloadCancelled
from ..backend.download_state import DownloadJob, DownloadStateError
from ..backend.installer import InstallProgress, InstallResult, InstallerService, duplicate_name
from ..backend.library import (
    APP_BUNDLE_NAMES,
    InstallationRecord,
    LibraryDataError,
)
from ..backend.launch_config import LaunchConfigStore, effective_environment
from ..backend.rosemary_paths import guest_path
from ..backend.mods import prepare_launch as prepare_mods
from ..backend.preferences import PreferencesError
from ..backend.rosemary_install import INSTALL_METHOD_AUR, RosemaryInstallState
from ..backend.discord_rpc import effective_application_id, valid_application_id
from ..backend.setup import (
    ACCENT_CHOICES,
    COLOR_STYLES,
    COLOR_THEMES,
    Appearance,
    ClosePreference,
    SetupSettingsError,
    WhilePlaying,
)
from ..backend.game_info import LOCATION_SERVICE
from ..backend.microphone import MAX_RELEASE_DELAY_MS, MICROPHONE_MODES, MODE_HOLD, microphone_problem
from ..backend.microphone_sounds import (
    MAX_VOLUME, OFF, ON, SOUND_SUFFIXES, MicrophoneSoundError, SoundPlayer, sound_problem, store_sound,
)
from .microphone_hotkey import MicrophoneHotkey
from ..backend.updates import APP_TITLES, NotificationLog, UpdateCheckerError, _spawn_detached
from ..activity_service import watcher_arguments
from .rosemary_text import (
    ABOUT_ROW_SUBTITLES,
    INSTALL_BUTTON_LABELS,
    INSTALL_METHOD_MANUAL,
    INSTALL_ROW_SUBTITLES,
    INSTALL_ROW_TITLES,
    rosemary_install_method,
)
from .sections import CollapsibleSection
from .update_checks import UpdateCheckRow
from ..theme import ACCENT_NAMES, system_accent_available
from ..backend.rosemary import (
    LaunchSession,
    ProcessIdentityError,
    RosemaryStatus,
    default_log_directory,
    launch_application,
    stop_application,
    wait_for_running,
)
from ..backend.transfer_rate import TransferRate, format_size, progress_text
from ..backend.versions import VersionCacheError, VersionInfo, VersionTestResult
from ..backend.version_settings import (
    VersionSettingsError,
    VersionSettingsStore,
    VersionSource,
    default_version_source,
)


_NOT_CHECKED = object()
MICROPHONE_MODE_TITLES = {"toggle": "Toggle", "hold": "Hold"}


@dataclass(frozen=True)
class OperationState:
    busy: bool
    message: str
    fraction: float | None
    cancellable: bool
    progress_text: str = ""


class InstallationPage(Gtk.Box):
    def __init__(
        self,
        title: str,
        icon_name: str,
        app_type: str | None,
        *,
        window: Gtk.Window,
        store,
        installer: InstallerService,
        rosemary_status: RosemaryStatus,
        library_error: str | None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.window = window
        self.store = store
        self.installer = installer
        self.app_type = app_type
        self.rosemary_status = rosemary_status
        self.library_error = library_error
        self._launching_ids: set[str] = window.launching_installations
        self._stopping_ids: set[str] = window.stopping_installations
        self._copying_ids: set[str] = getattr(window, "copying_installations", set())
        self._operation_failure_title = "Installation failed"
        self._sessions: dict[str, LaunchSession] = window.process_sessions
        self._launch_buttons: dict[str, Gtk.Button] = {}
        self._remove_buttons: dict[str, Gtk.Button] = {}
        self._duplicate_buttons: dict[str, Gtk.Button] = {}
        self._preferred_buttons: dict[str, Gtk.Button] = {}
        self._validation_by_id: dict[str, str] = {}
        self._preferred_ids: dict[str, str | None] = {"player": None, "studio": None}
        self._resolved_preferred_ids: dict[str, str | None] = {"player": None, "studio": None}
        self._preference_errors: dict[str, str] = {}
        self._preference_busy = False
        self._busy = False
        self._make_preferred_after_install = False
        self._transfer_rate = TransferRate()
        self._library_loading = True
        self._cancel_event: threading.Event | None = None
        self._checking_version = False
        self._version_info: VersionInfo | None = None
        self._version_error: str | None = None
        self._action_buttons: list[Gtk.Button] = []
        self._recovery_buttons: list[Gtk.Button] = []
        self._operation_listeners: list = []
        self._add_button: Gtk.Button | None = None
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_margin_top(28)
        self.set_margin_bottom(28)
        self.set_margin_start(28)
        self.set_margin_end(28)
        self.add_css_class("management-page")

        heading_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(28)
        icon.add_css_class("page-icon")
        heading_row.append(icon)
        heading = Gtk.Label(label=title)
        heading.add_css_class("title-1")
        heading.set_halign(Gtk.Align.START)
        heading.set_hexpand(True)
        heading_row.append(heading)
        if app_type:
            add_button = Gtk.Button.new_with_label("Add Existing…")
            add_button.set_valign(Gtk.Align.CENTER)
            add_button.set_tooltip_text(f"Register a {APP_BUNDLE_NAMES[app_type]} bundle you already have")
            add_button.set_sensitive(False)
            add_button.connect("clicked", self._on_add_clicked)
            heading_row.append(add_button)
            self._add_button = add_button
        self.append(heading_row)

        if app_type:
            self._build_version_controls()
        else:
            import_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            import_app = Gtk.Button.new_with_label("Import Existing App…")
            import_app.add_css_class("suggested-action")
            import_app.set_sensitive(False)
            import_app.connect("clicked", self._on_import_app_clicked)
            import_row.append(import_app)
            self._action_buttons.append(import_app)
            import_archive = Gtk.Button.new_with_label("Import ZIP Archive…")
            import_archive.set_sensitive(False)
            import_archive.connect("clicked", self._on_import_archive_clicked)
            import_row.append(import_archive)
            self._action_buttons.append(import_archive)
            import_row.set_halign(Gtk.Align.START)
            self.append(import_row)

        self.notice = Gtk.Label()
        self.notice.set_xalign(0)
        self.notice.set_wrap(True)
        self.notice.add_css_class("status-notice")
        self.notice.set_visible(False)
        self.append(self.notice)

        self.workflow_status = Gtk.Label()
        self.workflow_status.set_xalign(0)
        self.workflow_status.set_wrap(True)
        self.workflow_status.set_visible(False)
        self.workflow_status.add_css_class("dim-label")
        self.append(self.workflow_status)

        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_show_text(True)
        self.progress_bar.set_visible(False)
        self.append(self.progress_bar)

        self.cancel_button = Gtk.Button.new_with_label("Cancel Download")
        self.cancel_button.set_halign(Gtk.Align.START)
        self.cancel_button.set_visible(False)
        self.cancel_button.connect("clicked", self._on_cancel_clicked)
        self.append(self.cancel_button)

        self.list_box = Gtk.ListBox()
        self.list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list_box.set_valign(Gtk.Align.START)
        self.list_box.add_css_class("boxed-list")
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.list_box)
        scroller.set_vexpand(True)
        self.append(scroller)
        self.apply_library_snapshot([], {}, None, loading=True)
        if app_type:
            self._load_cached_version()

    def _build_version_controls(self) -> None:
        controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        controls.add_css_class("version-controls")
        top_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.latest_label = Gtk.Label(label="Latest available version: not checked")
        self.latest_label.set_xalign(0)
        self.latest_label.set_hexpand(True)
        top_row.append(self.latest_label)
        self.refresh_version_button = Gtk.Button.new_with_label("Refresh Version")
        self.refresh_version_button.connect("clicked", self._on_refresh_version_clicked)
        top_row.append(self.refresh_version_button)
        controls.append(top_row)
        self.last_checked_label = Gtk.Label(label="No successful version check yet.")
        self.last_checked_label.set_xalign(0)
        self.last_checked_label.add_css_class("dim-label")
        controls.append(self.last_checked_label)
        self.installed_versions_label = Gtk.Label(label="Installed versions: checking library…")
        self.installed_versions_label.set_xalign(0)
        self.installed_versions_label.set_wrap(True)
        self.installed_versions_label.add_css_class("dim-label")
        controls.append(self.installed_versions_label)
        action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.download_button = Gtk.Button.new_with_label("Download and Install Latest")
        self.download_button.add_css_class("suggested-action")
        self.download_button.set_sensitive(False)
        self.download_button.connect("clicked", self._on_download_clicked)
        action_row.append(self.download_button)
        controls.append(action_row)
        self.recovery_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.recovery_box.set_visible(False)
        controls.append(self.recovery_box)
        self.append(controls)

    def refresh(self) -> None:
        if hasattr(self.window, "refresh_pages"):
            self.window.refresh_pages()
        else:
            self.apply_library_snapshot([], {}, "Library refresh is unavailable.", loading=False)

    def apply_library_snapshot(
        self,
        records: list[InstallationRecord],
        validation_by_id: dict[str, str],
        error: str | None,
        *,
        loading: bool,
        preferred_ids: dict[str, str | None] | None = None,
        resolved_preferred_ids: dict[str, str | None] | None = None,
        preference_errors: dict[str, str] | None = None,
    ) -> None:
        self._library_loading = loading
        self.library_error = error
        self._validation_by_id = dict(validation_by_id)
        if preferred_ids is not None:
            self._preferred_ids = dict(preferred_ids)
        if resolved_preferred_ids is not None:
            self._resolved_preferred_ids = dict(resolved_preferred_ids)
        if preference_errors is not None:
            self._preference_errors = dict(preference_errors)
        if loading:
            self._launch_buttons = {}
            self._remove_buttons = {}
            self._duplicate_buttons = {}
            self._preferred_buttons = {}
            self._clear_rows()
            self.notice.set_label("Loading registered installations…")
            self.notice.set_visible(True)
            return
        if error:
            self._launch_buttons = {}
            self._remove_buttons = {}
            self._duplicate_buttons = {}
            self._preferred_buttons = {}
            self._clear_rows()
            self.notice.set_label(f"{error} No changes will be saved until the library file is repaired.")
            self.notice.set_visible(True)
            self._set_library_actions(False)
            return
        self.notice.set_visible(False)
        if self.app_type:
            records = [record for record in records if record.app_type == self.app_type]
        self._render_records(records)
        self._refresh_version_summary(records)
        self._set_library_actions(True)

    def _set_library_actions(self, enabled: bool) -> None:
        enabled = enabled and not self._busy
        if self._add_button is not None:
            self._add_button.set_sensitive(enabled)
        if self.app_type:
            self.download_button.set_sensitive(enabled)
        for button in self._action_buttons:
            button.set_sensitive(enabled)
        for button in self._duplicate_buttons.values():
            button.set_sensitive(enabled)

    def _clear_rows(self) -> None:
        child = self.list_box.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.list_box.remove(child)
            child = following

    def _render_records(self, records: list[InstallationRecord]) -> None:
        self._launch_buttons = {}
        self._remove_buttons = {}
        self._duplicate_buttons = {}
        self._preferred_buttons = {}
        self._clear_rows()
        if not records:
            empty = Gtk.Label(
                label=(
                    f"No {self.app_type.title()} installations registered yet."
                    if self.app_type
                    else "No Roblox installations registered yet."
                )
            )
            empty.add_css_class("dim-label")
            empty.set_margin_top(24)
            empty.set_margin_bottom(24)
            self.list_box.append(empty)
            return

        for record in records:
            self.list_box.append(self._make_row(record))

    def _make_row(self, record: InstallationRecord) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        contents = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        contents.set_margin_top(12)
        contents.set_margin_bottom(12)
        contents.set_margin_start(14)
        contents.set_margin_end(14)

        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        details.set_hexpand(True)
        name = Gtk.Label(label=record.display_name)
        name.set_xalign(0)
        name.add_css_class("heading")
        details.append(name)
        kind = "Player" if record.app_type == "player" else "Studio"
        state = self._validation_by_id.get(record.id, record.validation_state.title())
        version_text = record.version or "Unknown version"
        source_text = "Managed" if record.managed else "External"
        details_text = f"{kind} · {version_text} · {source_text} · {state}"
        if self.app_type == record.app_type and record.version and self._version_info:
            version_state = "Latest" if record.version == self._version_info.version else "Update available"
            details_text += f" · {version_state}"
        meta = Gtk.Label(label=details_text)
        meta.set_xalign(0)
        meta.add_css_class("dim-label")
        details.append(meta)
        bundle_path = Gtk.Label(label=record.bundle_path)
        bundle_path.set_xalign(0)
        bundle_path.set_selectable(True)
        bundle_path.set_max_width_chars(64)
        bundle_path.add_css_class("dim-label")
        details.append(bundle_path)
        contents.append(details)

        preferred_id = self._preferred_ids.get(record.app_type)
        if record.id == preferred_id:
            available = self._validation_by_id.get(record.id) == "Valid"
            marker_text = (
                f"Preferred {kind} installation"
                if available
                else f"Saved preferred {kind} installation · unavailable"
            )
            preferred_marker = Gtk.Label(label=marker_text)
            preferred_marker.set_xalign(0)
            preferred_marker.add_css_class("dim-label")
            details.append(preferred_marker)

        if self.app_type:
            preferred_button = Gtk.Button.new_with_label(
                "Preferred" if record.id == preferred_id else "Set as preferred"
            )
            preferred_button.set_valign(Gtk.Align.CENTER)
            preferred_button.set_sensitive(record.id != preferred_id and not self._preference_busy)
            preferred_button.connect("clicked", self._on_set_preferred_clicked, record)
            self._preferred_buttons[record.id] = preferred_button
            contents.append(preferred_button)

            if record.app_type == "studio":
                place_button = Gtk.Button.new_with_label("Open Place…")
                place_button.set_valign(Gtk.Align.CENTER)
                place_button.set_tooltip_text("Open a .rbxl or .rbxlx file in this Studio installation")
                place_button.set_sensitive(
                    record.id not in self._launching_ids
                    and record.id not in self._sessions
                    and record.id not in self._stopping_ids
                )
                place_button.connect("clicked", self._on_open_place_clicked, record)
                contents.append(place_button)

            launch_button = Gtk.Button()
            launch_button.set_valign(Gtk.Align.CENTER)
            launch_button.add_css_class("suggested-action")
            launch_button.connect("clicked", self._on_launch_clicked, record)
            self._launch_buttons[record.id] = launch_button
            self._update_launch_button(record.id)
            contents.append(launch_button)

            if self._multiple_allowed(record):
                duplicate = Gtk.Button.new_with_label("Duplicate…")
                duplicate.set_valign(Gtk.Align.CENTER)
                duplicate.set_tooltip_text("Copy this installation so a second Player can run from the copy")
                duplicate.set_sensitive(not self._busy and self._validation_by_id.get(record.id) == "Valid")
                duplicate.connect("clicked", self._on_duplicate_clicked, record)
                self._duplicate_buttons[record.id] = duplicate
                contents.append(duplicate)

        remove = Gtk.Button.new_with_label("Remove")
        remove.set_valign(Gtk.Align.CENTER)
        remove.set_tooltip_text("Remove this registration from Roseblox only")
        remove.set_sensitive(
            record.id not in self._launching_ids
            and record.id not in self._sessions
            and record.id not in self._stopping_ids
            and record.id not in self._copying_ids
        )
        remove.connect("clicked", self._on_remove_clicked, record)
        self._remove_buttons[record.id] = remove
        contents.append(remove)
        row.set_child(contents)
        return row

    def _on_set_preferred_clicked(self, _button: Gtk.Button, record: InstallationRecord) -> None:
        if self._preference_busy or self.app_type is None:
            return
        self._preference_busy = True
        for button in self._preferred_buttons.values():
            button.set_sensitive(False)
        threading.Thread(target=self._set_preferred_worker, args=(record,), daemon=True).start()

    def _set_preferred_worker(self, record: InstallationRecord) -> None:
        try:
            self.window.preferences_store.set_preferred(record.app_type, record.id, self.store)
        except Exception as error:
            GLib.idle_add(self._preferred_save_finished, str(error))
            return
        GLib.idle_add(self._preferred_save_finished, None)

    def _preferred_save_finished(self, error: str | None) -> bool:
        self._preference_busy = False
        if error is not None:
            self._show_error("Could not set preferred installation", error)
            self._render_records(self._current_records())
            return GLib.SOURCE_REMOVE
        self._refresh_all(reload_library=False, reload_preferences=True)
        return GLib.SOURCE_REMOVE

    def _on_add_clicked(self, _button: Gtk.Button) -> None:
        picker = Gtk.FileDialog.new()
        title = f"Select {APP_BUNDLE_NAMES[self.app_type]}" if self.app_type else "Select Roblox application bundle"
        picker.set_title(title)
        picker.set_accept_label("Select Bundle")
        picker.select_folder(self.window, None, self._on_folder_selected, None)

    def _on_folder_selected(self, picker: Gtk.FileDialog, result: Gio.AsyncResult, _data) -> None:
        try:
            selected = picker.select_folder_finish(result)
        except GLib.Error as error:
            if error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                return
            self._show_error("Could not select bundle", str(error))
            return
        if selected is None or selected.get_path() is None:
            self._show_error("Could not select bundle", "The selected location is not a local folder.")
            return

        bundle = Path(selected.get_path())
        if self.app_type is not None:
            self._prompt_bundle_name(bundle, self.app_type)
            return
        app_type = next(
            (kind for kind, bundle_name in APP_BUNDLE_NAMES.items() if bundle.name == bundle_name),
            None,
        )
        if app_type:
            self._prompt_bundle_name(bundle, app_type)
        else:
            self._prompt_type(
                "Choose application type",
                "Roseblox could not identify this bundle by its folder name.",
                self._prompt_bundle_name,
                bundle,
            )

    def _prompt_bundle_name(self, bundle: Path, app_type: str) -> None:
        default_name = bundle.name.removesuffix(".app")
        prompt = Adw.MessageDialog.new(
            self.window,
            "Name this installation",
            "Choose a recognizable name for this registered bundle.",
        )
        prompt.set_close_response("cancel")
        prompt.add_response("cancel", "Cancel")
        prompt.add_response("add", "Add to Library")
        prompt.set_default_response("add")
        prompt.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)
        name_entry = Gtk.Entry()
        name_entry.set_text(default_name)
        name_entry.set_activates_default(True)
        prompt.set_extra_child(name_entry)
        prompt.connect("response", self._on_name_response, bundle, app_type, name_entry)
        prompt.present()

    def _on_name_response(
        self,
        dialog: Adw.MessageDialog,
        response: str,
        bundle: Path,
        app_type: str,
        name_entry: Gtk.Entry,
    ) -> None:
        dialog.destroy()
        if response != "add":
            return
        display_name = name_entry.get_text()
        threading.Thread(
            target=self._register_bundle_worker,
            args=(bundle, app_type, display_name),
            daemon=True,
        ).start()

    def _register_bundle_worker(self, bundle: Path, app_type: str, display_name: str) -> None:
        try:
            self.store.add(app_type, bundle, display_name)
        except (ValueError, LibraryDataError, OSError) as error:
            GLib.idle_add(self._registration_failed, str(error))
            return
        GLib.idle_add(self._registration_succeeded)

    def _registration_failed(self, message: str) -> bool:
        self._show_error("Could not add installation", message)
        return GLib.SOURCE_REMOVE

    def _registration_succeeded(self) -> bool:
        self._refresh_all()
        return GLib.SOURCE_REMOVE

    def _prompt_type(self, heading, body, continuation, *args) -> None:
        dialog = Adw.MessageDialog.new(self.window, heading, body)
        dialog.set_close_response("cancel")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("player", "Player")
        dialog.add_response("studio", "Studio")
        dialog.set_default_response("cancel")
        dialog.connect("response", self._on_type_response, continuation, args)
        dialog.present()

    def _on_type_response(self, dialog, response, continuation, args) -> None:
        dialog.destroy()
        if response in APP_BUNDLE_NAMES:
            continuation(*args, response)

    def _on_import_app_clicked(self, _button: Gtk.Button) -> None:
        self._on_add_clicked(_button)

    def _on_import_archive_clicked(self, _button: Gtk.Button) -> None:
        picker = Gtk.FileDialog.new()
        picker.set_title("Select Roblox ZIP archive")
        picker.set_accept_label("Inspect Archive")
        file_filter = Gtk.FileFilter()
        file_filter.set_name("ZIP archives")
        file_filter.add_pattern("*.zip")
        picker.set_default_filter(file_filter)
        picker.open(self.window, None, self._on_archive_selected, None)

    def _on_archive_selected(self, picker: Gtk.FileDialog, result: Gio.AsyncResult, _data) -> None:
        try:
            selected = picker.open_finish(result)
        except GLib.Error as error:
            if error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                return
            self._show_error("Could not select archive", str(error))
            return
        if selected is None or selected.get_path() is None:
            self._show_error("Could not select archive", "The selected archive is not a local file.")
            return
        archive_path = Path(selected.get_path())
        threading.Thread(
            target=self._inspect_archive_worker,
            args=(archive_path,),
            daemon=True,
        ).start()

    def _inspect_archive_worker(self, archive_path: Path) -> None:
        try:
            inspection = inspect_archive(archive_path)
        except Exception as error:
            GLib.idle_add(self._archive_inspection_failed, str(error))
            return
        GLib.idle_add(self._archive_inspected, archive_path, inspection)

    def _archive_inspection_failed(self, message: str) -> bool:
        self._show_error("Could not inspect archive", message)
        return GLib.SOURCE_REMOVE

    def _archive_inspected(self, archive_path: Path, inspection) -> bool:
        if inspection.detected_type:
            self._prompt_archive_name(archive_path, inspection.detected_type)
        else:
            self._prompt_type(
                "Choose archive type",
                "The archive contents do not identify one Roblox application type reliably.",
                self._prompt_archive_name,
                archive_path,
            )
        return GLib.SOURCE_REMOVE

    def _prompt_archive_name(self, archive_path: Path, app_type: str) -> None:
        default_name = archive_path.stem
        for suffix in ("-RobloxPlayer", "-RobloxStudioApp"):
            if default_name.endswith(suffix):
                default_name = default_name[: -len(suffix)]
        if default_name.startswith("version-"):
            default_name = f"Roblox {app_type.title()} {default_name}"
        prompt = Adw.MessageDialog.new(
            self.window,
            "Name this installation",
            "Choose a recognizable name for the imported archive.",
        )
        prompt.set_close_response("cancel")
        prompt.add_response("cancel", "Cancel")
        prompt.add_response("import", "Import Archive")
        prompt.set_default_response("import")
        prompt.set_response_appearance("import", Adw.ResponseAppearance.SUGGESTED)
        name_entry = Gtk.Entry()
        name_entry.set_text(default_name)
        name_entry.set_activates_default(True)
        prompt.set_extra_child(name_entry)
        prompt.connect("response", self._on_archive_name_response, archive_path, app_type, name_entry)
        prompt.present()

    def _on_archive_name_response(self, dialog, response, archive_path, app_type, name_entry) -> None:
        dialog.destroy()
        if response != "import":
            return
        self._begin_operation("Preparing archive import…", cancellable=False)
        threading.Thread(
            target=self._import_archive_worker,
            args=(archive_path, app_type, name_entry.get_text()),
            daemon=True,
        ).start()

    def _import_archive_worker(self, archive_path, app_type, display_name) -> None:
        try:
            result = self.installer.import_archive(
                archive_path,
                app_type=app_type,
                display_name=display_name,
                progress_callback=self._post_progress,
            )
        except Exception as error:
            GLib.idle_add(self._operation_finished, None, error)
            return
        GLib.idle_add(self._operation_finished, result, None)

    def _load_cached_version(self) -> None:
        threading.Thread(target=self._load_cached_version_worker, daemon=True).start()

    def _load_cached_version_worker(self) -> None:
        version_info = None
        version_error = None
        try:
            version_info = self.installer.version_cache.load().get(self.app_type)
        except VersionCacheError as error:
            version_error = str(error)
        GLib.idle_add(self._cached_version_loaded, version_info, version_error)

    def _current_records(self) -> list[InstallationRecord]:
        records = getattr(self.window, "library_records", [])
        return [record for record in records if self.app_type is None or record.app_type == self.app_type]

    def _cached_version_loaded(self, version_info, version_error) -> bool:
        self._version_info = version_info
        self._version_error = version_error
        self._render_version_labels()
        records = self._current_records()
        self._refresh_version_summary(records)
        self._render_records(records)
        return GLib.SOURCE_REMOVE

    def _start_version_check(self) -> None:
        if not self.app_type or self._checking_version:
            return
        self._checking_version = True
        self.refresh_version_button.set_sensitive(False)
        if self._version_info is None:
            self.latest_label.set_text("Latest available version: checking…")
        threading.Thread(target=self._version_check_worker, daemon=True).start()

    def _version_check_worker(self) -> None:
        info = None
        error = None
        cache_error = None
        try:
            source = self.installer.version_settings.load_for(self.app_type)
            info = self.installer.version_client.fetch(self.app_type, source)
            try:
                self.installer.version_cache.update(self.app_type, info)
            except VersionCacheError as write_error:
                cache_error = str(write_error)
        except Exception as fetch_error:
            error = str(fetch_error)
        GLib.idle_add(self._version_check_complete, info, error, cache_error)

    def _version_check_complete(self, info, error, cache_error) -> bool:
        self._checking_version = False
        self.refresh_version_button.set_sensitive(True)
        if info is not None:
            self._version_info = info
            self._version_error = (
                f"Version found, but could not update the cache: {cache_error}" if cache_error else None
            )
        elif error:
            self._version_error = error
        self._render_version_labels()
        records = self._current_records()
        self._refresh_version_summary(records)
        self._render_records(records)
        return GLib.SOURCE_REMOVE

    def _render_version_labels(self) -> None:
        if self._version_info:
            self.latest_label.set_text(f"Latest available version: {self._version_info.version}")
            checked = self._version_info.checked_at
            suffix = f" · Refresh failed: {self._version_error}" if self._version_error else ""
            self.last_checked_label.set_text(f"Last successful check: {checked}{suffix}")
        elif self._version_error:
            self.latest_label.set_text("Latest available version: unavailable")
            self.last_checked_label.set_text(f"Version check failed: {self._version_error}")
        else:
            self.latest_label.set_text("Latest available version: not checked")
            self.last_checked_label.set_text("No successful version check yet.")

    def _refresh_version_summary(self, records: list[InstallationRecord]) -> None:
        if not self.app_type:
            return
        versions = []
        for record in records:
            if record.version:
                if self._version_info is None:
                    state = "installed"
                elif record.version == self._version_info.version:
                    state = "latest"
                else:
                    state = "update available"
                versions.append(f"{record.display_name}: {record.version} ({state})")
            else:
                versions.append(f"{record.display_name}: version unknown")
        self.installed_versions_label.set_text(
            "Installed versions: " + ("; ".join(versions) if versions else "none registered")
        )

    def _on_refresh_version_clicked(self, _button: Gtk.Button) -> None:
        self._start_version_check()

    def _on_download_clicked(self, _button: Gtk.Button) -> None:
        if self._busy or self.library_error:
            return
        self._make_preferred_after_install = False
        self._cancel_event = threading.Event()
        self._begin_operation("Checking the latest version…", cancellable=True)
        threading.Thread(target=self._download_worker, daemon=True).start()

    def start_install_from_setup(self, *, make_preferred: bool = False) -> bool:
        """Start the same current-version installation flow used by this page.

        With ``make_preferred`` the installed version becomes the one Home and links
        launch, which is what accepting an update notification means.
        """
        if self._busy or self.library_error:
            return False
        self._on_download_clicked(self.download_button)
        self._make_preferred_after_install = make_preferred
        return True

    @property
    def operation_busy(self) -> bool:
        return self._busy

    def add_operation_listener(self, callback) -> None:
        """Call ``callback(app_type, OperationState)`` whenever an install operation changes."""
        self._operation_listeners.append(callback)

    def cancel_operation(self) -> None:
        self._on_cancel_clicked(self.cancel_button)

    def _emit_operation(self, message: str | None = None) -> None:
        if message is None:
            message = self.workflow_status.get_text() if self.workflow_status.get_visible() else ""
        state = OperationState(
            busy=self._busy,
            message=message,
            fraction=self.progress_bar.get_fraction() if self.progress_bar.get_visible() else None,
            cancellable=self.cancel_button.get_visible() and self.cancel_button.get_sensitive(),
            progress_text=self.progress_bar.get_text() or "" if self.progress_bar.get_visible() else "",
        )
        for callback in list(self._operation_listeners):
            callback(self.app_type, state)

    def _load_recovery_jobs(self) -> None:
        if not self.app_type:
            return
        if hasattr(self.window, "refresh_pages"):
            self.window.refresh_pages(reload_library=False, reload_recovery=True)
            return
        threading.Thread(target=self._recovery_jobs_worker, daemon=True).start()

    def _recovery_jobs_worker(self) -> None:
        try:
            jobs = self.installer.recoverable_downloads(self.app_type)
            error = None
        except (DownloadStateError, OSError) as failure:
            jobs = []
            error = str(failure)
        GLib.idle_add(self._render_recovery_jobs, jobs, error)

    def _render_recovery_jobs(self, jobs: list[DownloadJob], error: str | None) -> bool:
        if self.app_type:
            jobs = [job for job in jobs if job.app_type == self.app_type]
        child = self.recovery_box.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.recovery_box.remove(child)
            child = following
        self._recovery_buttons = []
        if error:
            label = Gtk.Label(label=f"Saved download recovery is unavailable: {error}")
            label.set_xalign(0)
            label.set_wrap(True)
            self.recovery_box.append(label)
            self.recovery_box.set_visible(True)
            return GLib.SOURCE_REMOVE
        for job in jobs:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            label_text = f"Saved {job.version} download · {self._format_size(job.downloaded)}"
            if job.total:
                label_text += f" of {self._format_size(job.total)}"
            if job.state == "downloaded":
                label_text += " · ready to install"
            elif job.state == "invalid":
                label_text += " · unavailable"
            label = Gtk.Label(label=label_text)
            label.set_xalign(0)
            details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            details.set_hexpand(True)
            details.append(label)
            progress = Gtk.ProgressBar()
            progress.set_show_text(True)
            if job.total:
                fraction = min(job.downloaded / job.total, 1.0)
                progress.set_fraction(fraction)
                progress.set_text(f"{fraction:.0%}")
            else:
                progress.set_text("Progress unknown")
            details.append(progress)
            row.append(details)
            retry = Gtk.Button.new_with_label(
                "Install Download" if job.state == "downloaded" else "Resume / Retry"
            )
            retry.set_sensitive(job.state in {"interrupted", "downloaded"} and not self._busy)
            retry.connect("clicked", self._on_recovery_retry_clicked, job.id)
            row.append(retry)
            self._recovery_buttons.append(retry)
            if job.state != "downloaded":
                discard = Gtk.Button.new_with_label("Discard")
                discard.set_sensitive(not self._busy)
                discard.connect("clicked", self._on_recovery_discard_clicked, job.id)
                row.append(discard)
                self._recovery_buttons.append(discard)
            self.recovery_box.append(row)
        self.recovery_box.set_visible(bool(jobs))
        return GLib.SOURCE_REMOVE

    def set_recovery_jobs(self, jobs: list[DownloadJob], error: str | None) -> None:
        """Apply the window's shared recovery snapshot to this app page."""
        if self.app_type is not None:
            self._render_recovery_jobs(jobs, error)

    def _on_recovery_retry_clicked(self, _button: Gtk.Button, job_id: str) -> None:
        if self._busy:
            return
        self._cancel_event = threading.Event()
        self._begin_operation("Restoring saved download…", cancellable=True)
        threading.Thread(target=self._retry_download_worker, args=(job_id,), daemon=True).start()

    def _retry_download_worker(self, job_id: str) -> None:
        try:
            result = self.installer.retry_download(
                job_id,
                self._cancel_event,
                self._post_progress,
            )
        except Exception as error:
            GLib.idle_add(self._operation_finished, None, error)
            return
        GLib.idle_add(self._operation_finished, result, None)

    def _on_recovery_discard_clicked(self, _button: Gtk.Button, job_id: str) -> None:
        if self._busy:
            return
        threading.Thread(target=self._discard_download_worker, args=(job_id,), daemon=True).start()

    def _discard_download_worker(self, job_id: str) -> None:
        try:
            self.installer.discard_download(job_id)
        except (DownloadStateError, OSError) as error:
            GLib.idle_add(self._show_error, "Could not discard saved download", str(error))
            return
        self._load_recovery_jobs()

    def _download_worker(self) -> None:
        try:
            result = self.installer.download_latest(
                self.app_type,
                self._cancel_event,
                self._post_progress,
            )
        except Exception as error:
            GLib.idle_add(self._operation_finished, None, error)
            return
        GLib.idle_add(self._operation_finished, result, None)

    def _begin_operation(self, message: str, *, cancellable: bool) -> None:
        self._busy = True
        self.workflow_status.set_text(message)
        self.workflow_status.set_visible(True)
        self.progress_bar.set_fraction(0.0)
        self.progress_bar.set_text("")
        self.progress_bar.set_visible(True)
        self._transfer_rate.reset()
        self.cancel_button.set_visible(False)
        self.cancel_button.set_label("Cancel Download")
        if self.app_type:
            self.download_button.set_sensitive(False)
        for button in self._action_buttons:
            button.set_sensitive(False)
        for button in self._duplicate_buttons.values():
            button.set_sensitive(False)
        for button in self._recovery_buttons:
            button.set_sensitive(False)
        self._emit_operation()

    def _post_progress(self, progress: InstallProgress) -> None:
        GLib.idle_add(self._show_progress, progress)

    def _show_progress(self, progress: InstallProgress) -> bool:
        if progress.phase == "checking-version":
            message = "Checking the latest Roblox version…"
        elif progress.phase == "downloading":
            message = f"Downloaded {self._format_size(progress.downloaded)}"
            self._transfer_rate.add(progress.downloaded)
            if progress.total is not None:
                message += f" of {self._format_size(progress.total)}"
                fraction = min(max(progress.downloaded / progress.total if progress.total else 0.0, 0.0), 1.0)
                self.progress_bar.set_fraction(fraction)
            else:
                fraction = None
                self.progress_bar.pulse()
            self.progress_bar.set_text(
                progress_text(fraction, self._transfer_rate, progress.downloaded, progress.total)
            )
            self.cancel_button.set_visible(self._cancel_event is not None)
            self.cancel_button.set_sensitive(True)
        elif progress.phase == "copying":
            message = f"Copied {self._format_size(progress.downloaded)}"
            fraction = None
            if progress.total:
                message += f" of {self._format_size(progress.total)}"
                fraction = min(max(progress.downloaded / progress.total, 0.0), 1.0)
                self.progress_bar.set_fraction(fraction)
            else:
                self.progress_bar.pulse()
            self.progress_bar.set_text(f"{fraction:.0%}" if fraction is not None else "")
            self.cancel_button.set_visible(self._cancel_event is not None)
        elif progress.phase == "extracting":
            message = "Extracting and validating application bundle with bsdtar…"
            self.progress_bar.set_text("")
            self.cancel_button.set_visible(False)
            self.progress_bar.pulse()
        elif progress.phase == "registering":
            message = "Registering installation in the Library…"
            self.cancel_button.set_visible(False)
            self.progress_bar.pulse()
        elif progress.phase == "finalizing-archive":
            message = "Preserving the completed archive…"
            self.cancel_button.set_visible(False)
            self.progress_bar.pulse()
        elif progress.phase == "downloaded":
            message = "Download complete. Preparing installation…"
            self.cancel_button.set_visible(False)
            self.progress_bar.set_fraction(1.0)
            self.progress_bar.set_text("100%")
        elif progress.phase == "complete":
            message = "Installation complete."
            self.progress_bar.set_fraction(1.0)
            self.cancel_button.set_visible(False)
        else:
            message = progress.phase.replace("-", " ").capitalize() + "…"
        self.workflow_status.set_text(message)
        self._emit_operation()
        return GLib.SOURCE_REMOVE

    def _on_cancel_clicked(self, _button: Gtk.Button) -> None:
        if self._cancel_event is not None:
            self._cancel_event.set()
            self.cancel_button.set_label("Canceling…")
            self.cancel_button.set_sensitive(False)
            self._emit_operation()

    def _operation_finished(self, result: InstallResult | None, error: Exception | None) -> bool:
        failure_title = self._operation_failure_title
        self._operation_failure_title = "Installation failed"
        self._busy = False
        self._cancel_event = None
        make_preferred = self._make_preferred_after_install
        self._make_preferred_after_install = False
        self.cancel_button.set_visible(False)
        self.progress_bar.set_visible(False)
        self._set_library_actions(not self._library_loading and self.library_error is None)
        if error is not None:
            self._refresh_all(reload_library=False, reload_recovery=True)
            if isinstance(error, DownloadCancelled):
                self.workflow_status.set_text(str(error))
                self.workflow_status.set_visible(True)
                self._emit_operation()
            else:
                self.workflow_status.set_visible(False)
                self._emit_operation(message=f"{failure_title}: {error}")
                self._show_error(failure_title, str(error))
            return GLib.SOURCE_REMOVE
        if result is not None:
            if result.already_installed:
                message = f"{result.record.display_name} is already registered at {result.record.bundle_path}."
            elif result.copied_from:
                message = (
                    f"Copied {result.copied_from} to {result.record.display_name}. "
                    "Launch it while the other Player runs to play a second instance."
                )
            else:
                message = f"Installed {result.record.display_name}. Archive preserved at {result.archive_path}."
            if make_preferred:
                message += " It is now the preferred installation."
                self._preference_busy = True
                threading.Thread(
                    target=self._set_preferred_worker, args=(result.record,), daemon=True
                ).start()
            self.workflow_status.set_text(message)
            self.workflow_status.set_visible(True)
            self._refresh_all(reload_library=True, reload_recovery=True)
        else:
            self._refresh_all(reload_library=False, reload_recovery=True)
        self._emit_operation()
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _format_size(size: int) -> str:
        return format_size(size)

    def _on_launch_clicked(self, _button: Gtk.Button, record: InstallationRecord) -> None:
        self.request_launch_toggle(record)

    def request_launch_toggle(self, record: InstallationRecord) -> None:
        session = self._sessions.get(record.id)
        if session is not None:
            self._stopping_ids.add(record.id)
            self._refresh_all(reload_library=False)
            threading.Thread(
                target=self._stop_worker,
                args=(record.id, session),
                daemon=True,
            ).start()
            return
        if record.id in self._launching_ids or record.id in self._stopping_ids:
            return

        self._launching_ids.add(record.id)
        self._refresh_all(reload_library=False)
        threading.Thread(
            target=self._launch_worker,
            args=(record.id, record),
            daemon=True,
        ).start()

    def request_launch_url(self, record: InstallationRecord, uri: str) -> None:
        """Route a validated deep link through the existing Rosemary session flow."""
        if self._active(record.id) and self._multiple_allowed(record):
            idle = self._idle_copy(record)
            if idle is None:
                self._show_error(
                    "Every Roblox Player is already running",
                    "Use Duplicate… on the Player page to make another copy, then open the link again.",
                )
                return
            record = idle
        if record.id in self._sessions or record.id in self._launching_ids or record.id in self._stopping_ids:
            self._show_error("Application already active", "Stop this installation before opening another Roblox link.")
            return
        self._launching_ids.add(record.id)
        self._refresh_all(reload_library=False)
        threading.Thread(
            target=self._launch_worker, args=(record.id, record, (uri,)), daemon=True
        ).start()

    def _active(self, installation_id: str) -> bool:
        return (
            installation_id in self._sessions
            or installation_id in self._launching_ids
            or installation_id in self._stopping_ids
        )

    def _multiple_allowed(self, record: InstallationRecord) -> bool:
        state = getattr(self.window, "setup_state", None)
        return record.app_type == "player" and bool(getattr(state, "multiple_players", False))

    def _idle_copy(self, record: InstallationRecord) -> InstallationRecord | None:
        """Another valid installation of the same app that is not running, the same version first."""
        candidates = [
            other
            for other in getattr(self.window, "library_records", [])
            if other.app_type == record.app_type
            and other.id != record.id
            and not self._active(other.id)
            and self._validation_by_id.get(other.id) == "Valid"
        ]
        candidates.sort(key=lambda other: other.version != record.version)
        return candidates[0] if candidates else None

    def _on_duplicate_clicked(self, _button: Gtk.Button, record: InstallationRecord) -> None:
        if self._busy:
            return
        taken = {other.display_name for other in getattr(self.window, "library_records", [])}
        prompt = Adw.MessageDialog.new(
            self.window,
            "Duplicate this installation",
            "Roseblox copies the whole app so a second Roblox Player can run from the copy. "
            f"This uses about as much disk space as {record.display_name}. Roblox settings and mods are shared.",
        )
        prompt.set_close_response("cancel")
        prompt.add_response("cancel", "Cancel")
        prompt.add_response("duplicate", "Duplicate")
        prompt.set_default_response("duplicate")
        prompt.set_response_appearance("duplicate", Adw.ResponseAppearance.SUGGESTED)
        name_entry = Gtk.Entry()
        name_entry.set_text(duplicate_name(record.display_name, taken))
        name_entry.set_activates_default(True)
        prompt.set_extra_child(name_entry)
        prompt.connect("response", self._on_duplicate_response, record, name_entry)
        prompt.present()

    def _on_duplicate_response(self, dialog, response, record, name_entry) -> None:
        dialog.destroy()
        if response == "duplicate":
            self.start_duplicate(record, name_entry.get_text())

    def start_duplicate(self, record: InstallationRecord, display_name: str) -> None:
        if self._busy:
            return
        self._cancel_event = threading.Event()
        self._copying_ids.add(record.id)
        self._operation_failure_title = "Copy failed"
        self._begin_operation(f"Copying {record.display_name}…", cancellable=True)
        self._refresh_all(reload_library=False)
        self.cancel_button.set_label("Cancel Copy")
        self.cancel_button.set_visible(True)
        threading.Thread(
            target=self._duplicate_worker, args=(record, display_name, self._cancel_event), daemon=True
        ).start()

    def _duplicate_worker(self, record, display_name, cancel_event) -> None:
        try:
            result = self.installer.duplicate(record, display_name, cancel_event, self._post_progress)
        except Exception as error:
            GLib.idle_add(self._duplicate_finished, record.id, None, error)
            return
        GLib.idle_add(self._duplicate_finished, record.id, result, None)

    def _duplicate_finished(self, installation_id: str, result, error) -> bool:
        self._copying_ids.discard(installation_id)
        return self._operation_finished(result, error)

    def _on_open_place_clicked(self, _button: Gtk.Button, record: InstallationRecord) -> None:
        file_filter = Gtk.FileFilter()
        file_filter.set_name("Roblox places")
        for pattern in ("*.rbxl", "*.rbxlx", "*.RBXL", "*.RBXLX"):
            file_filter.add_pattern(pattern)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(file_filter)
        dialog = Gtk.FileDialog(title="Open Place in Studio", modal=True, filters=filters)

        def finished(dialog, result) -> None:
            try:
                file = dialog.open_finish(result)
            except GLib.Error:
                return
            if file.get_path():
                self.request_open_place(record, Path(file.get_path()))

        dialog.open(self.window, None, finished)

    def request_open_place(self, record: InstallationRecord, place: Path) -> None:
        if record.app_type != "studio":
            return
        if record.id in self._sessions or record.id in self._launching_ids or record.id in self._stopping_ids:
            self._show_error("Studio is already open", "Close this installation before opening another place.")
            return
        if place.suffix.lower() not in (".rbxl", ".rbxlx") or not place.is_file():
            self._show_error("Not a place file", f"{place.name} is not a .rbxl or .rbxlx file.")
            return
        self._launching_ids.add(record.id)
        self._refresh_all(reload_library=False)
        threading.Thread(target=self._open_place_worker, args=(record, place), daemon=True).start()

    def _open_place_worker(self, record: InstallationRecord, place: Path) -> None:
        try:
            environment = effective_environment(LaunchConfigStore().load("studio"), os.environ)
        except Exception as error:
            GLib.idle_add(self._launch_failed, record.id, str(error))
            return
        arguments = ("-task", "EditFile", "-localPlaceFile", guest_path(place, environment))
        self._launch_worker(record.id, record, arguments, from_link=False)

    def _launch_worker(
        self, installation_id: str, record: InstallationRecord, guest_arguments=(), from_link: bool | None = None
    ) -> None:
        try:
            setup_state = getattr(self.window, "setup_state", None)
            mods_message = prepare_mods(record)
            session = launch_application(
                record,
                self.rosemary_status,
                guest_arguments=guest_arguments,
                plain_ctrl_click=getattr(setup_state, "plain_ctrl_click", True),
                launch_config=LaunchConfigStore().load(record.app_type),
                direct=self._multiple_allowed(record),
            )
            session.mods_message = mods_message
            wait_for_running(session)
        except Exception as error:
            GLib.idle_add(self._launch_failed, installation_id, str(error))
            return
        GLib.idle_add(
            self._launch_ready, installation_id, session, record.app_type,
            bool(guest_arguments) if from_link is None else from_link,
        )

    def _launch_ready(
        self,
        installation_id: str,
        session: LaunchSession,
        app_type: str | None = None,
        from_link: bool = False,
    ) -> bool:
        self._launching_ids.discard(installation_id)
        if session.process.poll() is not None:
            self._refresh_all(reload_library=False)
            self._show_error(
                "Roblox stopped during launch",
                "The verified application process exited before Roseblox could mark it running.",
            )
            return GLib.SOURCE_REMOVE
        self._sessions[installation_id] = session
        self._refresh_all(reload_library=False)
        center = getattr(self.window, "notification_center", None)
        gamemode_error = getattr(session, "gamemode_error", None)
        if gamemode_error and center is not None and app_type is not None:
            center.notify(app_type, f"GameMode was not started: {gamemode_error}", "failed")
        mods_message = getattr(session, "mods_message", None)
        if mods_message and center is not None and app_type is not None:
            center.notify(app_type, mods_message, "failed")
        threading.Thread(
            target=self._watch_worker,
            args=(installation_id, session),
            daemon=True,
        ).start()
        if app_type == "player":
            self._start_activity_watcher(session)
        started = getattr(self.window, "application_started", None)
        if started is not None and app_type is not None:
            started(app_type, session, from_link)
        return GLib.SOURCE_REMOVE

    def _start_activity_watcher(self, session: LaunchSession) -> None:
        settings = getattr(getattr(self.window, "setup_state", None), "while_playing", None)
        if settings is None or not settings.any_enabled:
            return
        if session.log_path is None or session.start_time is None:
            return
        try:
            process = _spawn_detached(
                watcher_arguments(
                    session.process.pid,
                    session.start_time,
                    session.log_path,
                    getattr(session, "roblox_log_directory", None),
                ),
                default_log_directory() / "activity.log",
            )
        except OSError as error:
            center = getattr(self.window, "notification_center", None)
            if center is not None:
                center.notify("player", f"While Playing features did not start: {error}", "failed")
            return
        GLib.child_watch_add(GLib.PRIORITY_DEFAULT, process.pid, lambda *_: None)

    def _launch_failed(self, installation_id: str, message: str) -> bool:
        self._launching_ids.discard(installation_id)
        self._refresh_all(reload_library=False)
        self._show_error("Could not launch Roblox", message)
        return GLib.SOURCE_REMOVE

    def _watch_worker(self, installation_id: str, session: LaunchSession) -> None:
        return_code = session.process.wait()
        if session.output_done is not None:
            session.output_done.wait(timeout=2.0)
        output = session.output_text()
        GLib.idle_add(self._session_exited, installation_id, session, return_code, output)

    def _session_exited(
        self,
        installation_id: str,
        session: LaunchSession,
        return_code: int,
        output: str,
    ) -> bool:
        if self._sessions.get(installation_id) is not session:
            return GLib.SOURCE_REMOVE
        was_stopping = installation_id in self._stopping_ids
        del self._sessions[installation_id]
        self._stopping_ids.discard(installation_id)
        self._refresh_all(reload_library=False)
        if not was_stopping and return_code != 0:
            self._show_error(
                "Roblox exited unexpectedly",
                output or f"Rosemary exited with status {return_code}.",
            )
        return GLib.SOURCE_REMOVE

    def _stop_worker(self, installation_id: str, session: LaunchSession) -> None:
        try:
            stop_application(session)
        except (OSError, ProcessIdentityError, subprocess.TimeoutExpired) as error:
            GLib.idle_add(self._stop_failed, installation_id, session, str(error))
            return
        GLib.idle_add(self._stop_finished, installation_id, session)

    def _stop_finished(self, installation_id: str, session: LaunchSession) -> bool:
        if self._sessions.get(installation_id) is session and session.process.poll() is not None:
            del self._sessions[installation_id]
        self._stopping_ids.discard(installation_id)
        self._refresh_all(reload_library=False)
        return GLib.SOURCE_REMOVE

    def _stop_failed(self, installation_id: str, session: LaunchSession, message: str) -> bool:
        self._stopping_ids.discard(installation_id)
        if session.process.poll() is not None and self._sessions.get(installation_id) is session:
            del self._sessions[installation_id]
        self._refresh_all(reload_library=False)
        self._show_error("Could not stop Roblox", message)
        return GLib.SOURCE_REMOVE

    def _update_launch_button(self, installation_id: str) -> None:
        button = self._launch_buttons.get(installation_id)
        if button is None:
            return
        label, sensitive = self.launch_button_state(installation_id)
        button.set_label(label)
        button.set_sensitive(sensitive)
        remove_button = self._remove_buttons.get(installation_id)
        if remove_button is not None:
            remove_button.set_sensitive(
                installation_id not in self._launching_ids
                and installation_id not in self._sessions
                and installation_id not in self._stopping_ids
            )

    def launch_button_state(self, installation_id: str) -> tuple[str, bool]:
        if installation_id in self._launching_ids:
            return "Launching...", False
        if installation_id in self._stopping_ids:
            return "Stopping…", False
        if installation_id in self._sessions:
            return "Stop", True
        return "Launch", True

    def _on_remove_clicked(self, _button: Gtk.Button, record: InstallationRecord) -> None:
        if record.id in self._copying_ids:
            return
        if record.managed:
            dialog = Adw.MessageDialog.new(
                self.window,
                "Remove managed installation?",
                "You can remove only the Library registration, or explicitly delete the managed application bundle. Any downloaded archive will be kept.",
            )
            dialog.set_close_response("cancel")
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("registration", "Remove Registration")
            dialog.add_response("delete", "Delete Managed Bundle")
            dialog.set_default_response("cancel")
            dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.connect("response", self._on_managed_remove_response, record)
            dialog.present()
            return
        self._remove_registration_async(record, False)

    def _on_managed_remove_response(self, dialog, response, record: InstallationRecord) -> None:
        dialog.destroy()
        if response == "registration":
            self._remove_registration_async(record, False)
        elif response == "delete":
            self._remove_registration_async(record, True)

    def _remove_registration_async(self, record: InstallationRecord, delete_managed_files: bool) -> None:
        threading.Thread(
            target=self._remove_worker,
            args=(record, delete_managed_files),
            daemon=True,
        ).start()

    def _remove_worker(self, record: InstallationRecord, delete_managed_files: bool) -> None:
        preference_error = None
        try:
            with self.store.mutation_lock:
                removed = self.store.remove(record.id, delete_managed_files=delete_managed_files)
                if removed:
                    try:
                        self.window.preferences_store.clear_if_matches(record.app_type, record.id)
                    except PreferencesError as error:
                        preference_error = str(error)
        except (LibraryDataError, OSError) as error:
            GLib.idle_add(self._remove_failed, str(error))
            return
        if removed:
            GLib.idle_add(self._remove_finished, preference_error)

    def _remove_finished(self, preference_error: str | None) -> bool:
        self._refresh_all(reload_library=True, reload_recovery=False, reload_preferences=True)
        if preference_error:
            self._show_error(
                "Installation removed",
                "The registration was removed, but its saved preferred selection could not be cleared. "
                f"Roseblox will use a valid fallback. {preference_error}",
            )
        return GLib.SOURCE_REMOVE

    def _remove_failed(self, message: str) -> bool:
        self._show_error("Could not remove registration", message)
        return GLib.SOURCE_REMOVE

    def _refresh_all(
        self,
        *,
        reload_library: bool = True,
        reload_recovery: bool = False,
        reload_preferences: bool = False,
    ) -> bool:
        if hasattr(self.window, "refresh_pages"):
            self.window.refresh_pages(
                reload_library=reload_library,
                reload_recovery=reload_recovery,
                reload_preferences=reload_preferences,
            )
        else:
            self.refresh()
        return GLib.SOURCE_REMOVE

    def _show_error(self, heading: str, message: str) -> None:
        if len(message) > 8192:
            message = message[:8192] + "\n\n[Additional error output was truncated.]"
        dialog = Adw.MessageDialog.new(self.window, heading, message)
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.present()


class SettingsPage(Gtk.Box):
    def __init__(
        self,
        status: RosemaryStatus,
        *,
        window: Gtk.Window,
        version_settings: VersionSettingsStore,
        version_client,
        setup_store=None,
        url_handlers=None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_margin_top(28)
        self.set_margin_bottom(28)
        self.set_margin_start(28)
        self.set_margin_end(28)
        self.window = window
        self.version_settings = version_settings
        self.version_client = version_client
        self.setup_store = setup_store
        self.url_handlers = url_handlers

        heading = Gtk.Label(label="Settings")
        heading.add_css_class("title-1")
        heading.set_halign(Gtk.Align.START)
        self.append(heading)
        self._sections: dict[str, CollapsibleSection] = {}
        self._sections_restored = False

        appearance = self._section(
            "appearance", "Appearance", "Colors, light or dark style, and the accent color."
        )
        self._appearance_block = False
        self.colors_row = Adw.ComboRow.new()
        self.colors_row.set_title("Colors")
        self.colors_row.set_model(Gtk.StringList.new(["Roseblox", "System"]))
        self.style_row = Adw.ComboRow.new()
        self.style_row.set_title("Style")
        self.style_row.set_model(Gtk.StringList.new(["Dark", "Light", "Follow system"]))
        self.accent_row = Adw.ComboRow.new()
        self.accent_row.set_title("Accent color")
        self.accent_row.set_model(Gtk.StringList.new([ACCENT_NAMES[choice] for choice in ACCENT_CHOICES]))
        appearance_group = Adw.PreferencesGroup.new()
        for row in (self.colors_row, self.style_row, self.accent_row):
            row.connect("notify::selected", self._appearance_changed)
            appearance_group.add(row)
        appearance.add(appearance_group)
        self._show_appearance(Appearance())

        rosemary = self._section(
            "rosemary", "Rosemary", "Roseblox launches Player and Studio through Rosemary."
        )
        rosemary_group = Adw.PreferencesGroup.new()
        self.rosemary_row = Adw.ActionRow.new()
        self.rosemary_row.set_title("Rosemary")
        self.rosemary_row.set_subtitle_selectable(True)
        self.rosemary_icon = Gtk.Image.new_from_icon_name("content-loading-symbolic")
        self.rosemary_row.add_prefix(self.rosemary_icon)
        recheck = Gtk.Button.new_with_label("Check Again")
        recheck.set_valign(Gtk.Align.CENTER)
        recheck.connect("clicked", lambda *_: self.window.recheck_rosemary())
        self.rosemary_row.add_suffix(recheck)
        rosemary_group.add(self.rosemary_row)

        method = rosemary_install_method(self.window)
        self.install_rosemary_row = Adw.ActionRow.new()
        self.install_rosemary_row.set_title(INSTALL_ROW_TITLES[method])
        self.rosemary_spinner = Gtk.Spinner()
        self.rosemary_spinner.set_visible(False)
        self.install_rosemary_row.add_suffix(self.rosemary_spinner)
        self.stop_waiting_button = Gtk.Button.new_with_label("Stop Waiting")
        self.stop_waiting_button.set_valign(Gtk.Align.CENTER)
        self.stop_waiting_button.set_visible(False)
        self.stop_waiting_button.connect("clicked", lambda *_: self.window.stop_waiting_for_rosemary())
        self.install_rosemary_row.add_suffix(self.stop_waiting_button)
        self.install_rosemary_button = Gtk.Button.new_with_label(INSTALL_BUTTON_LABELS[method])
        self.install_rosemary_button.set_valign(Gtk.Align.CENTER)
        self.install_rosemary_button.add_css_class("suggested-action")
        self.install_rosemary_button.connect("clicked", lambda *_: self.window.install_rosemary())
        self.install_rosemary_row.add_suffix(self.install_rosemary_button)
        rosemary_group.add(self.install_rosemary_row)

        self.appimage_row = Adw.ActionRow.new()
        self.appimage_row.set_title("Rosemary AppImage found")
        self.appimage_row.set_subtitle_selectable(True)
        use_appimage = Gtk.Button.new_with_label("Use This File")
        use_appimage.set_valign(Gtk.Align.CENTER)
        use_appimage.add_css_class("suggested-action")
        use_appimage.connect("clicked", lambda *_: self.window.use_rosemary_appimage())
        self.appimage_row.add_suffix(use_appimage)
        self.appimage_row.set_visible(False)
        rosemary_group.add(self.appimage_row)

        if method == INSTALL_METHOD_MANUAL:
            manual_row = Adw.ActionRow.new()
            manual_row.set_title("Install Rosemary")
            manual_row.set_subtitle(ABOUT_ROW_SUBTITLES[method])
            manual_row.set_subtitle_selectable(True)
            rosemary_group.add(manual_row)
        self._rosemary_install_state = RosemaryInstallState()
        self._rosemary_status = status

        self.rosemary_input_row = Adw.ActionRow.new()
        self.rosemary_input_row.set_title("Ctrl + left-click compatibility")
        self.rosemary_input_row.set_subtitle("Checking this Rosemary build…")
        self.input_switch = Gtk.Switch()
        self.input_switch.set_valign(Gtk.Align.CENTER)
        self.input_switch.set_active(True)
        self.input_switch.set_sensitive(False)
        self.input_switch.connect("notify::active", self._input_switch_changed)
        self.rosemary_input_row.add_suffix(self.input_switch)
        self.rosemary_input_row.set_activatable_widget(self.input_switch)
        rosemary_group.add(self.rosemary_input_row)
        self._input_block = False
        self._input_supported: bool | None = None

        setup_row = Adw.ActionRow.new()
        setup_row.set_title("First-time setup")
        setup_row.set_subtitle("Review Rosemary, Roblox links, and installations in one place.")
        setup_button = Gtk.Button.new_with_label("Open Setup")
        setup_button.set_valign(Gtk.Align.CENTER)
        setup_button.connect("clicked", lambda *_: self.window.navigate_to("setup"))
        setup_row.add_suffix(setup_button)
        rosemary_group.add(setup_row)

        import_row = Adw.ActionRow.new()
        import_row.set_title("Import from another app")
        import_row.set_subtitle(
            "FastFlags and settings from Roséline, Quadra, Sober, Vinegar, Bloxstrap and other tools, "
            "with a preview first."
        )
        import_button = Gtk.Button.new_with_label("Import…")
        import_button.set_valign(Gtk.Align.CENTER)
        import_button.connect("clicked", lambda *_: self.window.open_import())
        import_row.add_suffix(import_button)
        rosemary_group.add(import_row)
        rosemary.add(rosemary_group)
        self._input_check_path: object = _NOT_CHECKED
        self._input_check_generation = 0

        launching = self._section(
            "launching",
            "After Launching",
            "Close Roseblox once Roblox has started so the launcher is not left open. "
            "Roblox keeps running; open Roseblox again any time.",
        )
        close_group = Adw.PreferencesGroup.new()
        self._close_rows: dict[str, tuple[Adw.ExpanderRow, Gtk.Switch]] = {}
        self._close_block = False
        for app_type, title in (("player", "Roblox Player"), ("studio", "Roblox Studio")):
            expander = Adw.ExpanderRow.new()
            expander.set_title(f"Close after opening {title}")
            expander.set_show_enable_switch(True)
            expander.set_enable_expansion(False)
            expander.set_expanded(False)
            link_switch = Gtk.Switch()
            link_switch.set_valign(Gtk.Align.CENTER)
            link_row = Adw.ActionRow.new()
            link_row.set_title("Only when opened from a link")
            link_row.set_subtitle(
                "Stay open when you start it from Roseblox; close only for links from a website."
            )
            link_row.add_suffix(link_switch)
            link_row.set_activatable_widget(link_switch)
            expander.add_row(link_row)
            expander.connect("notify::enable-expansion", self._close_preference_changed, app_type)
            link_switch.connect("notify::active", self._close_preference_changed, app_type)
            close_group.add(expander)
            self._close_rows[app_type] = (expander, link_switch)
        launching.add(close_group)
        multiple_group = Adw.PreferencesGroup.new()
        self.multiple_row = Adw.ActionRow.new()
        self.multiple_row.set_title("Allow more than one Roblox Player")
        self.multiple_row.set_subtitle(
            "Adds Duplicate… to Player installations. Each copy runs its own Player, so you can sign in to a "
            "different account in each. Roblox links open in a copy that is not running yet."
        )
        self.multiple_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.multiple_switch.connect("notify::active", self._multiple_changed)
        self.multiple_row.add_suffix(self.multiple_switch)
        self.multiple_row.set_activatable_widget(self.multiple_switch)
        multiple_group.add(self.multiple_row)
        launching.add(multiple_group)
        self._multiple_block = False

        playing = self._section(
            "playing",
            "While Playing",
            "For Roblox Player. Turn these on before starting it; they apply to the games you join.",
        )
        playing_group = Adw.PreferencesGroup.new()
        self._playing_block = False
        self.discord_id_row = None
        self._playing_rows: dict[str, tuple[Adw.ActionRow, Gtk.Switch]] = {}
        for field_name, title in (
            ("discord_presence", "Discord Rich Presence"),
            ("discord_join_button", "Join Server button"),
            ("server_location", "Server location notification"),
            ("close_on_leave", "Close Roblox when leaving a game"),
        ):
            row = Adw.ActionRow.new()
            row.set_title(title)
            switch = Gtk.Switch()
            switch.set_valign(Gtk.Align.CENTER)
            switch.connect("notify::active", self._playing_changed)
            row.add_suffix(switch)
            row.set_activatable_widget(switch)
            playing_group.add(row)
            self._playing_rows[field_name] = (row, switch)
        self._build_microphone_rows(playing_group)
        self.playing_message = Gtk.Label(xalign=0, wrap=True)
        self.playing_message.add_css_class("dim-label")
        self.playing_message.set_visible(False)
        playing.add(playing_group)
        playing.add(self.playing_message)
        self._show_while_playing(WhilePlaying())

        links = self._section(
            "links",
            "Roblox Links",
            "Open roblox://, roblox-player://, roblox-studio:// and roblox-studio-auth:// links with Roseblox. "
            "Turning this off restores the previous default application.",
        )
        url_group = Adw.PreferencesGroup.new()
        self.url_switch = Gtk.Switch()
        self.url_switch.set_valign(Gtk.Align.CENTER)
        self.url_switch.connect("state-set", self._url_switch_changed)
        self.url_row = Adw.ActionRow.new()
        self.url_row.set_title("Open Roblox links with Roseblox")
        self.url_row.set_subtitle("Checking desktop registration…")
        self.url_repair_button = Gtk.Button.new_with_label("Repair")
        self.url_repair_button.set_valign(Gtk.Align.CENTER)
        self.url_repair_button.set_visible(False)
        self.url_repair_button.connect("clicked", lambda *_: self._run_url_change(True))
        self.url_row.add_suffix(self.url_repair_button)
        self.url_row.add_suffix(self.url_switch)
        self.url_row.set_activatable_widget(self.url_switch)
        url_group.add(self.url_row)
        links.add(url_group)
        self._url_busy = False
        self._url_message: str | None = None
        self._render_rosemary()

        updates = self._section(
            "updates",
            "Updates",
            "A small background checker starts when you log in, looks for new Player and Studio "
            "versions every few hours, and sends a notification with Update and Skip buttons.",
        )
        updates_group = Adw.PreferencesGroup.new()
        self.update_row = UpdateCheckRow(window)
        updates_group.add(self.update_row)
        self.check_now_row = Adw.ActionRow.new()
        self.check_now_row.set_title("Check for updates now")
        self.check_now_row.set_subtitle("Get a notification right away if a newer version is available.")
        self.check_now_button = Gtk.Button.new_with_label("Check Now")
        self.check_now_button.set_valign(Gtk.Align.CENTER)
        self.check_now_button.connect("clicked", self._check_now_clicked)
        self.check_now_row.add_suffix(self.check_now_button)
        self.check_now_row.set_visible(False)
        updates_group.add(self.check_now_row)
        self.skipped_row = Adw.ActionRow.new()
        self.skipped_row.set_title("Skipped versions")
        self.skipped_row.set_subtitle("None")
        self.ask_again_button = Gtk.Button.new_with_label("Ask Again")
        self.ask_again_button.set_valign(Gtk.Align.CENTER)
        self.ask_again_button.set_sensitive(False)
        self.ask_again_button.connect("clicked", self._ask_again_clicked)
        self.skipped_row.add_suffix(self.ask_again_button)
        updates_group.add(self.skipped_row)
        self.roseblox_updates_row = Adw.SwitchRow.new()
        self.roseblox_updates_row.set_title("Check for new Roseblox versions")
        self.roseblox_updates_row.set_subtitle(
            "Once a day while Roseblox is open, look at Roseblox’s GitHub releases and show "
            "new versions in the notifications."
        )
        self.roseblox_updates_row.set_active(True)
        self._roseblox_updates_block = False
        self.roseblox_updates_row.connect("notify::active", self._roseblox_updates_changed)
        updates_group.add(self.roseblox_updates_row)
        updates.add(updates_group)

        advanced = self._section(
            "advanced",
            "Advanced",
            "Only change these if you need to. Wrong values stop Roseblox from finding Rosemary "
            "or new Roblox versions.",
            expanded=False,
        )
        executable_group = Adw.PreferencesGroup.new()
        executable_group.set_title("Rosemary Executable")
        executable_row = Adw.ActionRow.new()
        executable_row.set_title("Executable")
        executable_row.set_subtitle("Choose a specific build, or find “rosemary” in PATH automatically.")
        use_path = Gtk.Button.new_with_label("Use PATH")
        use_path.set_valign(Gtk.Align.CENTER)
        use_path.connect("clicked", self._use_rosemary_path)
        executable_row.add_suffix(use_path)
        choose_rosemary = Gtk.Button.new_with_label("Choose…")
        choose_rosemary.set_valign(Gtk.Align.CENTER)
        choose_rosemary.connect("clicked", lambda *_: self.window.choose_rosemary_executable())
        executable_row.add_suffix(choose_rosemary)
        executable_group.add(executable_row)
        advanced.add(executable_group)

        discord_group = Adw.PreferencesGroup.new()
        discord_group.set_title("Discord Application")
        discord_group.set_description(
            "Discord Rich Presence uses Roseblox's own Discord application. Enter another application ID "
            "from the Discord Developer Portal to show its name instead; leave it empty for Roseblox's."
        )
        self.discord_id_row = Adw.EntryRow.new()
        self.discord_id_row.set_title("Discord application ID")
        self.discord_id_row.set_show_apply_button(True)
        self.discord_id_row.set_input_purpose(Gtk.InputPurpose.DIGITS)
        self.discord_id_row.connect("apply", self._discord_id_applied)
        discord_group.add(self.discord_id_row)
        self.discord_id_message = Gtk.Label(xalign=0, wrap=True)
        self.discord_id_message.add_css_class("dim-label")
        self.discord_id_message.set_visible(False)
        advanced.add(discord_group)
        advanced.add(self.discord_id_message)

        settings = {app: default_version_source(app) for app in ("player", "studio")}
        self._source_widgets = {}
        for app_type, title in (("player", "Roblox Player"), ("studio", "Roblox Studio")):
            group = Adw.PreferencesGroup.new()
            group.set_title(f"{title} Version Source")
            group.set_description("Configure the HTTPS JSON endpoint used to discover the latest version.")
            endpoint = Gtk.Entry()
            endpoint.set_text(settings[app_type].endpoint)
            endpoint.set_hexpand(True)
            endpoint.set_placeholder_text("https://example.org/version.json")
            endpoint.set_tooltip_text("HTTPS endpoint returning a JSON response")
            pointer = Gtk.Entry()
            pointer.set_text(settings[app_type].json_pointer)
            pointer.set_hexpand(True)
            endpoint.set_width_chars(36)
            pointer.set_width_chars(36)
            pointer.set_placeholder_text("/clientVersionUpload")
            pointer.set_tooltip_text("RFC 6901 JSON Pointer, for example /data/0/version")
            group.add(self._entry_row("Endpoint URL", endpoint))
            group.add(self._entry_row("JSON field path", pointer))

            result_label = Gtk.Label(label="Loading saved configuration…")
            result_label.set_xalign(0)
            result_label.set_wrap(True)
            result_label.set_selectable(True)
            result_label.add_css_class("dim-label")
            result_row = Adw.ActionRow.new()
            result_row.set_title("Configuration status")
            result_row.add_suffix(result_label)
            group.add(result_row)

            actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            test_button = Gtk.Button.new_with_label("Test Configuration")
            test_button.connect("clicked", self._test_source_clicked, app_type, endpoint, pointer, result_label)
            actions.append(test_button)
            save_button = Gtk.Button.new_with_label("Save")
            save_button.add_css_class("suggested-action")
            save_button.connect("clicked", self._save_source_clicked, app_type, endpoint, pointer, result_label)
            actions.append(save_button)
            reset_button = Gtk.Button.new_with_label("Restore Defaults")
            reset_button.connect("clicked", self._reset_source_clicked, app_type, endpoint, pointer, result_label)
            actions.append(reset_button)
            actions_row = Adw.ActionRow.new()
            actions_row.set_title("Actions")
            actions_row.add_suffix(actions)
            group.add(actions_row)
            self._source_widgets[app_type] = (endpoint, pointer, result_label, test_button, save_button, reset_button)
            advanced.add(group)
            for action in (test_button, save_button, reset_button):
                action.set_sensitive(False)
        threading.Thread(target=self._load_sources_worker, daemon=True).start()

    def _section(self, section_id: str, title: str, description: str, *, expanded: bool = True) -> CollapsibleSection:
        section = CollapsibleSection(
            section_id, title, description, expanded=expanded, on_toggled=self._section_toggled
        )
        self._sections[section_id] = section
        self.append(section)
        return section

    def _section_toggled(self, section_id: str, expanded: bool) -> None:
        if self.setup_store is None:
            return
        try:
            self.window.setup_state = self.setup_store.save_section_expanded(section_id, expanded)
        except SetupSettingsError:
            pass  # Only the layout is lost; the section still opens and closes.

    def _restore_sections(self, setup_state) -> None:
        if self._sections_restored:
            return
        self._sections_restored = True
        for section_id, expanded in setup_state.expanded_sections.items():
            section = self._sections.get(section_id)
            if section is not None:
                section.set_expanded(expanded)

    def _show_appearance(self, appearance: Appearance) -> None:
        self._appearance_block = True
        try:
            self.colors_row.set_selected(COLOR_THEMES.index(appearance.colors))
            self.style_row.set_selected(COLOR_STYLES.index(appearance.style))
            self.accent_row.set_selected(ACCENT_CHOICES.index(appearance.accent))
        finally:
            self._appearance_block = False
        self._describe_appearance(appearance)

    def _describe_appearance(self, appearance: Appearance, error: str | None = None) -> None:
        self.colors_row.set_subtitle(
            "Roseblox’s own pink and purple tones." if appearance.colors == "roseblox"
            else "Your desktop’s standard window colors."
        )
        self.style_row.set_subtitle(
            "Follows your desktop’s light or dark preference." if appearance.style == "system" else ""
        )
        if error:
            self.accent_row.set_subtitle(error)
        elif appearance.accent == "system":
            self.accent_row.set_subtitle(
                "Uses your desktop’s accent color." if system_accent_available()
                else "Your desktop does not share an accent color with apps, so blue is used."
            )
        else:
            self.accent_row.set_subtitle("")

    def _appearance_changed(self, *_args) -> None:
        if self._appearance_block:
            return
        appearance = Appearance(
            COLOR_THEMES[self.colors_row.get_selected()],
            COLOR_STYLES[self.style_row.get_selected()],
            ACCENT_CHOICES[self.accent_row.get_selected()],
        )
        error = self.window.apply_appearance(appearance)
        self._describe_appearance(appearance, f"Not saved: {error}" if error else None)

    def _multiple_changed(self, switch, _pspec) -> None:
        if self._multiple_block or self.setup_store is None:
            return
        try:
            self.window.setup_state = self.setup_store.save_multiple_players(switch.get_active())
        except SetupSettingsError as error:
            self._show_multiple(not switch.get_active())
            self.multiple_row.set_subtitle(f"Not saved: {str(error)[:300]}")
            return
        if hasattr(self.window, "refresh_pages"):
            self.window.refresh_pages(reload_library=False)

    def _show_multiple(self, active: bool) -> None:
        self._multiple_block = True
        self.multiple_switch.set_active(active)
        self._multiple_block = False

    def _input_switch_changed(self, switch, _pspec) -> None:
        if self._input_block or self.setup_store is None:
            return
        try:
            self.window.setup_state = self.setup_store.save_plain_ctrl_click(switch.get_active())
        except SetupSettingsError as error:
            self._set_input_switch(not switch.get_active())
            self.rosemary_input_row.set_subtitle(f"Not saved: {str(error)[:300]}")
            return
        self._describe_input()

    def _set_input_switch(self, active: bool) -> None:
        self._input_block = True
        self.input_switch.set_active(active)
        self._input_block = False

    def _describe_input(self) -> None:
        supported = self._input_supported
        saved = getattr(getattr(self.window, "setup_state", None), "plain_ctrl_click", True)
        self._set_input_switch(saved and supported is not False)
        self.input_switch.set_sensitive(supported is True)
        if supported is None:
            message = "Available once Rosemary is detected."
        elif not supported:
            message = "Not supported by this Rosemary build; default input behavior is kept."
        elif self.input_switch.get_active():
            message = "On. Ctrl + left-click acts as a normal left-click in Player and Studio."
        else:
            message = "Off. Ctrl + left-click keeps Rosemary’s default behavior."
        self.rosemary_input_row.set_subtitle(message)

    def apply_setup_snapshot(self, rosemary_status, setup_state, setup_error, handler_status) -> None:
        if setup_state is not None:
            self._restore_sections(setup_state)
            self._show_appearance(setup_state.appearance)
            self._describe_input()
            self._show_close_preferences(setup_state)
            self._show_multiple(setup_state.multiple_players)
            self._show_while_playing(setup_state.while_playing)
            if setup_state.while_playing.microphone_toggle:
                self.microphone_hotkey.start()
            self._show_skipped_versions(setup_state.skipped_versions)
            self._set_roseblox_updates(setup_state.roseblox_update_checks)
        enabled = bool(setup_state and setup_state.url_handlers_enabled)
        self._url_switch_block = True
        self.url_switch.set_active(enabled)
        self.url_switch.set_state(enabled)
        self._url_switch_block = False
        if not self._url_busy:
            if self._url_message:
                subtitle = self._url_message
            elif setup_error:
                subtitle = f"Unable to load setup settings: {setup_error}"
            elif handler_status is None:
                subtitle = "Registration status is unavailable."
            else:
                subtitle = handler_status.error or handler_status.message
            self.url_row.set_subtitle(subtitle)
        self._url_message = None
        self.url_repair_button.set_visible(
            enabled and handler_status is not None and not handler_status.registered
        )
        self.url_switch.set_sensitive(not self._url_busy)
        self.url_repair_button.set_sensitive(not self._url_busy)
        self._rosemary_status = rosemary_status
        self._render_rosemary()
        if rosemary_status.executable != self._input_check_path:
            self._input_check_path = rosemary_status.executable
            self._input_check_generation += 1
            generation = self._input_check_generation
            threading.Thread(
                target=self._check_rosemary_input_worker,
                args=(rosemary_status.executable, generation), daemon=True,
            ).start()

    def apply_rosemary_install_state(self, state: RosemaryInstallState) -> None:
        self._rosemary_install_state = state
        self._render_rosemary()

    def _render_rosemary(self) -> None:
        status = self._rosemary_status
        install = self._rosemary_install_state
        if status.available:
            self.rosemary_icon.set_from_icon_name("object-select-symbolic")
            self.rosemary_row.set_subtitle(status.executable)
        elif status.error:
            self.rosemary_icon.set_from_icon_name("dialog-warning-symbolic")
            self.rosemary_row.set_subtitle(status.error)
        else:
            self.rosemary_icon.set_from_icon_name("dialog-warning-symbolic")
            self.rosemary_row.set_subtitle("Not installed. Rosemary is required to launch Roblox.")
        method = rosemary_install_method(self.window)
        self.install_rosemary_row.set_visible(
            method != INSTALL_METHOD_MANUAL and (not status.available or install.waiting)
        )
        self.install_rosemary_row.set_subtitle(
            install.message
            or (
                "Build the “rosemary” package with paru or yay in a terminal window."
                if method == INSTALL_METHOD_AUR
                else INSTALL_ROW_SUBTITLES[method]
            )
        )
        appimage = None if status.available else getattr(self.window, "rosemary_appimage", None)
        self.appimage_row.set_visible(bool(appimage))
        self.appimage_row.set_subtitle(appimage or "")
        if appimage:
            self.install_rosemary_button.remove_css_class("suggested-action")
        else:
            self.install_rosemary_button.add_css_class("suggested-action")
        if install.error:
            self.install_rosemary_row.add_css_class("error-row")
        else:
            self.install_rosemary_row.remove_css_class("error-row")
        self.rosemary_spinner.set_visible(install.waiting)
        self.rosemary_spinner.set_spinning(install.waiting)
        self.stop_waiting_button.set_visible(install.waiting)
        self.install_rosemary_button.set_visible(not install.waiting and not status.available)
        self.install_rosemary_button.set_label("Try Again" if install.error else INSTALL_BUTTON_LABELS[method])

    def _check_rosemary_input_worker(self, executable, generation) -> None:
        if not executable:
            supported = None
        else:
            from ..backend.rosemary import rosemary_supports_plain_ctrl_click
            supported = rosemary_supports_plain_ctrl_click(executable)
        GLib.idle_add(self._show_rosemary_input_status, supported, generation)

    def _show_rosemary_input_status(self, supported, generation) -> bool:
        if generation != self._input_check_generation:
            return GLib.SOURCE_REMOVE
        self._input_supported = supported
        self._describe_input()
        return GLib.SOURCE_REMOVE

    def apply_update_status(self, status) -> None:
        self.check_now_row.set_visible(status.enabled)

    def _set_roseblox_updates(self, active: bool) -> None:
        self._roseblox_updates_block = True
        self.roseblox_updates_row.set_active(active)
        self._roseblox_updates_block = False

    def _roseblox_updates_changed(self, row, _pspec) -> None:
        if self._roseblox_updates_block or self.setup_store is None:
            return
        try:
            self.window.setup_state = self.setup_store.save_roseblox_update_checks(row.get_active())
        except SetupSettingsError as error:
            self._set_roseblox_updates(not row.get_active())
            row.set_subtitle(f"Not saved: {str(error)[:300]}")
            return
        center = getattr(self.window, "notification_center", None)
        if row.get_active() and center is not None:
            center.check_roseblox(automatic=True)

    def _show_skipped_versions(self, skipped: dict[str, str]) -> None:
        if skipped:
            self.skipped_row.set_subtitle(
                "; ".join(f"{APP_TITLES[app]} {version}" for app, version in sorted(skipped.items()))
                + ". You will be asked again when a newer version comes out."
            )
        else:
            self.skipped_row.set_subtitle("None")
        self.ask_again_button.set_sensitive(bool(skipped))

    def _check_now_clicked(self, _button) -> None:
        try:
            self.window.update_checker.check_now()
        except (UpdateCheckerError, OSError) as error:
            self.check_now_row.set_subtitle(f"Could not check: {str(error)[:300]}")
            return
        self.check_now_row.set_subtitle("Checking… a notification appears if an update is available.")

    def _ask_again_clicked(self, _button) -> None:
        try:
            state = self.setup_store.clear_skipped_versions()
        except SetupSettingsError as error:
            self.skipped_row.set_subtitle(f"Not changed: {str(error)[:300]}")
            return
        NotificationLog().clear()
        self.window.setup_state = state
        self._show_skipped_versions(state.skipped_versions)
        center = getattr(self.window, "notification_center", None)
        if center is not None:
            center.forget_skipped()

    def _show_close_preferences(self, setup_state) -> None:
        self._close_block = True
        try:
            for app_type, (expander, link_switch) in self._close_rows.items():
                preference = setup_state.close_preference(app_type)
                expander.set_enable_expansion(preference.enabled)
                link_switch.set_active(preference.only_from_link)
                self._describe_close_preference(app_type, preference)
        finally:
            self._close_block = False

    def _describe_close_preference(self, app_type: str, preference: ClosePreference, error: str | None = None) -> None:
        expander, _link_switch = self._close_rows[app_type]
        if error:
            subtitle = error
        elif not preference.enabled:
            subtitle = "Roseblox stays open."
        elif preference.only_from_link:
            subtitle = "Closes after opening a Roblox link."
        else:
            subtitle = "Closes after every launch."
        expander.set_subtitle(subtitle)

    def _build_microphone_rows(self, group: Adw.PreferencesGroup) -> None:
        self.microphone_row = Adw.ExpanderRow.new()
        self.microphone_row.set_title("Microphone toggle")
        self.microphone_row.set_subtitle(
            "Keeps Roblox's microphone silent until you use a hotkey. Roblox stays connected to voice chat, "
            "and other apps' microphones are not affected."
        )
        self.microphone_row.set_show_enable_switch(True)
        self.microphone_row.set_enable_expansion(False)
        self.microphone_row.connect("notify::enable-expansion", self._microphone_changed)

        self.hotkey_row = Adw.ActionRow.new()
        self.hotkey_row.set_title("Microphone hotkey")
        self.hotkey_row.set_subtitle("Set by your desktop, so it works while Roblox has focus.")
        self.hotkey_button = Gtk.Button.new_with_label("Set Hotkey")
        self.hotkey_button.set_valign(Gtk.Align.CENTER)
        self.hotkey_button.connect("clicked", lambda *_: self.microphone_hotkey.change())
        self.hotkey_row.add_suffix(self.hotkey_button)
        self.microphone_row.add_row(self.hotkey_row)
        self.microphone_hotkey = MicrophoneHotkey(self._hotkey_changed)

        self.microphone_mode_row = Adw.ComboRow.new()
        self.microphone_mode_row.set_title("Mode")
        self.microphone_mode_row.set_model(Gtk.StringList.new([MICROPHONE_MODE_TITLES[mode] for mode in MICROPHONE_MODES]))
        self.microphone_mode_row.connect("notify::selected", self._microphone_changed)
        self.microphone_row.add_row(self.microphone_mode_row)

        self.microphone_inverted_row = Adw.SwitchRow.new()
        self.microphone_inverted_row.set_title("Inverted")
        self.microphone_inverted_row.set_subtitle("Hold the hotkey to mute instead of to talk (push to mute).")
        self.microphone_inverted_row.connect("notify::active", self._microphone_changed)
        self.microphone_row.add_row(self.microphone_inverted_row)

        self.release_delay_row = Adw.SpinRow.new_with_range(0, MAX_RELEASE_DELAY_MS, 50)
        self.release_delay_row.set_title("Release delay")
        self.release_delay_row.connect("notify::value", self._microphone_changed)
        self.microphone_row.add_row(self.release_delay_row)

        self.microphone_sounds_row = Adw.SwitchRow.new()
        self.microphone_sounds_row.set_title("Sound effects")
        self.microphone_sounds_row.set_subtitle("Play a short sound when Roblox's microphone turns on or off.")
        self.microphone_sounds_row.connect("notify::active", self._microphone_changed)
        self.microphone_row.add_row(self.microphone_sounds_row)
        self.sound_player = SoundPlayer(log=self._sound_message)
        self._sound_rows: dict[str, tuple[Adw.ActionRow, Gtk.Button]] = {}
        for kind, title in ((ON, "Microphone on sound"), (OFF, "Microphone off sound")):
            row = Adw.ActionRow.new()
            row.set_title(title)
            play = Gtk.Button.new_from_icon_name("media-playback-start-symbolic")
            play.set_valign(Gtk.Align.CENTER)
            play.set_tooltip_text("Play")
            play.add_css_class("flat")
            play.connect("clicked", lambda *_, k=kind: self._preview_sound(k))
            row.add_suffix(play)
            reset = Gtk.Button.new_with_label("Use Roseblox's")
            reset.set_valign(Gtk.Align.CENTER)
            reset.connect("clicked", lambda *_, k=kind: self._set_sound(k, ""))
            row.add_suffix(reset)
            choose = Gtk.Button.new_with_label("Choose…")
            choose.set_valign(Gtk.Align.CENTER)
            choose.connect("clicked", lambda *_, k=kind: self._choose_sound(k))
            row.add_suffix(choose)
            self.microphone_row.add_row(row)
            self._sound_rows[kind] = (row, reset)
        self.sound_volume_row = Adw.SpinRow.new_with_range(0, MAX_VOLUME, 5)
        self.sound_volume_row.set_title("Sound volume")
        self.sound_volume_row.set_subtitle("Percent of your system volume.")
        self.sound_volume_row.connect("notify::value", self._microphone_changed)
        self.microphone_row.add_row(self.sound_volume_row)
        self.sounds_warning = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        self.sounds_warning.set_tooltip_text("Sound effects cannot play")
        self.microphone_sounds_row.add_suffix(self.sounds_warning)
        group.add(self.microphone_row)

        self.microphone_warning_row = Adw.ActionRow.new()
        self.microphone_warning_row.set_title("The microphone toggle cannot work here")
        self.microphone_warning_row.set_subtitle_selectable(True)
        self.microphone_warning_row.add_prefix(Gtk.Image.new_from_icon_name("dialog-warning-symbolic"))
        self.microphone_warning_row.add_css_class("error-row")
        self.microphone_warning_row.set_visible(False)
        group.add(self.microphone_warning_row)

    def _sound_message(self, text: str) -> None:
        self.playing_message.set_text(f"Microphone sound: {text}")
        self.playing_message.set_visible(True)

    def _preview_sound(self, kind: str) -> None:
        self.playing_message.set_visible(False)
        self.sound_player.play(self._current_while_playing().sounds, kind, force=True)

    def _choose_sound(self, kind: str) -> None:
        file_filter = Gtk.FileFilter()
        file_filter.set_name("Sounds (WAV, Ogg, FLAC)")
        for suffix in SOUND_SUFFIXES:
            file_filter.add_pattern(f"*{suffix}")
            file_filter.add_pattern(f"*{suffix.upper()}")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(file_filter)
        dialog = Gtk.FileDialog(title="Choose a Sound", modal=True, filters=filters)

        def finished(dialog, result) -> None:
            try:
                file = dialog.open_finish(result)
            except GLib.Error:
                return
            if file.get_path():
                self.use_sound_file(kind, Path(file.get_path()))

        dialog.open(self.window, None, finished)

    def use_sound_file(self, kind: str, source: Path) -> None:
        try:
            stored = store_sound(kind, source)
        except MicrophoneSoundError as error:
            self._sound_message(str(error))
            return
        self._set_sound(kind, str(stored))

    def _set_sound(self, kind: str, path: str) -> None:
        field_name = "microphone_sound_on" if kind == ON else "microphone_sound_off"
        self._save_while_playing(replace(self._current_while_playing(), **{field_name: path}))

    def _hotkey_changed(self, text: str, can_change: bool) -> None:
        self.hotkey_row.set_subtitle(text)
        self.hotkey_button.set_visible(not self.microphone_hotkey.unavailable)
        self.hotkey_button.set_sensitive(can_change)
        self.hotkey_button.set_label("Change Hotkey" if self.microphone_hotkey.trigger else "Set Hotkey")

    def _microphone_changed(self, *_args) -> None:
        if self._playing_block:
            return
        mode = MICROPHONE_MODES[min(self.microphone_mode_row.get_selected(), len(MICROPHONE_MODES) - 1)]
        enabled = self.microphone_row.get_enable_expansion()
        self._save_while_playing(replace(
            self._current_while_playing(),
            microphone_toggle=enabled,
            microphone_mode=mode,
            microphone_release_delay_ms=int(self.release_delay_row.get_value()),
            microphone_inverted=self.microphone_inverted_row.get_active(),
            microphone_sounds=self.microphone_sounds_row.get_active(),
            microphone_sound_volume=int(self.sound_volume_row.get_value()),
        ))
        if enabled:
            self.microphone_hotkey.start()

    def _show_microphone(self, settings: WhilePlaying) -> None:
        self.microphone_row.set_enable_expansion(settings.microphone_toggle)
        self.microphone_mode_row.set_selected(MICROPHONE_MODES.index(settings.microphone_mode))
        self.release_delay_row.set_value(settings.microphone_release_delay_ms)
        self.microphone_inverted_row.set_active(settings.microphone_inverted)
        self.microphone_sounds_row.set_active(settings.microphone_sounds)
        self.sound_volume_row.set_value(settings.microphone_sound_volume)
        self.sound_volume_row.set_visible(settings.microphone_sounds)
        for kind, (row, reset) in self._sound_rows.items():
            chosen = settings.sounds.path(kind)
            row.set_visible(settings.microphone_sounds)
            row.set_subtitle(Path(chosen).name if chosen else "Roseblox's sound")
            reset.set_visible(bool(chosen))
        problem = microphone_problem()
        self.microphone_warning_row.set_visible(problem is not None)
        self.microphone_warning_row.set_subtitle(problem or "")
        missing_player = sound_problem() if settings.microphone_sounds else None
        self.sounds_warning.set_visible(missing_player is not None)
        self.microphone_sounds_row.set_subtitle(
            missing_player or "Play a short sound when Roblox's microphone turns on or off."
        )
        hold = settings.microphone_mode == MODE_HOLD
        inverted = hold and settings.microphone_inverted
        self.release_delay_row.set_visible(hold)
        self.microphone_inverted_row.set_visible(hold)
        if not hold:
            description = "Press the hotkey once to talk and again to mute."
        elif inverted:
            description = "Your microphone is on, and muted while the hotkey is held down."
        else:
            description = "Talk while the hotkey is held down."
        self.microphone_mode_row.set_subtitle(description)
        self.release_delay_row.set_subtitle(
            "Milliseconds you stay muted after you let go of the hotkey."
            if inverted
            else "Milliseconds the microphone stays on after you let go of the hotkey."
        )

    def _show_while_playing(self, settings: WhilePlaying) -> None:
        self._playing_block = True
        try:
            self._show_microphone(settings)
            for field_name, (_row, switch) in self._playing_rows.items():
                switch.set_active(getattr(settings, field_name))
            if self.discord_id_row is not None and self.discord_id_row.get_text() != settings.discord_application_id:
                self.discord_id_row.set_text(settings.discord_application_id)
        finally:
            self._playing_block = False
        has_id = valid_application_id(effective_application_id(settings.discord_application_id))
        discord_row, discord_switch = self._playing_rows["discord_presence"]
        discord_switch.set_sensitive(has_id or settings.discord_presence)
        discord_row.set_subtitle(
            "Shows the game you are in on your Discord profile, with a View Game button. "
            "The Discord app must be running."
            if has_id
            else "Enter a Discord application ID under Advanced first."
        )
        join_row, join_switch = self._playing_rows["discord_join_button"]
        join_switch.set_sensitive(settings.discord_presence)
        join_row.set_subtitle("Lets your friends join your server from Discord. Never shown for private servers.")
        self._playing_rows["server_location"][0].set_subtitle(
            f"Says where the server is when you join one. The server's address, not yours, is looked up with "
            f"{LOCATION_SERVICE}."
        )
        self._playing_rows["close_on_leave"][0].set_subtitle(
            "Closes Roblox when you leave a game, instead of going back to the Roblox home screen."
        )

    def _current_while_playing(self) -> WhilePlaying:
        return getattr(getattr(self.window, "setup_state", None), "while_playing", None) or WhilePlaying()

    def _save_while_playing(self, settings: WhilePlaying) -> None:
        if self.setup_store is None:
            return
        try:
            state = self.setup_store.save_while_playing(settings)
        except (SetupSettingsError, ValueError) as error:
            self.playing_message.set_text(f"Not saved: {str(error)[:300]}")
            self.playing_message.set_visible(True)
            self._show_while_playing(self._current_while_playing())
            return
        self.window.setup_state = state
        self.playing_message.set_visible(False)
        self._show_while_playing(state.while_playing)

    def _playing_changed(self, _switch, _pspec) -> None:
        if self._playing_block:
            return
        values = {field_name: switch.get_active() for field_name, (_row, switch) in self._playing_rows.items()}
        self._save_while_playing(replace(self._current_while_playing(), **values))

    def _discord_id_applied(self, row) -> None:
        text = row.get_text().strip()
        if text and not valid_application_id(text):
            self.discord_id_message.set_text(
                "A Discord application ID is the long number under General Information in the Discord Developer Portal."
            )
            self.discord_id_message.set_visible(True)
            return
        self.discord_id_message.set_text("Saved." if text else "Saved. Roseblox's own Discord application is used.")
        self.discord_id_message.set_visible(True)
        self._save_while_playing(replace(self._current_while_playing(), discord_application_id=text))

    def _close_preference_changed(self, _widget, _pspec, app_type: str) -> None:
        if self._close_block or self.setup_store is None:
            return
        expander, link_switch = self._close_rows[app_type]
        preference = ClosePreference(expander.get_enable_expansion(), link_switch.get_active())
        try:
            state = self.setup_store.save_close_after_launch(app_type, preference)
        except SetupSettingsError as error:
            try:
                saved = self.setup_store.load().close_preference(app_type)
            except SetupSettingsError:
                saved = ClosePreference()
            self._close_block = True
            expander.set_enable_expansion(saved.enabled)
            link_switch.set_active(saved.only_from_link)
            self._close_block = False
            self._describe_close_preference(app_type, saved, f"Not saved: {str(error)[:300]}")
            return
        self.window.setup_state = state
        if preference.enabled:
            expander.set_expanded(True)
        self._describe_close_preference(app_type, state.close_preference(app_type))

    def _url_switch_changed(self, switch, active) -> bool:
        if getattr(self, "_url_switch_block", False):
            return False
        self._run_url_change(bool(active))
        return True

    def _run_url_change(self, enable: bool) -> None:
        if self._url_busy or self.url_handlers is None:
            return
        self._url_busy = True
        self.url_switch.set_sensitive(False)
        self.url_repair_button.set_sensitive(False)
        self.url_row.set_subtitle("Updating desktop registration…")
        threading.Thread(target=self._url_worker, args=(enable,), daemon=True).start()

    def _url_worker(self, enable: bool) -> None:
        try:
            status = self.url_handlers.enable() if enable else self.url_handlers.disable()
            message = None
        except Exception as error:
            status = None
            message = f"Roblox link handling was not changed: {str(error)[:500]}"
        GLib.idle_add(self._url_done, enable, status, message)

    def _url_done(self, enable, status, message) -> bool:
        self._url_busy = False
        self._url_message = message
        if message:
            self.url_row.set_subtitle(message)
        elif status is not None:
            self.url_row.set_subtitle(status.message)
        self.url_switch.set_sensitive(True)
        self.url_repair_button.set_sensitive(True)
        self.window.refresh_pages(reload_library=False, reload_preferences=True)
        return GLib.SOURCE_REMOVE

    def _use_rosemary_path(self, _button) -> None:
        self.window.save_rosemary_choice(None)

    @staticmethod
    def _entry_row(title: str, entry: Gtk.Entry) -> Adw.ActionRow:
        row = Adw.ActionRow.new()
        row.set_title(title)
        row.add_suffix(entry)
        return row

    def _load_sources_worker(self) -> None:
        try:
            settings, errors = self.version_settings.load_independent()
            error = None
        except VersionSettingsError as failure:
            settings = None
            errors = {}
            error = str(failure)
        GLib.idle_add(self._show_sources_loaded, settings, errors, error)

    def _show_sources_loaded(self, settings, errors, error) -> bool:
        for app_type, widgets in self._source_widgets.items():
            endpoint, pointer, result_label, test_button, save_button, reset_button = widgets
            if settings is not None:
                source = settings.get(app_type, default_version_source(app_type))
                endpoint.set_text(source.endpoint)
                pointer.set_text(source.json_pointer)
                if app_type in errors:
                    result_label.set_text(
                        f"Saved {app_type.title()} settings are invalid: {errors[app_type]}. "
                        "Saving or restoring defaults will replace only this app's record."
                    )
                else:
                    result_label.set_text("Test the current fields without saving them.")
                self._set_source_actions(app_type, True)
            else:
                result_label.set_text(f"Could not load saved settings: {error}. The file was not changed.")
        return GLib.SOURCE_REMOVE

    def _set_source_actions(self, app_type: str, sensitive: bool) -> None:
        widgets = self._source_widgets.get(app_type)
        if widgets is None:
            return
        for action in widgets[3:]:
            action.set_sensitive(sensitive)

    def _test_source_clicked(self, button, app_type, endpoint, pointer, result_label) -> None:
        source = VersionSource(endpoint.get_text(), pointer.get_text())
        result_label.set_text("Testing configuration…")
        self._set_source_actions(app_type, False)
        threading.Thread(
            target=self._test_source_worker,
            args=(app_type, source, result_label, button),
            daemon=True,
        ).start()

    def _test_source_worker(self, app_type, source, result_label, button) -> None:
        try:
            result = self.version_client.test_configuration(source)
        except Exception as error:
            message = str(error)[:500] or type(error).__name__
            result = VersionTestResult(
                "Failed", None, "Not completed", None, "Not completed",
                f"Unexpected configuration test failure: {message}",
            )
        GLib.idle_add(self._show_source_test, app_type, result, result_label, button)

    def _show_source_test(self, app_type, result, result_label, button) -> bool:
        extracted = repr(result.extracted_version) if result.extracted_version is not None else "None"
        status = (
            f"Connection: {result.connection_status}\nHTTP status: {result.http_status if result.http_status is not None else 'Unavailable'}"
            f"\nJSON parsing: {result.json_result}\nExtracted version: {extracted}"
            f"\nVersion validation: {result.validation_result}"
        )
        if result.error:
            status += f"\nDetails: {result.error}"
        result_label.set_text(status)
        self._set_source_actions(app_type, True)
        return GLib.SOURCE_REMOVE

    def _save_source_clicked(self, button, app_type, endpoint, pointer, result_label) -> None:
        source = VersionSource(endpoint.get_text(), pointer.get_text())
        self._set_source_actions(app_type, False)
        result_label.set_text("Saving configuration…")
        threading.Thread(
            target=self._save_source_worker,
            args=(app_type, source, result_label, button),
            daemon=True,
        ).start()

    def _save_source_worker(self, app_type, source, result_label, button) -> None:
        try:
            self.version_settings.save(app_type, source)
            message = "Configuration saved. Refresh the app page to use the new version source."
        except Exception as error:
            message = f"Could not save configuration: {str(error)[:500]}"
        GLib.idle_add(self._show_source_saved, app_type, message, result_label)

    def _show_source_saved(self, app_type, message, result_label) -> bool:
        result_label.set_text(message)
        self._set_source_actions(app_type, True)
        return GLib.SOURCE_REMOVE

    def _reset_source_clicked(self, button, app_type, endpoint, pointer, result_label) -> None:
        self._set_source_actions(app_type, False)
        result_label.set_text("Restoring default configuration…")
        threading.Thread(
            target=self._reset_source_worker,
            args=(app_type, endpoint, pointer, result_label, button),
            daemon=True,
        ).start()

    def _reset_source_worker(self, app_type, endpoint, pointer, result_label, button) -> None:
        try:
            self.version_settings.reset(app_type)
            source = default_version_source(app_type)
            message = "Default configuration restored and saved."
        except Exception as error:
            source = None
            message = f"Could not restore defaults: {str(error)[:500]}"
        GLib.idle_add(self._show_source_reset, app_type, source, endpoint, pointer, message, result_label)

    def _show_source_reset(self, app_type, source, endpoint, pointer, message, result_label) -> bool:
        if source is not None:
            endpoint.set_text(source.endpoint)
            pointer.set_text(source.json_pointer)
        result_label.set_text(message)
        self._set_source_actions(app_type, True)
        return GLib.SOURCE_REMOVE
