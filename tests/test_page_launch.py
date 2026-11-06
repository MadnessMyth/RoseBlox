import unittest
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch

from roseblox.backend.version_settings import VersionSource, default_version_source
from roseblox.pages.home import _LaunchCard
from roseblox.pages.manager_page import InstallationPage, SettingsPage


class LaunchPageWorkerTests(unittest.TestCase):
    def test_unexpected_launch_error_is_dispatched_to_failure_handler(self):
        page = SimpleNamespace(
            rosemary_status=object(),
            _launching_ids={"install-id"},
            _refresh_all=Mock(),
            _show_error=Mock(),
        )
        page._launch_failed = MethodType(InstallationPage._launch_failed, page)
        record = object()
        dispatched = []

        with patch(
            "roseblox.pages.manager_page.launch_application",
            side_effect=TypeError("unexpected launch backend error"),
        ):
            with patch(
                "roseblox.pages.manager_page.GLib.idle_add",
                side_effect=lambda callback, *args: dispatched.append((callback, args)),
            ):
                InstallationPage._launch_worker(page, "install-id", record)

        self.assertEqual(1, len(dispatched))
        callback, args = dispatched[0]
        self.assertEqual("_launch_failed", callback.__name__)
        self.assertEqual("install-id", args[0])
        self.assertIn("unexpected launch backend error", args[1])
        callback(*args)
        self.assertNotIn("install-id", page._launching_ids)
        page._show_error.assert_called_once()


class SettingsPageWorkerTests(unittest.TestCase):
    def test_per_app_invalid_saved_value_leaves_both_settings_controls_available(self):
        player_widgets = [Mock() for _ in range(6)]
        studio_widgets = [Mock() for _ in range(6)]
        page = SimpleNamespace(_source_widgets={"player": tuple(player_widgets), "studio": tuple(studio_widgets)})
        page._set_source_actions = MethodType(SettingsPage._set_source_actions, page)
        studio = VersionSource("https://studio.example/version", "/payload/0/version")

        SettingsPage._show_sources_loaded(
            page,
            {"studio": studio},
            {"player": "saved endpoint is invalid"},
            None,
        )

        self.assertEqual(default_version_source("player").endpoint, player_widgets[0].set_text.call_args.args[0])
        self.assertEqual(studio.endpoint, studio_widgets[0].set_text.call_args.args[0])
        self.assertIn("replace only this app's record", player_widgets[2].set_text.call_args.args[0])
        for widget in (*player_widgets[3:], *studio_widgets[3:]):
            widget.set_sensitive.assert_called_with(True)

    def test_unexpected_configuration_error_is_shown_and_all_controls_recover(self):
        actions = [Mock() for _ in range(3)]
        label = Mock()
        page = SimpleNamespace(
            version_client=SimpleNamespace(test_configuration=Mock(side_effect=RuntimeError("unexpected test failure"))),
            _source_widgets={"player": (None, None, label, *actions)},
        )
        page._set_source_actions = MethodType(SettingsPage._set_source_actions, page)
        page._show_source_test = MethodType(SettingsPage._show_source_test, page)
        dispatched = []
        with patch(
            "roseblox.pages.manager_page.GLib.idle_add",
            side_effect=lambda callback, *args: dispatched.append((callback, args)),
        ):
            SettingsPage._test_source_worker(
                page, "player", VersionSource("https://example.org/version"), label, actions[0]
            )

        self.assertEqual(1, len(dispatched))
        callback, args = dispatched[0]
        callback(*args)
        self.assertIn("Unexpected configuration test failure", label.set_text.call_args.args[0])
        for action in actions:
            action.set_sensitive.assert_called_with(True)


class HomeLaunchDelegationTests(unittest.TestCase):
    def test_home_delegates_to_existing_application_page_controller(self):
        record = object()
        controller = SimpleNamespace(request_launch_toggle=Mock())
        window = SimpleNamespace(pages={"player": controller})
        card = SimpleNamespace(
            home=SimpleNamespace(window=window),
            app_type="player",
            _selected_record=lambda: record,
        )
        _LaunchCard._launch_clicked(card, None)
        controller.request_launch_toggle.assert_called_once_with(record)

    def test_launch_state_uses_shared_lifecycle_collections(self):
        page = SimpleNamespace(
            _launching_ids={"launching"},
            _stopping_ids={"stopping"},
            _sessions={"running": object()},
        )
        self.assertEqual(("Launching...", False), InstallationPage.launch_button_state(page, "launching"))
        self.assertEqual(("Stopping…", False), InstallationPage.launch_button_state(page, "stopping"))
        self.assertEqual(("Stop", True), InstallationPage.launch_button_state(page, "running"))
        self.assertEqual(("Launch", True), InstallationPage.launch_button_state(page, "stopped"))


if __name__ == "__main__":
    unittest.main()
