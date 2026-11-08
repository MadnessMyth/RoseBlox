"""GTK application setup for Roseblox."""

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gtk
from gi.repository import Gio, GLib

from .backend.setup import Appearance, SetupSettingsError, SetupStore
from .command_line import parse_window_arguments, print_error
from .theme import ThemeManager
from .window import RosebloxWindow

APP_ID = "io.github.roseblox.Roseblox"


class RosebloxApplication(Adw.Application):
    """The Roseblox application object."""

    def __init__(self) -> None:
        # On X11 the window class comes from the program name; it must match the desktop
        # entry's name so docks and taskbars show Roseblox's icon and group its windows.
        GLib.set_prgname(APP_ID)
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self.connect("activate", self._on_activate)
        self.connect("command-line", self._on_command_line)

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        self.theme = None
        Gtk.Window.set_default_icon_name(APP_ID)
        display = Gdk.Display.get_default()
        if display is None:
            return
        try:
            appearance = SetupStore().load().appearance
        except SetupSettingsError:
            appearance = Appearance()
        self.theme = ThemeManager(display, appearance)

    def _on_activate(self, _application: Adw.Application) -> None:
        window = self.props.active_window
        if window is None:
            window = RosebloxWindow(application=self)
        window.present()

    def _on_command_line(self, _application, command_line) -> int:
        try:
            request = parse_window_arguments(command_line.get_arguments()[1:])
        except ValueError as error:
            print_error(command_line, f"{error}\n")
            return 2
        self.activate()
        window = self.props.active_window
        if request.uri is not None:
            GLib.idle_add(window.handle_roblox_url, request.uri)
        elif request.install_update is not None:
            GLib.idle_add(window.install_update, request.install_update)
        elif request.show is not None:
            GLib.idle_add(window.show_page, request.show)
        return 0
