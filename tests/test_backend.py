"""Backend tests that do not launch external applications."""

import json
import io
import os
import signal
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from roseblox.backend.library import (
    BundleValidationError,
    LibraryDataError,
    LibraryStore,
    validate_bundle,
)
from roseblox.backend.rosemary import (
    LaunchIdentityError,
    LaunchSession,
    ProcessIdentityError,
    ProcessSnapshot,
    RosemaryStatus,
    RosemaryUnavailableError,
    build_launch_arguments,
    detect_rosemary,
    launch_application,
    _drain_output,
    _matches_session,
    stop_application,
    wait_for_running,
)


class RosemaryTests(unittest.TestCase):
    def test_process_output_capture_is_bounded_and_drains_stream(self):
        payload = b"x" * (70 * 1024)
        captured = _drain_output(io.BytesIO(payload))
        self.assertLess(len(captured), 65 * 1024)
        self.assertIn(b"omitted middle", captured)

    def test_detects_rosemary_using_path_lookup(self):
        with patch("roseblox.backend.rosemary.shutil.which", return_value="/usr/bin/rosemary"):
            status = detect_rosemary(config_path=Path("/path/that/does/not/exist"))
        self.assertEqual(status.executable, "/usr/bin/rosemary")
        self.assertTrue(status.available)

    def test_missing_rosemary_is_reported(self):
        with patch("roseblox.backend.rosemary.shutil.which", return_value=None):
            status = detect_rosemary(config_path=Path("/path/that/does/not/exist"))
        self.assertFalse(status.available)
        self.assertIsNone(status.executable)

    def test_configured_rosemary_path_is_used(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "rosemary"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
            settings = root / "settings.json"
            settings.write_text(json.dumps({"rosemary_executable": str(executable)}), encoding="utf-8")
            with patch("roseblox.backend.rosemary.shutil.which") as which:
                status = detect_rosemary(settings)
        self.assertEqual(status.executable, str(executable))
        self.assertTrue(status.configured)
        which.assert_not_called()

    def test_launch_without_rosemary_fails_before_spawning(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = make_bundle(Path(directory), "player")
            store = LibraryStore(Path(directory) / "library.json")
            record = store.add("player", bundle, "Test Player")
            with patch("roseblox.backend.rosemary.subprocess.Popen") as popen:
                with self.assertRaises(RosemaryUnavailableError):
                    launch_application(record, RosemaryStatus(None))
        popen.assert_not_called()

    def test_running_requires_verified_bundle_identity(self):
        process = unittest.mock.Mock()
        process.poll.return_value = None
        session = LaunchSession(process, "/usr/bin/rosemary", "/apps/RobloxPlayer.app")
        with patch(
            "roseblox.backend.rosemary._matches_session",
            side_effect=[False, True],
        ):
            with patch("roseblox.backend.rosemary.time.sleep"):
                wait_for_running(session, timeout=1)

    def test_process_identity_binds_executable_bundle_and_pid_start_time(self):
        process = unittest.mock.Mock()
        process.pid = 1234
        process.poll.return_value = None
        session = LaunchSession(
            process,
            "/usr/bin/rosemary",
            "/apps/RobloxPlayer.app",
            start_time=987,
        )
        snapshot = ProcessSnapshot(
            "/usr/bin/rosemary",
            ("/usr/bin/rosemary", "/apps/RobloxPlayer.app/"),
            987,
        )
        with patch("roseblox.backend.rosemary.read_process_snapshot", return_value=snapshot):
            with patch("roseblox.backend.rosemary.os.path.samefile", return_value=True):
                self.assertTrue(_matches_session(session))

        wrong_bundle = ProcessSnapshot(
            "/usr/bin/rosemary",
            ("/usr/bin/rosemary", "/apps/Other.app/"),
            987,
        )
        with patch("roseblox.backend.rosemary.read_process_snapshot", return_value=wrong_bundle):
            with patch("roseblox.backend.rosemary.os.path.samefile", return_value=True):
                self.assertFalse(_matches_session(session))

        reused_pid = ProcessSnapshot(
            "/usr/bin/rosemary",
            ("/usr/bin/rosemary", "/apps/RobloxPlayer.app/"),
            988,
        )
        with patch("roseblox.backend.rosemary.read_process_snapshot", return_value=reused_pid):
            with patch("roseblox.backend.rosemary.os.path.samefile", return_value=True):
                self.assertFalse(_matches_session(session))

    def test_launch_that_exits_before_identity_is_confirmed_fails(self):
        process = unittest.mock.Mock()
        process.poll.return_value = 1
        done = threading.Event()
        done.set()
        session = LaunchSession(
            process,
            "/usr/bin/rosemary",
            "/apps/RobloxPlayer.app",
            stdout=b"",
            stderr=b"bundle failed",
            output_done=done,
        )
        with self.assertRaisesRegex(LaunchIdentityError, "bundle failed"):
            wait_for_running(session)

    def test_stop_signals_only_a_matching_process_gracefully(self):
        process = unittest.mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        session = LaunchSession(process, "/usr/bin/rosemary", "/apps/RobloxStudio.app", 42)
        with patch("roseblox.backend.rosemary._matches_session", return_value=True):
            stop_application(session, timeout=5)
        process.send_signal.assert_called_once_with(signal.SIGTERM)
        process.kill.assert_not_called()
        process.wait.assert_called_once_with(timeout=5)

    def test_stop_escalates_after_timeout_only_for_same_process(self):
        process = unittest.mock.Mock()
        process.poll.side_effect = [None, None, None, None]
        process.wait.side_effect = [subprocess.TimeoutExpired("rosemary", 1), 0]
        session = LaunchSession(process, "/usr/bin/rosemary", "/apps/RobloxPlayer.app", 42)
        with patch("roseblox.backend.rosemary._matches_session", return_value=True):
            stop_application(session, timeout=1)
        process.send_signal.assert_called_once_with(signal.SIGTERM)
        process.kill.assert_called_once_with()
        self.assertEqual(2, process.wait.call_count)

    def test_stop_refuses_a_process_with_mismatched_identity(self):
        process = unittest.mock.Mock()
        process.poll.return_value = None
        session = LaunchSession(process, "/usr/bin/rosemary", "/apps/RobloxPlayer.app", 42)
        with patch("roseblox.backend.rosemary._matches_session", return_value=False):
            with self.assertRaises(ProcessIdentityError):
                stop_application(session)
        process.send_signal.assert_not_called()
        process.kill.assert_not_called()


class LibraryTests(unittest.TestCase):
    def test_validates_player_and_studio_bundles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            player = make_bundle(root, "player")
            studio = make_bundle(root, "studio")
            self.assertEqual(validate_bundle(player, "player"), player)
            self.assertEqual(validate_bundle(studio, "studio"), studio)

    def test_rejects_wrong_or_incomplete_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrong_name = root / "Other.app"
            (wrong_name / "Contents").mkdir(parents=True)
            with self.assertRaises(BundleValidationError):
                validate_bundle(wrong_name, "player")
            incomplete = root / "RobloxPlayer.app"
            incomplete.mkdir()
            with self.assertRaises(BundleValidationError):
                validate_bundle(incomplete, "player")

    def test_registers_player_and_studio_and_persists_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = LibraryStore(root / "data" / "library.json")
            player = make_bundle(root, "player")
            studio = make_bundle(root, "studio")
            player_record = store.add("player", player, "Main Player")
            studio_record = store.add("studio", studio, "Creator Studio")

            loaded = LibraryStore(store.path).load()
            self.assertEqual([player_record, studio_record], loaded)
            self.assertEqual(str(player.absolute()), player_record.bundle_path)
            self.assertEqual("valid", player_record.validation_state)
            self.assertTrue(player_record.registered_at)

    def test_malformed_or_partially_invalid_data_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library_file = root / "library.json"
            original = b'{"installations": [{"id": "incomplete"}]}'
            library_file.write_bytes(original)
            store = LibraryStore(library_file)
            with self.assertRaises(LibraryDataError):
                store.load()
            bundle = make_bundle(root, "player")
            with self.assertRaises(LibraryDataError):
                store.add("player", bundle, "Player")
            self.assertEqual(original, library_file.read_bytes())

            library_file.write_text("{broken", encoding="utf-8")
            malformed = library_file.read_bytes()
            with self.assertRaises(LibraryDataError):
                store.load()
            self.assertEqual(malformed, library_file.read_bytes())

    def test_remove_only_removes_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = make_bundle(root, "player")
            marker = bundle / "Contents" / "keep.txt"
            marker.write_text("preserve", encoding="utf-8")
            store = LibraryStore(root / "library.json")
            record = store.add("player", bundle, "Player")

            self.assertTrue(store.remove(record.id))
            self.assertEqual([], store.load())
            self.assertTrue(marker.is_file())
            self.assertEqual("preserve", marker.read_text(encoding="utf-8"))

    def test_legacy_library_records_load_with_external_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = make_bundle(root, "player")
            library_file = root / "library.json"
            library_file.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "installations": [
                            {
                                "id": "legacy-id",
                                "display_name": "Old Player",
                                "app_type": "player",
                                "bundle_path": str(bundle),
                                "registered_at": "2026-01-01T00:00:00+00:00",
                                "validation_state": "valid",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            [record] = LibraryStore(library_file).load()
            self.assertEqual("legacy-id", record.id)
            self.assertIsNone(record.version)
            self.assertFalse(record.managed)
            self.assertEqual("existing_bundle", record.source)

    def test_managed_bundle_deletion_requires_flag_and_stays_within_managed_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            managed_root = root / "installations"
            bundle = make_bundle(managed_root / "player" / "version-test", "player")
            store = LibraryStore(root / "library.json")
            record = store.add("player", bundle, "Managed", managed=True, source="download")
            with patch("roseblox.backend.library.managed_install_root", return_value=managed_root):
                self.assertTrue(store.remove(record.id))
                self.assertTrue(bundle.is_dir())
                second = store.add("player", bundle, "Managed", managed=True, source="download")

                external_bundle = make_bundle(root / "external", "player")
                external = store.add("player", external_bundle, "External")
                with self.assertRaises(LibraryDataError):
                    store.remove(external.id, delete_managed_files=True)
                self.assertTrue(external_bundle.is_dir())

                # A valid explicit managed deletion removes the bundle only.
                self.assertTrue(store.remove(second.id, delete_managed_files=True))
                self.assertFalse(bundle.exists())

    def test_builds_and_uses_exact_launch_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = make_bundle(root, "studio")
            store = LibraryStore(root / "library.json")
            record = store.add("studio", bundle, "Studio")
            self.assertEqual(
                ["/usr/bin/rosemary", f"{bundle.absolute()}{os.sep}"],
                build_launch_arguments("/usr/bin/rosemary", bundle),
            )

            with patch("roseblox.backend.rosemary.subprocess.Popen") as popen:
                popen.return_value.communicate.return_value = (b"", b"")
                session = launch_application(record, RosemaryStatus("/usr/bin/rosemary"))

        args, kwargs = popen.call_args
        self.assertEqual(["/usr/bin/rosemary", f"{bundle.absolute()}{os.sep}"], args[0])
        self.assertEqual({"stdout": -1, "stderr": -1}, kwargs)
        self.assertEqual(str(bundle.absolute()), session.bundle_path)


def make_bundle(root: Path, app_type: str) -> Path:
    name = "RobloxPlayer.app" if app_type == "player" else "RobloxStudio.app"
    bundle = root / name
    (bundle / "Contents").mkdir(parents=True, exist_ok=True)
    return bundle


if __name__ == "__main__":
    unittest.main()
