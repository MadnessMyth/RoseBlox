"""GTK application setup for Roseblox."""

from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gtk

from .window import RosebloxWindow


class RosebloxApplication(Adw.Application):
    """The Roseblox application object."""

    def __init__(self) -> None:
        super().__init__(application_id="io.github.roseblox.Roseblox")
        self.connect("activate", self._on_activate)

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
