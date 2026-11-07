"""Builds and applies the stylesheet for the chosen appearance."""

from __future__ import annotations

from pathlib import Path

import gi
gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gtk

from .backend.setup import Appearance

STYLESHEET = Path(__file__).parent / "resources" / "style.css"

# libadwaita's own accent palette, so presets match the GNOME and KDE choices.
ACCENT_COLORS = {
    "blue": "#3584e4",
    "teal": "#2190a4",
    "green": "#3a944a",
    "yellow": "#c88800",
    "orange": "#ed5b00",
    "red": "#e62d42",
    "pink": "#d56199",
    "purple": "#9141ac",
    "slate": "#6f8396",
}
ACCENT_NAMES = {
    "roseblox": "Roseblox pink",
    "system": "System accent color",
    **{name: name.title() for name in ACCENT_COLORS},
}
_ROSEBLOX_ACCENT = {True: ("#e968a6", "#b93f7c"), False: ("#a8306a", "#b93f7c")}

_ROSEBLOX_PALETTE = {
    True: {
        "window_bg_color": "#17151a",
        "window_fg_color": "#f4eff3",
        "view_bg_color": "#1d1a20",
        "view_fg_color": "#f4eff3",
        "headerbar_bg_color": "#201c23",
        "headerbar_fg_color": "#f4eff3",
        "sidebar_bg_color": "#201c23",
        "sidebar_fg_color": "#e8dfe6",
    },
    False: {
        "window_bg_color": "#f8f3f7",
        "window_fg_color": "#2a2029",
        "view_bg_color": "#fffbfe",
        "view_fg_color": "#2a2029",
        "headerbar_bg_color": "#f0e6ee",
        "headerbar_fg_color": "#2a2029",
        "sidebar_bg_color": "#f0e6ee",
        "sidebar_fg_color": "#3b2e39",
    },
}

# Colors used only by Roseblox's own stylesheet (Home artwork, cards, setup glow).
_DECORATION = {
    ("roseblox", True): {
        "rb_home_bg": "#19151d",
        "rb_home_glow": "alpha(@accent_bg_color, 0.42)",
        "rb_home_second_glow": "alpha(#62477a, 0.30)",
        "rb_scrim_top": "alpha(#17151a, 0.20)",
        "rb_scrim_bottom": "alpha(#17151a, 0.66)",
        "rb_card_bg": "alpha(#211c25, 0.94)",
        "rb_setup_glow": "alpha(@accent_bg_color, 0.30)",
    },
    ("roseblox", False): {
        "rb_home_bg": "#f6eef4",
        "rb_home_glow": "alpha(@accent_bg_color, 0.22)",
        "rb_home_second_glow": "alpha(#9b7bb8, 0.20)",
        "rb_scrim_top": "alpha(#f8f3f7, 0.45)",
        "rb_scrim_bottom": "alpha(#f8f3f7, 0.82)",
        "rb_card_bg": "alpha(#ffffff, 0.90)",
        "rb_setup_glow": "alpha(@accent_bg_color, 0.16)",
    },
    ("system", True): {
        "rb_home_bg": "@view_bg_color",
        "rb_home_glow": "alpha(@accent_bg_color, 0.30)",
        "rb_home_second_glow": "alpha(@accent_bg_color, 0.10)",
        "rb_scrim_top": "alpha(@window_bg_color, 0.25)",
        "rb_scrim_bottom": "alpha(@window_bg_color, 0.70)",
        "rb_card_bg": "alpha(@view_bg_color, 0.94)",
        "rb_setup_glow": "alpha(@accent_bg_color, 0.20)",
    },
    ("system", False): {
        "rb_home_bg": "@view_bg_color",
        "rb_home_glow": "alpha(@accent_bg_color, 0.18)",
        "rb_home_second_glow": "alpha(@accent_bg_color, 0.08)",
        "rb_scrim_top": "alpha(@window_bg_color, 0.45)",
        "rb_scrim_bottom": "alpha(@window_bg_color, 0.82)",
        "rb_card_bg": "alpha(@view_bg_color, 0.92)",
        "rb_setup_glow": "alpha(@accent_bg_color, 0.12)",
    },
}
_ARTWORK_OPACITY = {("roseblox", True): 0.62, ("roseblox", False): 0.30, ("system", True): 0.45, ("system", False): 0.25}
_COLOR_SCHEMES = {
    "dark": Adw.ColorScheme.FORCE_DARK,
    "light": Adw.ColorScheme.FORCE_LIGHT,
    "system": Adw.ColorScheme.DEFAULT,
}


def _mix(color: str, other: str, amount: float) -> str:
    first = [int(color[index:index + 2], 16) for index in (1, 3, 5)]
    second = [int(other[index:index + 2], 16) for index in (1, 3, 5)]
    mixed = [round(a + (b - a) * amount) for a, b in zip(first, second)]
    return "#" + "".join(f"{value:02x}" for value in mixed)


def accent_colors(accent: str, dark: bool) -> tuple[str, str] | None:
    """``(accent_color, accent_bg_color)`` for an accent choice, or None to use the desktop's."""
    if accent == "roseblox":
        return _ROSEBLOX_ACCENT[dark]
    if accent in ACCENT_COLORS:
        background = ACCENT_COLORS[accent]
        # The standalone accent is used for text and icons, so it needs contrast with the window.
        return (_mix(background, "#ffffff", 0.35) if dark else _mix(background, "#000000", 0.18)), background
    return None


def palette(appearance: Appearance, dark: bool) -> dict[str, str]:
    colors = dict(_ROSEBLOX_PALETTE[dark]) if appearance.colors == "roseblox" else {}
    accent = accent_colors(appearance.accent, dark)
    if accent is not None:
        colors["accent_color"], colors["accent_bg_color"] = accent
        colors["accent_fg_color"] = "#ffffff"
    return colors


def build_css(appearance: Appearance, dark: bool, base_css: str, *, css_variables: bool) -> str:
    """The full stylesheet: palette definitions, then Roseblox's rules.

    libadwaita 1.6 and later read CSS variables as well as the older named colors, so
    with ``css_variables`` the palette is set both ways.
    """
    colors = palette(appearance, dark)
    lines = [f"@define-color {name} {value};" for name, value in colors.items()]
    lines += [
        f"@define-color {name} {value};"
        for name, value in _DECORATION[(appearance.colors, dark)].items()
    ]
    if css_variables and colors:
        lines.append(":root {")
        lines += [f"  --{name.replace('_', '-')}: {value};" for name, value in colors.items()]
        lines.append("}")
    lines.append(
        f".home-background-art {{ opacity: {_ARTWORK_OPACITY[(appearance.colors, dark)]}; }}"
    )
    return "\n".join(lines) + "\n\n" + base_css


def system_accent_available() -> bool:
    manager = Adw.StyleManager.get_default()
    supports = getattr(manager, "get_system_supports_accent_colors", None)
    return bool(supports and supports())


class ThemeManager:
    """Owns the application stylesheet and keeps it in step with light and dark mode."""

    def __init__(self, display: Gdk.Display, appearance: Appearance | None = None) -> None:
        self.appearance = appearance or Appearance()
        self.style_manager = Adw.StyleManager.get_default()
        self.provider = Gtk.CssProvider()
        self._base_css = STYLESHEET.read_text(encoding="utf-8")
        self._css_variables = (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 6)
        Gtk.StyleContext.add_provider_for_display(
            display, self.provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self.style_manager.connect("notify::dark", lambda *_: self._reload())
        self.apply(self.appearance)

    def apply(self, appearance: Appearance) -> None:
        self.appearance = appearance.validate()
        self.style_manager.set_color_scheme(_COLOR_SCHEMES[appearance.style])
        self._reload()

    def _reload(self) -> None:
        css = build_css(
            self.appearance,
            self.style_manager.get_dark(),
            self._base_css,
            css_variables=self._css_variables,
        )
        if hasattr(self.provider, "load_from_string"):
            self.provider.load_from_string(css)
        else:
            self.provider.load_from_data(css, -1)
