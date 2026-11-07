"""Mods: replacement fonts, cursors, sounds and textures for Player and Studio."""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

from gi.repository import Adw, Gio, GLib, Gtk

from ..backend.mods import (
    APP_TYPES,
    ModsError,
    ModsStore,
    ModSettings,
    default_mods_directory,
    fetch_avatar_background,
    folder_files,
    png_size,
)
from ..backend.transfer_rate import format_size
from .sections import CollapsibleSection

FILES_SHOWN = 50
APP_TITLES = {"player": "Roblox Player", "studio": "Roblox Studio"}


class ModsPage(Gtk.Box):
    def __init__(self, window, *, store: ModsStore | None = None, background_fetcher=fetch_avatar_background) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        self.set_hexpand(True)
        self.set_margin_top(28)
        self.set_margin_bottom(28)
        self.set_margin_start(28)
        self.set_margin_end(28)
        self.window = window
        self.store = store or ModsStore()
        self.background_fetcher = background_fetcher
        self.app_type = "player"
        self.settings = {app_type: ModSettings() for app_type in APP_TYPES}
        self.files: dict[str, list] = {}
        self.downloading = False
        self._updating = False

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        heading = Gtk.Label(label="Mods", xalign=0, hexpand=True)
        heading.add_css_class("title-1")
        header.append(heading)
        switcher = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        switcher.add_css_class("linked")
        switcher.set_valign(Gtk.Align.CENTER)
        self.app_buttons: dict[str, Gtk.ToggleButton] = {}
        for app_type, label in (("player", "Player"), ("studio", "Studio")):
            button = Gtk.ToggleButton(label=label)
            if self.app_buttons:
                button.set_group(next(iter(self.app_buttons.values())))
            button.connect("toggled", self._app_toggled, app_type)
            switcher.append(button)
            self.app_buttons[app_type] = button
        header.append(switcher)
        self.append(header)

        self.status_label = Gtk.Label(xalign=0, wrap=True)
        self.status_label.add_css_class("dim-label")
        self.append(self.status_label)
        self.error_label = Gtk.Label(xalign=0, wrap=True, visible=False, selectable=True)
        self.error_label.add_css_class("error")
        self.append(self.error_label)

        quick = CollapsibleSection("quick", "Quick mods", "Common changes, each with its own switch or file.")
        self.append(quick)
        group = Adw.PreferencesGroup.new()
        self.font_row, self.font_remove = self._file_row("Font", self._choose_font, self._remove_font)
        group.add(self.font_row)
        self.cursor_row, self.cursor_remove = self._file_row("Mouse cursor", self._choose_cursor, self._remove_cursor)
        group.add(self.cursor_row)
        self.death_row, self.death_switch = self._switch_row(
            "Classic death sound",
            "Plays the classic “oof” when your character dies, using the oof.ogg Roblox still ships.",
            "classic_death_sound",
        )
        group.add(self.death_row)
        self.background_row, self.background_switch = self._switch_row(
            "Classic avatar editor background",
            "The avatar editor scene from before 2024, from Bloxstrap (MIT). Roseblox downloads one reviewed "
            "copy from GitHub and checks it before using it.",
            "classic_avatar_background",
        )
        self.background_spinner = Gtk.Spinner(valign=Gtk.Align.CENTER, visible=False)
        self.background_row.add_suffix(self.background_spinner)
        group.add(self.background_row)
        quick.add(group)

        folder_section = CollapsibleSection(
            "folder", "Mods folder", "Your own replacement files for textures, skyboxes, sounds and fonts."
        )
        self.append(folder_section)
        folder_group = Adw.PreferencesGroup.new()
        folder_group.set_description(
            "Put files here at the same path they have in the app's Contents/Resources folder, for example "
            "content/textures/Cursors/KeyboardMouse/ArrowCursor.png, content/sounds/ouch.ogg or "
            "PlatformContent/pc/textures/sky/sky512_bk.tex. Quick mods win over files here."
        )
        self.folder_row = Adw.ActionRow.new()
        self.folder_row.set_title("Folder")
        self.folder_row.set_subtitle_selectable(True)
        open_button = Gtk.Button(label="Open Folder", valign=Gtk.Align.CENTER)
        open_button.connect("clicked", self._open_folder)
        self.folder_row.add_suffix(open_button)
        refresh = Gtk.Button(label="Refresh", valign=Gtk.Align.CENTER)
        refresh.connect("clicked", lambda *_: self.scan())
        self.folder_row.add_suffix(refresh)
        folder_group.add(self.folder_row)
        folder_section.add(folder_group)
        self.files_group = Adw.PreferencesGroup.new()
        folder_section.add(self.files_group)
        self._file_rows: list[Gtk.Widget] = []

        self.app_buttons["player"].set_active(True)
        self.reload()
        self.connect("map", lambda *_: self.scan())

    def _file_row(self, title: str, choose, remove) -> tuple[Adw.ActionRow, Gtk.Button]:
        row = Adw.ActionRow.new()
        row.set_title(title)
        remove_button = Gtk.Button(label="Remove", valign=Gtk.Align.CENTER)
        remove_button.connect("clicked", lambda *_: remove())
        row.add_suffix(remove_button)
        choose_button = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        choose_button.connect("clicked", lambda *_: choose())
        row.add_suffix(choose_button)
        return row, remove_button

    def _switch_row(self, title: str, subtitle: str, field_name: str) -> tuple[Adw.ActionRow, Gtk.Switch]:
        row = Adw.ActionRow.new()
        row.set_title(title)
        row.set_subtitle(subtitle)
        switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        switch.connect("notify::active", self._switch_changed, field_name)
        row.add_suffix(switch)
        row.set_activatable_widget(switch)
        return row, switch

    @property
    def current(self) -> ModSettings:
        return self.settings[self.app_type]

    def reload(self) -> None:
        try:
            self.settings = {app_type: self.store.load(app_type) for app_type in APP_TYPES}
        except ModsError as error:
            self._error(str(error))
        self._show()

    def _show(self) -> None:
        self._updating = True
        try:
            settings = self.current
            title = APP_TITLES[self.app_type]
            self.status_label.set_text(
                f"Applied to {title} installations Roseblox installed, the next time they start. "
                "Turning a mod off puts Roblox's own file back. Installations you added from an existing "
                "app bundle are never changed."
            )
            self.font_row.set_subtitle(
                f"{Path(settings.font).name}, used for all text except monospace and other scripts' fallback fonts"
                if settings.font
                else "Roblox's fonts. Choose a .ttf or .otf file to use it for all text instead."
            )
            self.font_remove.set_visible(bool(settings.font))
            cursor_text = "Roblox's cursor. Choose a PNG image, up to 256 × 256 pixels."
            if settings.cursor:
                try:
                    width, height = png_size(Path(settings.cursor))
                    cursor_text = f"Your {width} × {height} image, for the arrow cursor"
                except ModsError as error:
                    cursor_text = str(error)
            self.cursor_row.set_subtitle(cursor_text)
            self.cursor_remove.set_visible(bool(settings.cursor))
            player = self.app_type == "player"
            self.death_row.set_visible(player)
            self.background_row.set_visible(player)
            self.death_switch.set_active(settings.classic_death_sound)
            self.background_switch.set_active(settings.classic_avatar_background)
            self.background_switch.set_sensitive(not self.downloading)
            self.background_spinner.set_visible(self.downloading)
            self.background_spinner.set_spinning(self.downloading)
            self.folder_row.set_subtitle(str(default_mods_directory(self.app_type)))
        finally:
            self._updating = False
        self._show_files()

    def _show_files(self) -> None:
        for row in self._file_rows:
            self.files_group.remove(row)
        self._file_rows = []
        files = self.files.get(self.app_type)
        if files is None:
            self.files_group.set_title("Checking the folder…")
            return
        used = [item for item in files if item.problem is None]
        refused = len(files) - len(used)
        if not files:
            self.files_group.set_title("The folder is empty")
            return
        size = format_size(sum(item.size for item in used))
        title = f"{len(used)} file{'s' if len(used) != 1 else ''} to apply, {size}"
        if refused:
            title += f"; {refused} not used"
        self.files_group.set_title(title)
        for item in sorted(files, key=lambda item: (item.problem is None, item.relative))[:FILES_SHOWN]:
            row = Adw.ActionRow.new()
            row.set_title(GLib.markup_escape_text(item.relative.removeprefix("Contents/Resources/")))
            row.set_subtitle(GLib.markup_escape_text(item.problem or format_size(item.size)))
            if item.problem:
                icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
                icon.add_css_class("warning")
                row.add_prefix(icon)
            self.files_group.add(row)
            self._file_rows.append(row)
        if len(files) > FILES_SHOWN:
            more = Adw.ActionRow.new()
            more.set_title(f"and {len(files) - FILES_SHOWN} more")
            self.files_group.add(more)
            self._file_rows.append(more)

    def scan(self) -> None:
        app_type = self.app_type
        folder = default_mods_directory(app_type)

        def worker() -> None:
            files = folder_files(folder)
            GLib.idle_add(self._scanned, app_type, files)

        threading.Thread(target=worker, daemon=True).start()

    def _scanned(self, app_type: str, files: list) -> bool:
        self.files[app_type] = files
        if app_type == self.app_type:
            self._show_files()
        return GLib.SOURCE_REMOVE

    def _app_toggled(self, button: Gtk.ToggleButton, app_type: str) -> None:
        if not button.get_active():
            return
        self.app_type = app_type
        self._error(None)
        self._show()
        if self.get_mapped():
            self.scan()

    def _error(self, message: str | None) -> None:
        self.error_label.set_text(message or "")
        self.error_label.set_visible(bool(message))

    def _save(self, settings: ModSettings) -> None:
        try:
            self.settings[self.app_type] = self.store.save(self.app_type, settings)
        except (ModsError, OSError) as error:
            self._error(f"Not saved: {error}")
        else:
            self._error(None)
        self._show()

    def _switch_changed(self, switch: Gtk.Switch, _pspec, field_name: str) -> None:
        if self._updating:
            return
        active = switch.get_active()
        if field_name == "classic_avatar_background" and active:
            self._download_background()
            return
        self._save(replace(self.current, **{field_name: active}))

    def _download_background(self) -> None:
        self.downloading = True
        app_type = self.app_type
        self._show()

        def worker() -> None:
            try:
                self.background_fetcher()
                error = None
            except ModsError as failure:
                error = str(failure)
            GLib.idle_add(self._background_ready, app_type, error)

        threading.Thread(target=worker, daemon=True).start()

    def _background_ready(self, app_type: str, error: str | None) -> bool:
        self.downloading = False
        if error:
            self._error(error)
            self._show()
            return GLib.SOURCE_REMOVE
        if app_type == self.app_type:
            self._save(replace(self.current, classic_avatar_background=True))
        else:
            self.settings[app_type] = self.store.save(app_type, replace(self.settings[app_type], classic_avatar_background=True))
            self._show()
        return GLib.SOURCE_REMOVE

    def _choose(self, title: str, patterns: tuple[str, ...], on_chosen) -> None:
        file_filter = Gtk.FileFilter()
        file_filter.set_name(title)
        for pattern in patterns:
            file_filter.add_pattern(pattern)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(file_filter)
        dialog = Gtk.FileDialog(title=title, modal=True, filters=filters)
        app_type = self.app_type

        def finished(dialog, result) -> None:
            try:
                file = dialog.open_finish(result)
            except GLib.Error:
                return
            if file.get_path():
                on_chosen(app_type, Path(file.get_path()))

        dialog.open(self.window, None, finished)

    def _choose_font(self) -> None:
        self._choose("Fonts", ("*.ttf", "*.otf", "*.TTF", "*.OTF"), self.set_font)

    def _choose_cursor(self) -> None:
        self._choose("PNG images", ("*.png", "*.PNG"), self.set_cursor)

    def set_font(self, app_type: str, path: Path | None) -> None:
        self._set_file(app_type, self.store.set_font, path)

    def set_cursor(self, app_type: str, path: Path | None) -> None:
        self._set_file(app_type, self.store.set_cursor, path)

    def _set_file(self, app_type: str, setter, path: Path | None) -> None:
        try:
            self.settings[app_type] = setter(app_type, path)
        except (ModsError, OSError) as error:
            self._error(str(error))
        else:
            self._error(None)
        self._show()

    def _remove_font(self) -> None:
        self.set_font(self.app_type, None)

    def _remove_cursor(self) -> None:
        self.set_cursor(self.app_type, None)

    def _open_folder(self, _button) -> None:
        folder = default_mods_directory(self.app_type)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            self._error(f"Could not create the folder: {error}")
            return
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(folder)))

        def finished(launcher, result) -> None:
            try:
                launcher.launch_finish(result)
            except GLib.Error as error:
                self._error(f"Could not open the folder: {error.message}")

        launcher.launch(self.window, None, finished)
        self.scan()
