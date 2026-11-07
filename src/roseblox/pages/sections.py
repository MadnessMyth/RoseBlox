"""Collapsible category used to organize the Settings page."""

from __future__ import annotations

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk


class CollapsibleSection(Gtk.Box):
    """A titled category whose content can be shown or hidden by clicking its header."""

    def __init__(self, section_id: str, title: str, description: str, *, expanded: bool = True, on_toggled=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.section_id = section_id
        self.on_toggled = on_toggled
        self.add_css_class("settings-section")

        self.header = Gtk.Button()
        self.header.add_css_class("flat")
        self.header.add_css_class("settings-section-header")
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        texts.set_hexpand(True)
        self.title_label = Gtk.Label(label=title, xalign=0)
        self.title_label.add_css_class("title-3")
        texts.append(self.title_label)
        self.description_label = Gtk.Label(label=description, xalign=0)
        self.description_label.set_wrap(True)
        self.description_label.add_css_class("dim-label")
        texts.append(self.description_label)
        header_box.append(texts)
        self.arrow = Gtk.Image()
        self.arrow.set_valign(Gtk.Align.CENTER)
        header_box.append(self.arrow)
        self.header.set_child(header_box)
        self.header.connect("clicked", self._header_clicked)
        self.append(self.header)

        self.revealer = Gtk.Revealer()
        self.revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.content.set_margin_top(10)
        self.revealer.set_child(self.content)
        self.append(self.revealer)
        self.set_expanded(expanded)

    @property
    def expanded(self) -> bool:
        return self.revealer.get_reveal_child()

    def add(self, widget: Gtk.Widget) -> None:
        self.content.append(widget)

    def set_expanded(self, expanded: bool) -> None:
        self.revealer.set_reveal_child(expanded)
        self.arrow.set_from_icon_name("pan-down-symbolic" if expanded else "pan-end-symbolic")
        self.header.set_tooltip_text(f"{'Hide' if expanded else 'Show'} {self.title_label.get_label()} settings")
        self.header.update_state([Gtk.AccessibleState.EXPANDED], [int(expanded)])

    def _header_clicked(self, _button) -> None:
        self.set_expanded(not self.expanded)
        if self.on_toggled is not None:
            self.on_toggled(self.section_id, self.expanded)
