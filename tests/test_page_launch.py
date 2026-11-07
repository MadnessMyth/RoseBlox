import unittest
import tempfile
from types import MethodType, SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

from roseblox.backend.library import LibraryStore
from roseblox.backend.preferences import PreferencesStore
from roseblox.backend.version_settings import VersionSource, default_version_source
from roseblox.pages.home import _LaunchCard
from roseblox.backend.installer import InstallResult
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
        records = [
            SimpleNamespace(id="a", app_type="player", version="v1", display_name="Roblox Player v1"),
            SimpleNamespace(id="b", app_type="player", version="v0", display_name="Old Player"),
            SimpleNamespace(id="c", app_type="player", version="v1", display_name="Roblox Player v1 (2)"),
            SimpleNamespace(id="s", app_type="studio", version="v1", display_name="Studio"),
        ]
        page = SimpleNamespace(
            window=SimpleNamespace(
                setup_state=SimpleNamespace(multiple_players=allowed), library_records=records
            ),
            _sessions={"a": object()},
            _launching_ids=set(),
            _stopping_ids=set(),
            _validation_by_id={"a": "Valid", "b": "Valid", "c": "Valid", "s": "Valid"},
            _refresh_all=Mock(),
            _show_error=Mock(),
            _launch_worker=Mock(),
        )
        for name in ("_active", "_multiple_allowed", "_idle_copy", "request_launch_url"):
            setattr(page, name, MethodType(getattr(InstallationPage, name), page))
        return page, records

    def _open_link(self, page, record):
        started = []
        with patch(
            "roseblox.pages.manager_page.threading.Thread",
            side_effect=lambda target, args, daemon: SimpleNamespace(start=lambda: started.append(args)),
        ):
            page.request_launch_url(record, "roblox://placeId=1")
        return started

    def test_a_link_opens_in_a_copy_that_is_not_running_same_version_first(self):
        page, records = self._page()
        started = self._open_link(page, records[0])
        self.assertEqual([("c", records[2], ("roblox://placeId=1",))], started)
        self.assertIn("c", page._launching_ids)
        page._show_error.assert_not_called()

    def test_running_copies_and_unavailable_ones_are_skipped(self):
        page, records = self._page()
        page._launching_ids.add("c")
        page._validation_by_id["b"] = "Missing"
        self.assertEqual([], self._open_link(page, records[0]))
        self.assertEqual("Every Roblox Player is already running", page._show_error.call_args.args[0])

    def test_without_the_setting_a_link_is_refused_while_player_runs(self):
        page, records = self._page(allowed=False)
        self.assertEqual([], self._open_link(page, records[0]))
        self.assertEqual("Application already active", page._show_error.call_args.args[0])
        self.assertFalse(page._multiple_allowed(records[3]))
        page.window.setup_state.multiple_players = True
        self.assertFalse(page._multiple_allowed(records[3]))
        self.assertTrue(page._multiple_allowed(records[0]))

    def test_duplicate_runs_in_the_background_and_reports_the_copy(self):
        record = SimpleNamespace(id="a", app_type="player", display_name="Roblox Player v1")
        copy = SimpleNamespace(display_name="Roblox Player v1 (2)")
        installer = SimpleNamespace(
            duplicate=Mock(return_value=InstallResult(copy, None, copied_from="Roblox Player v1"))
        )
        finished = []
        page = SimpleNamespace(
            _busy=False,
            installer=installer,
            cancel_button=Mock(),
            _begin_operation=Mock(),
            _refresh_all=Mock(),
            _copying_ids=set(),
            _post_progress=object(),
            _operation_finished=lambda result, error: finished.append((result, error, set(page._copying_ids))),
        )
        page._duplicate_worker = MethodType(InstallationPage._duplicate_worker, page)
        page._duplicate_finished = MethodType(InstallationPage._duplicate_finished, page)
        installer.duplicate.side_effect = lambda *_args: (
            self.assertEqual({"a"}, page._copying_ids) or installer.duplicate.return_value
        )
        with patch(
            "roseblox.pages.manager_page.threading.Thread",
            side_effect=lambda target, args, daemon: SimpleNamespace(start=lambda: target(*args)),
        ), patch("roseblox.pages.manager_page.GLib.idle_add", side_effect=lambda callback, *args: callback(*args)):
            InstallationPage.start_duplicate(page, record, "Roblox Player v1 (2)")
        installer.duplicate.assert_called_once_with(
            record, "Roblox Player v1 (2)", page._cancel_event, page._post_progress
        )
        page.cancel_button.set_label.assert_called_with("Cancel Copy")
        self.assertEqual("Copy failed", page._operation_failure_title)
        self.assertEqual([(installer.duplicate.return_value, None, set())], finished)

    def test_a_failed_copy_is_reported_and_unblocks_remove(self):
        record = SimpleNamespace(id="a", app_type="player", display_name="Roblox Player v1")
        failure = RuntimeError("disk full")
        finished = []
        page = SimpleNamespace(
            installer=SimpleNamespace(duplicate=Mock(side_effect=failure)),
            _copying_ids={"a"},
            _post_progress=None,
            _operation_finished=lambda result, error: finished.append((result, error)),
        )
        page._duplicate_finished = MethodType(InstallationPage._duplicate_finished, page)
        with patch("roseblox.pages.manager_page.GLib.idle_add", side_effect=lambda callback, *args: callback(*args)):
            InstallationPage._duplicate_worker(page, record, "Copy", None)
        self.assertEqual([(None, failure)], finished)
        self.assertEqual(set(), page._copying_ids)


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
