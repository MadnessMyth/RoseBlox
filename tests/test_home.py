import threading
import unittest
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch

from roseblox.backend.library import BundleValidationError, InstallationRecord
from roseblox.pages.home import selected_installation_index
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
        window = SimpleNamespace(library=library, installer=installer)
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
                    args=(window, True, True),
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
        self.assertEqual(records, args[1])
        self.assertEqual({"player-one": "Valid", "studio-one": "Valid"}, args[2])
        self.assertEqual(jobs, args[5])

    def test_invalid_bundle_is_reported_without_failing_the_snapshot(self):
        records = [installation("missing")]
        window = SimpleNamespace(
            library=SimpleNamespace(load=Mock(return_value=records)),
            installer=SimpleNamespace(recoverable_downloads=Mock(return_value=[])),
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
                RosebloxWindow._load_shared_state_worker(window, True, False)
        self.assertEqual("Missing or invalid", dispatched[0][1][2]["missing"])

    def test_refresh_requests_are_coalesced_while_a_snapshot_is_loading(self):
        window = SimpleNamespace(
            _refresh_pending_library=False,
            _refresh_pending_recovery=False,
            _refresh_in_progress=True,
            _apply_shared_state=Mock(),
            _start_pending_refresh=Mock(),
        )
        RosebloxWindow.refresh_pages(window, reload_library=True, reload_recovery=True)
        self.assertTrue(window._refresh_pending_library)
        self.assertTrue(window._refresh_pending_recovery)
        window._start_pending_refresh.assert_not_called()
        window._apply_shared_state.assert_not_called()


if __name__ == "__main__":
    unittest.main()
