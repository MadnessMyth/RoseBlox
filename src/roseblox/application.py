"""GTK application setup for Roseblox."""

import sys
from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gtk
from gi.repository import Gio, GLib

from .window import RosebloxWindow
from .backend.url_handlers import parse_roblox_url


def _print_error(command_line, message: str) -> None:
    """Print on the invoking terminal; g_application_command_line_printerr is variadic."""
    printerr = getattr(command_line, "printerr_literal", None)
    if printerr is not None:
        printerr(message)
    else:
        sys.stderr.write(message)


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
        arguments = command_line.get_arguments()[1:]
        uri = None
        if arguments:
            if len(arguments) != 2 or arguments[0] != "--roblox-url":
                _print_error(command_line, "Usage: roseblox [--roblox-url URI]\n")
                return 2
            try:
                uri = parse_roblox_url(arguments[1]).uri
            except ValueError as error:
                _print_error(command_line, f"Invalid Roblox link: {error}\n")
                return 2
        self.activate()
        if uri is not None:
            window = self.props.active_window
            GLib.idle_add(window.handle_roblox_url, uri)
        return 0
