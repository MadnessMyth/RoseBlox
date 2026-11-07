import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gtk

from roseblox.backend.rosemary_install import RosemaryInstallError
from roseblox.pages.manager_page import OperationState

DISPLAY_AVAILABLE = Gtk.init_check()


def _write_library(data_home: Path) -> list[dict]:
    records = []
    for app_type, name in (("player", "RobloxPlayer.app"), ("studio", "RobloxStudio.app")):
        bundle = data_home / "roseblox" / "installations" / f"{app_type}-version-0123456789abcdef" / name
        (bundle / "Contents").mkdir(parents=True)
        records.append(
            {
                "id": str(uuid.uuid4()),
                "display_name": f"Roblox {app_type.title()}",
                "app_type": app_type,
                "bundle_path": str(bundle),
                "registered_at": "2026-11-07T00:00:00+00:00",
                "validation_state": "Valid",
                "version": "version-0123456789abcdef",
                "managed": True,
                "source": "download",
                "archive_path": None,
            }
        )
    (data_home / "roseblox" / "library.json").write_text(
        json.dumps({"version": 1, "installations": records}), encoding="utf-8"
    )
    return records


@unittest.skipUnless(DISPLAY_AVAILABLE, "GTK needs a display")
class RealWindowTests(unittest.TestCase):
    """Builds the actual window so wiring between pages is exercised end to end."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.data_home = root / "data"
        self.config_home = root / "config"
        self.data_home.mkdir()
        self.config_home.mkdir()
        environment = {
            "HOME": str(root),
            "XDG_DATA_HOME": str(self.data_home),
            "XDG_CONFIG_HOME": str(self.config_home),
            "PATH": str(root / "bin"),
        }
        self.environment = patch.dict(os.environ, environment)
        self.environment.start()
        Adw.init()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def _build_window(self):
        from roseblox.window import RosebloxWindow

        window = RosebloxWindow()
        self.addCleanup(window.destroy)
        return window

    @staticmethod
    def _wait_until(predicate, timeout=10.0):
        context = GLib.MainContext.default()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_startup_snapshot_reaches_every_page(self):
        records = _write_library(self.data_home)
        window = self._build_window()
        from roseblox.pages.manager_page import SettingsPage

        self.assertIsInstance(window.settings_page, SettingsPage)
        self.assertTrue(self._wait_until(lambda: not window._refresh_in_progress and not window.library_loading))
        self.assertEqual({record["id"] for record in records}, {record.id for record in window.library_records})
        self.assertEqual("setup", window._stack.get_visible_child_name())
        player_page = window.pages["player"]
        self.assertFalse(player_page.notice.get_visible())
        self.assertTrue(player_page.download_button.get_sensitive())

    def test_completed_onboarding_opens_home(self):
        (self.config_home / "roseblox").mkdir()
        (self.config_home / "roseblox" / "setup.json").write_text(
            json.dumps({"version": 1, "onboarding_completed": True, "url_handlers_enabled": False}),
            encoding="utf-8",
        )
        window = self._build_window()
        self.assertTrue(self._wait_until(lambda: not window._refresh_in_progress and not window.library_loading))
        self.assertEqual("home", window._stack.get_visible_child_name())

    def _loaded_window(self):
        window = self._build_window()
        self.assertTrue(self._wait_until(lambda: not window._refresh_in_progress and not window.library_loading))
        return window

    def test_setup_hides_navigation_until_another_page_is_opened(self):
        window = self._loaded_window()
        self.assertFalse(window._sidebar.get_visible())
        self.assertEqual("First-time setup", window._window_title.get_subtitle())
        window.navigate_to("library")
        self.assertTrue(window._sidebar.get_visible())
        window.navigate_to("setup")
        self.assertFalse(window._sidebar.get_visible())

    def test_rosemary_install_waits_in_terminal_then_detects_the_new_executable(self):
        window = self._loaded_window()
        page = window.onboarding_page
        self.assertEqual("Required", page.rosemary_badge.get_text())
        self.assertTrue(page.install_rosemary_button.get_visible())

        terminal = Mock()
        terminal.poll.return_value = None
        plan = Mock(display_command="paru -S --needed rosemary")
        with patch("roseblox.window.plan_rosemary_install", return_value=plan), patch(
            "roseblox.window.start_rosemary_install", return_value=terminal
        ) as start, patch("roseblox.window.GLib.timeout_add_seconds") as timer:
            window.install_rosemary()
            window.install_rosemary()
        start.assert_called_once_with(plan)
        timer.assert_called_once()
        self.assertEqual("Installing", page.rosemary_badge.get_text())
        self.assertTrue(page.stop_waiting_button.get_visible())
        self.assertFalse(page.install_rosemary_button.get_visible())
        self.assertIn("paru -S --needed rosemary", page.install_rosemary_row.get_subtitle())
        self.assertTrue(window.settings_page.stop_waiting_button.get_visible())

        rosemary = Path(os.environ["PATH"]) / "rosemary"
        rosemary.parent.mkdir(exist_ok=True)
        rosemary.write_text("#!/bin/sh\n", encoding="utf-8")
        rosemary.chmod(0o755)
        self.assertTrue(window._poll_rosemary_install())
        self.assertTrue(self._wait_until(lambda: not window.rosemary_install.waiting))
        self.assertTrue(window.rosemary_status.available)
        self.assertEqual("Ready", page.rosemary_badge.get_text())
        self.assertEqual(str(rosemary), page.rosemary_row.get_subtitle())
        self.assertFalse(page.stop_waiting_button.get_visible())
        self.assertFalse(window._poll_rosemary_install())

    def test_failed_or_unavailable_rosemary_install_is_explained(self):
        window = self._loaded_window()
        page = window.onboarding_page
        terminal = Mock()
        terminal.poll.return_value = 1
        with patch("roseblox.window.plan_rosemary_install", return_value=Mock(display_command="yay")), patch(
            "roseblox.window.start_rosemary_install", return_value=terminal
        ), patch("roseblox.window.GLib.timeout_add_seconds"):
            window.install_rosemary()
        self.assertFalse(window._poll_rosemary_install())
        self.assertFalse(window.rosemary_install.waiting)
        self.assertIn("did not finish", page.install_rosemary_row.get_subtitle())
        self.assertEqual("Try Again", page.install_rosemary_button.get_label())
        self.assertTrue(page.install_rosemary_row.has_css_class("error-row"))

        with patch(
            "roseblox.window.plan_rosemary_install",
            side_effect=RosemaryInstallError("No AUR helper was found."),
        ):
            window.install_rosemary()
        self.assertEqual("No AUR helper was found.", page.install_rosemary_row.get_subtitle())
        self.assertIn("No AUR helper", window.settings_page.install_rosemary_row.get_subtitle())

    def test_app_installs_report_progress_on_the_setup_page(self):
        window = self._loaded_window()
        page = window.onboarding_page
        row, progress, cancel, action = page._app_widgets["player"]
        self.assertEqual("Install", action.get_label())
        controller = window.pages["player"]
        with patch.object(controller, "start_install_from_setup") as start:
            action.emit("clicked")
        start.assert_called_once_with()
        self.assertEqual("setup", window._stack.get_visible_child_name())

        controller._operation_listeners[0]("player", OperationState(True, "Downloaded 1.0 MiB of 4.0 MiB", 0.25, True))
        self.assertTrue(progress.get_visible())
        self.assertAlmostEqual(0.25, progress.get_fraction())
        self.assertTrue(cancel.get_visible())
        self.assertFalse(action.get_visible())
        self.assertEqual("Downloaded 1.0 MiB of 4.0 MiB", row.get_subtitle())
        self.assertEqual("Installing", page.apps_badge.get_text())
        with patch.object(controller, "cancel_operation") as cancel_operation:
            cancel.emit("clicked")
        cancel_operation.assert_called_once_with()

        controller._operation_listeners[0]("player", OperationState(False, "Installation failed: offline", None, False))
        self.assertFalse(progress.get_visible())
        self.assertTrue(action.get_visible())
        self.assertEqual("Installation failed: offline", row.get_subtitle())
        self.assertEqual("Required", page.apps_badge.get_text())

    def test_controller_emits_operation_state_for_download_lifecycle(self):
        window = self._loaded_window()
        controller = window.pages["studio"]
        states = []
        controller.add_operation_listener(lambda app_type, state: states.append((app_type, state)))
        controller._cancel_event = threading.Event()
        controller._begin_operation("Checking the latest version…", cancellable=True)
        controller._show_progress(SimpleNamespace(phase="downloading", downloaded=512, total=1024))
        controller._operation_finished(None, RuntimeError("offline"))
        self.assertEqual(["studio"] * 3, [app_type for app_type, _state in states])
        self.assertTrue(states[0][1].busy)
        self.assertAlmostEqual(0.5, states[1][1].fraction)
        self.assertTrue(states[1][1].cancellable)
        self.assertFalse(states[2][1].busy)
        self.assertEqual("Installation failed: offline", states[2][1].message)

    def test_link_switch_on_setup_page_enables_registration(self):
        window = self._loaded_window()
        page = window.onboarding_page
        page.url_handlers = Mock()
        page.url_switch.set_active(True)
        self.assertTrue(self._wait_until(lambda: page.url_handlers.enable.called and not page._url_busy))
        page.url_handlers.disable.assert_not_called()

    def test_link_received_during_startup_waits_for_the_library(self):
        records = _write_library(self.data_home)
        window = self._build_window()
        player_page = window.pages["player"]
        launched = []
        player_page.request_launch_url = lambda record, uri: launched.append((record.id, uri))
        window.handle_roblox_url("roblox://placeId=1")
        self.assertTrue(self._wait_until(lambda: bool(launched)))
        player_id = next(record["id"] for record in records if record["app_type"] == "player")
        self.assertEqual([(player_id, "roblox://placeId=1")], launched)


class EntryPointTests(unittest.TestCase):
    def test_links_are_accepted_bare_or_after_the_option(self):
        from roseblox.application import roblox_url_from_arguments

        uri = "roblox-player:1+launchmode:play+gameinfo:abc"
        self.assertIsNone(roblox_url_from_arguments([]))
        self.assertEqual(uri, roblox_url_from_arguments([uri]))
        self.assertEqual(uri, roblox_url_from_arguments(["--roblox-url", uri]))
        for arguments in (["--roblox-url"], ["--bogus"], [uri, uri], ["--roblox-url", "--x"]):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(ValueError, "^Usage"):
                roblox_url_from_arguments(arguments)
        with self.assertRaisesRegex(ValueError, "^Invalid Roblox link"):
            roblox_url_from_arguments(["https://example.com"])

    def test_command_line_arguments_reach_the_application(self):
        from roseblox import __main__ as entry_point

        argv = ["roseblox", "--roblox-url", "roblox://placeId=1"]
        with patch.object(entry_point.sys, "argv", argv), patch.object(
            entry_point.RosebloxApplication, "__init__", return_value=None
        ), patch.object(entry_point.RosebloxApplication, "run", return_value=0) as run:
            self.assertEqual(0, entry_point.main())
        run.assert_called_once_with(argv)


if __name__ == "__main__":
    unittest.main()
