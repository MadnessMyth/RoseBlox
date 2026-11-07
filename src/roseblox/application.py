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


USAGE = "Usage: roseblox [[--roblox-url] URI]"


def roblox_url_from_arguments(arguments: list[str]) -> str | None:
    """Return the Roblox link to open, or raise ValueError with a message for the user.

    The app's desktop entry passes links as a bare %u (as "Open with" dialogs do);
    the URL-handler entry passes them after --roblox-url.
    """
    explicit = arguments[:1] == ["--roblox-url"]
    if explicit:
        arguments = arguments[1:]
    if not arguments and not explicit:
        return None
    if len(arguments) != 1 or arguments[0].startswith("-"):
        raise ValueError(USAGE)
    try:
        return parse_roblox_url(arguments[0]).uri
    except ValueError as error:
        raise ValueError(f"Invalid Roblox link: {error}") from error


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
            uri = roblox_url_from_arguments(command_line.get_arguments()[1:])
        except ValueError as error:
            _print_error(command_line, f"{error}\n")
            return 2
        self.activate()
        if uri is not None:
            window = self.props.active_window
            GLib.idle_add(window.handle_roblox_url, uri)
        return 0
