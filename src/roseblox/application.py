"""GTK application setup for Roseblox."""

from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gtk
from gi.repository import Gio, GLib

from .command_line import parse_window_arguments, print_error
from .window import RosebloxWindow


class RosebloxApplication(Adw.Application):
    """The Roseblox application object."""

    def __init__(self) -> None:
        super().__init__(
            application_id="io.github.roseblox.Roseblox",
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self.connect("activate", self._on_activate)
        self.connect("command-line", self._on_command_line)

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        # The stylesheet defines a dark palette; a light system scheme would otherwise
        # draw libadwaita's white lists and entries on top of it.
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        display = Gdk.Display.get_default()
        if display is None:
            return

        stylesheet = Path(__file__).parent / "resources" / "style.css"
        provider = Gtk.CssProvider()
        provider.load_from_path(str(stylesheet))
        Gtk.StyleContext.add_provider_for_display(
            display,
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        self._style_provider = provider

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
