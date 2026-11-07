import unittest

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gtk

from roseblox.backend.setup import ACCENT_CHOICES, Appearance
from roseblox.theme import ACCENT_NAMES, STYLESHEET, ThemeManager, accent_colors, build_css, palette

DISPLAY_AVAILABLE = Gtk.init_check()


class ThemeCssTests(unittest.TestCase):
    def test_every_accent_choice_has_a_name(self):
        self.assertEqual(len(ACCENT_CHOICES), len(ACCENT_NAMES))

    def test_roseblox_colors_define_the_full_palette(self):
        colors = palette(Appearance(), dark=True)
        self.assertEqual("#b93f7c", colors["accent_bg_color"])
        self.assertIn("window_bg_color", colors)
        self.assertIn("headerbar_bg_color", colors)

    def test_system_colors_leave_the_window_palette_alone(self):
        colors = palette(Appearance("system", "light", "teal"), dark=False)
        self.assertNotIn("window_bg_color", colors)
        self.assertEqual("#2190a4", colors["accent_bg_color"])

    def test_system_accent_is_left_to_the_desktop(self):
        self.assertIsNone(accent_colors("system", True))
        self.assertEqual({}, palette(Appearance("system", "system", "system"), dark=True))
        css = build_css(Appearance("roseblox", "dark", "system"), True, "", css_variables=True)
        self.assertNotIn("@define-color accent_bg_color", css)
        self.assertNotIn("--accent-bg-color", css)
        self.assertIn("@define-color window_bg_color", css)

    def test_light_accent_text_is_darker_than_dark_accent_text(self):
        light, _ = accent_colors("blue", dark=False)
        dark, _ = accent_colors("blue", dark=True)
        self.assertLess(sum(int(light[i:i + 2], 16) for i in (1, 3, 5)), sum(int(dark[i:i + 2], 16) for i in (1, 3, 5)))

    def test_css_variables_are_only_written_for_newer_libadwaita(self):
        appearance = Appearance("roseblox", "dark", "purple")
        self.assertIn("--accent-bg-color: #9141ac;", build_css(appearance, True, "", css_variables=True))
        self.assertNotIn(":root", build_css(appearance, True, "", css_variables=False))

    def test_every_combination_defines_the_colors_the_stylesheet_uses(self):
        base = STYLESHEET.read_text(encoding="utf-8")
        used = {word.split()[0].rstrip(";,)") for word in base.split("@")[1:] if word.startswith("rb_")}
        for colors in ("roseblox", "system"):
            for dark in (True, False):
                css = build_css(Appearance(colors, "dark", "roseblox"), dark, base, css_variables=False)
                defined = {line.split()[1] for line in css.splitlines() if line.startswith("@define-color")}
                self.assertLessEqual(used, defined, (colors, dark))


@unittest.skipUnless(DISPLAY_AVAILABLE, "GTK needs a display")
class ThemeManagerTests(unittest.TestCase):
    def setUp(self):
        Adw.init()
        self.manager = Adw.StyleManager.get_default()
        self.addCleanup(self.manager.set_color_scheme, Adw.ColorScheme.DEFAULT)

    def test_style_choice_sets_the_color_scheme(self):
        theme = ThemeManager(Gdk.Display.get_default(), Appearance(style="light"))
        self.addCleanup(Gtk.StyleContext.remove_provider_for_display, Gdk.Display.get_default(), theme.provider)
        self.assertEqual(Adw.ColorScheme.FORCE_LIGHT, self.manager.get_color_scheme())
        theme.apply(Appearance(style="dark"))
        self.assertEqual(Adw.ColorScheme.FORCE_DARK, self.manager.get_color_scheme())
        theme.apply(Appearance(style="system"))
        self.assertEqual(Adw.ColorScheme.DEFAULT, self.manager.get_color_scheme())


if __name__ == "__main__":
    unittest.main()
