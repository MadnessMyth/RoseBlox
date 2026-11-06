import threading
import unittest
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch

from roseblox.backend.library import BundleValidationError, InstallationRecord
from roseblox.backend.preferences import PreferredInstallations
from roseblox.pages.home import choose_home_installation_id, selected_installation_index
from roseblox.window import RosebloxWindow


def installation(installation_id, app_type="player"):
    return InstallationRecord(
        id=installation_id,
        display_name=installation_id.title(),
        app_type=app_type,
        bundle_path=f"/games/{installation_id}.app",
        registered_at="2026-11-06T00:00:00+00:00",
        validation_state="valid",
    )


class HomeSelectionTests(unittest.TestCase):
    def test_selection_is_preserved_and_falls_back_when_removed(self):
        records = [installation("one"), installation("two")]
        self.assertEqual(1, selected_installation_index(records, "two"))
        self.assertEqual(0, selected_installation_index(records, "missing"))
        self.assertEqual(0, selected_installation_index([], "two"))

    def test_startup_uses_preferred_valid_installation(self):
        records = [installation("one"), installation("two")]
        self.assertEqual(
            "two",
            choose_home_installation_id(
                records,
                {"one": "Valid", "two": "Valid"},
                current_id=None,
                resolved_preferred_id="two",
                preference_changed=True,
            ),
        )

    def test_temporary_selection_survives_refresh_without_changing_preference(self):
        records = [installation("one"), installation("two")]
        self.assertEqual(
            "one",
            choose_home_installation_id(
                records,
                {"one": "Valid", "two": "Valid"},
                current_id="one",
                resolved_preferred_id="two",
                preference_changed=False,
            ),
        )

    def test_removed_or_invalid_preference_uses_valid_fallback(self):
        records = [installation("broken"), installation("fallback")]
        self.assertEqual(
            "fallback",
            choose_home_installation_id(
                records,
                {"broken": "Missing or invalid", "fallback": "Valid"},
                current_id="broken",
                resolved_preferred_id="fallback",
                preference_changed=True,
            ),
        )
        self.assertIsNone(
            choose_home_installation_id(
                records,
                {"broken": "Missing or invalid", "fallback": "Missing or invalid"},
                current_id=None,
                resolved_preferred_id=None,
                preference_changed=True,
            )
        )

    def test_decorative_artwork_asset_is_packaged_locally(self):
        root = Path(__file__).resolve().parents[1]
        asset = root / "src" / "roseblox" / "resources" / "home-artwork-placeholder.svg"
        self.assertTrue(asset.is_file())
        self.assertIn("resources/*.svg", (root / "pyproject.toml").read_text(encoding="utf-8"))
        stylesheet = (root / "src" / "roseblox" / "resources" / "style.css").read_text(encoding="utf-8")
        self.assertIn(".home-dashboard", stylesheet)
        self.assertIn(".home-artwork-scrim", stylesheet)


class SharedSnapshotTests(unittest.TestCase):
    def test_library_validation_and_recovery_are_loaded_once_in_worker(self):
        records = [installation("player-one"), installation("studio-one", "studio")]
        jobs = [object(), object()]
        library = SimpleNamespace(load=Mock(return_value=records))
        installer = SimpleNamespace(recoverable_downloads=Mock(return_value=jobs))
        preferred = PreferredInstallations("player-one", "studio-one")
        preferences = SimpleNamespace(load=Mock(return_value=preferred))
        window = SimpleNamespace(
            library=library,
            installer=installer,
            preferences_store=preferences,
            library_records=[],
            library_validation={},
        )
        window._shared_state_loaded = MethodType(RosebloxWindow._shared_state_loaded, window)
        dispatched = []
        validation_threads = []

        def validate(_path, _app_type):
            validation_threads.append(threading.get_ident())

        with patch("roseblox.window.validate_bundle", side_effect=validate):
            with patch(
                "roseblox.window.GLib.idle_add",
                side_effect=lambda callback, *args: dispatched.append((callback, args)),
            ):
                worker = threading.Thread(
                    target=RosebloxWindow._load_shared_state_worker,
                    args=(window, True, True, True, 0),
                )
                worker.start()
                worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(1, library.load.call_count)
        self.assertEqual(1, installer.recoverable_downloads.call_count)
        self.assertEqual(2, len(validation_threads))
        self.assertTrue(all(thread_id != threading.get_ident() for thread_id in validation_threads))
        self.assertEqual(1, len(dispatched))
        _callback, args = dispatched[0]
        self.assertEqual(records, args[2])
        self.assertEqual({"player-one": "Valid", "studio-one": "Valid"}, args[3])
        self.assertEqual(jobs, args[6])
        self.assertEqual(preferred, args[9])
        self.assertEqual({"player": "player-one", "studio": "studio-one"}, args[10])

    def test_invalid_bundle_is_reported_without_failing_the_snapshot(self):
        records = [installation("missing")]
        window = SimpleNamespace(
            library=SimpleNamespace(load=Mock(return_value=records)),
            installer=SimpleNamespace(recoverable_downloads=Mock(return_value=[])),
            preferences_store=SimpleNamespace(load=Mock(return_value=PreferredInstallations())),
            library_records=[],
            library_validation={},
        )
        window._shared_state_loaded = MethodType(RosebloxWindow._shared_state_loaded, window)
        dispatched = []
        with patch(
            "roseblox.window.validate_bundle",
            side_effect=BundleValidationError("missing bundle"),
        ):
            with patch(
                "roseblox.window.GLib.idle_add",
                side_effect=lambda callback, *args: dispatched.append((callback, args)),
            ):
                RosebloxWindow._load_shared_state_worker(window, True, False, False, 0)
        self.assertEqual("Missing or invalid", dispatched[0][1][3]["missing"])

    def test_refresh_requests_are_coalesced_while_a_snapshot_is_loading(self):
        window = SimpleNamespace(
            _refresh_pending_library=False,
            _refresh_pending_recovery=False,
            _refresh_pending_preferences=False,
            _refresh_in_progress=True,
            _snapshot_generation=0,
            _apply_shared_state=Mock(),
            _start_pending_refresh=Mock(),
        )
        RosebloxWindow.refresh_pages(window, reload_library=True, reload_recovery=True)
        self.assertTrue(window._refresh_pending_library)
        self.assertTrue(window._refresh_pending_recovery)
        self.assertTrue(window._refresh_pending_preferences)
        self.assertEqual(1, window._snapshot_generation)
        window._start_pending_refresh.assert_not_called()
        window._apply_shared_state.assert_not_called()

    def test_stale_snapshot_is_discarded_and_requested_dimensions_are_reloaded(self):
        window = SimpleNamespace(
            _snapshot_generation=2,
            _refresh_pending_library=False,
            _refresh_pending_recovery=False,
            _refresh_pending_preferences=False,
            _refresh_in_progress=True,
            _start_pending_refresh=Mock(),
            _apply_shared_state=Mock(),
        )
        RosebloxWindow._shared_state_loaded(
            window,
            1,
            True,
            [],
            {},
            None,
            True,
            [],
            None,
            True,
            PreferredInstallations(),
            {"player": None, "studio": None},
            {},
        )
        self.assertTrue(window._refresh_pending_library)
        self.assertTrue(window._refresh_pending_recovery)
        self.assertTrue(window._refresh_pending_preferences)
        window._apply_shared_state.assert_not_called()
        window._start_pending_refresh.assert_called_once()

    def test_preferred_ids_are_shared_with_manager_and_home_consumers(self):
        page_manager = SimpleNamespace(apply_library_snapshot=Mock(), set_recovery_jobs=Mock())
        page_home = SimpleNamespace(apply_snapshot=Mock())
        window = SimpleNamespace(
            pages={"player": page_manager, "home": page_home},
            library_records=[installation("player-one")],
            library_validation={"player-one": "Valid"},
            library_error=None,
            library_loading=False,
            preferred_installations=PreferredInstallations("player-one", None),
            resolved_preferred_ids={"player": "player-one", "studio": None},
            preference_errors={},
            download_recovery_jobs=[],
            download_recovery_error=None,
        )
        RosebloxWindow._apply_shared_state(window)
        self.assertEqual(
            {"player": "player-one", "studio": None},
            page_manager.apply_library_snapshot.call_args.kwargs["preferred_ids"],
        )
        self.assertEqual(
            {"player": "player-one", "studio": None},
            page_home.apply_snapshot.call_args.kwargs["resolved_preferred_ids"],
        )


if __name__ == "__main__":
    unittest.main()
