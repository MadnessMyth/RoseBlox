"""Launcher dashboard backed by the shared Player and Studio controllers."""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from ..backend.library import InstallationRecord


def selected_installation_index(
    installations: list[InstallationRecord], previous_id: str | None
) -> int:
    """Keep the current Home selection when it still exists; otherwise choose the first."""
    return next(
        (index for index, record in enumerate(installations) if record.id == previous_id),
        0,
    )


def choose_home_installation_id(
    installations: list[InstallationRecord],
    validation_by_id: dict[str, str],
    *,
    current_id: str | None,
    resolved_preferred_id: str | None,
    preference_changed: bool,
) -> str | None:
    """Preserve a temporary valid choice unless the saved preference changed."""
    valid_ids = {
        record.id for record in installations if validation_by_id.get(record.id) == "Valid"
    }
    if current_id in valid_ids and not preference_changed:
        return current_id
    if resolved_preferred_id in valid_ids:
        return resolved_preferred_id
    return next((record.id for record in installations if record.id in valid_ids), None)


class HomePage(Gtk.Overlay):
    def __init__(self, window: Gtk.Window) -> None:
        super().__init__()
        self.window = window
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.add_css_class("home-dashboard")

        artwork_path = Path(__file__).resolve().parent.parent / "resources" / "home-artwork-placeholder.svg"
        artwork = Gtk.Picture()
        artwork.set_can_shrink(True)
        artwork.set_content_fit(Gtk.ContentFit.COVER)
        artwork.set_halign(Gtk.Align.FILL)
        artwork.set_valign(Gtk.Align.FILL)
        artwork.set_accessible_role(Gtk.AccessibleRole.PRESENTATION)
        artwork.add_css_class("home-background-art")
        if artwork_path.is_file():
            artwork.set_filename(str(artwork_path))
        self.set_child(artwork)

        scrim = Gtk.Box()
        scrim.set_can_target(False)
        scrim.add_css_class("home-artwork-scrim")
        self.add_overlay(scrim)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_hexpand(True)
        scroller.set_vexpand(True)
        scroller.add_css_class("home-content-scroller")
        self.add_overlay(scroller)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22)
        content.set_margin_top(30)
        content.set_margin_bottom(28)
        content.set_margin_start(28)
        content.set_margin_end(28)
        content.add_css_class("home-content")
        scroller.set_child(content)

        introduction = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        title = Gtk.Label(label="Welcome to Roseblox")
        title.set_xalign(0)
        title.add_css_class("title-1")
        introduction.append(title)
        subtitle = Gtk.Label(label="Launch Roblox Player and Studio through Rosemary.")
        subtitle.set_xalign(0)
        subtitle.add_css_class("dim-label")
        introduction.append(subtitle)
        content.append(introduction)

        self.cards: dict[str, _LaunchCard] = {}
        cards = Gtk.FlowBox()
        cards.set_selection_mode(Gtk.SelectionMode.NONE)
        cards.set_min_children_per_line(1)
        cards.set_max_children_per_line(2)
        cards.set_homogeneous(True)
        cards.set_row_spacing(16)
        cards.set_column_spacing(16)
        cards.set_hexpand(True)
        content.append(cards)
        for app_type, title_text, icon_name in (
            ("player", "Roblox Player", "applications-games-symbolic"),
            ("studio", "Roblox Studio", "applications-graphics-symbolic"),
        ):
            card = _LaunchCard(self, app_type, title_text, icon_name)
            self.cards[app_type] = card
            cards.append(card)

        navigation = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        library_button = Gtk.Button.new_with_label("Open Library")
        library_button.connect("clicked", self._navigate, "library")
        navigation.append(library_button)
        settings_button = Gtk.Button.new_with_label("Settings")
        settings_button.connect("clicked", self._navigate, "settings")
        navigation.append(settings_button)
        content.append(navigation)

    def apply_snapshot(
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
        preferred_ids = preferred_ids or {"player": None, "studio": None}
        resolved_preferred_ids = resolved_preferred_ids or {"player": None, "studio": None}
        for app_type, card in self.cards.items():
            installations = [record for record in records if record.app_type == app_type]
            card.set_installations(
                installations,
                validation_by_id,
                error,
                loading,
                preferred_ids.get(app_type),
                resolved_preferred_ids.get(app_type),
                (preference_errors or {}).get(app_type),
            )

    def _navigate(self, _button: Gtk.Button, page_id: str) -> None:
        self.window.navigate_to(page_id)


class _LaunchCard(Gtk.Box):
    def __init__(self, home: HomePage, app_type: str, title: str, icon_name: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.home = home
        self.app_type = app_type
        self.title = title
        self.installations: list[InstallationRecord] = []
        self.validation_by_id: dict[str, str] = {}
        self.library_error: str | None = None
        self.library_loading = True
        self.selected_id: str | None = None
        self._last_preferred_id: str | None = None
        self._last_resolved_preferred_id: str | None = None
        self._has_snapshot = False
        self._preference_error: str | None = None
        self._updating_model = False
        self.set_hexpand(True)
        self.set_valign(Gtk.Align.START)
        self.add_css_class("home-launch-card")

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(30)
        icon.add_css_class("page-icon")
        header.append(icon)
        heading = Gtk.Label(label=title)
        heading.set_xalign(0)
        heading.set_hexpand(True)
        heading.add_css_class("title-2")
        header.append(heading)
        self.append(header)

        self.status = Gtk.Label(label="Loading registered installations…")
        self.status.set_xalign(0)
        self.status.set_wrap(True)
        self.status.add_css_class("home-card-status")
        self.append(self.status)

        self.installation_label = Gtk.Label(label="Installation")
        self.installation_label.set_xalign(0)
        self.installation_label.add_css_class("dim-label")
        self.dropdown = Gtk.DropDown()
        self.dropdown.set_model(Gtk.StringList.new([]))
        self.dropdown.set_hexpand(True)
        self.dropdown.set_tooltip_text(f"Select a {title} installation")
        self.installation_label.set_mnemonic_widget(self.dropdown)
        self.selection_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        self.selection_row.append(self.installation_label)
        self.selection_row.append(self.dropdown)
        self.append(self.selection_row)
        self.dropdown.connect("notify::selected", self._selection_changed)

        self.details = Gtk.Label()
        self.details.set_xalign(0)
        self.details.set_wrap(True)
        self.details.set_selectable(True)
        self.details.add_css_class("dim-label")
        self.append(self.details)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.launch_button = Gtk.Button.new_with_label("Launch")
        self.launch_button.add_css_class("suggested-action")
        self.launch_button.connect("clicked", self._launch_clicked)
        actions.append(self.launch_button)
        self.manage_button = Gtk.Button.new_with_label("Manage")
        self.manage_button.connect("clicked", self._manage_clicked)
        actions.append(self.manage_button)
        self.append(actions)

    def set_installations(
        self,
        installations: list[InstallationRecord],
        validation_by_id: dict[str, str],
        error: str | None,
        loading: bool,
        preferred_id: str | None,
        resolved_preferred_id: str | None,
        preference_error: str | None,
    ) -> None:
        previous_id = self._selected_record_id()
        preference_changed = (
            not self._has_snapshot
            or preferred_id != self._last_preferred_id
            or resolved_preferred_id != self._last_resolved_preferred_id
        )
        self.installations = installations
        self.validation_by_id = validation_by_id
        self.library_error = error
        self.library_loading = loading
        self._last_preferred_id = preferred_id
        self._last_resolved_preferred_id = resolved_preferred_id
        labels = [
            f"{record.display_name} · {record.version or 'Unknown version'}"
            for record in installations
        ]
        self._updating_model = True
        self.dropdown.set_model(Gtk.StringList.new(labels))
        selected_id = choose_home_installation_id(
            installations,
            validation_by_id,
            current_id=previous_id,
            resolved_preferred_id=resolved_preferred_id,
            preference_changed=preference_changed,
        )
        selected_index = next(
            (index for index, record in enumerate(installations) if record.id == selected_id),
            Gtk.INVALID_LIST_POSITION,
        )
        if installations and selected_index != Gtk.INVALID_LIST_POSITION:
            self.dropdown.set_selected(selected_index)
            self.selected_id = installations[selected_index].id
        else:
            self.dropdown.set_selected(Gtk.INVALID_LIST_POSITION)
            self.selected_id = None
        self._updating_model = False
        self._has_snapshot = not loading
        self.selection_row.set_visible(len(installations) > 1)
        self._preference_error = preference_error
        self._update_display()

    def _selected_record_id(self) -> str | None:
        selected = self.dropdown.get_selected()
        if selected < len(self.installations):
            return self.installations[selected].id
        return self.selected_id

    def _selection_changed(self, _dropdown: Gtk.DropDown, _parameter) -> None:
        if self._updating_model:
            return
        selected = self.dropdown.get_selected()
        self.selected_id = self.installations[selected].id if selected < len(self.installations) else None
        self._update_display()

    def _selected_record(self) -> InstallationRecord | None:
        return next((item for item in self.installations if item.id == self.selected_id), None)

    def _update_display(self) -> None:
        record = self._selected_record()
        if self.library_loading:
            self.status.set_text("Loading registered installations…")
            self.details.set_text("")
            self.launch_button.set_label("Launch")
            self.launch_button.set_sensitive(False)
            self.manage_button.set_sensitive(False)
            return
        self.manage_button.set_sensitive(True)
        if self.library_error:
            self.status.set_text("The installation library could not be read.")
            self.details.set_text(self.library_error)
            self.launch_button.set_label("Launch")
            self.launch_button.set_sensitive(False)
            return
        if record is None:
            if self.installations:
                self.status.set_text(f"No valid {self.title} installations are available.")
                self.details.set_text("Choose or register a valid application bundle from the management page.")
            else:
                self.status.set_text(f"No {self.title} installations are registered.")
                self.details.set_text("Add an existing app or install the latest version from the management page.")
            if self._last_preferred_id and self._last_preferred_id != self._last_resolved_preferred_id:
                self.details.set_text(f"{self.details.get_text()} The saved preferred installation is unavailable.")
            if self._preference_error:
                self.details.set_text(f"{self.details.get_text()} Saved preference could not be read; using a safe fallback.")
            self.launch_button.set_label("Launch")
            self.launch_button.set_sensitive(False)
            return

        validation = self.validation_by_id.get(record.id, "Unknown")
        controller = self.home.window.pages.get(self.app_type)
        launch_label, lifecycle_sensitive = (
            controller.launch_button_state(record.id) if controller is not None else ("Launch", False)
        )
        running = launch_label == "Stop"
        if validation == "Valid":
            self.status.set_text("Running" if running else "Available")
        elif running:
            self.status.set_text("Running · bundle is missing or invalid")
        else:
            self.status.set_text(validation)
        version = record.version or "Unknown version"
        source = "Managed installation" if record.managed else "External application"
        preferred = " · Preferred installation" if record.id == self._last_preferred_id else ""
        preference_notice = (
            " · Saved preferred choice could not be read; using a valid fallback" if self._preference_error else ""
        )
        unavailable_notice = (
            " · Saved preferred installation unavailable; using a valid fallback"
            if self._last_preferred_id and self._last_preferred_id != self._last_resolved_preferred_id
            else ""
        )
        self.details.set_text(
            f"{version} · {source}{preferred}{unavailable_notice}{preference_notice} — {record.bundle_path}"
        )
        self.launch_button.set_label(launch_label)
        can_launch = validation == "Valid" or launch_label in {"Launching...", "Stopping…", "Stop"}
        self.launch_button.set_sensitive(lifecycle_sensitive and can_launch)

    def _launch_clicked(self, _button: Gtk.Button) -> None:
        record = self._selected_record()
        controller = self.home.window.pages.get(self.app_type)
        if record is not None and controller is not None:
            controller.request_launch_toggle(record)

    def _manage_clicked(self, _button: Gtk.Button) -> None:
        self.home.window.navigate_to(self.app_type)
