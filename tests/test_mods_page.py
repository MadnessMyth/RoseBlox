import os
import struct
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib  # noqa: E402

from roseblox.backend.mods import ModsError, ModSettings, ModsStore  # noqa: E402
from roseblox.pages.mods_page import ModsPage  # noqa: E402


def png(width=32, height=32):
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"


class ModsPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Adw.init()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        patcher = mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root / "data")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = ModsStore(self.root / "mods.json", assets_directory=self.root / "assets")
        self.fetches = []

    def tearDown(self):
        self.directory.cleanup()

    def _page(self, fetcher=None):
        def fetch():
            self.fetches.append(True)

        return ModsPage(None, store=self.store, background_fetcher=fetcher or fetch)

    def _wait_until(self, condition, timeout=3.0):
        deadline = time.monotonic() + timeout
        context = GLib.MainContext.default()
        while time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            if condition():
                return True
            time.sleep(0.01)
        return condition()

    def test_switches_and_files_save_per_application(self):
        page = self._page()
        page.death_switch.set_active(True)
        self.assertTrue(self.store.load("player").classic_death_sound)
        cursor = self.root / "cursor.png"
        cursor.write_bytes(png(48, 48))
        page.set_cursor("player", cursor)
        self.assertIn("48 × 48", page.cursor_row.get_subtitle())
        self.assertTrue(page.cursor_remove.get_visible())
        page.cursor_remove.emit("clicked")
        self.assertIsNone(self.store.load("player").cursor)

        page.app_buttons["studio"].set_active(True)
        self.assertFalse(page.death_row.get_visible())
        self.assertFalse(page.background_row.get_visible())
        bad = self.root / "font.ttf"
        bad.write_bytes(b"nope")
        page.set_font("studio", bad)
        self.assertIn("TrueType", page.error_label.get_text())
        self.assertEqual(ModSettings(), self.store.load("studio"))

    def test_the_avatar_background_is_downloaded_before_it_is_turned_on(self):
        page = self._page()
        page.background_switch.set_active(True)
        self.assertTrue(self._wait_until(lambda: self.store.load("player").classic_avatar_background))
        self.assertEqual([True], self.fetches)

        def failing():
            raise ModsError("Could not download the avatar editor background: offline")

        self.store.save("player", ModSettings())
        page = self._page(failing)
        page.background_switch.set_active(True)
        self.assertTrue(self._wait_until(lambda: "offline" in page.error_label.get_text()))
        self.assertFalse(page.background_switch.get_active())
        self.assertFalse(self.store.load("player").classic_avatar_background)

    def test_the_folder_list_shows_used_and_refused_files(self):
        folder = self.root / "data" / "roseblox" / "mods" / "player"
        (folder / "content" / "sounds").mkdir(parents=True)
        (folder / "content" / "sounds" / "ouch.ogg").write_bytes(b"x" * 2048)
        (folder / "content" / "thing.lua").write_text("--")
        page = self._page()
        page.scan()
        self.assertTrue(self._wait_until(lambda: "player" in page.files))
        self.assertEqual("1 file to apply, 2.0 KiB; 1 not used", page.files_group.get_title())
        self.assertEqual(2, len(page._file_rows))
        self.assertIn(str(folder), page.folder_row.get_subtitle())


if __name__ == "__main__":
    unittest.main()
