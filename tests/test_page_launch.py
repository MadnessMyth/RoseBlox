import unittest
import tempfile
from types import MethodType, SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

from roseblox.backend.library import LibraryStore
from roseblox.backend.preferences import PreferencesStore
from roseblox.backend.version_settings import VersionSource, default_version_source
from roseblox.pages.home import _LaunchCard
from roseblox.pages.manager_page import InstallationPage, SettingsPage


class LaunchPageWorkerTests(unittest.TestCase):
    def test_unexpected_launch_error_is_dispatched_to_failure_handler(self):
        page = SimpleNamespace(
            rosemary_status=object(),
            window=SimpleNamespace(),
            _launching_ids={"install-id"},
            _refresh_all=Mock(),
            _show_error=Mock(),
        )
        page._launch_failed = MethodType(InstallationPage._launch_failed, page)
        record = SimpleNamespace(app_type="player")
        dispatched = []

        with patch(
            "roseblox.pages.manager_page.launch_application",
            side_effect=TypeError("unexpected launch backend error"),
        ), patch("roseblox.pages.manager_page.LaunchConfigStore") as store:
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
        store.return_value.load.assert_called_once_with("player")
        callback(*args)
        self.assertNotIn("install-id", page._launching_ids)
        page._show_error.assert_called_once()

    def test_unreadable_launch_configuration_fails_the_launch_with_its_message(self):
        page = SimpleNamespace(
            rosemary_status=object(),
            window=SimpleNamespace(),
            _launching_ids={"install-id"},
            _refresh_all=Mock(),
            _show_error=Mock(),
        )
        page._launch_failed = MethodType(InstallationPage._launch_failed, page)
        dispatched = []
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "roseblox" / "launch-config.json"
            config.parent.mkdir()
            config.write_text("{broken")
            with patch.dict("os.environ", {"XDG_CONFIG_HOME": directory}), patch(
                "roseblox.pages.manager_page.launch_application"
            ) as launch, patch(
                "roseblox.pages.manager_page.GLib.idle_add",
                side_effect=lambda callback, *args: dispatched.append((callback, args)),
            ):
                InstallationPage._launch_worker(page, "install-id", SimpleNamespace(app_type="player"))
        launch.assert_not_called()
        self.assertEqual("_launch_failed", dispatched[0][0].__name__)
        self.assertIn("Restore a backup", dispatched[0][1][1])


class OpenPlaceTests(unittest.TestCase):
    def _page(self):
        page = SimpleNamespace(
            _sessions={}, _launching_ids=set(), _stopping_ids=set(), _refresh_all=Mock(), _show_error=Mock(),
            _launch_worker=Mock(), _launch_failed=Mock(),
        )
        page._open_place_worker = MethodType(InstallationPage._open_place_worker, page)
        return page

    def test_a_place_file_opens_in_studio_with_its_guest_path(self):
        page = self._page()
        record = SimpleNamespace(id="studio-id", app_type="studio")
        with tempfile.TemporaryDirectory() as directory:
            place = Path(directory) / "My Game.rbxl"
            place.write_bytes(b"<roblox!")
            with patch.dict("os.environ", {"XDG_CONFIG_HOME": directory, "XDG_DATA_HOME": directory + "/data"}), patch(
                "roseblox.pages.manager_page.threading.Thread",
                side_effect=lambda target, args, daemon: SimpleNamespace(start=lambda: target(*args)),
            ):
                InstallationPage.request_open_place(page, record, place)
        page._launch_worker.assert_called_once_with(
            "studio-id", record, ("-task", "EditFile", "-localPlaceFile", f"/Volumes/Host{place}"), from_link=False
        )
        self.assertIn("studio-id", page._launching_ids)

    def test_other_files_and_busy_installations_are_refused(self):
        page = self._page()
        record = SimpleNamespace(id="studio-id", app_type="studio")
        with tempfile.TemporaryDirectory() as directory:
            other = Path(directory) / "notes.txt"
            other.write_text("x")
            InstallationPage.request_open_place(page, record, other)
            self.assertEqual("Not a place file", page._show_error.call_args.args[0])
            page._sessions["studio-id"] = object()
            InstallationPage.request_open_place(page, record, other)
            self.assertEqual("Studio is already open", page._show_error.call_args.args[0])
        page._launch_worker.assert_not_called()


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


class MultiplePlayerTests(unittest.TestCase):
    def _page(self, allowed=True):
        primary = SimpleNamespace(process=SimpleNamespace(poll=lambda: None, pid=1))
        page = SimpleNamespace(
            window=SimpleNamespace(setup_state=SimpleNamespace(multiple_players=allowed, plain_ctrl_click=True)),
            rosemary_status=object(),
            _sessions={"p": primary},
            _extra_sessions={},
            _launching_ids=set(),
            _stopping_ids=set(),
            _refresh_all=Mock(),
            _show_error=Mock(),
            _start_activity_watcher=Mock(),
            _launch_worker=Mock(),
        )
        for name in (
            "launch_another", "_extra_launch_worker", "_extra_ready", "_extra_failed", "_replace_extra",
            "_extra_exited", "_extra_watch_worker", "_session_exited", "_multiple_allowed", "request_launch_url",
            "launch_button_state",
        ):
            setattr(page, name, MethodType(getattr(InstallationPage, name), page))
        return page, primary

    def _run(self, page, record, sessions, arguments=()):
        launched = []

        def fake_launch(_record, _status, **kwargs):
            launched.append(kwargs)
            return sessions.pop(0)

        with patch("roseblox.pages.manager_page.launch_application", side_effect=fake_launch), patch(
            "roseblox.pages.manager_page.wait_for_running"
        ), patch("roseblox.pages.manager_page.LaunchConfigStore"), patch(
            "roseblox.pages.manager_page.threading.Thread",
            side_effect=lambda target, args, daemon: SimpleNamespace(start=lambda: None if target.__name__ == "_extra_watch_worker" else target(*args)),
        ), patch("roseblox.pages.manager_page.GLib.idle_add", side_effect=lambda callback, *args: callback(*args)):
            if arguments:
                page.request_launch_url(record, arguments[0])
            else:
                page.launch_another(record)
        return launched

    def test_extra_players_get_their_own_logs_and_stop_all(self):
        page, primary = self._page()
        record = SimpleNamespace(id="p", app_type="player")
        second = SimpleNamespace(process=SimpleNamespace(poll=lambda: None, pid=2))
        third = SimpleNamespace(process=SimpleNamespace(poll=lambda: None, pid=3))
        launched = self._run(page, record, [second])
        launched += self._run(page, record, [third], ("roblox://placeId=1",))
        self.assertEqual(["player-2", "player-3"], [kwargs["log_name"] for kwargs in launched])
        self.assertEqual(("roblox://placeId=1",), launched[1]["guest_arguments"])
        self.assertEqual([(2, second), (3, third)], page._extra_sessions["p"])
        self.assertEqual(("Stop All", True), page.launch_button_state("p"))
        page._launch_worker.assert_not_called()
        page._extra_exited("p", 2, second)
        self.assertEqual([(3, third)], page._extra_sessions["p"])
        page._session_exited("p", primary, 0, "")
        self.assertIs(third, page._sessions["p"])
        self.assertNotIn("p", page._extra_sessions)
        page._extra_exited("p", 3, third)
        self.assertNotIn("p", page._sessions)

    def test_extra_player_that_finishes_starting_during_stop_all_is_stopped(self):
        page, _primary = self._page()
        page._stop_worker = Mock()
        page._stopping_ids.add("p")
        late = SimpleNamespace(process=SimpleNamespace(poll=lambda: None, pid=4))
        page._extra_sessions["p"] = [(2, None)]
        stopped = []
        with patch(
            "roseblox.pages.manager_page.threading.Thread",
            side_effect=lambda target, args, daemon: SimpleNamespace(start=lambda: stopped.append(target)),
        ):
            InstallationPage._extra_ready(page, "p", 2, late)
        self.assertIn(page._stop_worker, stopped)
        page._start_activity_watcher.assert_not_called()

    def test_without_the_setting_a_second_launch_is_refused(self):
        page, _primary = self._page(allowed=False)
        record = SimpleNamespace(id="p", app_type="player")
        self.assertEqual([], self._run(page, record, [], ("roblox://placeId=1",)))
        self.assertEqual("Application already active", page._show_error.call_args.args[0])
        page.launch_another(record)
        self.assertEqual({}, page._extra_sessions)
        studio = SimpleNamespace(id="p", app_type="studio")
        page.window.setup_state.multiple_players = True
        page.launch_another(studio)
        self.assertEqual({}, page._extra_sessions)


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

    def test_preference_worker_validates_and_refreshes_shared_snapshot(self):
        record = SimpleNamespace(id="player-id", app_type="player")
        preferences = SimpleNamespace(set_preferred=Mock())
        store = object()
        page = SimpleNamespace(
            app_type="player",
            window=SimpleNamespace(preferences_store=preferences),
            store=store,
            _preferred_save_finished=MethodType(InstallationPage._preferred_save_finished, SimpleNamespace(
                _preference_busy=True,
                _refresh_all=Mock(),
                _show_error=Mock(),
                _render_records=Mock(),
                _current_records=Mock(return_value=[]),
            )),
        )
        dispatched = []
        with patch(
            "roseblox.pages.manager_page.GLib.idle_add",
            side_effect=lambda callback, *args: dispatched.append((callback, args)),
        ):
            InstallationPage._set_preferred_worker(page, record)
        preferences.set_preferred.assert_called_once_with("player", "player-id", store)
        self.assertEqual(1, len(dispatched))
        callback, args = dispatched[0]
        callback(*args)

    def test_preference_success_requests_preferences_refresh(self):
        page = SimpleNamespace(
            _preference_busy=True,
            _refresh_all=Mock(),
            _show_error=Mock(),
            _render_records=Mock(),
            _current_records=Mock(return_value=[]),
        )
        InstallationPage._preferred_save_finished(page, None)
        self.assertFalse(page._preference_busy)
        page._refresh_all.assert_called_once_with(reload_library=False, reload_preferences=True)

    def test_temporary_home_selection_does_not_write_preferences(self):
        record = SimpleNamespace(id="temporary", app_type="player")
        preferences = Mock()
        card = SimpleNamespace(
            _updating_model=False,
            dropdown=SimpleNamespace(get_selected=lambda: 0),
            installations=[record],
            selected_id=None,
            home=SimpleNamespace(window=SimpleNamespace(preferences_store=preferences)),
            _update_display=Mock(),
        )
        _LaunchCard._selection_changed(card, None, None)
        self.assertEqual("temporary", card.selected_id)
        preferences.assert_not_called()

    def test_removing_preferred_registration_clears_only_that_app_and_refreshes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = LibraryStore(root / "library.json")
            player_bundle = root / "player" / "RobloxPlayer.app"
            (player_bundle / "Contents").mkdir(parents=True)
            studio_bundle = root / "studio" / "RobloxStudio.app"
            (studio_bundle / "Contents").mkdir(parents=True)
            player = library.add("player", player_bundle, "Player")
            studio = library.add("studio", studio_bundle, "Studio")
            preferences = PreferencesStore(root / "preferences.json")
            preferences.set_preferred("player", player.id, library)
            preferences.set_preferred("studio", studio.id, library)
            refresh = Mock()
            page = SimpleNamespace(
                store=library,
                window=SimpleNamespace(preferences_store=preferences),
                _remove_failed=Mock(),
                _remove_finished=MethodType(
                    InstallationPage._remove_finished,
                    SimpleNamespace(
                        _refresh_all=refresh,
                        _show_error=Mock(),
                    ),
                ),
            )
            dispatched = []
            with patch(
                "roseblox.pages.manager_page.GLib.idle_add",
                side_effect=lambda callback, *args: dispatched.append((callback, args)),
            ):
                InstallationPage._remove_worker(page, player, False)
            self.assertEqual(1, len(dispatched))
            callback, args = dispatched[0]
            callback(*args)
            state = preferences.load()
            self.assertIsNone(state.player_id)
            self.assertEqual(studio.id, state.studio_id)
            self.assertEqual([studio], library.load())
            self.assertTrue(player_bundle.is_dir())
            refresh.assert_called_once_with(
                reload_library=True,
                reload_recovery=False,
                reload_preferences=True,
            )


if __name__ == "__main__":
    unittest.main()
