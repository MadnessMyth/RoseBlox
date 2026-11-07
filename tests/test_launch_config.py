import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from roseblox.backend import launch_config
from roseblox.backend.fast_flags import (
    FastFlagError,
    flag_file_path,
    flag_kind,
    normalize_flag_value,
    parse_flag_document,
    read_bundle_flags,
    write_bundle_flags,
)
from roseblox.backend.launch_config import (
    GUEST_ENVIRONMENT_VARIABLE,
    PRESENTATION_MODE_VARIABLE,
    REFRESH_RATE_VARIABLE,
    AppLaunchConfig,
    LaunchConfigError,
    LaunchConfigStore,
    effective_environment,
    environment_changes,
    validate_argument,
    validate_environment_name,
    validate_environment_value,
)
from roseblox.backend.rosemary import RosemaryStatus, RosemaryUnavailableError, launch_application


class FastFlagValueTests(unittest.TestCase):
    def test_kind_comes_from_the_roblox_prefix(self):
        self.assertEqual("bool", flag_kind("FFlagDebugSkyGray"))
        self.assertEqual("bool", flag_kind("DFFlagDisableDPIScale"))
        self.assertEqual("int", flag_kind("FIntDebugForceMSAASamples"))
        self.assertEqual("int", flag_kind("DFIntTextureQualityOverride"))
        self.assertEqual("int", flag_kind("FLogNetwork"))
        self.assertEqual("string", flag_kind("SFStringExample"))
        self.assertIsNone(flag_kind("DebugSkyGray"))
        self.assertIsNone(flag_kind("FFlag"))
        self.assertIsNone(flag_kind("FFlag Debug"))

    def test_values_are_written_the_way_roblox_reads_them(self):
        self.assertEqual("True", normalize_flag_value("FFlagDebugSkyGray", True))
        self.assertEqual("True", normalize_flag_value("FFlagDebugSkyGray", "true"))
        self.assertEqual("False", normalize_flag_value("FFlagDebugSkyGray", "0"))
        self.assertEqual("4", normalize_flag_value("FIntDebugForceMSAASamples", 4))
        self.assertEqual("-2", normalize_flag_value("FIntDebugForceMSAASamples", " -2 "))
        self.assertEqual("abc", normalize_flag_value("FStringExample", "abc"))

    def test_invalid_values_are_rejected_with_the_flag_name(self):
        cases = [
            ("FFlagDebugSkyGray", "maybe"),
            ("FIntDebugForceMSAASamples", "four"),
            ("FIntDebugForceMSAASamples", True),
            ("FIntDebugForceMSAASamples", 2**31),
            ("FStringExample", "a\0b"),
            ("NotAFlag", "True"),
        ]
        for name, value in cases:
            with self.subTest(name=name, value=value), self.assertRaises(FastFlagError) as caught:
                normalize_flag_value(name, value)
            self.assertIn(name, str(caught.exception))

    def test_imported_documents_are_validated(self):
        self.assertEqual(
            {"FFlagDebugSkyGray": "True", "FIntDebugForceMSAASamples": "4"},
            parse_flag_document('{"FFlagDebugSkyGray": true, "FIntDebugForceMSAASamples": "4"}'),
        )
        for text in ("[]", "{", '{"FFlagDebugSkyGray": "sometimes"}'):
            with self.subTest(text=text), self.assertRaises(FastFlagError):
                parse_flag_document(text)


class BundleFlagFileTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.bundle = self.root / "RobloxPlayer.app"
        (self.bundle / "Contents" / "MacOS").mkdir(parents=True)
        self.backups = self.root / "backups"

    def tearDown(self):
        self._directory.cleanup()

    def test_flags_are_written_inside_the_bundle(self):
        self.assertIsNone(write_bundle_flags(self.bundle, {"FIntDebugForceMSAASamples": "4"}, self.backups))
        path = self.bundle / "Contents" / "MacOS" / "ClientSettings" / "ClientAppSettings.json"
        self.assertEqual(path, flag_file_path(self.bundle))
        self.assertEqual({"FIntDebugForceMSAASamples": "4"}, json.loads(path.read_text()))
        self.assertEqual({"FIntDebugForceMSAASamples": "4"}, read_bundle_flags(self.bundle))
        self.assertFalse(self.backups.exists())

    def test_roseblox_replaces_and_removes_its_own_file_without_backups(self):
        write_bundle_flags(self.bundle, {"FIntDebugForceMSAASamples": "4"}, self.backups)
        write_bundle_flags(self.bundle, {"FFlagDebugSkyGray": "True"}, self.backups)
        self.assertEqual({"FFlagDebugSkyGray": "True"}, read_bundle_flags(self.bundle))
        write_bundle_flags(self.bundle, {}, self.backups)
        self.assertIsNone(read_bundle_flags(self.bundle))
        self.assertFalse(self.backups.exists())

    def test_a_file_roseblox_did_not_write_is_backed_up_before_it_is_replaced(self):
        path = flag_file_path(self.bundle)
        path.parent.mkdir(parents=True)
        path.write_text('{"FFlagMine": "True"}')
        backup = write_bundle_flags(self.bundle, {"FIntDebugForceMSAASamples": "4"}, self.backups)
        self.assertIsNotNone(backup)
        self.assertEqual('{"FFlagMine": "True"}', backup.read_text())
        self.assertEqual({"FIntDebugForceMSAASamples": "4"}, read_bundle_flags(self.bundle))

    def test_an_empty_configuration_never_deletes_a_file_roseblox_did_not_write(self):
        path = flag_file_path(self.bundle)
        path.parent.mkdir(parents=True)
        path.write_text('{"FFlagMine": "True"}')
        self.assertIsNone(write_bundle_flags(self.bundle, {}, self.backups))
        self.assertEqual('{"FFlagMine": "True"}', path.read_text())

    def test_an_edited_file_is_treated_as_someone_elses(self):
        write_bundle_flags(self.bundle, {"FIntDebugForceMSAASamples": "4"}, self.backups)
        flag_file_path(self.bundle).write_text('{"FIntDebugForceMSAASamples": "8"}')
        self.assertIsNone(write_bundle_flags(self.bundle, {}, self.backups))
        self.assertTrue(flag_file_path(self.bundle).exists())
        backup = write_bundle_flags(self.bundle, {"FFlagDebugSkyGray": "True"}, self.backups)
        self.assertEqual('{"FIntDebugForceMSAASamples": "8"}', backup.read_text())


class ValidationTests(unittest.TestCase):
    def test_library_injection_and_internal_variables_are_refused(self):
        for name in ("LD_PRELOAD", "LD_LIBRARY_PATH", "ROSEMARY_DYLD_INSERT_LIBRARIES", "ROSEMARY_EXE",
                     "ROSEMARY_GUEST_ENVIRON_2"):
            with self.subTest(name=name):
                self.assertIsNotNone(validate_environment_name(name, guest=False))
        self.assertIsNotNone(validate_environment_name("DYLD_INSERT_LIBRARIES", guest=True))
        self.assertIsNotNone(validate_environment_name("dyld_library_path", guest=True))

    def test_settings_with_their_own_control_point_to_it(self):
        for name in (PRESENTATION_MODE_VARIABLE, REFRESH_RATE_VARIABLE, GUEST_ENVIRONMENT_VARIABLE,
                     "ROSEMARY_PLAIN_CTRL_CLICK"):
            with self.subTest(name=name):
                self.assertIn("instead", validate_environment_name(name, guest=False))

    def test_credentials_are_never_stored(self):
        for name in ("ROBLOSECURITY", "MY_TOKEN", "api_key", "SESSION_ID"):
            with self.subTest(name=name):
                self.assertIn("credential", validate_environment_name(name, guest=False))
        cookie = "_|WARNING:-DO-NOT-SHARE-THIS.--Sharing-this-will-allow-someone|_ABC"
        self.assertIsNotNone(validate_environment_value("FOO", cookie, guest=False))
        self.assertIsNotNone(validate_argument(cookie))

    def test_ordinary_variables_are_accepted(self):
        self.assertIsNone(validate_environment_name("ROSEMARY_SHADER_CACHE", guest=False))
        self.assertIsNone(validate_environment_name("MESA_VK_DEVICE_SELECT", guest=False))
        self.assertIsNone(validate_environment_name("MY_SETTING", guest=True))
        self.assertIsNone(validate_environment_value("FOO", "a b\nc", guest=False))
        self.assertIsNotNone(validate_environment_value("FOO", "a\nb", guest=True))
        self.assertIsNotNone(validate_environment_name("1ABC", guest=False))
        self.assertIsNotNone(validate_environment_name("A-B", guest=False))

    def test_arguments_that_carry_login_or_launch_data_are_refused(self):
        for argument in ("-ticket", "--authcode", "-rbxTransportToken=abc", "-protocolString"):
            with self.subTest(argument=argument):
                self.assertIsNotNone(validate_argument(argument))
        for argument in ("-task", "EditFile", "-localPlaceFile", "/home/me/My Place.rbxl"):
            with self.subTest(argument=argument):
                self.assertIsNone(validate_argument(argument))
        self.assertIsNotNone(validate_argument(""))
        self.assertIsNotNone(validate_argument("a\0b"))

    def test_configurations_report_every_problem(self):
        config = AppLaunchConfig(
            rosemary_environment={"LD_PRELOAD": "/x.so"},
            arguments=("-ticket",),
            fast_flags={"FIntDebugForceMSAASamples": "lots"},
            presentation_mode="vsync",
            refresh_rate=5,
        )
        self.assertEqual(5, len(config.problems()))
        with self.assertRaises(LaunchConfigError):
            config.validated()
        self.assertEqual([], AppLaunchConfig(presentation_mode="fifo", refresh_rate=144).problems())


class EffectiveEnvironmentTests(unittest.TestCase):
    def test_frame_pacing_and_rosemary_variables_are_applied(self):
        config = AppLaunchConfig(
            rosemary_environment={"ROSEMARY_SHADER_CACHE": "off"},
            presentation_mode="fifo_relaxed",
            refresh_rate=144,
        )
        environment = effective_environment(config, {"HOME": "/home/me"})
        self.assertEqual("/home/me", environment["HOME"])
        self.assertEqual("off", environment["ROSEMARY_SHADER_CACHE"])
        self.assertEqual("fifo_relaxed", environment[PRESENTATION_MODE_VARIABLE])
        self.assertEqual("144", environment[REFRESH_RATE_VARIABLE])

    def test_unset_choices_keep_what_the_user_already_exported(self):
        base = {PRESENTATION_MODE_VARIABLE: "immediate"}
        self.assertEqual(base, effective_environment(AppLaunchConfig(), base))
        self.assertEqual({}, environment_changes(AppLaunchConfig(), base))

    def test_roblox_variables_are_merged_into_the_guest_environment(self):
        config = AppLaunchConfig(guest_environment={"B": "new", "C": "3"})
        environment = effective_environment(config, {GUEST_ENVIRONMENT_VARIABLE: "A=1\nB=old\nLANG"})
        self.assertEqual("A=1\nB=new\nLANG\nC=3", environment[GUEST_ENVIRONMENT_VARIABLE])
        self.assertEqual("B=x", effective_environment(AppLaunchConfig(guest_environment={"B": "x"}), {})[
            GUEST_ENVIRONMENT_VARIABLE
        ])


class KeyboardAndWindowTests(unittest.TestCase):
    def test_command_key_and_titlebar_set_rosemary_variables(self):
        config = AppLaunchConfig(command_key="super+ctrl:edit", decorations="server")
        environment = effective_environment(config, {})
        self.assertEqual("super+ctrl:edit", environment["ROSEMARY_COMMAND_KEY"])
        self.assertEqual("server", environment["ROSEMARY_DECORATIONS"])
        self.assertNotIn("ROSEMARY_COMMAND_KEY", effective_environment(AppLaunchConfig(), {}))
        self.assertEqual(config, AppLaunchConfig.from_document(config.to_document()))
        self.assertEqual(2, len(AppLaunchConfig(command_key="meta", decorations="none").problems()))
        self.assertIn("instead", validate_environment_name("ROSEMARY_COMMAND_KEY", guest=False))
        self.assertEqual(
            ["Command key: Rosemary default → ctrl", "Titlebar: Rosemary default → server"],
            launch_config.describe_changes(AppLaunchConfig(), AppLaunchConfig(command_key="ctrl", decorations="server")),
        )


class StudioPluginTests(unittest.TestCase):
    def test_the_plugin_switch_is_a_studio_argument(self):
        config = AppLaunchConfig(arguments=("-x",), disable_user_plugins=True)
        self.assertEqual(("-disableLoadUserPlugins", "-x"), launch_config.effective_arguments(config, "studio"))
        self.assertEqual(("-x",), launch_config.effective_arguments(config, "player"))
        self.assertEqual(("-x",), launch_config.effective_arguments(AppLaunchConfig(arguments=("-x",)), "studio"))
        self.assertEqual(config, AppLaunchConfig.from_document(config.to_document()))
        self.assertFalse(AppLaunchConfig.from_document({}).disable_user_plugins)
        self.assertEqual(1, len(AppLaunchConfig(disable_user_plugins="yes").problems()))
        self.assertEqual(
            ["Studio user plugins: loaded → not loaded"],
            launch_config.describe_changes(AppLaunchConfig(), AppLaunchConfig(disable_user_plugins=True)),
        )

    def test_studio_launches_with_the_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "RobloxStudio.app"
            (bundle / "Contents" / "MacOS").mkdir(parents=True)
            executable = root / "rosemary"
            executable.write_bytes(b"ELF\0")
            executable.chmod(0o755)
            process = subprocess.CompletedProcess([], 0)
            process.pid = 5150
            process.poll = lambda: None
            with patch("roseblox.backend.rosemary.subprocess.Popen", return_value=process) as popen:
                launch_application(
                    {"app_type": "studio", "bundle_path": str(bundle)},
                    RosemaryStatus(str(executable)),
                    guest_arguments=("-task", "EditFile", "-localPlaceFile", "/Volumes/Host/p.rbxl"),
                    log_directory=root / "logs",
                    launch_config=AppLaunchConfig(disable_user_plugins=True),
                    flag_backup_directory=root / "backups",
                    log_name="studio-2",
                )
            self.assertTrue((root / "logs" / "studio-2.log").exists())
        command = popen.call_args.args[0]
        self.assertEqual(
            ["--", "-task", "EditFile", "-localPlaceFile", "/Volumes/Host/p.rbxl", "-disableLoadUserPlugins"],
            command[command.index("--"):],
        )


class LaunchConfigStoreTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.store = LaunchConfigStore(self.root / "launch-config.json")

    def tearDown(self):
        self._directory.cleanup()

    def test_manual_backup_and_whole_configuration_export(self):
        self.store.save("studio", AppLaunchConfig(disable_user_plugins=True), "Studio plugins")
        before = len(self.store.backups())
        self.store.back_up_now()
        self.assertEqual(before + 1, len(self.store.backups()))
        self.assertEqual("Manual backup", self.store.backups()[0].reason)
        configs = LaunchConfigStore.parse_export(self.store.export_text())
        self.assertTrue(configs["studio"].disable_user_plugins)
        self.assertTrue(configs["player"].is_default)
        for text in ("{broken", json.dumps({"version": 1, "apps": {"other": {}}}), " " * (2 * 1024 * 1024)):
            with self.assertRaises(LaunchConfigError):
                LaunchConfigStore.parse_export(text)
        with self.assertRaises(LaunchConfigError):
            LaunchConfigStore.parse_export(json.dumps(
                {"version": 1, "apps": {"player": {"rosemary_environment": {"LD_PRELOAD": "/x.so"}}}}
            ))

    def test_player_and_studio_are_configured_separately(self):
        self.store.save("player", AppLaunchConfig(presentation_mode="fifo"), "Player pacing")
        self.store.save("studio", AppLaunchConfig(arguments=("-task", "EditFile")), "Studio arguments")
        self.assertEqual(AppLaunchConfig(presentation_mode="fifo"), self.store.load("player"))
        self.assertEqual(("-task", "EditFile"), self.store.load("studio").arguments)
        self.assertIsNone(self.store.load("studio").presentation_mode)
        with self.assertRaises(ValueError):
            self.store.load("other")

    def test_missing_file_means_defaults(self):
        self.assertTrue(self.store.load("player").is_default)
        self.assertEqual([], self.store.backups())

    def test_every_change_keeps_the_previous_configuration(self):
        self.store.save("player", AppLaunchConfig(refresh_rate=144), "First")
        self.store.save("player", AppLaunchConfig(refresh_rate=240), "Second")
        backups = self.store.backups()
        self.assertEqual(["Second", "First"], [entry.reason for entry in backups])
        self.store.restore(backups[0].id)
        self.assertEqual(144, self.store.load("player").refresh_rate)
        self.store.restore(backups[1].id)
        self.assertTrue(self.store.load("player").is_default)
        self.assertTrue(self.store.backups()[0].reason.startswith("Before restoring"))

    def test_saving_the_same_configuration_makes_no_backup(self):
        self.store.save("player", AppLaunchConfig(), "Nothing")
        self.store.save("player", AppLaunchConfig(refresh_rate=60), "Change")
        self.store.save("player", AppLaunchConfig(refresh_rate=60), "Again")
        self.assertEqual(["Change"], [entry.reason for entry in self.store.backups()])

    def test_invalid_configurations_are_not_saved(self):
        with self.assertRaises(LaunchConfigError):
            self.store.save("player", AppLaunchConfig(rosemary_environment={"LD_PRELOAD": "x"}), "Bad")
        self.assertFalse(self.store.path.exists())

    def test_old_backups_are_pruned(self):
        with patch.object(launch_config, "MAX_BACKUPS", 3):
            for rate in range(60, 66):
                self.store.save("player", AppLaunchConfig(refresh_rate=rate), f"Rate {rate}")
        self.assertEqual(3, len(self.store.backups()))
        self.assertEqual("Rate 65", self.store.backups()[0].reason)

    def test_a_damaged_file_is_reported_and_can_be_rolled_back(self):
        self.store.save("player", AppLaunchConfig(refresh_rate=120), "Good")
        self.store.save("player", AppLaunchConfig(refresh_rate=144), "Newer")
        self.store.path.write_text("{broken")
        with self.assertRaises(LaunchConfigError) as caught:
            self.store.load("player")
        self.assertIn("Restore a backup", str(caught.exception))
        self.store.restore(self.store.backups()[0].id)
        self.assertEqual(120, self.store.load("player").refresh_rate)

    def test_backups_with_unsafe_content_are_refused(self):
        self.store.save("player", AppLaunchConfig(refresh_rate=120), "Good")
        backup = self.store.backups()[0]
        document = json.loads(backup.path.read_text())
        document["document"]["apps"]["player"] = {"rosemary_environment": {"LD_PRELOAD": "/evil.so"}}
        backup.path.write_text(json.dumps(document))
        with self.assertRaises(LaunchConfigError):
            self.store.restore(backup.id)
        self.assertEqual(120, self.store.load("player").refresh_rate)


class LaunchIntegrationTests(unittest.TestCase):
    def _launch(self, root, config, *, guest_arguments=()):
        bundle = root / "RobloxPlayer.app"
        (bundle / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
        executable = root / "rosemary"
        executable.write_bytes(b"ELF\0old build\0")
        executable.chmod(0o755)
        fake_process = subprocess.CompletedProcess([], 0)
        fake_process.pid = 34567
        fake_process.poll = lambda: None
        with patch.dict(os.environ, {"HOME": str(root)}, clear=True):
            with patch("roseblox.backend.rosemary.subprocess.Popen", return_value=fake_process) as popen:
                launch_application(
                    {"app_type": "player", "bundle_path": str(bundle)},
                    RosemaryStatus(str(executable)),
                    guest_arguments=guest_arguments,
                    log_directory=root / "logs",
                    launch_config=config,
                    flag_backup_directory=root / "backups",
                )
        return bundle, popen

    def test_configuration_reaches_rosemary_and_the_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = AppLaunchConfig(
                guest_environment={"MY_SETTING": "1"},
                arguments=("--extra",),
                fast_flags={"FIntDebugForceMSAASamples": "4"},
                presentation_mode="fifo",
            )
            bundle, popen = self._launch(root, config, guest_arguments=("roblox-player:1",))
            environment = popen.call_args.kwargs["env"]
            self.assertEqual("fifo", environment[PRESENTATION_MODE_VARIABLE])
            self.assertEqual("MY_SETTING=1", environment[GUEST_ENVIRONMENT_VARIABLE])
            self.assertEqual(["roblox-player:1", "--extra"], popen.call_args.args[0][-2:])
            self.assertEqual({"FIntDebugForceMSAASamples": "4"}, read_bundle_flags(bundle))

            self._launch(root, AppLaunchConfig())
            self.assertIsNone(read_bundle_flags(bundle))

    def test_flag_failures_stop_the_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "RobloxPlayer.app"
            (bundle / "Contents" / "MacOS" / "ClientSettings").mkdir(parents=True)
            flag_file_path(bundle).mkdir()
            with self.assertRaises(RosemaryUnavailableError):
                self._launch(root, AppLaunchConfig(fast_flags={"FFlagDebugSkyGray": "True"}))


if __name__ == "__main__":
    unittest.main()
