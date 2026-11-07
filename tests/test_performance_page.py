import base64
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib

from roseblox.backend.fast_flags import flag_file_path, write_bundle_flags
from roseblox.backend.flag_catalog import (
    ALLOWLIST_URL,
    BUILT_IN_ALLOWLIST,
    PROFILE_EXCLUDED_CATEGORIES,
    Allowlist,
    categorize,
    fetch_allowlist,
    load_cached_allowlist,
    parse_allowlist_response,
)
from roseblox.backend.launch_config import AppLaunchConfig, LaunchConfigStore, describe_changes


def _response(flags):
    encoded = base64.b64encode(json.dumps({"Allowed": flags, "Signature": "x"}).encode()).decode()
    return json.dumps({"applicationSettings": {"DFStringAllowedPublicFlags": encoded, "Other": "1"}}).encode()


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class AllowlistTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.cache = Path(self._directory.name) / "allowlist.json"

    def tearDown(self):
        self._directory.cleanup()

    def test_response_is_decoded(self):
        self.assertEqual(frozenset({"FFlagA", "DFIntB"}), parse_allowlist_response(_response(["FFlagA", "DFIntB"])))
        for data in (b"{}", b"not json", json.dumps({"applicationSettings": {"DFStringAllowedPublicFlags": "!!"}}).encode()):
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_allowlist_response(data)

    def test_the_built_in_list_is_used_until_one_is_fetched(self):
        allowlist = load_cached_allowlist(self.cache)
        self.assertTrue(allowlist.built_in)
        self.assertEqual(BUILT_IN_ALLOWLIST, allowlist.flags)
        self.assertEqual(18, len(BUILT_IN_ALLOWLIST))

    def test_a_fetched_list_is_cached_for_six_hours(self):
        requests = []

        def opener(request, timeout):
            requests.append(request.full_url)
            return _Response(_response(["FFlagA"]))

        fetched = fetch_allowlist(self.cache, opener=opener, clock=lambda: 1000.0)
        self.assertEqual(frozenset({"FFlagA"}), fetched.flags)
        self.assertEqual([ALLOWLIST_URL], requests)
        self.assertEqual(fetched, fetch_allowlist(self.cache, opener=opener, clock=lambda: 1000.0 + 3600))
        self.assertEqual(1, len(requests))
        fetch_allowlist(self.cache, opener=opener, clock=lambda: 1000.0 + 7 * 3600)
        self.assertEqual(2, len(requests))
        fetch_allowlist(self.cache, opener=opener, force=True, clock=lambda: 1000.0 + 7 * 3600)
        self.assertEqual(3, len(requests))

    def test_a_failed_fetch_keeps_the_previous_list_and_says_why(self):
        fetch_allowlist(self.cache, opener=lambda request, timeout: _Response(_response(["FFlagA"])), clock=lambda: 0.0)

        def offline(request, timeout):
            raise OSError("network is unreachable")

        allowlist = fetch_allowlist(self.cache, opener=offline, force=True)
        self.assertEqual(frozenset({"FFlagA"}), allowlist.flags)
        self.assertIn("network is unreachable", allowlist.error)

    def test_flags_are_sorted_into_categories(self):
        self.assertEqual("Rendering", categorize("FIntDebugForceMSAASamples"))
        self.assertEqual("Textures", categorize("DFIntTextureQualityOverride"))
        self.assertEqual("Level of detail", categorize("DFIntCSGLevelOfDetailSwitchingDistanceL12"))
        self.assertEqual("Networking", categorize("DFIntNetworkLatencyTolerance"))
        self.assertEqual("Physics", categorize("FFlagPhysicsSolverExample"))
        self.assertEqual("Security", categorize("FFlagHyperionExample"))
        self.assertEqual("Other", categorize("FFlagSomethingUnrelated"))
        self.assertEqual({"Networking", "Physics", "Security"}, set(PROFILE_EXCLUDED_CATEGORIES))


class DescribeChangesTests(unittest.TestCase):
    def test_every_kind_of_change_is_described(self):
        old = AppLaunchConfig(
            fast_flags={"FFlagA": "True", "FIntB": "1"},
            rosemary_environment={"KEEP": "1", "GONE": "x"},
            arguments=("-a",),
        )
        new = AppLaunchConfig(
            fast_flags={"FIntB": "2", "FIntC": "3"},
            rosemary_environment={"KEEP": "1"},
            guest_environment={"NEW": "y"},
            arguments=(),
            presentation_mode="fifo",
            refresh_rate=144,
        )
        self.assertEqual(
            [
                "Presentation mode: Rosemary default → fifo",
                "Refresh rate: display default → 144 Hz",
                "Remove FastFlag FFlagA",
                "FastFlag FIntB: 1 → 2",
                "Add FastFlag FIntC = 3",
                "Remove Rosemary variable GONE",
                "Add Roblox variable NEW = y",
                "Launch arguments: -a → (none)",
            ],
            describe_changes(old, new),
        )
        self.assertEqual([], describe_changes(new, new))


class PerformancePageTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.environment = patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / "cache")})
        self.environment.start()
        Adw.init()
        self.store = LaunchConfigStore(self.root / "launch-config.json")
        self.bundle = self.root / "RobloxPlayer.app"
        (self.bundle / "Contents" / "MacOS").mkdir(parents=True)
        self.window = SimpleNamespace(
            rosemary_status=SimpleNamespace(executable="/usr/bin/rosemary"),
            resolved_preferred_ids={"player": "player-1", "studio": None},
            library_records=[SimpleNamespace(id="player-1", bundle_path=str(self.bundle))],
        )
        self.fetches = []
        self.gpus = []
        self.gamemode_available = False
        for target, replacement in (
            ("roseblox.pages.performance_page.list_gpus", lambda: list(self.gpus)),
            ("roseblox.pages.performance_page.gamemode.available", lambda: self.gamemode_available),
        ):
            patcher = patch(target, side_effect=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.environment.stop()
        self._directory.cleanup()

    def test_gpu_choice_is_offered_only_with_several_cards(self):
        from roseblox.backend.gpu import Gpu

        self.gpus = [Gpu("0000:00:02.0", 0x8086, 0x4680, "i915", "Intel UHD", True)]
        page = self._page()
        self.assertFalse(page.gpu_row.get_sensitive())
        self.assertIn("Only one GPU was found: Intel UHD", page.gpu_row.get_subtitle())
        self.assertFalse(page.gamemode_row.switch.get_sensitive())

        self.gpus.append(Gpu("0000:01:00.0", 0x10DE, 0x2782, "nvidia", "NVIDIA RTX 4070 Ti"))
        self.gamemode_available = True
        page.reload(detect=True)
        self.assertTrue(self._wait_until(lambda: len(page.gpus) == 2))
        self.assertTrue(page.gpu_row.get_sensitive())
        page.gpu_row.set_selected(2)
        page.gamemode_row.switch.set_active(True)
        self.assertEqual("0000:01:00.0", page.draft.gpu)
        self.assertTrue(page.draft.gamemode)
        self.assertIn("GPU: chosen by Rosemary → NVIDIA RTX 4070 Ti", page.changes)
        self.assertIn("MESA_VK_DEVICE_SELECT=10de:2782", page.environment_preview.get_text())

    def test_a_saved_gpu_that_is_gone_stays_visible(self):
        self.store.save("player", AppLaunchConfig(gpu="0000:03:00.0"), "Old card")
        page = self._page()
        self.assertTrue(page.gpu_row.get_sensitive())
        self.assertEqual("0000:03:00.0", page._gpu_values[page.gpu_row.get_selected()])
        self.assertNotIn("MESA_VK_DEVICE_SELECT", page.environment_preview.get_text())

    def _fetcher(self, *, force):
        self.fetches.append(force)
        return Allowlist(frozenset({"FIntDebugForceMSAASamples"}), time.time())

    def _page(self):
        from roseblox.pages.performance_page import PerformancePage

        page = PerformancePage(self.window, store=self.store, allowlist_fetcher=self._fetcher)
        self.assertTrue(self._wait_until(lambda: page.loaded and not page.saving))
        return page

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

    def test_saved_configuration_is_shown_in_the_controls(self):
        self.store.save(
            "player",
            AppLaunchConfig(
                presentation_mode="fifo",
                refresh_rate=144,
                fast_flags={
                    "FIntDebugForceMSAASamples": "4",
                    "DFFlagTextureQualityOverrideEnabled": "True",
                    "DFIntTextureQualityOverride": "2",
                    "FIntFRMMinGrassDistance": "0",
                    "FIntFRMMaxGrassDistance": "0",
                },
            ),
            "Setup",
        )
        page = self._page()
        self.assertEqual(2, page.presentation_row.get_selected())
        self.assertEqual(144, page._refresh_values[page.refresh_row.get_selected()])
        self.assertEqual(3, page.msaa_row.get_selected())
        self.assertEqual(3, page.texture_row.get_selected())
        self.assertTrue(page.grass_row.switch.get_active())
        self.assertFalse(page.gray_sky_row.switch.get_active())
        self.assertEqual([], page.changes)
        self.assertFalse(page.review_button.get_sensitive())

    def test_controls_edit_flags_and_unapplied_changes_can_be_discarded(self):
        page = self._page()
        page.texture_row.set_selected(1)
        page.gray_sky_row.switch.set_active(True)
        page.refresh_row.set_selected(page._refresh_values.index(240))
        self.assertEqual(
            {"DFFlagTextureQualityOverrideEnabled": "True", "DFIntTextureQualityOverride": "0", "FFlagDebugSkyGray": "True"},
            page.draft.fast_flags,
        )
        self.assertEqual(240, page.draft.refresh_rate)
        self.assertEqual(4, len(page.changes))
        self.assertTrue(page.review_button.get_sensitive())
        self.assertIn("4 unapplied changes", page.status_label.get_text())
        page.texture_row.set_selected(0)
        self.assertEqual({"FFlagDebugSkyGray": "True"}, page.draft.fast_flags)
        page._discard_clicked(None)
        self.assertTrue(page.draft.is_default)
        self.assertFalse(page.gray_sky_row.switch.get_active())

    def test_applying_saves_with_a_backup_and_player_and_studio_stay_separate(self):
        page = self._page()
        page._add_flag("FIntDebugForceMSAASamples", "8")
        page.app_buttons["studio"].set_active(True)
        self.assertEqual({}, page.draft.fast_flags)
        page._add_argument("-task")
        page.app_buttons["player"].set_active(True)
        page.apply("player", page.draft, page.changes)
        self.assertTrue(self._wait_until(lambda: not page.saving and page.store.backups()))
        self.assertTrue(self._wait_until(lambda: page.backups))
        self.assertEqual({"FIntDebugForceMSAASamples": "8"}, self.store.load("player").fast_flags)
        self.assertTrue(self.store.load("studio").is_default)
        self.assertEqual([], page.changes)
        self.assertEqual(("-task",), page.drafts["studio"].arguments)
        self.assertIn("Add FastFlag FIntDebugForceMSAASamples = 8", page.backups[0].reason)

    def test_restoring_a_backup_replaces_the_configuration(self):
        self.store.save("player", AppLaunchConfig(refresh_rate=120), "First")
        self.store.save("player", AppLaunchConfig(refresh_rate=144), "Second")
        page = self._page()
        self.assertEqual(144, page.draft.refresh_rate)
        backup = next(entry for entry in page.backups if entry.reason == "Second")
        page.restore(backup)
        self.assertTrue(self._wait_until(lambda: page.loaded and not page.saving and page.draft.refresh_rate == 120))

    def test_invalid_entries_are_refused_where_they_are_typed(self):
        page = self._page()
        editor = page.rosemary_env_editor
        editor.name_entry.set_text("LD_PRELOAD")
        editor.value_entry.set_text("/tmp/x.so")
        editor._add_clicked(None)
        self.assertTrue(editor.error_label.get_visible())
        self.assertEqual({}, page.draft.rosemary_environment)
        editor.name_entry.set_text("ROSEMARY_SHADER_CACHE")
        editor.value_entry.set_text("off")
        editor._add_clicked(None)
        self.assertFalse(editor.error_label.get_visible())
        self.assertEqual({"ROSEMARY_SHADER_CACHE": "off"}, page.draft.rosemary_environment)
        self.assertIn("ROSEMARY_SHADER_CACHE=off", page.environment_preview.get_text())
        self.assertIn("True or False", page._add_flag("FFlagDebugSkyGray", "yes"))
        self.assertIsNotNone(page._add_argument("-ticket"))
        self.assertIsNone(page._add_argument("--my-option"))
        self.assertIn("--my-option", page.command_preview.get_text())
        self.assertIn(str(self.bundle) + "/", page.command_preview.get_text())

    def test_player_marks_flags_roblox_ignores_but_studio_does_not(self):
        page = self._page()
        page.allowlist = self._fetcher(force=False)
        page._add_flag("FFlagNotOnTheList", "True")
        page._add_flag("FIntDebugForceMSAASamples", "2")
        subtitles = {row.get_title(): row.get_subtitle() for row in page.flag_editor.rows}
        self.assertIn("ignores", subtitles["FFlagNotOnTheList"])
        self.assertNotIn("ignores", subtitles["FIntDebugForceMSAASamples"])
        page.app_buttons["studio"].set_active(True)
        page._add_flag("FFlagNotOnTheList", "True")
        self.assertNotIn("ignores", page.flag_editor.rows[0].get_subtitle())
        self.assertIn("no allowlist", page.flag_editor.group.get_description())

    def test_flags_already_in_the_installation_can_be_adopted(self):
        flag_file_path(self.bundle).parent.mkdir(parents=True)
        flag_file_path(self.bundle).write_text('{"FIntDebugForceMSAASamples": "4"}')
        page = self._page()
        page._adopt_clicked(None)
        self.assertEqual({"FIntDebugForceMSAASamples": "4"}, page.draft.fast_flags)
        self.assertIn("Review them", page.flag_message.get_text())

    def test_a_damaged_configuration_is_reported_and_cannot_be_applied_over(self):
        self.store.save("player", AppLaunchConfig(refresh_rate=120), "Good")
        self.store.path.write_text("{broken")
        page = self._page()
        self.assertTrue(page.error_label.get_visible())
        self.assertIn("Restore a backup", page.error_label.get_text())
        page._add_flag("FFlagDebugSkyGray", "True")
        self.assertFalse(page.review_button.get_sensitive())
        self.assertTrue(page.backups)

    def test_the_allowlist_is_fetched_once_the_page_is_first_shown(self):
        page = self._page()
        self.assertEqual([], self.fetches)
        page._mapped(page)
        page._mapped(page)
        self.assertTrue(self._wait_until(lambda: self.fetches and page.allowlist_refresh.get_sensitive()))
        self.assertEqual([False], self.fetches)
        self.assertIn("1 flags, fetched from Roblox", page.allowlist_row.get_subtitle())


if __name__ == "__main__":
    unittest.main()
