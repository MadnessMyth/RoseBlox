import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gtk

from roseblox.backend.rosemary_install import ROSEMARY_RELEASES_URL, RosemaryInstallError
from roseblox.backend.self_update import Release
from roseblox.backend.system import SystemInfo
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


def _notify(controller, app_type, state):
    for listener in list(controller._operation_listeners):
        listener(app_type, state)


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
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "PATH": str(root / "bin"),
        }
        self.environment = patch.dict(os.environ, environment)
        self.environment.start()
        self.latest_roseblox = Release("0.0.1", "https://github.com/MadnessMyth/RoseBlox/releases/tag/v0.0.1")
        latest = patch("roseblox.backend.self_update.fetch_latest_release", lambda: self.latest_roseblox)
        latest.start()
        self.addCleanup(latest.stop)
        self.system = SystemInfo("arch", (), "Arch Linux", "x86_64")
        Adw.init()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def _build_window(self):
        from roseblox.window import RosebloxWindow

        with patch("roseblox.window.detect_system", return_value=self.system):
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

    def test_import_from_another_app_previews_then_applies(self):
        source = self.config_home / "roseline" / "settings.toml"
        source.parent.mkdir(parents=True)
        source.write_text(
            '[user_flags]\nFFlagDebugSkyGray = "True"\n[behavior]\ngamemode = true\nmystery = 1\n',
            encoding="utf-8",
        )
        window = self._loaded_window()
        onboarding = window.onboarding_page
        self.assertTrue(self._wait_until(lambda: "Roséline" in (onboarding.import_row.get_subtitle() or "")))
        self.assertEqual("Optional", onboarding.import_badge.get_text())

        onboarding.import_button.emit("clicked")
        dialog = window.import_window
        self.addCleanup(dialog.destroy)
        self.assertTrue(self._wait_until(lambda: bool(dialog._source_rows)))
        self.assertIn("Roséline", dialog._source_rows[0].get_title())
        from roseblox.backend.importers import importer

        dialog.read(importer("roseline"), source)
        self.assertTrue(self._wait_until(lambda: dialog.plan is not None))
        self.assertEqual(2, len(dialog._checks))
        self.assertTrue(all(check.get_active() for check, _change in dialog._checks))
        self.assertIn("behavior.mystery", {problem.source_key for problem in dialog.plan.problems})
        dialog.import_button.emit("clicked")
        self.assertTrue(self._wait_until(lambda: dialog.navigation.get_visible_page().get_tag() == "done"))

        from roseblox.backend.launch_config import LaunchConfigStore

        config = LaunchConfigStore().load("player")
        self.assertEqual({"FFlagDebugSkyGray": "True"}, config.fast_flags)
        self.assertTrue(config.gamemode)
        self.assertTrue(self._wait_until(lambda: onboarding.import_badge.get_text() == "Imported"))
        self.assertTrue(self._wait_until(lambda: window.performance_page.saved["player"].gamemode))

        dialog.plan = None
        dialog.read(importer("roseline"), source)
        self.assertTrue(self._wait_until(lambda: dialog.plan is not None))
        self.assertEqual([], dialog.plan.actionable)
        self.assertFalse(dialog.import_button.get_sensitive())
        self.assertEqual("same", dialog.plan.changes[0].status)

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

    def test_other_distributions_open_the_releases_page_then_use_the_downloaded_appimage(self):
        self.system = SystemInfo("ubuntu", ("debian",), "Ubuntu 24.04 LTS", "x86_64")
        window = self._loaded_window()
        page = window.onboarding_page
        self.assertEqual("Download the AppImage", page.install_rosemary_row.get_title())
        self.assertEqual("Open Releases Page", page.install_rosemary_button.get_label())
        self.assertEqual("Download the AppImage", window.settings_page.install_rosemary_row.get_title())
        self.assertFalse(page.appimage_row.get_visible())

        launcher = Mock()
        with patch("roseblox.window.Gtk.UriLauncher.new", return_value=launcher) as new_launcher, patch(
            "roseblox.window.plan_rosemary_install"
        ) as plan, patch("roseblox.window.GLib.timeout_add_seconds"):
            window.install_rosemary()
        new_launcher.assert_called_once_with(ROSEMARY_RELEASES_URL)
        launcher.launch.assert_called_once()
        plan.assert_not_called()
        self.assertTrue(window.rosemary_install.waiting)
        self.assertIn("x86_64.AppImage", page.install_rosemary_row.get_subtitle())

        downloads = Path(os.environ["HOME"]) / "Downloads"
        downloads.mkdir()
        appimage = downloads / "Rosemary-0.5.0-x86_64.AppImage"
        appimage.write_text("#!/bin/sh\n", encoding="utf-8")
        appimage.chmod(0o644)
        self.assertTrue(window._poll_rosemary_install())
        self.assertTrue(self._wait_until(lambda: not window.rosemary_install.waiting))
        self.assertEqual(str(appimage), window.rosemary_appimage)
        self.assertTrue(page.appimage_row.get_visible())
        self.assertEqual(str(appimage), page.appimage_row.get_subtitle())
        self.assertTrue(window.settings_page.appimage_row.get_visible())
        self.assertIn("Use This File", page.install_rosemary_row.get_subtitle())

        window.use_rosemary_appimage()
        self.assertTrue(self._wait_until(lambda: window.rosemary_status.available))
        self.assertTrue(os.access(appimage, os.X_OK))
        self.assertEqual(str(appimage), window.rosemary_status.executable)
        self.assertEqual("Ready", page.rosemary_badge.get_text())
        self.assertFalse(page.appimage_row.get_visible())
        self.assertFalse(page.install_rosemary_row.get_visible())

    def test_appimage_found_at_startup_is_offered_without_downloading(self):
        self.system = SystemInfo("fedora", (), "Fedora Linux 41", "x86_64")
        bin_directory = Path(os.environ["HOME"]) / ".local" / "bin"
        bin_directory.mkdir(parents=True)
        (bin_directory / "Rosemary-0.4.1-x86_64.AppImage").write_text("", encoding="utf-8")
        (bin_directory / "Rosemary-0.5.0-x86_64.AppImage").write_text("", encoding="utf-8")
        window = self._loaded_window()
        self.assertEqual(str(bin_directory / "Rosemary-0.5.0-x86_64.AppImage"), window.rosemary_appimage)
        self.assertTrue(window.onboarding_page.appimage_row.get_visible())

    def test_systems_without_an_appimage_explain_the_other_ways(self):
        self.system = SystemInfo("debian", (), "Debian GNU/Linux 13", "aarch64")
        window = self._loaded_window()
        self.assertFalse(window.onboarding_page.install_rosemary_row.get_visible())
        self.assertFalse(window.settings_page.install_rosemary_row.get_visible())

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

        _notify(controller, "player", OperationState(True, "Downloaded 1.0 MiB of 4.0 MiB", 0.25, True))
        self.assertTrue(progress.get_visible())
        self.assertAlmostEqual(0.25, progress.get_fraction())
        self.assertTrue(cancel.get_visible())
        self.assertFalse(action.get_visible())
        self.assertEqual("Downloaded 1.0 MiB of 4.0 MiB", row.get_subtitle())
        self.assertEqual("Installing", page.apps_badge.get_text())
        with patch.object(controller, "cancel_operation") as cancel_operation:
            cancel.emit("clicked")
        cancel_operation.assert_called_once_with()

        _notify(controller, "player", OperationState(False, "Installation failed: offline", None, False))
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

    def test_link_switches_look_on_once_links_are_enabled(self):
        from roseblox.backend.url_handlers import URLHandlerStatus

        window = self._loaded_window()
        window.url_handlers.check = lambda: URLHandlerStatus(True, True, "Enabled")
        for page in (window.settings_page, window.onboarding_page):
            page.url_handlers = Mock()
            page.url_handlers.enable.return_value = URLHandlerStatus(True, True, "Enabled")
        window.setup_store.save_url_handlers(True, {})
        window.settings_page.url_switch.set_active(True)
        self.assertTrue(self._wait_until(
            lambda: not window.settings_page._url_busy and not window._refresh_in_progress
            and window.settings_page.url_switch.get_state()
        ))
        self.assertTrue(window.onboarding_page.url_switch.get_state())

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

    def test_close_after_launch_follows_each_application_preference(self):
        from roseblox.backend.setup import ClosePreference, SetupStore

        store = SetupStore(self.config_home / "roseblox" / "setup.json")
        store.save_close_after_launch("player", ClosePreference(True, only_from_link=True))
        store.save_close_after_launch("studio", ClosePreference(True, only_from_link=False))
        window = self._loaded_window()
        window.close = Mock()
        running = SimpleNamespace(process=SimpleNamespace(poll=lambda: None))
        exited = SimpleNamespace(process=SimpleNamespace(poll=lambda: 1))

        def closes(app_type, session, from_link):
            window.close.reset_mock()
            window.application_started(app_type, session, from_link)
            self._wait_until(lambda: window.close.called, timeout=0.3)
            return window.close.called

        with patch("roseblox.window.CLOSE_AFTER_LAUNCH_DELAY_SECONDS", 0):
            self.assertFalse(closes("player", running, from_link=False))
            self.assertTrue(closes("player", running, from_link=True))
            self.assertTrue(closes("studio", running, from_link=False))
            self.assertFalse(closes("studio", exited, from_link=False))
            window.pages["player"]._busy = True
            self.assertFalse(closes("studio", running, from_link=True))
            window.pages["player"]._busy = False
            store.save_close_after_launch("studio", ClosePreference(False, False))
            self.assertFalse(closes("studio", running, from_link=True))

    def test_settings_switches_save_close_preferences_separately(self):
        from roseblox.backend.setup import ClosePreference, SetupStore

        window = self._loaded_window()
        settings = window.settings_page
        player_row, player_link = settings._close_rows["player"]
        studio_row, _studio_link = settings._close_rows["studio"]
        self.assertFalse(player_row.get_enable_expansion())
        self.assertEqual("Roseblox stays open.", player_row.get_subtitle())

        player_row.set_enable_expansion(True)
        player_link.set_active(True)
        studio_row.set_enable_expansion(True)
        state = SetupStore(self.config_home / "roseblox" / "setup.json").load()
        self.assertEqual(ClosePreference(True, True), state.close_preference("player"))
        self.assertEqual(ClosePreference(True, False), state.close_preference("studio"))
        self.assertEqual("Closes after opening a Roblox link.", player_row.get_subtitle())
        self.assertEqual("Closes after every launch.", studio_row.get_subtitle())

        # A later status refresh must not flip the switches back.
        window._apply_rosemary_status(window.rosemary_status)
        self.assertTrue(player_link.get_active())
        self.assertTrue(studio_row.get_enable_expansion())

    def test_while_playing_switches_save_and_use_roseblox_discord_application(self):
        from roseblox.backend.setup import SetupStore, WhilePlaying

        window = self._loaded_window()
        settings = window.settings_page
        discord_row, discord = settings._playing_rows["discord_presence"]
        _join_row, join = settings._playing_rows["discord_join_button"]
        _location_row, location = settings._playing_rows["server_location"]
        self.assertTrue(discord.get_sensitive())
        self.assertIn("Discord profile", discord_row.get_subtitle())
        self.assertFalse(join.get_sensitive())

        location.set_active(True)
        discord.set_active(True)
        self.assertTrue(join.get_sensitive())
        settings.discord_id_row.set_text("not a number")
        settings.discord_id_row.emit("apply")
        self.assertIn("long number", settings.discord_id_message.get_text())
        settings.discord_id_row.set_text("123456789012345678")
        settings.discord_id_row.emit("apply")
        state = SetupStore(self.config_home / "roseblox" / "setup.json").load()
        self.assertEqual(
            WhilePlaying(discord_presence=True, discord_application_id="123456789012345678", server_location=True),
            state.while_playing,
        )
        self.assertIn("ipwho.is", settings._playing_rows["server_location"][0].get_subtitle())

    def test_in_game_server_location_switch_shows_under_the_notification_and_warns_without_mangohud(self):
        from roseblox.backend.setup import SetupStore

        window = self._loaded_window()
        settings = window.settings_page
        _location_row, location = settings._playing_rows["server_location"]
        in_game_row, in_game = settings._playing_rows["server_location_in_game"]
        self.assertEqual("Show inside the game", in_game_row.get_title())
        self.assertFalse(in_game_row.get_visible())
        with patch("roseblox.pages.manager_page.mangohud.layer_installed", return_value=False):
            location.set_active(True)
        self.assertTrue(in_game_row.get_visible())
        self.assertTrue(settings.in_game_warning.get_visible())
        self.assertIn("MangoHud, which is not installed", in_game_row.get_subtitle())
        self.assertIn("desktop notification", in_game_row.get_subtitle())
        self.assertTrue(in_game.get_active())
        from roseblox.pages.manager_page import _wants_in_game_banner

        self.assertTrue(_wants_in_game_banner(window.setup_state))
        with patch("roseblox.pages.manager_page.mangohud.layer_installed", return_value=True):
            in_game.set_active(False)
        self.assertFalse(settings.in_game_warning.get_visible())
        self.assertIn("top of the game", in_game_row.get_subtitle())
        self.assertIn("desktop notification instead", in_game_row.get_subtitle())
        state = SetupStore(self.config_home / "roseblox" / "setup.json").load()
        self.assertFalse(state.while_playing.server_location_in_game)
        self.assertFalse(_wants_in_game_banner(state))
        in_game.set_active(True)
        self.assertTrue(_wants_in_game_banner(window.setup_state))
        location.set_active(False)
        self.assertFalse(in_game_row.get_visible())
        self.assertFalse(_wants_in_game_banner(window.setup_state))

    def test_microphone_toggle_rows_save_and_ask_the_desktop_for_the_hotkey(self):
        from roseblox.backend.global_shortcuts import Shortcut
        from roseblox.backend.setup import SetupStore

        sessions = []

        class FakeSession:
            def __init__(self, _connection, **handlers):
                self.handlers = handlers
                self.configured = 0
                sessions.append(self)

            def start(self, shortcuts, done):
                self.shortcuts = shortcuts
                self.done = done

            def configure(self):
                self.configured += 1
                return True

            def close(self):
                pass

        with patch("roseblox.pages.microphone_hotkey.open_connection", return_value=object()), patch(
            "roseblox.pages.microphone_hotkey.ShortcutsSession", FakeSession
        ):
            window = self._loaded_window()
            settings = window.settings_page
            self.assertEqual([], sessions)
            self.assertFalse(settings.release_delay_row.get_visible())
            settings.microphone_row.set_enable_expansion(True)
            [session] = sessions
            self.assertEqual([("microphone", "Roblox microphone (push to talk or toggle)")], session.shortcuts)
            self.assertEqual("Asking the desktop…", settings.hotkey_row.get_subtitle())
            self.assertFalse(settings.hotkey_button.get_sensitive())
            session.done([Shortcut("microphone", "", "")], None)
            self.assertEqual("No key assigned yet.", settings.hotkey_row.get_subtitle())
            settings.hotkey_button.emit("clicked")
            self.assertEqual(1, session.configured)
            session.handlers["on_changed"]([Shortcut("microphone", "", "Ctrl+M")])
            self.assertEqual("Hotkey: Ctrl+M.", settings.hotkey_row.get_subtitle())
            self.assertEqual("Change Hotkey", settings.hotkey_button.get_label())

            self.assertFalse(settings.microphone_inverted_row.get_visible())
            settings.microphone_mode_row.set_selected(1)
            self.assertTrue(settings.release_delay_row.get_visible())
            self.assertTrue(settings.microphone_inverted_row.get_visible())
            settings.release_delay_row.set_value(400)
            settings.microphone_inverted_row.set_active(True)
            self.assertIn("muted while the hotkey is held", settings.microphone_mode_row.get_subtitle())
            self.assertIn("stay muted", settings.release_delay_row.get_subtitle())
        state = SetupStore(self.config_home / "roseblox" / "setup.json").load().while_playing
        self.assertEqual((True, "hold", 400, True), (
            state.microphone_toggle, state.microphone_mode, state.microphone_release_delay_ms,
            state.microphone_inverted,
        ))

    def test_without_global_shortcuts_the_hotkey_row_points_to_roseblox_mic(self):
        sessions = []

        class FakeSession:
            def __init__(self, _connection, **_handlers):
                sessions.append(self)

            def start(self, _shortcuts, done):
                done(None, "This desktop has no global shortcut service.")

        with patch("roseblox.pages.microphone_hotkey.open_connection", return_value=object()), patch(
            "roseblox.pages.microphone_hotkey.ShortcutsSession", FakeSession
        ):
            settings = self._loaded_window().settings_page
            settings.microphone_row.set_enable_expansion(True)
        self.assertIn("roseblox --mic toggle", settings.hotkey_row.get_subtitle())
        self.assertFalse(settings.hotkey_button.get_visible())
        settings.microphone_row.set_enable_expansion(False)
        settings.microphone_row.set_enable_expansion(True)
        self.assertEqual(1, len(sessions))

    def test_microphone_sound_rows_save_choose_and_preview(self):
        from roseblox.backend.setup import SetupStore

        with patch("roseblox.pages.microphone_hotkey.open_connection", side_effect=GLib.Error("no bus")):
            settings = self._loaded_window().settings_page
            settings.microphone_row.set_enable_expansion(True)
        on_row, on_reset = settings._sound_rows["on"]
        self.assertFalse(on_row.get_visible())
        self.assertFalse(settings.sound_volume_row.get_visible())
        settings.microphone_sounds_row.set_active(True)
        self.assertTrue(on_row.get_visible())
        self.assertEqual("Roseblox's sound", on_row.get_subtitle())
        self.assertFalse(on_reset.get_visible())
        settings.sound_volume_row.set_value(35)

        chosen = Path(self.temporary.name) / "click.wav"
        chosen.write_bytes(b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 16)
        settings.use_sound_file("on", chosen)
        self.assertEqual("microphone-on.wav", on_row.get_subtitle())
        self.assertTrue(on_reset.get_visible())
        bad = Path(self.temporary.name) / "song.mp3"
        bad.write_bytes(b"ID3" + b"\x00" * 16)
        settings.use_sound_file("off", bad)
        self.assertIn("WAV, Ogg or FLAC", settings.playing_message.get_text())

        played = []
        settings.sound_player.spawn = played.append
        settings.sound_player.which = lambda name: name
        settings._preview_sound("on")
        self.assertEqual(["pw-play", "--volume=0.35"], played[-1][:2])
        self.assertTrue(played[-1][-1].endswith("microphone-on.wav"))

        state = SetupStore(self.config_home / "roseblox" / "setup.json").load().while_playing
        self.assertEqual((True, 35, ""), (state.microphone_sounds, state.microphone_sound_volume, state.microphone_sound_off))
        self.assertTrue(Path(state.microphone_sound_on).is_file())
        on_reset.emit("clicked")
        self.assertEqual("Roseblox's sound", on_row.get_subtitle())

    def test_missing_audio_support_is_shown_instead_of_guessed(self):
        from roseblox.backend.microphone import NO_SOUND_SERVER
        from roseblox.backend.microphone_sounds import NO_PLAYER, MicrophoneSounds

        with patch("roseblox.pages.manager_page.microphone_problem", return_value=NO_SOUND_SERVER), patch(
            "roseblox.pages.manager_page.sound_problem", return_value=NO_PLAYER
        ), patch("roseblox.pages.microphone_hotkey.open_connection", side_effect=GLib.Error("no bus")):
            settings = self._loaded_window().settings_page
            self.assertTrue(settings.microphone_warning_row.get_visible())
            self.assertEqual(NO_SOUND_SERVER, settings.microphone_warning_row.get_subtitle())
            self.assertFalse(settings.sounds_warning.get_visible())
            settings.microphone_row.set_enable_expansion(True)
            settings.microphone_sounds_row.set_active(True)
            self.assertTrue(settings.sounds_warning.get_visible())
            self.assertEqual(NO_PLAYER, settings.microphone_sounds_row.get_subtitle())
        settings.sound_player.which = lambda _name: None
        settings._preview_sound("on")
        self.assertIn("pw-play", settings.playing_message.get_text())

        with patch("roseblox.pages.manager_page.microphone_problem", return_value=None), patch(
            "roseblox.pages.manager_page.sound_problem", return_value=None
        ):
            settings._show_while_playing(settings._current_while_playing())
        self.assertFalse(settings.microphone_warning_row.get_visible())
        self.assertFalse(settings.sounds_warning.get_visible())
        self.assertEqual(MicrophoneSounds(True), settings._current_while_playing().sounds)

    def test_the_activity_watcher_starts_only_when_a_while_playing_feature_is_on(self):
        from unittest import mock

        from roseblox.backend.setup import WhilePlaying

        window = self._loaded_window()
        page = window.pages["player"]
        session = SimpleNamespace(process=SimpleNamespace(pid=4242), start_time=777, log_path=Path("/tmp/player.log"))
        with mock.patch("roseblox.pages.manager_page._spawn_detached") as spawn, mock.patch(
            "roseblox.pages.manager_page.GLib.child_watch_add"
        ):
            page._start_activity_watcher(session)
            spawn.assert_not_called()
            window.setup_state = replace(window.setup_state, while_playing=WhilePlaying(close_on_leave=True))
            spawn.return_value = SimpleNamespace(pid=5000)
            page._start_activity_watcher(session)
        arguments = spawn.call_args.args[0]
        self.assertEqual(["--activity-watch", "4242", "777", "/tmp/player.log"], arguments[3:])

    def test_settings_are_grouped_into_sections_that_remember_their_state(self):
        from roseblox.backend.setup import SetupStore

        window = self._loaded_window()
        settings = window.settings_page
        self.assertEqual(
            ["appearance", "rosemary", "launching", "playing", "links", "updates", "advanced"], list(settings._sections)
        )
        advanced = settings._sections["advanced"]
        self.assertFalse(advanced.expanded)
        self.assertFalse(advanced.revealer.get_reveal_child())
        self.assertTrue(settings._sections["appearance"].expanded)

        advanced.header.emit("clicked")
        settings._sections["updates"].header.emit("clicked")
        self.assertTrue(advanced.revealer.get_reveal_child())
        state = SetupStore(self.config_home / "roseblox" / "setup.json").load()
        self.assertEqual({"advanced": True, "updates": False}, state.expanded_sections)

        reopened = self._loaded_window()
        self.assertTrue(reopened.settings_page._sections["advanced"].expanded)
        self.assertFalse(reopened.settings_page._sections["updates"].expanded)

    def test_ctrl_click_switch_is_saved_and_explained(self):
        from roseblox.backend.setup import SetupStore

        window = self._loaded_window()
        settings = window.settings_page
        settings._show_rosemary_input_status(True, settings._input_check_generation)
        self.assertTrue(settings.input_switch.get_active())
        self.assertTrue(settings.input_switch.get_sensitive())
        self.assertIn("On.", settings.rosemary_input_row.get_subtitle())

        settings.input_switch.set_active(False)
        store = SetupStore(self.config_home / "roseblox" / "setup.json")
        self.assertFalse(store.load().plain_ctrl_click)
        self.assertFalse(window.setup_state.plain_ctrl_click)
        self.assertIn("Off.", settings.rosemary_input_row.get_subtitle())

        # A refresh keeps the saved choice instead of switching it back on.
        window._apply_rosemary_status(window.rosemary_status)
        settings._show_rosemary_input_status(True, settings._input_check_generation)
        self.assertFalse(settings.input_switch.get_active())

        settings.input_switch.set_active(True)
        settings._show_rosemary_input_status(False, settings._input_check_generation)
        self.assertFalse(settings.input_switch.get_sensitive())
        self.assertFalse(settings.input_switch.get_active())
        self.assertIn("Not supported", settings.rosemary_input_row.get_subtitle())
        self.assertTrue(store.load().plain_ctrl_click)
        settings._show_rosemary_input_status(True, settings._input_check_generation)
        self.assertTrue(settings.input_switch.get_active())

    def test_appearance_choices_are_saved_and_applied(self):
        from roseblox.backend.setup import ACCENT_CHOICES, COLOR_STYLES, COLOR_THEMES, Appearance, SetupStore

        window = self._loaded_window()
        theme = Mock()
        with patch.object(window, "get_application", return_value=SimpleNamespace(theme=theme)):
            settings = window.settings_page
            settings.style_row.set_selected(COLOR_STYLES.index("light"))
            settings.accent_row.set_selected(ACCENT_CHOICES.index("teal"))
            settings.colors_row.set_selected(COLOR_THEMES.index("system"))
        expected = Appearance("system", "light", "teal")
        theme.apply.assert_called_with(expected)
        self.assertEqual(expected, SetupStore(self.config_home / "roseblox" / "setup.json").load().appearance)
        self.assertEqual(expected, window.setup_state.appearance)
        self.assertEqual("Your desktop’s standard window colors.", settings.colors_row.get_subtitle())

        reopened = self._loaded_window()
        rows = reopened.settings_page
        self.assertEqual("system", COLOR_THEMES[rows.colors_row.get_selected()])
        self.assertEqual("light", COLOR_STYLES[rows.style_row.get_selected()])
        self.assertEqual("teal", ACCENT_CHOICES[rows.accent_row.get_selected()])

    def _synchronous_center_checks(self):
        def run_now(target, args=(), daemon=True):
            return SimpleNamespace(start=lambda: target(*args))

        self.enterContext(patch("roseblox.pages.notification_center.threading.Thread", run_now))
        self.enterContext(
            patch("roseblox.pages.notification_center.GLib.idle_add", lambda function, *arguments: function(*arguments))
        )

    def test_notification_center_offers_new_roseblox_versions(self):
        from roseblox.backend import self_update

        self._synchronous_center_checks()
        self.latest_roseblox = Release(
            "99.0.0", "https://github.com/MadnessMyth/RoseBlox/releases/tag/v99.0.0",
            "https://github.com/a.AppImage", 10, "https://github.com/SHA256SUMS",
        )
        window = self._loaded_window()
        center = window.notification_center
        self.assertTrue(center.roseblox_box.get_visible())
        row = center.roseblox_list.get_row_at_index(0)
        self.assertEqual("Roseblox 99.0.0 is available", row.title_label.get_text())

        center._roseblox_dismiss_clicked(None)
        self.assertFalse(center.roseblox_box.get_visible())
        center.check_roseblox()
        self.assertFalse(center.roseblox_box.get_visible())

        center._roseblox_dismissed = None
        center.install_kind = self_update.INSTALL_APPIMAGE
        center.check_roseblox()
        with patch.object(self_update, "appimage_path", return_value="/tmp/Roseblox.AppImage"), patch.object(
            self_update, "update_appimage", side_effect=lambda release, target, progress: progress(5, 10)
        ) as update:
            center._roseblox_update_clicked(None)
        update.assert_called_once()
        row = center.roseblox_list.get_row_at_index(0)
        self.assertEqual("Roseblox 99.0.0 is ready", row.title_label.get_text())
        application = Mock()
        with patch.object(window, "get_application", return_value=application):
            center._roseblox_restart_clicked(None)
        application.restart.assert_called_once_with()

    def test_roseblox_checks_follow_the_setting(self):
        self._synchronous_center_checks()
        self.latest_roseblox = Release("99.0.0", "https://github.com/MadnessMyth/RoseBlox/releases/tag/v99.0.0")
        window = self._loaded_window()
        center = window.notification_center
        center.roseblox_release = None
        window.settings_page.roseblox_updates_row.set_active(False)
        self.assertFalse(window.setup_state.roseblox_update_checks)
        center._roseblox_checked_at = 0
        center.check_roseblox(automatic=True)
        self.assertIsNone(center.roseblox_release)
        center.check_roseblox()
        self.assertIsNotNone(center.roseblox_release)

    def test_notification_center_follows_downloads(self):
        window = self._loaded_window()
        center = window.notification_center
        self.assertTrue(center.empty_label.get_visible())
        self.assertFalse(center.badge.get_visible())
        controller = window.pages["player"]

        _notify(controller, "player", OperationState(True, "Downloaded 1.0 MiB of 4.0 MiB", 0.25, True))
        self.assertTrue(center.downloads_box.get_visible())
        self.assertFalse(center.empty_label.get_visible())
        self.assertEqual("1", center.badge.get_text())
        row = center.downloads_list.get_row_at_index(0)
        self.assertEqual("Installing Roblox Player", row.title_label.get_text())
        self.assertEqual("Downloaded 1.0 MiB of 4.0 MiB", row.message_label.get_text())
        with patch.object(controller, "cancel_operation") as cancel_operation:
            center._cancel_clicked(None, "player")
        cancel_operation.assert_called_once_with()

        _notify(controller, "player", OperationState(False, "Installation failed: offline", None, False))
        self.assertFalse(center.downloads_box.get_visible())
        self.assertEqual("failed", center.recent[0].outcome)
        self.assertEqual("1", center.badge.get_text())
        center.popover.popup()
        self.assertFalse(center.badge.get_visible())
        center.popover.popdown()

        # Updates to a state that never started (a plain refresh) do not add history.
        _notify(controller, "player", OperationState(False, "", None, False))
        self.assertEqual(1, len(center.recent))
        center.clear_button.emit("clicked")
        self.assertEqual([], center.recent)
        self.assertTrue(center.empty_label.get_visible())

    def test_download_bar_shows_speed_and_time_left(self):
        from roseblox.backend.installer import InstallProgress
        from roseblox.backend.transfer_rate import TransferRate

        window = self._loaded_window()
        controller = window.pages["player"]
        clock = SimpleNamespace(now=10.0)
        controller._transfer_rate = TransferRate(clock=lambda: clock.now)
        controller._begin_operation("Checking the latest version…", cancellable=True)
        mib = 1024 * 1024
        controller._show_progress(InstallProgress("downloading", 0, 100 * mib))
        self.assertEqual("0%", controller.progress_bar.get_text())
        clock.now += 2.0
        controller._show_progress(InstallProgress("downloading", 4 * mib, 100 * mib))
        expected = "4% · 2.0 MiB/s · 48 s left"
        self.assertEqual(expected, controller.progress_bar.get_text())

        row = window.notification_center.downloads_list.get_row_at_index(0)
        self.assertEqual(expected, row.progress_bar.get_text())
        setup_row = window.onboarding_page._app_widgets["player"][0]
        self.assertTrue(setup_row.get_subtitle().endswith(expected))

        controller._show_progress(InstallProgress("extracting"))
        self.assertEqual("", controller.progress_bar.get_text())

    def test_notification_center_offers_updates(self):
        from roseblox.backend.setup import SetupStore
        from roseblox.backend.updates import AvailableUpdate

        self._synchronous_center_checks()
        store = SetupStore(self.config_home / "roseblox" / "setup.json")
        store.save_update_checks(True)
        update = AvailableUpdate("player", "version-1111111111111111", "version-0123456789abcdef")
        checks = []

        def checker(skipped):
            checks.append(dict(skipped))
            if skipped.get("player") == update.version:
                return [], {}
            return [update], {}

        with patch("roseblox.pages.notification_center.check_for_updates", lambda *_a: checker(_a[-1])):
            window = self._loaded_window()
            center = window.notification_center
            self.assertEqual([{}], checks)
            self.assertEqual([update], center.updates)
            self.assertEqual("1", center.badge.get_text())
            row = center.updates_list.get_row_at_index(0)
            self.assertEqual("Roblox Player update", row.title_label.get_text())
            self.assertIn("You have: version-0123456789abcdef", row.message_label.get_text())

            with patch.object(window, "install_update") as install_update:
                center._update_clicked(None, "player")
            install_update.assert_called_once_with("player")

            center._skip_clicked(None, update)
            self.assertEqual({"player": update.version}, store.load().skipped_versions)
            self.assertEqual([], center.updates)
            self.assertIn(update.version, window.settings_page.skipped_row.get_subtitle())

            window.settings_page.ask_again_button.emit("clicked")
            self.assertEqual({}, checks[-1])
            self.assertEqual([update], center.updates)

            # A failed lookup keeps showing the update that was already found.
            with patch.object(center, "_checker", lambda _skipped: ([], {"player": "offline"})):
                center.check_updates()
            self.assertEqual([update], center.updates)
            self.assertIn("offline", center.status_label.get_text())

    def test_notification_center_only_checks_automatically_when_enabled(self):
        self._synchronous_center_checks()
        checks = []
        with patch("roseblox.pages.notification_center.check_for_updates", lambda *_a: checks.append(1) or ([], {})):
            window = self._loaded_window()
            self.assertEqual([], checks)
            window.notification_center.check_button.emit("clicked")
            self.assertEqual([1], checks)

    def test_update_from_notification_installs_and_becomes_preferred(self):
        from roseblox.backend.installer import InstallResult

        records = _write_library(self.data_home)
        window = self._build_window()
        page = window.pages["player"]
        with patch.object(page, "_on_download_clicked") as download:
            window.install_update("player")
            self.assertTrue(self._wait_until(lambda: download.called))
        self.assertEqual("player", window._stack.get_visible_child_name())
        self.assertTrue(page._make_preferred_after_install)

        older = next(item for item in window.library_records if item.app_type == "player")
        window.preferences_store.set_preferred("player", older.id, window.library)
        bundle = self.data_home / "roseblox" / "installations" / "player-version-1111111111111111" / "RobloxPlayer.app"
        (bundle / "Contents").mkdir(parents=True)
        newer = window.library.add(
            "player", bundle, "Roblox Player (new)", version="version-1111111111111111", source="download"
        )
        page._busy = True
        page._operation_finished(InstallResult(newer, None), None)
        self.assertTrue(self._wait_until(lambda: window.preferences_store.load().player_id == newer.id))
        self.assertIn("It is now the preferred installation.", page.workflow_status.get_text())
        self.assertFalse(page._make_preferred_after_install)

        # A normal "Download and Install Latest" leaves the preferred installation alone.
        page._make_preferred_after_install = False
        page._busy = True
        page._operation_finished(InstallResult(older, None, already_installed=True), None)
        self._wait_until(lambda: False, timeout=0.3)
        self.assertEqual(newer.id, window.preferences_store.load().player_id)
        self.assertTrue(records)

    def _fake_update_checker(self, window):
        from roseblox.backend.updates import UpdateCheckerError, UpdateCheckerStatus

        store = window.setup_store
        checker = Mock()

        def status():
            enabled = store.load().update_checks_enabled
            return UpdateCheckerStatus(enabled, "On." if enabled else "Off.")

        def enable():
            store.save_update_checks(True)
            return status()

        def disable():
            if checker.disk_full:
                raise UpdateCheckerError("disk full")
            store.save_update_checks(False)
            return status()

        checker.disk_full = False
        checker.status.side_effect = status
        checker.enable.side_effect = enable
        checker.disable.side_effect = disable
        window.update_checker = checker
        return checker

    def _toggle(self, row, active):
        row.switch.set_active(active)
        self.assertTrue(self._wait_until(lambda: not row.busy and row.switch.get_active() == active or (not row.busy and row._message)))

    def test_update_switches_are_per_app_and_stay_in_sync(self):
        window = self._loaded_window()
        settings, setup = window.settings_page.update_rows, window.onboarding_page.update_rows
        self.assertEqual("Notify me about Roblox Player updates", settings["player"].get_title())
        self.assertEqual("Notify me about Roblox Studio updates", settings["studio"].get_title())
        self.assertFalse(settings["player"].switch.get_active())
        self.assertEqual("Optional", window.onboarding_page.updates_badge.get_text())
        self.assertFalse(window.settings_page.check_now_row.get_visible())
        checker = self._fake_update_checker(window)

        self._toggle(setup["player"], True)
        self.assertTrue(self._wait_until(lambda: settings["player"].switch.get_active()))
        # The drawn state must follow too, or the switch shows a grey track while on.
        self.assertTrue(settings["player"].switch.get_state())
        self.assertFalse(settings["studio"].switch.get_active())
        self.assertFalse(setup["studio"].switch.get_active())
        checker.enable.assert_called_once_with()
        self.assertEqual({"player": True, "studio": False}, window.setup_store.load().update_apps)
        self.assertTrue(window.settings_page.check_now_row.get_visible())

        self._toggle(settings["studio"], True)
        self.assertTrue(setup["studio"].switch.get_active())
        checker.enable.assert_called_once_with()

        self._toggle(settings["player"], False)
        self.assertFalse(setup["player"].switch.get_active())
        self.assertTrue(settings["studio"].switch.get_active())
        checker.disable.assert_not_called()
        self.assertTrue(window.setup_store.load().update_checks_enabled)

        checker.disk_full = True
        settings["studio"].switch.set_active(False)
        self.assertTrue(self._wait_until(lambda: not settings["studio"].busy))
        self.assertTrue(settings["studio"].switch.get_active())
        self.assertEqual("disk full", settings["studio"].get_subtitle())
        window.refresh_pages(reload_library=False, reload_preferences=True)
        self.assertTrue(self._wait_until(lambda: not window._refresh_in_progress))
        self.assertEqual("disk full", settings["studio"].get_subtitle())

        checker.disk_full = False
        self._toggle(settings["studio"], False)
        checker.disable.assert_called()
        self.assertFalse(window.setup_store.load().update_checks_enabled)
        self.assertFalse(window.settings_page.check_now_row.get_visible())

    def test_auto_update_switches_show_under_their_app_and_save(self):
        window = self._loaded_window()
        player, studio = window.settings_page.auto_update_rows.values()
        setup_player = window.onboarding_page.auto_update_rows["player"]
        self.assertEqual("Automatically update Roblox Player in the background", player.get_title())
        self.assertEqual("Automatically update Roblox Studio in the background", studio.get_title())
        self.assertFalse(player.get_visible())
        self.assertFalse(studio.get_visible())

        self._fake_update_checker(window)
        self._toggle(window.settings_page.update_rows["player"], True)
        self.assertTrue(player.get_visible())
        self.assertFalse(studio.get_visible())
        self.assertFalse(player.get_active())
        player.set_active(True)
        saved = window.setup_store.load()
        self.assertEqual({"player": True}, saved.auto_updates)
        self.assertTrue(saved.auto_update("player"))
        self.assertTrue(setup_player.get_active())
        self.assertFalse(saved.auto_update("studio"))

    def test_skipped_versions_can_be_asked_again(self):
        from roseblox.backend.setup import SetupStore

        store = SetupStore(self.config_home / "roseblox" / "setup.json")
        store.skip_version("player", "version-0123456789abcdef")
        window = self._loaded_window()
        settings = window.settings_page
        self.assertIn("Roblox Player version-0123456789abcdef", settings.skipped_row.get_subtitle())
        self.assertTrue(settings.ask_again_button.get_sensitive())
        log = Path(self.temporary.name) / "state" / "roseblox" / "update-notifications.json"
        log.parent.mkdir(parents=True)
        log.write_text("{}", encoding="utf-8")
        settings.ask_again_button.emit("clicked")
        self.assertEqual({}, store.load().skipped_versions)
        self.assertFalse(log.exists())
        self.assertEqual("None", settings.skipped_row.get_subtitle())

    def test_launches_report_whether_they_came_from_a_link(self):
        records = _write_library(self.data_home)
        window = self._loaded_window()
        page = window.pages["player"]
        record = next(item for item in window.library_records if item.app_type == "player")
        session = SimpleNamespace(process=SimpleNamespace(poll=lambda: None))
        for guest_arguments, from_link in (((), False), (("roblox://placeId=1",), True)):
            with self.subTest(from_link=from_link), patch(
                "roseblox.pages.manager_page.launch_application", return_value=session
            ) as launch, patch("roseblox.pages.manager_page.wait_for_running"), patch(
                "roseblox.pages.manager_page.threading.Thread"
            ), patch.object(window, "application_started") as started:
                page._launching_ids.add(record.id)
                page._launch_worker(record.id, record, guest_arguments)
                self.assertTrue(self._wait_until(lambda: started.called))
                started.assert_called_once_with("player", session, from_link)
                self.assertEqual(guest_arguments, launch.call_args.kwargs["guest_arguments"])
                page._sessions.pop(record.id)
        self.assertTrue(records)


class EntryPointTests(unittest.TestCase):
    def test_links_are_accepted_bare_or_after_the_option(self):
        from roseblox.command_line import roblox_url_from_arguments

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

        from roseblox.application import RosebloxApplication

        argv = ["roseblox", "--roblox-url", "roblox://placeId=1"]
        with patch.object(entry_point.sys, "argv", argv), patch.object(
            RosebloxApplication, "__init__", return_value=None
        ), patch.object(RosebloxApplication, "run", return_value=0) as run:
            self.assertEqual(0, entry_point.main())
        run.assert_called_once_with(argv)

    def test_update_service_runs_without_the_window(self):
        from roseblox import __main__ as entry_point

        argv = ["roseblox", "--update-service", "--check-now"]
        with patch.object(entry_point.sys, "argv", argv), patch(
            "roseblox.update_service.main", return_value=0
        ) as service:
            self.assertEqual(0, entry_point.main())
        service.assert_called_once_with(argv)

    def test_notification_commands_are_parsed(self):
        from roseblox.command_line import WindowRequest, parse_window_arguments

        self.assertEqual(WindowRequest(install_update="studio"), parse_window_arguments(["--install-update", "studio"]))
        self.assertEqual(WindowRequest(show="player"), parse_window_arguments(["--show", "player"]))
        self.assertEqual(WindowRequest(), parse_window_arguments([]))
        for arguments in (["--install-update"], ["--install-update", "phone"], ["--show", "player", "x"]):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(ValueError, "^Usage"):
                parse_window_arguments(arguments)


if __name__ == "__main__":
    unittest.main()
