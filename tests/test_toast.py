import os
import sys
import unittest
from unittest.mock import patch

from roseblox.toast_service import (
    UNSUPPORTED,
    display_reason,
    main,
    pass_through_supported,
    toast_arguments,
    unsupported_reason,
)

SUPPORTED = {"appimage": False, "wayland": True, "layer_shell": True, "installed": True, "pass_through": True}


class ReasonTests(unittest.TestCase):
    def test_each_missing_piece_has_its_own_reason(self):
        self.assertIsNone(unsupported_reason(**SUPPORTED))
        self.assertIn("AppImage", unsupported_reason(**{**SUPPORTED, "appimage": True}))
        self.assertEqual("Needs a Wayland session.", unsupported_reason(**{**SUPPORTED, "wayland": False}))
        self.assertEqual(
            "GNOME cannot show a window above a fullscreen game: it has no layer-shell (wlr-layer-shell).",
            unsupported_reason(**{**SUPPORTED, "layer_shell": False}, desktop="GNOME"),
        )
        hint = "Install it with “sudo pacman -S --needed gtk4-layer-shell”."
        with patch("roseblox.toast_service.install_hint", return_value=hint):
            self.assertEqual(
                f"Needs gtk4-layer-shell, which is not installed. {hint}",
                unsupported_reason(**{**SUPPORTED, "installed": False}),
            )
        # Installing gtk4-layer-shell would not help where the desktop has no layer-shell.
        self.assertIn("no layer-shell", unsupported_reason(**{**SUPPORTED, "layer_shell": False, "installed": False}))

    def test_without_the_cairo_bindings_it_would_take_the_clicks(self):
        hint = "Install it with “sudo pacman -S --needed python-cairo”."
        with patch("roseblox.toast_service.install_hint", return_value=hint) as install_hint:
            self.assertEqual(
                f"Needs the Python cairo bindings, so that clicks go through the toast. {hint}",
                unsupported_reason(**{**SUPPORTED, "pass_through": False}),
            )
        install_hint.assert_called_once_with("python-cairo", "Install python-cairo first.")
        self.assertIn("gtk4-layer-shell", unsupported_reason(**{**SUPPORTED, "installed": False, "pass_through": False}))
        with patch("gi.require_foreign", side_effect=ImportError("No module named 'cairo'")):
            self.assertFalse(pass_through_supported())

    def test_a_display_that_is_not_wayland_cannot_show_it(self):
        with patch("roseblox.toast_service.appimage_path", return_value=None):
            self.assertEqual("Needs a Wayland session.", display_reason(object()))


class MainTests(unittest.TestCase):
    def test_it_needs_one_text(self):
        with patch("sys.stderr"):
            self.assertEqual(2, main(["roseblox", "--toast"]))
            self.assertEqual(2, main(["roseblox", "--toast", " "]))

    def test_without_gtk4_layer_shell_or_in_the_appimage_it_exits_as_unsupported(self):
        with patch("roseblox.toast_service.layer_shell_library", return_value=None), patch("sys.stderr"):
            self.assertEqual(UNSUPPORTED, main(["roseblox", "--toast", "Server: Frankfurt"]))
        with patch("roseblox.toast_service.appimage_path", return_value="/home/u/Roseblox.AppImage"), patch(
            "sys.stderr"
        ):
            self.assertEqual(UNSUPPORTED, main(["roseblox", "--toast", "Server: Frankfurt"]))

    def test_it_loads_gtk4_layer_shell_before_gtk(self):
        with patch("roseblox.toast_service.layer_shell_library", return_value="libgtk4-layer-shell.so.0"), patch(
            "roseblox.toast_service.layer_shell_installed", return_value=True
        ), patch("roseblox.toast_service.ctypes.CDLL", side_effect=SystemExit(0)) as load, patch.dict(os.environ):
            with self.assertRaises(SystemExit):
                main(["roseblox", "--toast", "Server: Frankfurt"])
        load.assert_called_once_with("libgtk4-layer-shell.so.0")

    def test_the_watcher_starts_it_with_its_own_python(self):
        with patch("roseblox.toast_service.appimage_path", return_value=None):
            self.assertEqual([sys.executable, "-m", "roseblox", "--toast", "x"], toast_arguments("x"))


if __name__ == "__main__":
    unittest.main()
