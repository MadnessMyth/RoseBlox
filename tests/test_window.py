import json
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gtk

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


if __name__ == "__main__":
    unittest.main()
