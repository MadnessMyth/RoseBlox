"""Main application window and sidebar navigation."""

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gtk

from .pages import PAGE_DEFINITIONS, create_page


class RosebloxWindow(Adw.ApplicationWindow):
    """Main Roseblox window with a native stack sidebar."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.set_title("Roseblox")
        self.set_default_size(960, 640)
        self.set_size_request(680, 480)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(root)

        header = Adw.HeaderBar()
        header.add_css_class("main-header")
        header.set_title_widget(Adw.WindowTitle(title="Roseblox", subtitle="Roblox through Rosemary"))
        root.append(header)

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        content.add_css_class("window-content")
        root.append(content)

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_transition_duration(160)
        self._stack.set_hexpand(True)
        self._stack.set_vexpand(True)

        for page_id, title, icon_name, description in PAGE_DEFINITIONS:
            self._stack.add_titled(
                create_page(page_id, title, icon_name, description), page_id, title
            )

        sidebar = Gtk.StackSidebar()
        sidebar.set_stack(self._stack)
        sidebar.set_size_request(200, -1)
        sidebar.add_css_class("navigation-sidebar")
        content.append(sidebar)
        content.append(self._stack)
