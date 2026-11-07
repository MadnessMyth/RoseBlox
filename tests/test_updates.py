import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from roseblox.backend.library import InstallationRecord
from roseblox.backend.setup import SetupStore
from roseblox.backend.updates import (
    UPDATER_DESKTOP_ID,
    AvailableUpdate,
    NotificationLog,
    UpdateCheckerError,
    UpdateCheckerService,
    check_for_updates,
    find_available_updates,
)
from roseblox.backend.versions import VersionInfo

OLD = "version-0000000000000001"
NEW = "version-0000000000000002"
NEWER = "version-0000000000000003"


def _record(app_type, version, number=1):
    return InstallationRecord(
        id=f"{app_type}-{number}",
        display_name=f"Roblox {app_type.title()}",
        app_type=app_type,
        bundle_path=f"/tmp/{app_type}-{number}",
        registered_at="2026-11-07T00:00:00+00:00",
        validation_state="Valid",
        version=version,
        managed=True,
        source="download",
        archive_path=None,
    )


class FindUpdatesTests(unittest.TestCase):
    def test_offers_only_installed_apps_without_the_latest_version(self):
        updates = find_available_updates(
            [_record("player", OLD)], {"player": NEW, "studio": NEW}, {}
        )
        self.assertEqual([AvailableUpdate("player", NEW, OLD)], updates)

    def test_latest_already_installed_alongside_older_is_not_offered(self):
        records = [_record("player", OLD, 1), _record("player", NEW, 2)]
        self.assertEqual([], find_available_updates(records, {"player": NEW}, {}))

    def test_skipped_version_is_quiet_until_a_newer_one(self):
        records = [_record("studio", OLD)]
        self.assertEqual([], find_available_updates(records, {"studio": NEW}, {"studio": NEW}))
        self.assertEqual(
            [AvailableUpdate("studio", NEWER, OLD)],
            find_available_updates(records, {"studio": NEWER}, {"studio": NEW}),
        )

    def test_imported_installation_without_a_version_is_offered_the_latest(self):
        self.assertEqual(
            [AvailableUpdate("player", NEW, None)],
            find_available_updates([_record("player", None)], {"player": NEW}, {}),
        )

    def test_failed_lookup_for_one_app_still_reports_the_other(self):
        library = SimpleNamespace(load=lambda: [_record("player", OLD), _record("studio", OLD)])

        def fetch(app_type, _source):
            if app_type == "studio":
                raise RuntimeError("offline")
            return VersionInfo(NEW, "2026-11-07T00:00:00+00:00")

        client = SimpleNamespace(fetch=fetch)
        settings = SimpleNamespace(load_for=lambda app_type: f"source-{app_type}")
        cache = Mock()
        updates, errors = check_for_updates(library, client, settings, cache, {})
        self.assertEqual([AvailableUpdate("player", NEW, OLD)], updates)
        self.assertEqual({"studio": "offline"}, errors)
        cache.update.assert_called_once_with("player", VersionInfo(NEW, "2026-11-07T00:00:00+00:00"))


class NotificationLogTests(unittest.TestCase):
    def test_reminds_once_a_day_and_immediately_for_a_new_version(self):
        with tempfile.TemporaryDirectory() as directory:
            log = NotificationLog(Path(directory) / "state" / "updates.json", remind_after=100)
            update = AvailableUpdate("player", NEW, OLD)
            self.assertTrue(log.should_notify(update, now=1000))
            log.record(update, now=1000)
            self.assertFalse(log.should_notify(update, now=1099))
            self.assertTrue(log.should_notify(update, now=1100))
            self.assertTrue(log.should_notify(AvailableUpdate("player", NEWER, OLD), now=1001))
            self.assertTrue(log.should_notify(AvailableUpdate("studio", NEW, OLD), now=1001))

    def test_clearing_the_log_allows_an_immediate_reminder(self):
        with tempfile.TemporaryDirectory() as directory:
            log = NotificationLog(Path(directory) / "updates.json")
            update = AvailableUpdate("player", NEW)
            log.record(update)
            log.clear()
            log.clear()
            self.assertTrue(log.should_notify(update))

    def test_unreadable_log_means_notify_again(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "updates.json"
            path.write_text("{broken", encoding="utf-8")
            log = NotificationLog(path)
            update = AvailableUpdate("player", NEW)
            self.assertTrue(log.should_notify(update))
            log.record(update)
            self.assertFalse(log.should_notify(update))


class UpdateCheckerServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.executable = root / "bin" / "roseblox launcher"
        self.executable.parent.mkdir()
        self.executable.write_text("#!/bin/sh\n", encoding="utf-8")
        self.executable.chmod(0o755)
        self.spawned = []
        self.store = SetupStore(root / "config" / "roseblox" / "setup.json")
        self.service = UpdateCheckerService(
            self.store,
            config_home=root / "config",
            data_home=root / "data",
            executable_lookup=lambda name: str(self.executable) if name == "roseblox" else None,
            spawn=self.spawned.append,
        )

    def test_enable_installs_login_entry_and_starts_the_checker(self):
        status = self.service.enable()
        self.assertTrue(status.enabled)
        self.assertIsNone(status.error)
        self.assertTrue(self.store.load().update_checks_enabled)
        autostart = self.service.autostart_path.read_text(encoding="utf-8")
        self.assertEqual(UPDATER_DESKTOP_ID, self.service.autostart_path.name)
        self.assertIn(f'Exec="{self.executable}" --update-service\n', autostart)
        self.assertIn("X-GNOME-Autostart-enabled=true\n", autostart)
        self.assertIn("X-Roseblox-Managed=true\n", autostart)
        application_entry = self.service.desktop_path.read_text(encoding="utf-8")
        self.assertIn("NoDisplay=true\n", application_entry)
        self.assertNotIn("Autostart", application_entry)
        self.assertEqual([[str(self.executable), "--update-service"]], self.spawned)

    def test_disable_removes_entries_and_stops_the_checker(self):
        self.service.enable()
        self.spawned.clear()
        status = self.service.disable()
        self.assertFalse(status.enabled)
        self.assertFalse(self.store.load().update_checks_enabled)
        self.assertFalse(self.service.autostart_path.exists())
        self.assertFalse(self.service.desktop_path.exists())
        self.assertEqual([[str(self.executable), "--update-service", "--quit"]], self.spawned)

    def test_foreign_autostart_entry_is_never_overwritten(self):
        self.service.autostart_path.parent.mkdir(parents=True)
        self.service.autostart_path.write_text("[Desktop Entry]\nName=Mine\n", encoding="utf-8")
        with self.assertRaisesRegex(UpdateCheckerError, "not created by Roseblox"):
            self.service.enable()
        self.assertEqual("[Desktop Entry]\nName=Mine\n", self.service.autostart_path.read_text(encoding="utf-8"))
        self.assertFalse(self.service.desktop_path.exists())
        self.assertFalse(self.store.load().update_checks_enabled)
        self.assertEqual([], self.spawned)
        self.service.disable()
        self.assertTrue(self.service.autostart_path.exists())

    def test_missing_launcher_is_explained(self):
        self.service.executable_lookup = lambda _name: None
        with self.assertRaisesRegex(UpdateCheckerError, "not available in PATH"):
            self.service.enable()
        self.assertFalse(self.store.load().update_checks_enabled)

    def test_status_reports_a_missing_login_entry(self):
        self.service.enable()
        os.unlink(self.service.autostart_path)
        status = self.service.status()
        self.assertTrue(status.enabled)
        self.assertIn("missing", status.message)
        self.assertIsNotNone(status.error)


class UpdateNotifierTests(unittest.TestCase):
    def setUp(self):
        from roseblox.update_service import UpdateNotifierApplication

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.store = SetupStore(root / "setup.json")
        self.spawned = []
        self.records = [_record("player", OLD), _record("studio", OLD)]
        self.latest = {"player": NEW, "studio": NEW}
        self.application = UpdateNotifierApplication(
            setup_store=self.store,
            library=SimpleNamespace(load=lambda: self.records),
            version_client=SimpleNamespace(
                fetch=lambda app_type, _source: VersionInfo(self.latest[app_type], "now")
            ),
            version_settings=SimpleNamespace(load_for=lambda app_type: None),
            version_cache=Mock(),
            notification_log=NotificationLog(root / "log.json"),
            executable_lookup=lambda name: "/usr/bin/roseblox",
            spawn=lambda arguments, log_path=None: self.spawned.append(arguments),
        )
        self.sent = []
        self.withdrawn = []
        self.application.send_notification = lambda identifier, notification: self.sent.append(identifier)
        self.application.withdraw_notification = self.withdrawn.append

    @staticmethod
    def _command_line(*options):
        errors = []
        return SimpleNamespace(
            get_arguments=lambda: ["roseblox", "--update-service", *options],
            printerr_literal=errors.append,
            errors=errors,
        ), errors

    def test_disabled_checker_exits_and_says_why(self):
        command_line, errors = self._command_line()
        self.assertEqual(0, self.application._on_command_line(self.application, command_line))
        self.assertFalse(self.application._running)
        self.assertIn("turned off", errors[0])

    def test_enabled_checker_stays_running_until_quit(self):
        self.store.save_update_checks(True)
        command_line, _errors = self._command_line()
        self.assertEqual(0, self.application._on_command_line(self.application, command_line))
        self.assertTrue(self.application._running)
        self.assertTrue(self.application._timer_id)
        command_line, _errors = self._command_line("--quit")
        self.assertEqual(0, self.application._on_command_line(self.application, command_line))
        self.assertFalse(self.application._running)
        self.assertFalse(self.application._timer_id)

    def test_unknown_options_are_rejected(self):
        command_line, errors = self._command_line("--bogus")
        self.assertEqual(2, self.application._on_command_line(self.application, command_line))
        self.assertIn("Usage", errors[0])

    def test_check_notifies_once_and_withdraws_when_up_to_date(self):
        self.store.save_update_checks(True)
        self.store.skip_version("studio", NEW)
        synchronous = SimpleNamespace(
            Thread=lambda target, daemon: SimpleNamespace(start=target)
        )
        idle = SimpleNamespace(idle_add=lambda function, *arguments: function(*arguments))
        self.enterContext(patch("roseblox.update_service.threading", synchronous))
        self.enterContext(patch("roseblox.update_service.GLib.idle_add", idle.idle_add))
        self.application.check()
        self.assertEqual(["update-player"], self.sent)
        self.assertEqual(["update-studio"], self.withdrawn)
        self.application.check()
        self.assertEqual(["update-player"], self.sent)
        self.application.check(force=True)
        self.assertEqual(["update-player", "update-player"], self.sent)
        self.records = [_record("player", NEW), _record("studio", OLD)]
        self.application.check()
        self.assertIn("update-player", self.withdrawn)

    def test_disabled_while_running_stops_at_the_next_check(self):
        self.store.save_update_checks(True)
        command_line, _errors = self._command_line()
        self.application._on_command_line(self.application, command_line)
        self.store.save_update_checks(False)
        self.application.check()
        self.assertFalse(self.application._running)

    def test_notification_has_update_and_skip_buttons(self):
        notification = self.application.build_notification(AvailableUpdate("player", NEW, OLD))
        self.assertIsNotNone(notification)

    def test_notification_buttons_update_or_skip(self):
        from gi.repository import GLib

        self.application.do_startup()
        activate = lambda name, value: self.application.lookup_action(name).activate(value)
        activate("update", GLib.Variant("s", "player"))
        self.assertEqual([["/usr/bin/roseblox", "--install-update", "player"]], self.spawned)
        activate("skip", GLib.Variant("(ss)", ("studio", NEW)))
        self.assertEqual({"studio": NEW}, self.store.load().skipped_versions)
        activate("open", GLib.Variant("s", "studio"))
        self.assertEqual(["/usr/bin/roseblox", "--show", "studio"], self.spawned[-1])
        self.assertEqual(["update-player", "update-studio"], self.withdrawn)


if __name__ == "__main__":
    unittest.main()
