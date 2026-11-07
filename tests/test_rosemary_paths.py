import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import gi

gi.require_version("Adw", "1")
from gi.repository import Adw, GLib

from roseblox.backend import diagnostics
from roseblox.backend.gpu import Gpu
from roseblox.backend.launch_config import AppLaunchConfig, LaunchConfigStore
from roseblox.backend.rosemary_paths import (
    CacheError,
    cache_entries,
    guest_home,
    guest_path,
    login_name,
    prefix_directory,
    read_framerate_cap,
    roblox_settings_path,
    shader_cache_directory,
    trash_cache,
)

SETTINGS_XML = """<?xml version="1.0" encoding="utf-8"?>
<roblox xmlns:xmime="http://www.w3.org/2005/05/xmlmime" version="4">
  <Item class="UserGameSettings" referent="RBX0">
    <Properties>
      <int name="FramerateCap">{cap}</int>
      <token name="SavedQualityLevel">5</token>
    </Properties>
  </Item>
</roblox>
"""


class PathTests(unittest.TestCase):
    def test_the_default_prefix_and_guest_home(self):
        env = {"HOME": "/home/me"}
        self.assertEqual(Path("/home/me/.local/share/rosemary/prefixes/default"), prefix_directory(env))
        self.assertEqual(
            Path(f"/home/me/.local/share/rosemary/prefixes/default/root/Users/{login_name()}"), guest_home(env)
        )
        self.assertEqual(Path("/data/rosemary/prefixes/default"), prefix_directory({"XDG_DATA_HOME": "/data"}))

    def test_prefix_settings_are_followed(self):
        self.assertEqual(Path("/games/prefix"), prefix_directory({"ROSEMARY_PREFIX": "/games/prefix"}))
        for value in ("off", "0", ""):
            with self.subTest(value=value):
                self.assertIsNone(prefix_directory({"ROSEMARY_PREFIX": value}))
                self.assertIsNone(guest_home({"ROSEMARY_PREFIX": value}))
        self.assertEqual(Path("/home/me"), guest_home({"HOME": "/home/me", "ROSEMARY_PREFIX_HOME": "host"}))

    def test_host_files_have_guest_paths(self):
        env = {"HOME": "/home/me"}
        self.assertEqual("/Volumes/Host/home/me/Places/My Game.rbxl", guest_path(Path("/home/me/Places/My Game.rbxl"), env))
        inside = Path("/home/me/.local/share/rosemary/prefixes/default/root/Users/me/Documents/a.rbxlx")
        self.assertEqual("/Users/me/Documents/a.rbxlx", guest_path(inside, env))
        self.assertEqual("/home/me/a.rbxl", guest_path(Path("/home/me/a.rbxl"), {"ROSEMARY_PREFIX": "off"}))
        self.assertEqual("/Volumes/Host/srv/a.rbxl", guest_path(Path("/srv/x/../a.rbxl"), {"ROSEMARY_PREFIX": "/p"}))

    def test_shader_cache_location(self):
        self.assertEqual(Path("/home/me/.cache/rosemary/metal-shaders"), shader_cache_directory({"HOME": "/home/me"}))
        self.assertEqual(Path("/c/rosemary/metal-shaders"), shader_cache_directory({"XDG_CACHE_HOME": "/c"}))
        self.assertEqual(Path("/s"), shader_cache_directory({"ROSEMARY_SHADER_CACHE": "/s"}))
        self.assertIsNone(shader_cache_directory({"ROSEMARY_SHADER_CACHE": "off"}))


class RobloxFilesTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.env = {
            "HOME": str(self.root),
            "ROSEMARY_PREFIX": str(self.root / "prefix"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
        }
        self.home = guest_home(self.env)

    def tearDown(self):
        self._directory.cleanup()

    def _settings(self, text):
        path = roblox_settings_path(self.env)
        path.parent.mkdir(parents=True)
        path.write_text(text)
        return path

    def test_the_saved_frame_rate_cap_is_read(self):
        self.assertIsNone(read_framerate_cap(roblox_settings_path(self.env)))
        path = self._settings(SETTINGS_XML.format(cap=240))
        self.assertEqual(self.home / "Library" / "Roblox" / "GlobalBasicSettings_13.xml", path)
        self.assertEqual(240, read_framerate_cap(path))
        path.write_text(SETTINGS_XML.format(cap=-1))
        self.assertEqual(-1, read_framerate_cap(path))
        path.write_text("<roblox><broken")
        self.assertIsNone(read_framerate_cap(path))
        self.assertIsNone(read_framerate_cap(None))

    def test_only_rosemary_and_roblox_caches_are_offered(self):
        shaders = self.root / "cache" / "rosemary" / "metal-shaders"
        shaders.mkdir(parents=True)
        (shaders / "a.spv").write_bytes(b"x" * 100)
        caches = self.home / "Library" / "Caches"
        (caches / "com.roblox.RobloxPlayer").mkdir(parents=True)
        (caches / "com.roblox.RobloxPlayer" / "asset").write_bytes(b"y" * 50)
        (caches / "com.apple.other").mkdir()
        (self.home / "Library" / "Cookies").mkdir(parents=True)
        (caches / "com.roblox.linked").symlink_to(self.root)
        entries = cache_entries(self.env)
        self.assertEqual([shaders, caches / "com.roblox.RobloxPlayer"], [entry.path for entry in entries])
        self.assertEqual((100, 1), (entries[0].size, entries[0].files))
        self.assertEqual((50, 1), (entries[1].size, entries[1].files))

    def test_trashing_is_limited_to_offered_caches(self):
        cache = self.home / "Library" / "Caches" / "com.roblox.RobloxPlayer"
        cache.mkdir(parents=True)
        cookies = self.home / "Library" / "Cookies"
        cookies.mkdir(parents=True)
        for path in (cookies, self.home, self.root):
            with self.subTest(path=path), self.assertRaises(CacheError):
                trash_cache(path, self.env)
        with patch("roseblox.backend.rosemary_paths.Gio.File.new_for_path") as new_for_path:
            trash_cache(cache, self.env)
        new_for_path.assert_called_once_with(str(cache))
        new_for_path.return_value.trash.assert_called_once()
        with patch("roseblox.backend.rosemary_paths.Gio.File.new_for_path") as new_for_path:
            new_for_path.return_value.trash.side_effect = GLib.Error("Trash is not supported")
            with self.assertRaises(CacheError):
                trash_cache(cache, self.env)


class DiagnosticsTests(unittest.TestCase):
    def test_report_lists_real_facts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "rosemary"
            executable.write_text(
                "#!/bin/sh\nif [ \"$1\" = prefix ]; then\n"
                "echo \"prefix:   $ROSEMARY_PREFIX\"\necho 'guest /:  /x/root'\n"
                "echo 'made by:  rosemary 0.5.0'\necho 'base:     macOS 15.5'\nexit 0\nfi\n"
                "echo 'rosemary 0.5.0'\necho 'commit: abc'\necho 'built: today'\n"
            )
            executable.chmod(0o755)
            env = {"HOME": directory, "ROSEMARY_PREFIX": str(root / "prefix"), "WAYLAND_DISPLAY": "wayland-0"}
            items = dict(
                diagnostics.collect(
                    rosemary_executable=str(executable),
                    env=env,
                    gpus=[Gpu("0000:01:00.0", 0x10DE, 0x2782, "nvidia", "NVIDIA RTX")],
                    gamemode_available=False,
                )
            )
        self.assertEqual("rosemary 0.5.0, commit: abc", items["Rosemary"])
        self.assertIn("not created yet", items["Prefix"])
        self.assertEqual("rosemary 0.5.0", items["Prefix made by"])
        self.assertEqual("macOS 15.5", items["Prefix base"])
        self.assertNotIn("Prefix guest /", items)
        self.assertEqual("NVIDIA RTX (nvidia, 0000:01:00.0)", items["GPUs"])
        self.assertEqual("Not available", items["GameMode"])
        self.assertEqual("Wayland", items["Session"])
        self.assertIn("not saved", items["Roblox frame rate cap"])
        report = diagnostics.format_report([("A", "1"), ("B", "2")])
        self.assertEqual("A: 1\nB: 2\n", report)

    def test_missing_rosemary_is_reported(self):
        self.assertEqual("Not found", diagnostics.rosemary_version(None))
        self.assertIn("Could not run", diagnostics.rosemary_version("/nonexistent/rosemary"))

    def test_prefix_info_failures_and_a_disabled_prefix(self):
        self.assertEqual([], diagnostics.prefix_info("/bin/true", {"ROSEMARY_PREFIX": "off"}))
        self.assertEqual([], diagnostics.prefix_info(None, {"HOME": "/home/me"}))
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "rosemary"
            executable.write_text("#!/bin/sh\necho 'rosemary: no prefix at /p' >&2\nexit 1\n")
            executable.chmod(0o755)
            self.assertEqual(
                [("Prefix info", "rosemary: no prefix at /p")],
                diagnostics.prefix_info(str(executable), {"ROSEMARY_PREFIX": "/p"}),
            )

    def test_vulkan_drivers_come_from_icd_files(self):
        with tempfile.TemporaryDirectory() as directory:
            icd = Path(directory)
            (icd / "nvidia_icd.json").write_text("{}")
            (icd / "radeon_icd.x86_64.json").write_text("{}")
            self.assertEqual(["nvidia_icd", "radeon_icd.x86_64"], diagnostics.vulkan_drivers((icd, icd / "none")))


class PageMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.environment = patch.dict(
            os.environ,
            {
                "HOME": str(self.root),
                "XDG_CACHE_HOME": str(self.root / "cache"),
                "XDG_DATA_HOME": str(self.root / "data"),
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for target, replacement in (
            ("roseblox.pages.performance_page.list_gpus", lambda: []),
            ("roseblox.pages.performance_page.gamemode.available", lambda: False),
        ):
            patcher = patch(target, side_effect=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        Adw.init()

    def tearDown(self):
        self._directory.cleanup()

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

    def test_caches_report_and_frame_rate_cap_follow_each_application(self):
        from roseblox.pages.performance_page import PerformancePage

        store = LaunchConfigStore(self.root / "launch-config.json")
        studio_prefix = self.root / "studio-prefix"
        store.save("studio", AppLaunchConfig(rosemary_environment={"ROSEMARY_PREFIX": str(studio_prefix)}), "Prefix")
        default_home = guest_home(dict(os.environ))
        (default_home / "Library" / "Caches" / "com.roblox.RobloxPlayer").mkdir(parents=True)
        (default_home / "Library" / "Roblox").mkdir(parents=True)
        (default_home / "Library" / "Roblox" / "GlobalBasicSettings_13.xml").write_text(SETTINGS_XML.format(cap=144))

        window = SimpleNamespace(rosemary_status=SimpleNamespace(executable=None), resolved_preferred_ids={}, library_records=[])
        page = PerformancePage(window, store=store, allowlist_fetcher=lambda force: None)
        self.assertTrue(self._wait_until(lambda: page.loaded))
        self.assertEqual("Not checked yet", page._cache_rows[0].get_title())
        page.scan()
        self.assertTrue(self._wait_until(lambda: not page.scanning and page.report))
        self.assertIn("144 FPS", page.framerate_row.get_subtitle())
        self.assertEqual(["Roblox cache (com.roblox.RobloxPlayer)"], [row.get_title() for row in page._cache_rows])
        self.assertIn(("Rosemary", "Not found"), page.report)

        page.app_buttons["studio"].set_active(True)
        self.assertIn("has not saved", page.framerate_row.get_subtitle())
        self.assertTrue(self._wait_until(lambda: not page.scanning))
        self.assertEqual("No caches to clear", page._cache_rows[0].get_title())
        self.assertIn(("Roblox home", str(guest_home({"ROSEMARY_PREFIX": str(studio_prefix)}))), page.report)

        page.app_buttons["player"].set_active(True)
        self.assertTrue(self._wait_until(lambda: not page.scanning))
        entry = page.caches["player"][0]
        with patch("roseblox.backend.rosemary_paths.Gio.File.new_for_path") as new_for_path:
            new_for_path.return_value.trash.side_effect = lambda _cancellable: (
                __import__("shutil").rmtree(entry.path)
            )
            page.trash(entry)
            self.assertTrue(self._wait_until(lambda: not page.scanning and page.caches["player"] == []))
        page._copy_report(None)
        self.assertTrue(page.diagnostics_message.get_visible())


if __name__ == "__main__":
    unittest.main()
