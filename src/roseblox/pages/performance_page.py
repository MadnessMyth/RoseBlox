"""Per-application performance and launch configuration for Player and Studio."""

from __future__ import annotations

import os
import shlex
import threading
import time
from dataclasses import replace
from pathlib import Path

import gi
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from ..backend.fast_flags import (
    FastFlagError,
    format_flag_document,
    normalize_flag_value,
    parse_flag_document,
    read_bundle_flags,
    validate_flag_name,
)
from ..backend.flag_catalog import (
    BUILT_IN_ALLOWLIST,
    CATEGORIES,
    Allowlist,
    categorize,
    fetch_allowlist,
    flag_info,
    load_cached_allowlist,
)
from ..backend import diagnostics, gamemode, mangohud
from ..backend.gpu import list_gpus
from ..backend.rosemary import build_launch_arguments
from ..backend.rosemary_paths import (
    CacheError,
    cache_entries,
    read_framerate_cap,
    roblox_settings_path,
    trash_cache,
)
from ..backend.transfer_rate import format_size
from ..backend.launch_config import (
    APP_TYPES,
    AppLaunchConfig,
    effective_environment,
    LaunchConfigError,
    LaunchConfigStore,
    describe_changes,
    environment_changes,
    validate_argument,
    validate_environment_name,
    validate_environment_value,
)
from ..backend.presets import (
    CUSTOM,
    GRASS_FLAGS,
    MSAA_FLAG,
    PRESETS,
    QUALITY_FLAG,
    TEXTURE_ENABLED_FLAG,
    TEXTURE_LEVEL_FLAG,
    matching_preset,
    preset,
)
from .sections import CollapsibleSection

APP_TITLES = {"player": "Roblox Player", "studio": "Roblox Studio"}
PRESENTATION_CHOICES = (
    (None, "Rosemary default (mailbox)"),
    ("mailbox", "Mailbox: low latency, no tearing"),
    ("fifo", "FIFO: V-Sync"),
    ("fifo_relaxed", "FIFO relaxed: V-Sync that may tear when late"),
    ("immediate", "Immediate: lowest latency, may tear"),
)
COMMON_REFRESH_RATES = (60, 75, 90, 120, 144, 165, 240, 360)
MSAA_CHOICES = ((None, "Roblox default"), ("1", "Off"), ("2", "2×"), ("4", "4×"), ("8", "8×"))
TEXTURE_CHOICES = ((None, "Roblox default"), ("0", "Lowest"), ("1", "Low"), ("2", "Medium"), ("3", "High"))
QUALITY_CHOICES = ((None, "Automatic"), *((str(level), f"Level {level}") for level in range(1, 22)))
PRESET_CHOICES = (*((candidate.id, candidate.title) for candidate in PRESETS), (CUSTOM, "Custom"))
GRAY_SKY_FLAG = "FFlagDebugSkyGray"
DPI_FLAG = "DFFlagDisableDPIScale"
COMMAND_KEY_CHOICES = (
    (None, "Super (default)"),
    ("super+ctrl:edit", "Super, and Ctrl for editing"),
    ("ctrl", "Ctrl"),
    ("alt", "Alt"),
)
COMMAND_KEY_DESCRIPTIONS = {
    None: "Super acts as the Mac Command key.",
    "super+ctrl:edit": "Super acts as Command, and Ctrl also works for copy, paste, cut, undo, redo and select all.",
    "ctrl": "Ctrl acts as Command, and Super as Ctrl.",
    "alt": "Alt acts as Command, and Super as Option.",
}
DECORATION_CHOICES = (
    (None, "Mac-style (default)"),
    ("server", "Desktop"),
)
DECORATION_DESCRIPTIONS = {
    None: "Rosemary draws a Mac-style titlebar with the app's menus in it.",
    "client": "Rosemary draws a Mac-style titlebar with the app's menus in it.",
    "server": "Your desktop draws the titlebar; the app's menus are in a row below it.",
}
BACKUPS_SHOWN = 10


def monitor_refresh_rates() -> set[int]:
    display = Gdk.Display.get_default()
    if display is None:
        return set()
    monitors = display.get_monitors()
    rates = set()
    for index in range(monitors.get_n_items()):
        millihertz = monitors.get_item(index).get_refresh_rate()
        if millihertz > 0:
            rates.add(round(millihertz / 1000))
    return rates


def describe_allowlist(allowlist: Allowlist) -> str:
    if allowlist.built_in:
        text = f"{len(allowlist.flags)} flags, from the list built into Roseblox."
    else:
        fetched = time.strftime("%Y-%m-%d %H:%M", time.localtime(allowlist.fetched_at))
        text = f"{len(allowlist.flags)} flags, fetched from Roblox at {fetched}."
    if allowlist.error:
        text += f"\n{allowlist.error}"
    return text


class _ListEditor:
    """A preferences group listing entries, with a row that adds or replaces one."""

    def __init__(self, title: str, description: str, *, placeholder_name: str, placeholder_value: str | None,
                 on_add, on_remove, on_edit=None) -> None:
        self.group = Adw.PreferencesGroup.new()
        self.group.set_title(title)
        self.group.set_description(description)
        self._rows: list[Gtk.Widget] = []
        self._on_add = on_add
        self._on_remove = on_remove
        self._on_edit = on_edit

        add_row = Adw.ActionRow.new()
        add_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        add_box.set_margin_top(6)
        add_box.set_margin_bottom(6)
        self.name_entry = Gtk.Entry(placeholder_text=placeholder_name, hexpand=True)
        add_box.append(self.name_entry)
        self.value_entry = None
        if placeholder_value is not None:
            self.value_entry = Gtk.Entry(placeholder_text=placeholder_value, hexpand=True)
            self.value_entry.connect("activate", self._add_clicked)
            add_box.append(self.value_entry)
        self.name_entry.connect("activate", self._add_clicked)
        self.add_button = Gtk.Button(label="Add")
        self.add_button.connect("clicked", self._add_clicked)
        add_box.append(self.add_button)
        add_row.set_child(add_box)
        self.group.add(add_row)
        self.error_label = Gtk.Label(xalign=0, wrap=True, visible=False)
        self.error_label.add_css_class("error")
        self.error_label.set_margin_top(6)
        self.group.add(self.error_label)

    def _add_clicked(self, _widget) -> None:
        name = self.name_entry.get_text().strip()
        value = self.value_entry.get_text() if self.value_entry is not None else None
        problem = self._on_add(name, value)
        self.error_label.set_text(problem or "")
        self.error_label.set_visible(bool(problem))
        if not problem:
            self.name_entry.set_text("")
            if self.value_entry is not None:
                self.value_entry.set_text("")

    def show(self, entries: list[tuple[str, str, str, bool]]) -> None:
        """Show ``(key, title, subtitle, warning)`` entries."""
        for row in self._rows:
            self.group.remove(row)
        self._rows = []
        for key, title, subtitle, warning in entries:
            row = Adw.ActionRow.new()
            row.set_title(GLib.markup_escape_text(title))
            row.set_subtitle(GLib.markup_escape_text(subtitle))
            row.set_subtitle_selectable(True)
            if warning:
                icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
                icon.add_css_class("warning")
                icon.set_tooltip_text(subtitle)
                row.add_prefix(icon)
            if self._on_edit is not None:
                edit = Gtk.Button.new_from_icon_name("document-edit-symbolic")
                edit.add_css_class("flat")
                edit.set_valign(Gtk.Align.CENTER)
                edit.set_tooltip_text(f"Edit {title}")
                edit.connect("clicked", lambda _button, key=key: self._on_edit(key))
                row.add_suffix(edit)
            remove = Gtk.Button.new_from_icon_name("user-trash-symbolic")
            remove.add_css_class("flat")
            remove.set_valign(Gtk.Align.CENTER)
            remove.set_tooltip_text(f"Remove {title}")
            remove.connect("clicked", lambda _button, key=key: self._on_remove(key))
            row.add_suffix(remove)
            self.group.add(row)
            self._rows.append(row)

    @property
    def rows(self) -> list[Gtk.Widget]:
        return list(self._rows)


class PerformancePage(Gtk.Box):
    """Edit each application's launch configuration, review it, then save it with a backup."""

    def __init__(self, window, *, store: LaunchConfigStore | None = None, allowlist_fetcher=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        self.set_hexpand(True)
        self.set_margin_top(28)
        self.set_margin_bottom(28)
        self.set_margin_start(28)
        self.set_margin_end(28)
        self.window = window
        self.store = store or LaunchConfigStore()
        self._allowlist_fetcher = allowlist_fetcher
        self.app_type = "player"
        self.saved = {app_type: AppLaunchConfig() for app_type in APP_TYPES}
        self.drafts = dict(self.saved)
        self.load_error: str | None = None
        self.loaded = False
        self.saving = False
        self.allowlist = Allowlist(BUILT_IN_ALLOWLIST, None)
        self.backups = []
        self.gpus = []
        self.gamemode_available = False
        self.mangohud_available = False
        self._gpu_values: list[str | None] = []
        self.framerate_caps: dict[str, int | None] = {}
        self.caches: dict[str, list] = {}
        self.report: list[tuple[str, str]] = []
        self.scanning = False
        self._updating = False

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        heading = Gtk.Label(label="Performance", xalign=0, hexpand=True)
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

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.status_label = Gtk.Label(xalign=0, hexpand=True, wrap=True)
        self.status_label.add_css_class("dim-label")
        bar.append(self.status_label)
        self.discard_button = Gtk.Button(label="Discard")
        self.discard_button.connect("clicked", self._discard_clicked)
        bar.append(self.discard_button)
        self.review_button = Gtk.Button(label="Review Changes…")
        self.review_button.add_css_class("suggested-action")
        self.review_button.connect("clicked", self._review_clicked)
        bar.append(self.review_button)
        self.append(bar)

        self.error_label = Gtk.Label(xalign=0, wrap=True, visible=False, selectable=True)
        self.error_label.add_css_class("error")
        self.append(self.error_label)

        self._build_graphics()
        self._build_flags()
        self._build_input()
        self._build_environment()
        self._build_arguments()
        self._build_backups()
        self._build_caches()
        self._build_diagnostics()

        self.app_buttons["player"].set_active(True)
        self.allowlist = load_cached_allowlist()
        self._show_draft()
        self._show_caches()
        self._show_report()
        self.reload()
        self._allowlist_requested = False
        self.connect("map", self._mapped)

    def _mapped(self, _widget) -> None:
        if not self._allowlist_requested:
            self._allowlist_requested = True
            self.refresh_allowlist(force=False)
            self.scan()

    def _section(self, section_id: str, title: str, description: str, *, expanded: bool = True) -> CollapsibleSection:
        section = CollapsibleSection(section_id, title, description, expanded=expanded)
        self.append(section)
        return section

    def _build_graphics(self) -> None:
        section = self._section(
            "graphics", "Graphics & frame pacing", "How Rosemary presents frames and what Roblox renders."
        )
        presets = Adw.PreferencesGroup.new()
        self.preset_row = Adw.ComboRow.new()
        self.preset_row.set_title("Preset")
        self.preset_row.set_model(Gtk.StringList.new([label for _value, label in PRESET_CHOICES]))
        self.preset_row.connect("notify::selected", self._preset_changed)
        presets.add(self.preset_row)
        section.add(presets)

        group = Adw.PreferencesGroup.new()
        renderer = Adw.ActionRow.new()
        renderer.set_title("Renderer")
        renderer.set_subtitle("Metal, translated to Vulkan by Rosemary. Roblox for macOS has no other renderer.")
        group.add(renderer)
        self.gpu_row = Adw.ComboRow.new()
        self.gpu_row.set_title("GPU")
        self.gpu_row.connect("notify::selected", self._gpu_changed)
        group.add(self.gpu_row)
        self.presentation_row = Adw.ComboRow.new()
        self.presentation_row.set_title("Presentation mode")
        self.presentation_row.set_subtitle("Rosemary uses FIFO when the GPU does not offer the chosen mode.")
        self.presentation_row.set_model(Gtk.StringList.new([label for _value, label in PRESENTATION_CHOICES]))
        self.presentation_row.connect("notify::selected", self._presentation_changed)
        group.add(self.presentation_row)
        self.refresh_row = Adw.ComboRow.new()
        self.refresh_row.set_title("Refresh rate")
        self.refresh_row.set_subtitle("The refresh rate Roblox is told the display has; it caps its frame rate to it.")
        self.refresh_row.connect("notify::selected", self._refresh_changed)
        self._refresh_values: list[int | None] = []
        group.add(self.refresh_row)
        self.framerate_row = Adw.ActionRow.new()
        self.framerate_row.set_title("Roblox frame rate cap")
        group.add(self.framerate_row)
        self.gamemode_row = self._switch_row("GameMode", self._gamemode_changed)
        group.add(self.gamemode_row)
        self.mangohud_row = self._switch_row("MangoHud overlay", self._mangohud_changed)
        group.add(self.mangohud_row)
        section.add(group)

        quality = Adw.PreferencesGroup.new()
        quality.set_title("Roblox graphics")
        quality.set_description("These set FastFlags from Roblox's allowlist; they also appear in the list below.")
        self.msaa_row = self._choice_row("Anti-aliasing", MSAA_CHOICES, self._msaa_changed)
        self.texture_row = self._choice_row("Texture quality", TEXTURE_CHOICES, self._texture_changed)
        self.quality_row = self._choice_row("Graphics quality level", QUALITY_CHOICES, self._quality_changed)
        self.grass_row = self._switch_row("Hide grass", self._grass_changed)
        self.gray_sky_row = self._switch_row("Gray sky", self._gray_sky_changed)
        self.dpi_row = self._switch_row("Disable DPI scaling", self._dpi_changed)
        for row in (self.msaa_row, self.texture_row, self.quality_row, self.grass_row, self.gray_sky_row, self.dpi_row):
            quality.add(row)
        section.add(quality)

    def _choice_row(self, title: str, choices, handler) -> Adw.ComboRow:
        row = Adw.ComboRow.new()
        row.set_title(title)
        row.set_model(Gtk.StringList.new([label for _value, label in choices]))
        row.connect("notify::selected", handler)
        return row

    def _switch_row(self, title: str, handler) -> Adw.ActionRow:
        row = Adw.ActionRow.new()
        row.set_title(title)
        switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        switch.connect("notify::active", handler)
        row.add_suffix(switch)
        row.set_activatable_widget(switch)
        row.switch = switch
        return row

    def _build_flags(self) -> None:
        section = self._section(
            "flags", "FastFlags", "Every flag written to ClientAppSettings.json when the application starts."
        )
        status = Adw.PreferencesGroup.new()
        self.allowlist_row = Adw.ActionRow.new()
        self.allowlist_row.set_title("Roblox's allowlist")
        self.allowlist_refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self.allowlist_refresh.add_css_class("flat")
        self.allowlist_refresh.set_valign(Gtk.Align.CENTER)
        self.allowlist_refresh.set_tooltip_text("Fetch Roblox's allowlist again")
        self.allowlist_refresh.connect("clicked", lambda _button: self.refresh_allowlist(force=True))
        self.allowlist_row.add_suffix(self.allowlist_refresh)
        status.add(self.allowlist_row)
        section.add(status)

        self.flag_editor = _ListEditor(
            "Flags",
            "",
            placeholder_name="FIntDebugForceMSAASamples",
            placeholder_value="4",
            on_add=self._add_flag,
            on_remove=self._remove_flag,
            on_edit=self._edit_flag,
        )
        section.add(self.flag_editor.group)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.import_button = Gtk.Button(label="Import…")
        self.import_button.set_tooltip_text("Add flags from a ClientAppSettings.json file")
        self.import_button.connect("clicked", self._import_clicked)
        actions.append(self.import_button)
        self.export_button = Gtk.Button(label="Export…")
        self.export_button.set_tooltip_text("Save these flags as a ClientAppSettings.json file")
        self.export_button.connect("clicked", self._export_clicked)
        actions.append(self.export_button)
        self.adopt_button = Gtk.Button(label="Use Flags from Installation")
        self.adopt_button.set_tooltip_text("Add the flags already in the preferred installation's ClientAppSettings.json")
        self.adopt_button.connect("clicked", self._adopt_clicked)
        actions.append(self.adopt_button)
        section.add(actions)
        self.flag_message = Gtk.Label(xalign=0, wrap=True, visible=False)
        self.flag_message.add_css_class("dim-label")
        section.add(self.flag_message)

    def _build_input(self) -> None:
        section = self._section(
            "input", "Keyboard & window", "How Mac shortcuts map to your keyboard, and who draws the titlebar.",
            expanded=False,
        )
        group = Adw.PreferencesGroup.new()
        self.command_key_row = self._choice_row("Command key", COMMAND_KEY_CHOICES, self._command_key_changed)
        group.add(self.command_key_row)
        self.decorations_row = self._choice_row("Titlebar", DECORATION_CHOICES, self._decorations_changed)
        group.add(self.decorations_row)
        section.add(group)

    def _build_environment(self) -> None:
        section = self._section(
            "environment", "Environment", "Variables for this application only; nothing is set globally.", expanded=False
        )
        self.rosemary_env_editor = _ListEditor(
            "Rosemary",
            "Read by Rosemary itself, such as ROSEMARY_SHADER_CACHE or MESA_VK_DEVICE_SELECT.",
            placeholder_name="NAME",
            placeholder_value="value",
            on_add=lambda name, value: self._add_variable(name, value, guest=False),
            on_remove=lambda name: self._remove_variable(name, guest=False),
        )
        section.add(self.rosemary_env_editor.group)
        self.guest_env_editor = _ListEditor(
            "Roblox",
            "Seen by Roblox inside Rosemary. Rosemary gives Roblox none of the Linux environment otherwise.",
            placeholder_name="NAME",
            placeholder_value="value",
            on_add=lambda name, value: self._add_variable(name, value, guest=True),
            on_remove=lambda name: self._remove_variable(name, guest=True),
        )
        section.add(self.guest_env_editor.group)
        self.environment_preview = self._preview_label()
        section.add(self.environment_preview)

    def _build_arguments(self) -> None:
        section = self._section(
            "arguments", "Launch arguments", "Passed to Roblox after the link Roseblox opens, without a shell.",
            expanded=False,
        )
        self.argument_editor = _ListEditor(
            "Arguments",
            "",
            placeholder_name="-argument",
            placeholder_value=None,
            on_add=lambda argument, _value: self._add_argument(argument),
            on_remove=self._remove_argument,
        )
        section.add(self.argument_editor.group)
        self.command_preview = self._preview_label()
        section.add(self.command_preview)

    @staticmethod
    def _preview_label() -> Gtk.Label:
        label = Gtk.Label(xalign=0, wrap=True, selectable=True)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.add_css_class("monospace")
        label.add_css_class("dim-label")
        return label

    def _build_backups(self) -> None:
        section = self._section(
            "backups", "Backups", "Every applied change keeps the previous configuration of both applications.",
            expanded=False,
        )
        self.backups_group = Adw.PreferencesGroup.new()
        self._backup_rows: list[Gtk.Widget] = []
        section.add(self.backups_group)
        self.defaults_button = Gtk.Button(label="Reset to Defaults…")
        self.defaults_button.set_halign(Gtk.Align.START)
        self.defaults_button.set_tooltip_text("Clear this application's configuration; you review it before it applies")
        self.defaults_button.connect("clicked", self._defaults_clicked)
        section.add(self.defaults_button)

    def _build_caches(self) -> None:
        section = self._section(
            "caches", "Caches", "Caches Roblox and Rosemary rebuild by themselves. Cleared caches go to the trash.",
            expanded=False,
        )
        self.caches_group = Adw.PreferencesGroup.new()
        self._cache_rows: list[Gtk.Widget] = []
        section.add(self.caches_group)
        self.caches_refresh = Gtk.Button(label="Refresh", halign=Gtk.Align.START)
        self.caches_refresh.connect("clicked", lambda _button: self.scan())
        section.add(self.caches_refresh)

    def _build_diagnostics(self) -> None:
        section = self._section(
            "diagnostics", "Diagnostics", "Facts about this system for troubleshooting; nothing here is estimated.",
            expanded=False,
        )
        self.diagnostics_group = Adw.PreferencesGroup.new()
        self._diagnostic_rows: list[Gtk.Widget] = []
        section.add(self.diagnostics_group)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.copy_report_button = Gtk.Button(label="Copy Report")
        self.copy_report_button.connect("clicked", self._copy_report)
        actions.append(self.copy_report_button)
        self.diagnostics_refresh = Gtk.Button(label="Refresh")
        self.diagnostics_refresh.connect("clicked", lambda _button: self.scan())
        actions.append(self.diagnostics_refresh)
        section.add(actions)
        self.diagnostics_message = Gtk.Label(xalign=0, visible=False)
        self.diagnostics_message.add_css_class("dim-label")
        section.add(self.diagnostics_message)

    @property
    def draft(self) -> AppLaunchConfig:
        return self.drafts[self.app_type]

    @property
    def changes(self) -> list[str]:
        return describe_changes(self.saved[self.app_type], self.draft, {gpu.slot: gpu.name for gpu in self.gpus})

    def _set_draft(self, config: AppLaunchConfig) -> None:
        self.drafts[self.app_type] = config
        self._show_draft()

    def _set_flags(self, flags: dict[str, str]) -> None:
        self._set_draft(replace(self.draft, fast_flags=flags))

    def _app_toggled(self, button: Gtk.ToggleButton, app_type: str) -> None:
        if button.get_active() and app_type != self.app_type:
            self.app_type = app_type
            self._show_draft()
            self._show_caches()
            if self.report:
                self.scan()

    def _show_draft(self) -> None:
        self._updating = True
        try:
            config = self.draft
            current = matching_preset(config, gamemode_available=self.gamemode_available)
            self.preset_row.set_selected([value for value, _label in PRESET_CHOICES].index(current))
            self.preset_row.set_subtitle(
                preset(current).description
                + " Your other flags, variables and arguments are kept; review the changes before applying."
                if current != CUSTOM
                else "These settings match no preset. Choosing one changes only the settings it lists."
            )
            self.presentation_row.set_selected(
                [value for value, _label in PRESENTATION_CHOICES].index(config.presentation_mode)
            )
            rates = set(COMMON_REFRESH_RATES) | monitor_refresh_rates()
            if config.refresh_rate is not None:
                rates.add(config.refresh_rate)
            values = [None, *sorted(rates)]
            if values != self._refresh_values:
                self._refresh_values = values
                self.refresh_row.set_model(
                    Gtk.StringList.new(["Display default", *(f"{rate} Hz" for rate in sorted(rates))])
                )
            self.refresh_row.set_selected(self._refresh_values.index(config.refresh_rate))
            self._show_gpu(config)
            self._select(self.command_key_row, COMMAND_KEY_CHOICES, config.command_key, describe=False)
            self.command_key_row.set_subtitle(COMMAND_KEY_DESCRIPTIONS[config.command_key])
            self._select(self.decorations_row, DECORATION_CHOICES, config.decorations, describe=False)
            self.decorations_row.set_subtitle(DECORATION_DESCRIPTIONS[config.decorations])
            self._show_framerate_cap()
            self.gamemode_row.switch.set_active(config.gamemode)
            self.gamemode_row.switch.set_sensitive(self.gamemode_available or config.gamemode)
            self.gamemode_row.set_subtitle(
                "Asks Feral GameMode to tune the system while the application runs."
                if self.gamemode_available
                else "GameMode is not installed or its daemon cannot be reached."
            )
            self.mangohud_row.switch.set_active(config.mangohud)
            self.mangohud_row.switch.set_sensitive(self.mangohud_available or config.mangohud)
            self.mangohud_row.set_subtitle(
                "Shows frame rate and frame times through MangoHud's Vulkan layer; configure it in "
                "~/.config/MangoHud/MangoHud.conf. With Rosemary's AppImage, add ROSEMARY_GLIBC=host to the "
                "Rosemary environment if it does not appear."
                if self.mangohud_available
                else "MangoHud's Vulkan layer is not installed."
            )
            flags = config.fast_flags
            self._select(self.msaa_row, MSAA_CHOICES, flags.get(MSAA_FLAG))
            texture = flags.get(TEXTURE_LEVEL_FLAG) if flags.get(TEXTURE_ENABLED_FLAG) == "True" else None
            self._select(self.texture_row, TEXTURE_CHOICES, texture)
            self._select(self.quality_row, QUALITY_CHOICES, flags.get(QUALITY_FLAG))
            self.grass_row.switch.set_active(all(flags.get(name) == "0" for name in GRASS_FLAGS))
            self.gray_sky_row.switch.set_active(flags.get(GRAY_SKY_FLAG) == "True")
            self.dpi_row.switch.set_active(flags.get(DPI_FLAG) == "True")
        finally:
            self._updating = False
        self._show_flags()
        self._show_environment()
        self._show_arguments()
        self._show_status()

    def _show_gpu(self, config: AppLaunchConfig) -> None:
        values: list[str | None] = [None, *(gpu.slot for gpu in self.gpus)]
        labels = ["Chosen by Rosemary (prefers a dedicated GPU)", *(gpu.name for gpu in self.gpus)]
        if config.gpu is not None and config.gpu not in values:
            values.append(config.gpu)
            labels.append(f"{config.gpu} (not found; Rosemary chooses)")
        if values != self._gpu_values:
            self._gpu_values = values
            self.gpu_row.set_model(Gtk.StringList.new(labels))
        self.gpu_row.set_selected(values.index(config.gpu))
        choosable = len(self.gpus) > 1 or config.gpu is not None
        self.gpu_row.set_sensitive(choosable)
        if choosable:
            self.gpu_row.set_subtitle("Pins one card through the Vulkan loader, for computers with several GPUs.")
        elif self.gpus:
            self.gpu_row.set_subtitle(f"Only one GPU was found: {self.gpus[0].name}.")
        else:
            self.gpu_row.set_subtitle("No GPU was found through /sys/class/drm.")

    def _show_framerate_cap(self) -> None:
        explanation = (
            "Set in Roblox under Settings → Maximum Frame Rate, which offers rates up to the refresh rate above."
        )
        if self.app_type not in self.framerate_caps:
            saved = ""
        elif self.framerate_caps[self.app_type] is None:
            saved = " Roblox has not saved its settings in this prefix yet."
        elif self.framerate_caps[self.app_type] <= 0:
            saved = " Roblox's saved choice: Default."
        else:
            saved = f" Roblox's saved choice: {self.framerate_caps[self.app_type]} FPS."
        self.framerate_row.set_subtitle(explanation + saved)

    def _show_caches(self) -> None:
        for row in self._cache_rows:
            self.caches_group.remove(row)
        self._cache_rows = []
        entries = self.caches.get(self.app_type)
        if not entries:
            row = Adw.ActionRow.new()
            if self.scanning:
                row.set_title("Checking caches…")
            elif entries is None:
                row.set_title("Not checked yet")
            else:
                row.set_title("No caches to clear")
            if entries == []:
                row.set_subtitle("Roblox and Rosemary have not cached anything for this application yet.")
            self.caches_group.add(row)
            self._cache_rows.append(row)
        for entry in entries or []:
            row = Adw.ActionRow.new()
            row.set_title(GLib.markup_escape_text(entry.title))
            row.set_subtitle(
                GLib.markup_escape_text(
                    f"{format_size(entry.size)} in {entry.files} files · {entry.path}\n{entry.description}"
                )
            )
            row.set_subtitle_selectable(True)
            button = Gtk.Button(label="Move to Trash…", valign=Gtk.Align.CENTER)
            button.set_sensitive(not self.scanning)
            button.connect("clicked", self._trash_clicked, entry)
            row.add_suffix(button)
            self.caches_group.add(row)
            self._cache_rows.append(row)

    def _show_report(self) -> None:
        for row in self._diagnostic_rows:
            self.diagnostics_group.remove(row)
        self._diagnostic_rows = []
        for label, value in self.report or [("Diagnostics", "Collecting…" if self.scanning else "Not collected yet")]:
            row = Adw.ActionRow.new()
            row.set_title(GLib.markup_escape_text(label))
            row.set_subtitle(GLib.markup_escape_text(value))
            row.set_subtitle_selectable(True)
            self.diagnostics_group.add(row)
            self._diagnostic_rows.append(row)
        self.copy_report_button.set_sensitive(bool(self.report))

    def _app_environment(self, app_type: str, gpus=None) -> dict[str, str]:
        return effective_environment(self.saved[app_type], os.environ, self.gpus if gpus is None else gpus)

    def scan(self) -> None:
        if self.scanning:
            return
        self.scanning = True
        for button in (self.caches_refresh, self.diagnostics_refresh):
            button.set_sensitive(False)
        self._show_caches()
        self._show_report()
        status = getattr(self.window, "rosemary_status", None)
        environments = {app_type: self._app_environment(app_type) for app_type in APP_TYPES}
        threading.Thread(
            target=self._scan_worker,
            args=(environments, getattr(status, "executable", None), self.app_type),
            daemon=True,
        ).start()

    def _scan_worker(self, environments, executable, app_type) -> None:
        caps = {app: read_framerate_cap(roblox_settings_path(env)) for app, env in environments.items()}
        caches = {app: cache_entries(env) for app, env in environments.items()}
        gpus = list_gpus()
        report = diagnostics.collect(
            rosemary_executable=executable,
            env=environments[app_type],
            gpus=gpus,
            gamemode_available=gamemode.available(),
        )
        GLib.idle_add(self._scanned, caps, caches, report)

    def _scanned(self, caps, caches, report) -> bool:
        self.scanning = False
        self.framerate_caps, self.caches, self.report = caps, caches, report
        for button in (self.caches_refresh, self.diagnostics_refresh):
            button.set_sensitive(True)
        self._show_framerate_cap()
        self._show_caches()
        self._show_report()
        return GLib.SOURCE_REMOVE

    def _copy_report(self, _button) -> None:
        text = f"Diagnostics for {APP_TITLES[self.app_type]}\n" + diagnostics.format_report(self.report)
        self.get_clipboard().set(text)
        self.diagnostics_message.set_text("Copied the report to the clipboard.")
        self.diagnostics_message.set_visible(True)

    def _trash_clicked(self, _button, entry) -> None:
        dialog = Adw.MessageDialog.new(
            self.window,
            f"Move {entry.title} to the trash?",
            f"{format_size(entry.size)} in {entry.path}. It is rebuilt when needed, and you can restore it "
            "from the trash. Close the application first so it is not using the cache.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("trash", "Move to Trash")
        dialog.set_response_appearance("trash", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")
        dialog.connect("response", self._trash_response, entry)
        dialog.present()

    def _trash_response(self, _dialog, response: str, entry) -> None:
        if response == "trash":
            self.trash(entry)

    def trash(self, entry) -> None:
        environment = self._app_environment(self.app_type)
        self.scanning = True
        self._show_caches()
        threading.Thread(target=self._trash_worker, args=(entry, environment), daemon=True).start()

    def _trash_worker(self, entry, environment) -> None:
        try:
            trash_cache(entry.path, environment)
            error = None
        except CacheError as failure:
            error = str(failure)
        GLib.idle_add(self._trashed, error)

    def _trashed(self, error: str | None) -> bool:
        self.scanning = False
        if error is not None:
            self._show_error("Could not clear the cache", error)
        self.scan()
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _select(row: Adw.ComboRow, choices, value: str | None, *, describe: bool = True) -> None:
        values = [choice for choice, _label in choices]
        row.set_selected(values.index(value) if value in values else 0)
        if describe:
            row.set_subtitle("" if value in values else f"Custom value {value} set in the FastFlag list")

    def _show_status(self) -> None:
        title = APP_TITLES[self.app_type]
        editable = self.loaded and not self.saving
        count = len(self.changes)
        if not self.loaded:
            text = "Loading the saved configuration…"
        elif self.saving:
            text = f"Saving {title}'s configuration…"
        elif count:
            text = f"{count} unapplied change{'s' if count != 1 else ''} for {title}."
        else:
            text = f"{title} uses the saved configuration. Changes apply the next time it starts."
        self.status_label.set_text(text)
        self.review_button.set_sensitive(editable and count > 0 and self.load_error is None)
        self.discard_button.set_sensitive(editable and count > 0)
        self.error_label.set_text(self.load_error or "")
        self.error_label.set_visible(self.load_error is not None)

    def _show_flags(self) -> None:
        flags = self.draft.fast_flags
        player = self.app_type == "player"
        order = {category: index for index, category in enumerate(CATEGORIES)}
        entries = []
        for name in sorted(flags, key=lambda flag: (order[categorize(flag)], flag)):
            info = flag_info(name)
            ignored = player and not self.allowlist.allows(name)
            parts = [f"= {flags[name]}", info.category]
            if info.title != name:
                parts.insert(1, info.title)
            if ignored:
                parts.append("Roblox Player ignores this flag: it is not on Roblox's allowlist")
            entries.append((name, name, " · ".join(parts), ignored))
        self.flag_editor.show(entries)
        self.flag_editor.group.set_description(
            "Roblox Player only reads flags on Roblox's allowlist and ignores the rest."
            if player
            else "Roblox Studio has no allowlist; flags outside it are experimental and may change without notice."
        )
        self.allowlist_row.set_subtitle(describe_allowlist(self.allowlist))
        self.export_button.set_sensitive(bool(flags))

    def _show_environment(self) -> None:
        config = self.draft
        self.rosemary_env_editor.show(
            [(name, name, value, False) for name, value in sorted(config.rosemary_environment.items())]
        )
        self.guest_env_editor.show(
            [(name, name, value, False) for name, value in sorted(config.guest_environment.items())]
        )
        changes = environment_changes(config, os.environ, self.gpus)
        if changes:
            text = "Roseblox sets for this application:\n" + "\n".join(
                f"{name}={value!r}" if "\n" in value else f"{name}={value}" for name, value in sorted(changes.items())
            )
        else:
            text = "Roseblox sets no extra variables for this application."
        self.environment_preview.set_text(text)

    def _show_arguments(self) -> None:
        arguments = self.draft.arguments
        self.argument_editor.show([(str(index), argument, "", False) for index, argument in enumerate(arguments)])
        status = getattr(self.window, "rosemary_status", None)
        rosemary = getattr(status, "executable", None) or "rosemary"
        bundle = self._bundle_path() or f"<{APP_TITLES[self.app_type]} bundle>"
        link = ["<link, when opened from a link>"] if self.app_type == "player" else []
        command = build_launch_arguments(rosemary, bundle, [*link, *arguments])
        self.command_preview.set_text("Command:\n" + shlex.join(command))

    def _bundle_path(self) -> str | None:
        resolved = getattr(self.window, "resolved_preferred_ids", {}) or {}
        installation_id = resolved.get(self.app_type)
        for record in getattr(self.window, "library_records", []) or []:
            if record.id == installation_id:
                return record.bundle_path
        return None

    def _presentation_changed(self, row, _pspec) -> None:
        if not self._updating:
            value = PRESENTATION_CHOICES[row.get_selected()][0]
            self._set_draft(replace(self.draft, presentation_mode=value))

    def _preset_changed(self, row, _pspec) -> None:
        if self._updating:
            return
        chosen = PRESET_CHOICES[row.get_selected()][0]
        if chosen == CUSTOM:
            self._show_draft()
            return
        self._set_draft(preset(chosen).apply(self.draft, gamemode_available=self.gamemode_available))

    def _gpu_changed(self, row, _pspec) -> None:
        if not self._updating and row.get_selected() < len(self._gpu_values):
            self._set_draft(replace(self.draft, gpu=self._gpu_values[row.get_selected()]))

    def _command_key_changed(self, row, _pspec) -> None:
        if not self._updating:
            self._set_draft(replace(self.draft, command_key=COMMAND_KEY_CHOICES[row.get_selected()][0]))

    def _decorations_changed(self, row, _pspec) -> None:
        if not self._updating:
            self._set_draft(replace(self.draft, decorations=DECORATION_CHOICES[row.get_selected()][0]))

    def _mangohud_changed(self, switch, _pspec) -> None:
        if not self._updating:
            self._set_draft(replace(self.draft, mangohud=switch.get_active()))

    def _gamemode_changed(self, switch, _pspec) -> None:
        if not self._updating:
            self._set_draft(replace(self.draft, gamemode=switch.get_active()))

    def _refresh_changed(self, row, _pspec) -> None:
        if not self._updating and row.get_selected() < len(self._refresh_values):
            self._set_draft(replace(self.draft, refresh_rate=self._refresh_values[row.get_selected()]))

    def _choice_flags(self, row, choices, names: tuple[str, ...], values) -> None:
        if self._updating:
            return
        flags = dict(self.draft.fast_flags)
        chosen = choices[row.get_selected()][0]
        for name, value in zip(names, values(chosen)):
            if value is None:
                flags.pop(name, None)
            else:
                flags[name] = value
        self._set_flags(flags)

    def _msaa_changed(self, row, _pspec) -> None:
        self._choice_flags(row, MSAA_CHOICES, (MSAA_FLAG,), lambda value: (value,))

    def _texture_changed(self, row, _pspec) -> None:
        self._choice_flags(
            row, TEXTURE_CHOICES, (TEXTURE_ENABLED_FLAG, TEXTURE_LEVEL_FLAG),
            lambda value: (None, None) if value is None else ("True", value),
        )

    def _quality_changed(self, row, _pspec) -> None:
        self._choice_flags(row, QUALITY_CHOICES, (QUALITY_FLAG,), lambda value: (value,))

    def _switch_flags(self, switch, values: dict[str, str]) -> None:
        if self._updating:
            return
        flags = dict(self.draft.fast_flags)
        for name, value in values.items():
            if switch.get_active():
                flags[name] = value
            else:
                flags.pop(name, None)
        self._set_flags(flags)

    def _grass_changed(self, switch, _pspec) -> None:
        self._switch_flags(switch, {name: "0" for name in GRASS_FLAGS})

    def _gray_sky_changed(self, switch, _pspec) -> None:
        self._switch_flags(switch, {GRAY_SKY_FLAG: "True"})

    def _dpi_changed(self, switch, _pspec) -> None:
        self._switch_flags(switch, {DPI_FLAG: "True"})

    def _add_flag(self, name: str, value: str) -> str | None:
        problem = validate_flag_name(name)
        if problem:
            return problem
        try:
            normalized = normalize_flag_value(name, value)
        except FastFlagError as error:
            return str(error)
        self._set_flags({**self.draft.fast_flags, name: normalized})
        self.flag_editor.add_button.set_label("Add")
        return None

    def _remove_flag(self, name: str) -> None:
        flags = dict(self.draft.fast_flags)
        flags.pop(name, None)
        self._set_flags(flags)

    def _edit_flag(self, name: str) -> None:
        self.flag_editor.name_entry.set_text(name)
        self.flag_editor.value_entry.set_text(self.draft.fast_flags.get(name, ""))
        self.flag_editor.add_button.set_label("Set")
        self.flag_editor.value_entry.grab_focus()

    def _add_variable(self, name: str, value: str, *, guest: bool) -> str | None:
        problem = validate_environment_name(name, guest=guest) or validate_environment_value(name, value, guest=guest)
        if problem:
            return problem
        key = "guest_environment" if guest else "rosemary_environment"
        self._set_draft(replace(self.draft, **{key: {**getattr(self.draft, key), name: value}}))
        return None

    def _remove_variable(self, name: str, *, guest: bool) -> None:
        key = "guest_environment" if guest else "rosemary_environment"
        entries = dict(getattr(self.draft, key))
        entries.pop(name, None)
        self._set_draft(replace(self.draft, **{key: entries}))

    def _add_argument(self, argument: str) -> str | None:
        problem = validate_argument(argument)
        if problem:
            return problem
        self._set_draft(replace(self.draft, arguments=(*self.draft.arguments, argument)))
        return None

    def _remove_argument(self, index: str) -> None:
        arguments = list(self.draft.arguments)
        del arguments[int(index)]
        self._set_draft(replace(self.draft, arguments=tuple(arguments)))

    def _flag_note(self, text: str) -> None:
        self.flag_message.set_text(text)
        self.flag_message.set_visible(bool(text))

    def _merge_flags(self, flags: dict[str, str], source: str) -> None:
        self._set_flags({**self.draft.fast_flags, **flags})
        self._flag_note(f"Added {len(flags)} flag{'s' if len(flags) != 1 else ''} from {source}. Review them before applying.")

    def _import_clicked(self, _button) -> None:
        dialog = Gtk.FileDialog(title="Import FastFlags", modal=True)
        dialog.open(self.window, None, self._import_chosen)

    def _import_chosen(self, dialog, result) -> None:
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return
        try:
            flags = parse_flag_document(Path(file.get_path()).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, FastFlagError) as error:
            self._flag_note(f"Could not import {file.get_basename()}: {error}")
            return
        self._merge_flags(flags, file.get_basename())

    def _export_clicked(self, _button) -> None:
        dialog = Gtk.FileDialog(title="Export FastFlags", modal=True, initial_name="ClientAppSettings.json")
        dialog.save(self.window, None, self._export_chosen)

    def _export_chosen(self, dialog, result) -> None:
        try:
            file = dialog.save_finish(result)
        except GLib.Error:
            return
        try:
            Path(file.get_path()).write_text(format_flag_document(self.draft.fast_flags), encoding="utf-8")
        except OSError as error:
            self._flag_note(f"Could not export the flags: {error}")
            return
        self._flag_note(f"Exported {len(self.draft.fast_flags)} flags to {file.get_basename()}.")

    def _adopt_clicked(self, _button) -> None:
        bundle = self._bundle_path()
        if bundle is None:
            self._flag_note(f"There is no preferred {APP_TITLES[self.app_type]} installation to read flags from.")
            return
        try:
            flags = read_bundle_flags(bundle)
        except FastFlagError as error:
            self._flag_note(str(error))
            return
        if not flags:
            self._flag_note("The preferred installation has no FastFlags of its own.")
            return
        self._merge_flags(flags, "the preferred installation")

    def _discard_clicked(self, _button) -> None:
        self._set_draft(self.saved[self.app_type])
        self._flag_note("")

    def _defaults_clicked(self, _button) -> None:
        self._set_draft(AppLaunchConfig())
        self._flag_note("")

    def _review_clicked(self, _button) -> None:
        changes = self.changes
        if not changes:
            return
        title = APP_TITLES[self.app_type]
        dialog = Adw.MessageDialog.new(
            self.window,
            f"Apply {len(changes)} change{'s' if len(changes) != 1 else ''} to {title}?",
            "\n".join(f"• {line}" for line in changes)
            + f"\n\nThey take effect the next time {title} starts. "
            "The current configuration is kept as a backup.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("apply", "Apply")
        dialog.set_response_appearance("apply", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("apply")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._review_response, self.app_type, self.draft, changes)
        dialog.present()

    def _review_response(self, _dialog, response: str, app_type: str, config: AppLaunchConfig, changes) -> None:
        if response == "apply":
            self.apply(app_type, config, changes)

    def apply(self, app_type: str, config: AppLaunchConfig, changes: list[str]) -> None:
        reason = f"{APP_TITLES[app_type]}: {changes[0]}" if changes else APP_TITLES[app_type]
        if len(changes) > 1:
            reason += f" and {len(changes) - 1} more"
        self.saving = True
        self._show_status()
        threading.Thread(target=self._save_worker, args=(app_type, config, reason), daemon=True).start()

    def _save_worker(self, app_type: str, config: AppLaunchConfig, reason: str) -> None:
        try:
            saved = self.store.save(app_type, config, reason)
            error = None
        except LaunchConfigError as failure:
            saved, error = None, str(failure)
        GLib.idle_add(self._saved, app_type, saved, error)

    def _saved(self, app_type: str, saved: AppLaunchConfig | None, error: str | None) -> bool:
        self.saving = False
        if error is not None:
            self._show_error("Could not apply the configuration", error)
        else:
            self.saved[app_type] = saved
            self.drafts[app_type] = saved
        self._show_draft()
        self.reload()
        return GLib.SOURCE_REMOVE

    def reload(self, *, detect: bool = False) -> None:
        threading.Thread(target=self._load_worker, args=(detect or not self.loaded,), daemon=True).start()

    def _load_worker(self, detect: bool) -> None:
        configs, error = {}, None
        try:
            for app_type in APP_TYPES:
                configs[app_type] = self.store.load(app_type)
        except LaunchConfigError as failure:
            error = str(failure)
        hardware = (list_gpus(), gamemode.available(), mangohud.layer_installed()) if detect else None
        GLib.idle_add(self._loaded, configs, error, self.store.backups(), hardware)

    def _loaded(self, configs: dict[str, AppLaunchConfig], error: str | None, backups, hardware=None) -> bool:
        if hardware is not None:
            self.gpus, self.gamemode_available, self.mangohud_available = hardware
        for app_type, config in configs.items():
            unchanged = self.drafts[app_type] == self.saved[app_type]
            self.saved[app_type] = config
            if unchanged or not self.loaded:
                self.drafts[app_type] = config
        self.load_error = error
        self.loaded = True
        self.backups = backups
        self._show_backups()
        self._show_draft()
        return GLib.SOURCE_REMOVE

    def _show_backups(self) -> None:
        for row in self._backup_rows:
            self.backups_group.remove(row)
        self._backup_rows = []
        if not self.backups:
            row = Adw.ActionRow.new()
            row.set_title("No backups yet")
            row.set_subtitle("Applying a change keeps the configuration it replaces here.")
            self.backups_group.add(row)
            self._backup_rows.append(row)
        for backup in self.backups[:BACKUPS_SHOWN]:
            row = Adw.ActionRow.new()
            row.set_title(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(backup.created_at)))
            row.set_subtitle(GLib.markup_escape_text(f"Before: {backup.reason}" if backup.reason else ""))
            restore = Gtk.Button(label="Restore…", valign=Gtk.Align.CENTER)
            restore.connect("clicked", self._restore_clicked, backup)
            row.add_suffix(restore)
            self.backups_group.add(row)
            self._backup_rows.append(row)

    def _restore_clicked(self, _button, backup) -> None:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(backup.created_at))
        dialog = Adw.MessageDialog.new(
            self.window,
            "Restore this backup?",
            f"Player and Studio go back to their configuration from {when}, before “{backup.reason}”. "
            "The current configuration is kept as a backup, and unapplied changes are discarded.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("restore", "Restore")
        dialog.set_response_appearance("restore", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_close_response("cancel")
        dialog.connect("response", self._restore_response, backup)
        dialog.present()

    def _restore_response(self, _dialog, response: str, backup) -> None:
        if response == "restore":
            self.restore(backup)

    def restore(self, backup) -> None:
        self.saving = True
        self._show_status()
        threading.Thread(target=self._restore_worker, args=(backup,), daemon=True).start()

    def _restore_worker(self, backup) -> None:
        try:
            self.store.restore(backup.id)
            error = None
        except LaunchConfigError as failure:
            error = str(failure)
        GLib.idle_add(self._restored, error)

    def _restored(self, error: str | None) -> bool:
        self.saving = False
        if error is not None:
            self._show_error("Could not restore the backup", error)
        else:
            self.drafts = dict(self.saved)
            self.loaded = False
        self._show_status()
        self.reload()
        return GLib.SOURCE_REMOVE

    def refresh_allowlist(self, *, force: bool) -> None:
        self.allowlist_refresh.set_sensitive(False)
        self.allowlist_row.set_subtitle("Checking Roblox's allowlist…")
        fetcher = self._allowlist_fetcher or fetch_allowlist
        threading.Thread(target=self._allowlist_worker, args=(fetcher, force), daemon=True).start()

    def _allowlist_worker(self, fetcher, force: bool) -> None:
        GLib.idle_add(self._allowlist_loaded, fetcher(force=force))

    def _allowlist_loaded(self, allowlist: Allowlist) -> bool:
        self.allowlist = allowlist
        self.allowlist_refresh.set_sensitive(True)
        self._show_flags()
        return GLib.SOURCE_REMOVE

    def apply_library_snapshot(self, *_args, **_kwargs) -> None:
        self._show_arguments()

    def _show_error(self, title: str, message: str) -> None:
        dialog = Adw.MessageDialog.new(self.window, title, message)
        dialog.add_response("ok", "OK")
        dialog.present()
