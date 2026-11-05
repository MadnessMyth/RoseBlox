"""GTK pages for registered bundles and Rosemary status."""

from __future__ import annotations

import threading
import subprocess
from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gio, GLib, Gtk

from ..backend.library import APP_BUNDLE_NAMES, InstallationRecord, LibraryDataError
from ..backend.rosemary import (
    LaunchSession,
    ProcessIdentityError,
    RosemaryStatus,
    launch_application,
    stop_application,
    wait_for_running,
)


class InstallationPage(Gtk.Box):
    def __init__(
        self,
        title: str,
        icon_name: str,
        app_type: str | None,
        *,
        window: Gtk.Window,
        store,
        rosemary_status: RosemaryStatus,
        library_error: str | None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.window = window
        self.store = store
        self.app_type = app_type
        self.rosemary_status = rosemary_status
        self.library_error = library_error
        self._launching_ids: set[str] = window.launching_installations
        self._stopping_ids: set[str] = window.stopping_installations
        self._sessions: dict[str, LaunchSession] = window.process_sessions
        self._launch_buttons: dict[str, Gtk.Button] = {}
        self._remove_buttons: dict[str, Gtk.Button] = {}
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
            add_button.add_css_class("suggested-action")
            add_button.set_sensitive(library_error is None)
            add_button.connect("clicked", self._on_add_clicked)
            heading_row.append(add_button)
        self.append(heading_row)

        self.notice = Gtk.Label()
        self.notice.set_xalign(0)
        self.notice.set_wrap(True)
        self.notice.add_css_class("status-notice")
        self.notice.set_visible(False)
        self.append(self.notice)

        self.list_box = Gtk.ListBox()
        self.list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list_box.add_css_class("boxed-list")
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.list_box)
        scroller.set_vexpand(True)
        self.append(scroller)
        self.refresh()

    def refresh(self) -> None:
        self._launch_buttons = {}
        self._remove_buttons = {}
        child = self.list_box.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.list_box.remove(child)
            child = following

        if self.library_error:
            self.notice.set_label(self.library_error)
            self.notice.set_visible(True)
            return
        self.notice.set_visible(False)
        try:
            records = self.store.load()
        except LibraryDataError as error:
            self.library_error = str(error)
            self.notice.set_label(f"{error} No changes will be saved until the library file is repaired.")
            self.notice.set_visible(True)
            return

        if self.app_type:
            records = [record for record in records if record.app_type == self.app_type]
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
        path = Path(record.bundle_path)
        state = "Available" if path.is_dir() and (path / "Contents").is_dir() else "Missing or invalid"
        meta = Gtk.Label(label=f"{kind} · {state}")
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

        if self.app_type:
            launch_button = Gtk.Button()
            launch_button.add_css_class("suggested-action")
            launch_button.connect("clicked", self._on_launch_clicked, record)
            self._launch_buttons[record.id] = launch_button
            self._update_launch_button(record.id)
            contents.append(launch_button)

        remove = Gtk.Button.new_with_label("Remove")
        remove.set_tooltip_text("Remove this registration from Roseblox only")
        remove.set_sensitive(
            record.id not in self._launching_ids
            and record.id not in self._sessions
            and record.id not in self._stopping_ids
        )
        remove.connect("clicked", self._on_remove_clicked, record)
        self._remove_buttons[record.id] = remove
        contents.append(remove)
        row.set_child(contents)
        return row

    def _on_add_clicked(self, _button: Gtk.Button) -> None:
        picker = Gtk.FileDialog.new()
        picker.set_title(f"Select {APP_BUNDLE_NAMES[self.app_type]}")
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
        prompt.connect("response", self._on_name_response, bundle, name_entry)
        prompt.present()

    def _on_name_response(
        self,
        dialog: Adw.MessageDialog,
        response: str,
        bundle: Path,
        name_entry: Gtk.Entry,
    ) -> None:
        dialog.destroy()
        if response != "add":
            return
        try:
            self.store.add(self.app_type, bundle, name_entry.get_text())
            self._refresh_all()
        except (ValueError, LibraryDataError, OSError) as error:
            self._show_error("Could not add installation", str(error))

    def _on_launch_clicked(self, _button: Gtk.Button, record: InstallationRecord) -> None:
        session = self._sessions.get(record.id)
        if session is not None:
            self._stopping_ids.add(record.id)
            self._refresh_all()
            threading.Thread(
                target=self._stop_worker,
                args=(record.id, session),
                daemon=True,
            ).start()
            return
        if record.id in self._launching_ids or record.id in self._stopping_ids:
            return

        self._launching_ids.add(record.id)
        self._refresh_all()
        threading.Thread(
            target=self._launch_worker,
            args=(record.id, record),
            daemon=True,
        ).start()

    def _launch_worker(self, installation_id: str, record: InstallationRecord) -> None:
        try:
            session = launch_application(record, self.rosemary_status)
            wait_for_running(session)
        except (OSError, ValueError, RuntimeError) as error:
            GLib.idle_add(self._launch_failed, installation_id, str(error))
            return
        GLib.idle_add(self._launch_ready, installation_id, session)

    def _launch_ready(self, installation_id: str, session: LaunchSession) -> bool:
        self._launching_ids.discard(installation_id)
        if session.process.poll() is not None:
            self._refresh_all()
            self._show_error(
                "Roblox stopped during launch",
                "The verified application process exited before Roseblox could mark it running.",
            )
            return GLib.SOURCE_REMOVE
        self._sessions[installation_id] = session
        self._refresh_all()
        threading.Thread(
            target=self._watch_worker,
            args=(installation_id, session),
            daemon=True,
        ).start()
        return GLib.SOURCE_REMOVE

    def _launch_failed(self, installation_id: str, message: str) -> bool:
        self._launching_ids.discard(installation_id)
        self._refresh_all()
        self._show_error("Could not launch Roblox", message)
        return GLib.SOURCE_REMOVE

    def _watch_worker(self, installation_id: str, session: LaunchSession) -> None:
        return_code = session.process.wait()
        if session.output_done is not None:
            session.output_done.wait(timeout=2.0)
        output = (session.stderr or session.stdout).decode("utf-8", errors="replace").strip()
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
        self._refresh_all()
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
        self._refresh_all()
        return GLib.SOURCE_REMOVE

    def _stop_failed(self, installation_id: str, session: LaunchSession, message: str) -> bool:
        self._stopping_ids.discard(installation_id)
        if session.process.poll() is not None and self._sessions.get(installation_id) is session:
            del self._sessions[installation_id]
        self._refresh_all()
        self._show_error("Could not stop Roblox", message)
        return GLib.SOURCE_REMOVE

    def _update_launch_button(self, installation_id: str) -> None:
        button = self._launch_buttons.get(installation_id)
        if button is None:
            return
        if installation_id in self._launching_ids:
            button.set_label("Launching...")
            button.set_sensitive(False)
        elif installation_id in self._stopping_ids:
            button.set_label("Stopping…")
            button.set_sensitive(False)
        elif installation_id in self._sessions:
            button.set_label("Stop")
            button.set_sensitive(True)
        else:
            button.set_label("Launch")
            button.set_sensitive(True)
        remove_button = self._remove_buttons.get(installation_id)
        if remove_button is not None:
            remove_button.set_sensitive(
                installation_id not in self._launching_ids
                and installation_id not in self._sessions
                and installation_id not in self._stopping_ids
            )

    def _on_remove_clicked(self, _button: Gtk.Button, record: InstallationRecord) -> None:
        try:
            if self.store.remove(record.id):
                self._refresh_all()
        except LibraryDataError as error:
            self._show_error("Could not remove registration", str(error))

    def _refresh_all(self) -> None:
        if hasattr(self.window, "refresh_pages"):
            self.window.refresh_pages()
        else:
            self.refresh()

    def _show_error(self, heading: str, message: str) -> None:
        dialog = Adw.MessageDialog.new(self.window, heading, message)
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.present()


class SettingsPage(Gtk.Box):
    def __init__(self, status: RosemaryStatus) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_margin_top(40)
        self.set_margin_bottom(40)
        self.set_margin_start(40)
        self.set_margin_end(40)
        self.add_css_class("page-content")

        icon = Gtk.Image.new_from_icon_name("emblem-system-symbolic")
        icon.set_pixel_size(44)
        icon.add_css_class("page-icon")
        self.append(icon)
        heading = Gtk.Label(label="Settings")
        heading.add_css_class("title-1")
        self.append(heading)

        status_label = Gtk.Label()
        status_label.set_wrap(True)
        status_label.set_justify(Gtk.Justification.CENTER)
        if status.available:
            status_label.set_markup("<b>Rosemary detected</b>")
            path_label = Gtk.Label(label=status.executable)
            path_label.set_selectable(True)
            path_label.set_wrap(True)
            path_label.add_css_class("dim-label")
            self.append(status_label)
            self.append(path_label)
        else:
            status_label.set_markup("<b>Rosemary not found</b>")
            self.append(status_label)
            message = "Rosemary is required to launch Roblox. Install it separately or configure its executable in a future settings update."
            if status.error:
                message += f"\n{status.error}"
            detail = Gtk.Label(label=message)
            detail.set_wrap(True)
            detail.set_justify(Gtk.Justification.CENTER)
            detail.set_max_width_chars(60)
            detail.add_css_class("dim-label")
            self.append(detail)
