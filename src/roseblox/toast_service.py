"""A short banner above every window, fullscreen games included: ``roseblox --toast TEXT``.

The banner is a layer-shell surface (gtk4-layer-shell) on the compositor's overlay layer, which KDE Plasma and
wlroots compositors such as Hyprland and sway draw above fullscreen windows; GNOME has no layer-shell. It takes
neither the keyboard nor clicks, so both stay with the game. It fades in, stays for ``TOAST_SECONDS``, fades out
and exits. The activity watcher starts it for the server location instead of a desktop notification, and sends
the notification after all when it exits with ``UNSUPPORTED``.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
from datetime import datetime

from .backend.appimage import appimage_path
from .backend.system import install_hint

USAGE = "Usage: roseblox --toast TEXT"
TOAST_SECONDS = 8.0
# The exit status when the toast cannot be shown on this desktop; the reason is on stderr.
UNSUPPORTED = 3
LAYER_SHELL_GLOBAL = "zwlr_layer_shell_v1"
FADE_MS = 250
TOP_MARGIN = 48
ICON = "io.github.roseblox.Roseblox"
_CSS = """
window.roseblox-toast { background: none; }
.roseblox-toast-box { padding: 12px 24px; border-radius: 9999px; }
"""


def _log(message: str) -> None:
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    print(f"{stamp} roseblox toast: {message}", file=sys.stderr, flush=True)


def unsupported_reason(
    *, appimage: bool, wayland: bool, layer_shell: bool, installed: bool, pass_through: bool = True, desktop: str = ""
) -> str | None:
    """Why the toast cannot be shown here, or None when it can."""
    if appimage:
        return "Not available in the AppImage: gtk4-layer-shell has to load into the system's GTK."
    if not wayland:
        return "Needs a Wayland session."
    if not layer_shell:
        return (
            f"{desktop or 'This desktop'} cannot show a window above a fullscreen game: "
            "it has no layer-shell (wlr-layer-shell)."
        )
    if not installed:
        return "Needs gtk4-layer-shell, which is not installed. " + install_hint(
            "gtk4-layer-shell", "Install gtk4-layer-shell first."
        )
    if not pass_through:
        return "Needs the Python cairo bindings, so that clicks go through the toast. " + install_hint(
            "python-cairo", "Install python-cairo first."
        )
    return None


def layer_shell_library() -> str | None:
    return ctypes.util.find_library("gtk4-layer-shell")


def layer_shell_installed() -> bool:
    import gi

    try:
        gi.require_version("Gtk4LayerShell", "1.0")
    except ValueError:
        return False
    return layer_shell_library() is not None


def pass_through_supported() -> bool:
    """Whether PyGObject can hand GTK the cairo region that lets clicks through the toast."""
    import gi

    try:
        gi.require_foreign("cairo")
        import cairo  # noqa: F401
    except ImportError:
        return False
    return True


def _has_global(display, name: str) -> bool:
    import gi
    from gi.repository import GdkWayland

    query = getattr(GdkWayland.WaylandDisplay, "query_registry", None)
    try:
        if query is None:
            # A display PyGObject wrapped before GdkWayland was imported keeps a class without GdkWayland's methods.
            info = gi.Repository.get_default().find_by_name("GdkWayland", "WaylandDisplay")
            query = info.find_method("query_registry")
        return bool(query(display, name))
    except (AttributeError, TypeError) as error:
        # Unknown is not a reason to grey the switch out: a toast that cannot be shown says so and falls back.
        _log(f"cannot ask the compositor for {name}: {error}")
        return True


def display_reason(display) -> str | None:
    """Why the toast cannot be shown on ``display``, a ``Gdk.Display``, or None when it can."""
    if appimage_path():
        return unsupported_reason(appimage=True, wayland=True, layer_shell=True, installed=True)
    import gi

    try:
        gi.require_version("GdkWayland", "4.0")
        from gi.repository import GdkWayland
    except (ImportError, ValueError):
        wayland = layer_shell = False
    else:
        wayland = isinstance(display, GdkWayland.WaylandDisplay)
        layer_shell = wayland and _has_global(display, LAYER_SHELL_GLOBAL)
    return unsupported_reason(
        appimage=False,
        wayland=wayland,
        layer_shell=layer_shell,
        installed=layer_shell_installed(),
        pass_through=pass_through_supported(),
        desktop=os.environ.get("XDG_CURRENT_DESKTOP", "").split(":")[-1],
    )


def toast_arguments(text: str) -> list[str]:
    executable = appimage_path()
    command = [executable] if executable else [sys.executable, "-m", "roseblox"]
    return [*command, "--toast", text]


def _pass_input_through(window) -> None:
    """An empty input region gives every click on the toast to the window under it."""
    import gi

    try:
        gi.require_foreign("cairo")
        import cairo
    except ImportError:
        _log("python-cairo is not installed, so the toast takes the clicks on it")
        return
    window.get_surface().set_input_region(cairo.Region())


class Toast:
    def __init__(self, text: str) -> None:
        from gi.repository import Gdk, GLib, Gtk, Gtk4LayerShell

        self.loop = GLib.MainLoop()
        display = Gdk.Display.get_default()
        provider = Gtk.CssProvider()
        if hasattr(provider, "load_from_string"):
            provider.load_from_string(_CSS)
        else:
            provider.load_from_data(_CSS, -1)
        Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.window = Gtk.Window(title="Roseblox", decorated=False, resizable=False)
        self.window.add_css_class("roseblox-toast")
        Gtk4LayerShell.init_for_window(self.window)
        Gtk4LayerShell.set_namespace(self.window, "roseblox-toast")
        Gtk4LayerShell.set_layer(self.window, Gtk4LayerShell.Layer.OVERLAY)
        Gtk4LayerShell.set_anchor(self.window, Gtk4LayerShell.Edge.TOP, True)
        Gtk4LayerShell.set_margin(self.window, Gtk4LayerShell.Edge.TOP, TOP_MARGIN)
        Gtk4LayerShell.set_keyboard_mode(self.window, Gtk4LayerShell.KeyboardMode.NONE)
        box = Gtk.Box(spacing=10)
        box.add_css_class("osd")
        box.add_css_class("roseblox-toast-box")
        if Gtk.IconTheme.get_for_display(display).has_icon(ICON):
            icon = Gtk.Image.new_from_icon_name(ICON)
            icon.set_pixel_size(24)
            box.append(icon)
        label = Gtk.Label(label=text)
        label.add_css_class("title-4")
        box.append(label)
        self.revealer = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.CROSSFADE, transition_duration=FADE_MS, child=box
        )
        self.revealer.connect("notify::child-revealed", self._revealed)
        self.window.set_child(self.revealer)
        self.window.connect("realize", _pass_input_through)
        # A revealer only fades once it is on screen.
        self.window.connect("map", lambda _window: GLib.idle_add(self._fade, True))

    def run(self) -> int:
        from gi.repository import GLib

        self.window.present()
        GLib.timeout_add(int(TOAST_SECONDS * 1000), self._fade, False)
        # A compositor that stops drawing the toast also stops its fade, which would then never end.
        GLib.timeout_add(int(TOAST_SECONDS * 1000) + FADE_MS + 2000, self._quit)
        self.loop.run()
        return 0

    def _fade(self, visible: bool) -> bool:
        self.revealer.set_reveal_child(visible)
        return False

    def _revealed(self, revealer, _pspec) -> None:
        if not revealer.get_child_revealed() and not revealer.get_reveal_child():
            self._quit()

    def _quit(self) -> bool:
        self.window.destroy()
        self.loop.quit()
        return False


def main(argv: list[str]) -> int:
    if len(argv) != 3 or not argv[2].strip():
        print(USAGE, file=sys.stderr)
        return 2
    if appimage_path():
        _log(unsupported_reason(appimage=True, wayland=True, layer_shell=True, installed=True))
        return UNSUPPORTED
    library = layer_shell_library()
    if library is None or not layer_shell_installed():
        _log("gtk4-layer-shell is not installed")
        return UNSUPPORTED
    # gtk4-layer-shell works only when it is loaded before libwayland-client, which GTK loads.
    ctypes.CDLL(library)
    os.environ["GDK_BACKEND"] = "wayland"
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("GdkWayland", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import GdkWayland  # noqa: F401 - before the display is wrapped, so it gets GdkWayland's class
    from gi.repository import Adw, Gdk, Gtk

    if not Gtk.init_check():
        _log("Needs a Wayland session.")
        return UNSUPPORTED
    reason = display_reason(Gdk.Display.get_default())
    if reason is not None:
        _log(reason)
        return UNSUPPORTED
    Adw.init()
    return Toast(argv[2]).run()
