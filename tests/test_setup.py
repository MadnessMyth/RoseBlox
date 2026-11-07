import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from roseblox.backend.rosemary import (
    PLAIN_CTRL_CLICK_VARIABLE,
    RosemaryStatus,
    build_launch_arguments,
    detect_rosemary,
    launch_application,
    rosemary_supports_plain_ctrl_click,
    save_rosemary_executable,
)
from roseblox.backend.setup import Appearance, ClosePreference, SetupSettingsError, SetupStore
from roseblox.backend.url_handlers import (
    APP_DESKTOP_ID,
    DESKTOP_ID,
    SCHEME_TO_APP,
    SUPPORTED_SCHEMES,
    URLHandlerError,
    URLHandlerService,
    parse_roblox_url,
)


class SetupStoreTests(unittest.TestCase):
    def test_missing_file_has_optional_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            state = SetupStore(Path(directory) / "setup.json").load()
        self.assertFalse(state.onboarding_completed)
        self.assertFalse(state.url_handlers_enabled)
        self.assertEqual({}, state.previous_url_defaults)

    def test_onboarding_and_url_choices_persist_independently(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SetupStore(Path(directory) / "setup.json")
            store.complete_onboarding()
            store.save_url_handlers(True, {"x-scheme-handler/roblox": "other.desktop"})
            loaded = SetupStore(store.path).load()
        self.assertTrue(loaded.onboarding_completed)
        self.assertTrue(loaded.url_handlers_enabled)
        self.assertEqual(
            {"x-scheme-handler/roblox": "other.desktop"}, loaded.previous_url_defaults
        )

    def test_malformed_settings_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "setup.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(SetupSettingsError):
                SetupStore(path).complete_onboarding()
            self.assertEqual("{broken", path.read_text(encoding="utf-8"))

    def test_launch_and_update_preferences_default_off(self):
        with tempfile.TemporaryDirectory() as directory:
            state = SetupStore(Path(directory) / "setup.json").load()
        for app_type in ("player", "studio"):
            self.assertEqual(ClosePreference(), state.close_preference(app_type))
        self.assertFalse(state.update_checks_enabled)
        self.assertEqual({}, state.skipped_versions)

    def test_close_preferences_are_separate_per_application(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SetupStore(Path(directory) / "setup.json")
            store.save_url_handlers(True, {"x-scheme-handler/roblox": "sober.desktop"})
            store.save_close_after_launch("player", ClosePreference(True, True))
            store.save_close_after_launch("studio", ClosePreference(True, False))
            loaded = SetupStore(store.path).load()
        self.assertEqual(ClosePreference(True, True), loaded.close_preference("player"))
        self.assertEqual(ClosePreference(True, False), loaded.close_preference("studio"))
        self.assertTrue(loaded.url_handlers_enabled)
        with self.assertRaises(ValueError):
            store.save_close_after_launch("roblox", ClosePreference(True))

    def test_close_preference_respects_link_only_option(self):
        self.assertFalse(ClosePreference(False, False).applies(from_link=True))
        self.assertTrue(ClosePreference(True, False).applies(from_link=False))
        self.assertTrue(ClosePreference(True, False).applies(from_link=True))
        self.assertFalse(ClosePreference(True, True).applies(from_link=False))
        self.assertTrue(ClosePreference(True, True).applies(from_link=True))

    def test_update_checks_and_skipped_versions_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SetupStore(Path(directory) / "setup.json")
            store.save_update_checks(True)
            store.skip_version("player", "version-0123456789abcdef")
            store.skip_version("player", "version-1111111111111111")
            store.skip_version("studio", "version-2222222222222222")
            loaded = SetupStore(store.path).load()
            self.assertTrue(loaded.update_checks_enabled)
            self.assertEqual(
                {"player": "version-1111111111111111", "studio": "version-2222222222222222"},
                loaded.skipped_versions,
            )
            self.assertEqual({}, store.clear_skipped_versions().skipped_versions)
            self.assertTrue(store.load().update_checks_enabled)

    def test_invalid_new_preferences_are_reported_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "setup.json"
            for document in (
                {"version": 1, "close_after_launch": {"player": {"enabled": "yes"}}},
                {"version": 1, "close_after_launch": {"phone": {"enabled": True}}},
                {"version": 1, "update_checks_enabled": 1},
                {"version": 1, "skipped_versions": {"player": 5}},
            ):
                with self.subTest(document=document):
                    text = json.dumps(document)
                    path.write_text(text, encoding="utf-8")
                    with self.assertRaises(SetupSettingsError):
                        SetupStore(path).load()
                    self.assertEqual(text, path.read_text(encoding="utf-8"))

    def test_appearance_input_and_layout_preferences_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SetupStore(Path(directory) / "setup.json")
            state = store.load()
            self.assertEqual(Appearance("roseblox", "dark", "roseblox"), state.appearance)
            self.assertTrue(state.plain_ctrl_click)
            self.assertEqual({}, state.expanded_sections)
            store.save_appearance(Appearance("system", "system", "teal"))
            store.save_plain_ctrl_click(False)
            self.assertFalse(state.multiple_players)
            store.save_multiple_players(True)
            store.save_section_expanded("advanced", True)
            store.save_section_expanded("appearance", False)
            loaded = SetupStore(store.path).load()
        self.assertEqual(Appearance("system", "system", "teal"), loaded.appearance)
        self.assertFalse(loaded.plain_ctrl_click)
        self.assertTrue(loaded.multiple_players)
        self.assertEqual({"advanced": True, "appearance": False}, loaded.expanded_sections)
        with self.assertRaises(ValueError):
            store.save_appearance(Appearance("roseblox", "dark", "magenta"))

    def test_invalid_appearance_is_reported_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "setup.json"
            for document in (
                {"version": 1, "appearance": {"colors": "neon"}},
                {"version": 1, "appearance": "dark"},
                {"version": 1, "plain_ctrl_click": "yes"},
                {"version": 1, "expanded_sections": {"advanced": 1}},
            ):
                with self.subTest(document=document):
                    text = json.dumps(document)
                    path.write_text(text, encoding="utf-8")
                    with self.assertRaises(SetupSettingsError):
                        SetupStore(path).load()
                    self.assertEqual(text, path.read_text(encoding="utf-8"))

    def test_writes_from_two_processes_do_not_lose_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "setup.json"
            script = (
                "import sys\n"
                "from roseblox.backend.setup import SetupStore\n"
                "store = SetupStore(sys.argv[1])\n"
                "for index in range(40):\n"
                "    store.skip_version(sys.argv[2], f'version-{index:016x}')\n"
            )
            environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
            workers = [
                subprocess.Popen(["python3", "-c", script, str(path), app_type], env=environment)
                for app_type in ("player", "studio")
            ]
            store = SetupStore(path)
            for _ in range(40):
                store.save_update_checks(True)
            self.assertEqual([0, 0], [worker.wait(timeout=60) for worker in workers])
            state = store.load()
        self.assertTrue(state.update_checks_enabled)
        self.assertEqual(
            {"player": f"version-{39:016x}", "studio": f"version-{39:016x}"}, state.skipped_versions
        )


class URLHandlerTests(unittest.TestCase):
    def _service(
        self, root, defaults=None, *, fail_on_set=None, fallback_to_installed=False, tools=(), app_entry=False
    ):
        """Fake xdg-mime; ``fallback_to_installed`` mimics its search of installed entries."""
        defaults = dict(defaults or {})
        calls = []
        executable = root / "bin" / "roseblox"
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
        paths = {"roseblox": str(executable), "xdg-mime": "/usr/bin/xdg-mime"}
        paths.update({name: f"/usr/bin/{name}" for name in tools})

        def runner(arguments, **_kwargs):
            calls.append(list(arguments))
            if arguments[0] != "/usr/bin/xdg-mime":
                return subprocess.CompletedProcess(arguments, 0, "", "")
            if arguments[1] == "query":
                mime_type = arguments[-1]
                value = defaults.get(mime_type, "")
                if value == DESKTOP_ID and service_ref:
                    try:
                        section = ""
                        for line in service_ref[0].mimeapps_path.read_text(encoding="utf-8").splitlines():
                            if line.startswith("["):
                                section = line
                            elif section == "[Default Applications]" and line.startswith(mime_type + "="):
                                entries = [item for item in line.split("=", 1)[1].split(";") if item]
                                value = entries[0] if entries else ""
                                break
                        else:
                            value = ""
                    except FileNotFoundError:
                        value = ""
                if not value and fallback_to_installed and service_ref:
                    try:
                        desktop = service_ref[0].desktop_path.read_text(encoding="utf-8")
                    except FileNotFoundError:
                        desktop = ""
                    if f"{mime_type};" in desktop:
                        value = DESKTOP_ID
                return subprocess.CompletedProcess(arguments, 0, value + "\n", "")
            if fail_on_set and arguments[-1] == fail_on_set:
                return subprocess.CompletedProcess(arguments, 1, "", "registration failed")
            defaults[arguments[-1]] = arguments[-2]
            path = root / "config" / "mimeapps.list"
            lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
            if "[Default Applications]" not in lines:
                lines.extend(["[Default Applications]"])
            index = lines.index("[Default Applications]") + 1
            key = arguments[-1]
            replacement = f"{key}={arguments[-2]};"
            for row in range(index, len(lines)):
                if lines[row].startswith("["):
                    break
                if lines[row].startswith(key + "="):
                    lines[row] = replacement
                    break
            else:
                lines.insert(index, replacement)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return subprocess.CompletedProcess(arguments, 0, "", "")

        service_ref = []
        service = URLHandlerService(
            SetupStore(root / "config" / "setup.json"),
            data_home=root / "data",
            config_home=root / "config",
            executable_lookup=paths.get,
            command_runner=runner,
            data_dirs=[root / "system"],
        )
        if app_entry:
            entry = root / "system" / "applications" / APP_DESKTOP_ID
            entry.parent.mkdir(parents=True, exist_ok=True)
            entry.write_text("[Desktop Entry]\nName=Roseblox\n", encoding="utf-8")
        service_ref.append(service)
        return service, defaults, calls

    def test_switching_refreshes_desktop_caches_when_available(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _defaults, calls = self._service(
                Path(directory), tools=("update-desktop-database", "kbuildsycoca6", "kbuildsycoca5")
            )
            service.enable()
            applications = str(service.desktop_path.parent)
            refreshes = [call for call in calls if call[0] != "/usr/bin/xdg-mime"]
            self.assertEqual(
                [["/usr/bin/update-desktop-database", "-q", applications], ["/usr/bin/kbuildsycoca6"]],
                refreshes,
            )
            calls.clear()
            service.disable()
            self.assertIn(["/usr/bin/kbuildsycoca6"], calls)

    def test_handler_entry_is_named_roseblox_and_replaces_older_owned_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _defaults, _calls = self._service(Path(directory))
            service.desktop_path.parent.mkdir(parents=True, exist_ok=True)
            service.desktop_path.write_text(
                "[Desktop Entry]\nName=Roseblox Roblox URL Handler\nX-Roseblox-Managed=true\n", encoding="utf-8"
            )
            self.assertTrue(service.enable().registered)
            desktop = service.desktop_path.read_text(encoding="utf-8")
            self.assertIn("\nName=Roseblox\n", desktop)
            self.assertIn("Icon=io.github.roseblox.Roseblox\n", desktop)

    def test_enabled_links_list_the_installed_app_in_open_with_dialogs(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _defaults, _calls = self._service(Path(directory), app_entry=True)
            service.mimeapps_path.parent.mkdir(parents=True, exist_ok=True)
            service.mimeapps_path.write_text(
                "[Added Associations]\nx-scheme-handler/roblox-player=org.vinegarhq.Sober.desktop;\n"
                "text/plain=editor.desktop;\n",
                encoding="utf-8",
            )
            self.assertTrue(service.enable().registered)
            mimeapps = service.mimeapps_path.read_text(encoding="utf-8")
            self.assertIn(
                f"x-scheme-handler/roblox-player=org.vinegarhq.Sober.desktop;{APP_DESKTOP_ID};", mimeapps
            )
            for scheme in ("roblox", "roblox-studio", "roblox-studio-auth"):
                self.assertIn(f"x-scheme-handler/{scheme}={APP_DESKTOP_ID};", mimeapps)
            self.assertEqual(1, mimeapps.count("[Added Associations]"))

            service.disable()
            mimeapps = service.mimeapps_path.read_text(encoding="utf-8")
            self.assertNotIn(APP_DESKTOP_ID, mimeapps)
            self.assertIn("x-scheme-handler/roblox-player=org.vinegarhq.Sober.desktop;", mimeapps)
            self.assertIn("text/plain=editor.desktop;", mimeapps)

    def test_app_association_is_skipped_when_the_app_entry_is_not_installed(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _defaults, _calls = self._service(Path(directory))
            service.enable()
            self.assertNotIn(APP_DESKTOP_ID, service.mimeapps_path.read_text(encoding="utf-8"))

    def test_the_app_entry_is_never_recorded_or_restored_as_the_previous_handler(self):
        with tempfile.TemporaryDirectory() as directory:
            mime = "x-scheme-handler/roblox-player"
            service, _defaults, calls = self._service(Path(directory), {mime: APP_DESKTOP_ID})
            service.enable()
            self.assertIsNone(service.setup_store.load().previous_url_defaults[mime])
            service.disable()
            self.assertNotIn(["/usr/bin/xdg-mime", "default", APP_DESKTOP_ID, mime], calls)

    def test_supported_schemes_route_to_player_or_studio(self):
        self.assertEqual(
            {"roblox": "player", "roblox-player": "player", "roblox-studio": "studio", "roblox-studio-auth": "studio"},
            SCHEME_TO_APP,
        )
        self.assertEqual(set(SUPPORTED_SCHEMES), set(SCHEME_TO_APP))
        for scheme, app_type in SCHEME_TO_APP.items():
            parsed = parse_roblox_url(f"{scheme}:play?launch=abc")
            self.assertEqual(app_type, parsed.app_type)
            self.assertEqual(scheme, parsed.scheme)

    def test_player_uri_is_passed_as_a_single_non_shell_launch_argument(self):
        uri = "roblox-player:1+launchmode:play+placeId:123"
        arguments = build_launch_arguments("/usr/bin/rosemary", "/tmp/RobloxPlayer.app", (uri,))
        self.assertEqual(["/usr/bin/rosemary", "/tmp/RobloxPlayer.app/", "--", uri], arguments)

    def test_guest_arguments_spelled_like_rosemary_flags_still_reach_roblox(self):
        arguments = build_launch_arguments("/usr/bin/rosemary", "/tmp/RobloxStudio.app", ("-n", "--native"))
        self.assertEqual(["/usr/bin/rosemary", "/tmp/RobloxStudio.app/", "--", "-n", "--native"], arguments)

    def test_rejects_unsupported_or_malformed_urls(self):
        for uri in ("https://roblox.com/", "file:///tmp/a", "roblox", "roblox-player:\nhello", "roblox-studio: bad"):
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                parse_roblox_url(uri)

    def test_enable_registers_all_schemes_and_preserves_previous_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous = {"x-scheme-handler/roblox-player": "browser.desktop"}
            service, defaults, calls = self._service(root, previous)
            status = service.enable()
            self.assertTrue(status.enabled)
            self.assertTrue(status.registered)
            self.assertEqual(DESKTOP_ID, defaults["x-scheme-handler/roblox-player"])
            self.assertIn("browser.desktop", service.setup_store.load().previous_url_defaults.values())
            self.assertEqual(len(SUPPORTED_SCHEMES), sum(call[1] == "default" for call in calls))
            desktop = service.desktop_path.read_text(encoding="utf-8")
            self.assertIn("--roblox-url %u", desktop)
            for scheme in SUPPORTED_SCHEMES:
                self.assertIn(f"x-scheme-handler/{scheme};", desktop)

    def test_disable_restores_previous_defaults_and_removes_only_owned_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mime = "x-scheme-handler/roblox-player"
            service, defaults, _calls = self._service(root, {mime: "browser.desktop"})
            service.enable()
            other_file = service.desktop_path.parent / "other.desktop"
            other_file.write_text("owned by another app", encoding="utf-8")
            service.mimeapps_path.parent.mkdir(parents=True, exist_ok=True)
            service.mimeapps_path.write_text(
                "[Default Applications]\n"
                f"{mime}={DESKTOP_ID};browser.desktop;\n"
                "x-scheme-handler/https=browser.desktop;\n"
                "[Added Associations]\n"
                f"{mime}={DESKTOP_ID};other.desktop;\n",
                encoding="utf-8",
            )
            status = service.disable()
            self.assertFalse(status.enabled)
            self.assertEqual("browser.desktop", defaults[mime])
            self.assertFalse(service.desktop_path.exists())
            self.assertTrue(other_file.is_file())
            updated = service.mimeapps_path.read_text(encoding="utf-8")
            self.assertNotIn(DESKTOP_ID, updated)
            self.assertIn("x-scheme-handler/https=browser.desktop;", updated)
            self.assertIn("other.desktop;", updated)

    def test_disable_without_previous_handlers_succeeds_when_lookup_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _defaults, _calls = self._service(Path(directory), fallback_to_installed=True)
            self.assertTrue(service.enable().enabled)
            status = service.disable()
            self.assertFalse(status.enabled)
            self.assertFalse(status.registered)
            self.assertFalse(service.setup_store.load().url_handlers_enabled)
            self.assertFalse(service.desktop_path.exists())
            for scheme in SUPPORTED_SCHEMES:
                self.assertIsNone(service._query_default(f"x-scheme-handler/{scheme}"))

    def test_failed_enable_without_previous_handlers_rolls_back_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _defaults, _calls = self._service(
                Path(directory),
                fail_on_set="x-scheme-handler/roblox-studio-auth",
                fallback_to_installed=True,
            )
            with self.assertRaises(URLHandlerError) as raised:
                service.enable()
            self.assertNotIn("Rollback was incomplete", str(raised.exception))
            self.assertFalse(service.desktop_path.exists())
            self.assertEqual({}, service.setup_store.load().previous_url_defaults)

    def test_disable_does_not_replace_external_default_changed_after_enable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mime = "x-scheme-handler/roblox"
            service, defaults, _calls = self._service(root, {mime: "original.desktop"})
            service.enable()
            defaults[mime] = "new-owner.desktop"
            service.disable()
            self.assertEqual("new-owner.desktop", defaults[mime])

    def test_external_file_with_same_desktop_id_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, _defaults, _calls = self._service(root)
            service.desktop_path.parent.mkdir(parents=True, exist_ok=True)
            service.desktop_path.write_text("user-owned desktop entry", encoding="utf-8")
            with self.assertRaises(URLHandlerError):
                service.enable()
            self.assertEqual("user-owned desktop entry", service.desktop_path.read_text(encoding="utf-8"))

    def test_failed_registration_rolls_back_and_does_not_mark_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            failing_type = "x-scheme-handler/roblox-studio-auth"
            service, defaults, _calls = self._service(
                root, {"x-scheme-handler/roblox": "old.desktop"}, fail_on_set=failing_type
            )
            with self.assertRaises(URLHandlerError):
                service.enable()
            self.assertFalse(service.setup_store.load().url_handlers_enabled)
            self.assertFalse(any(
                service._query_default(f"x-scheme-handler/{scheme}") == DESKTOP_ID
                for scheme in SUPPORTED_SCHEMES
            ))

    def test_missing_registration_is_reported_as_discrepancy(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _defaults, _calls = self._service(Path(directory))
            service.setup_store.save_url_handlers(True, {})
            status = service.check()
        self.assertFalse(status.enabled)
        self.assertIn("missing", status.message)


class RosemarySetupTests(unittest.TestCase):
    def _bundle(self, root, app_type):
        name = "RobloxPlayer.app" if app_type == "player" else "RobloxStudio.app"
        bundle = root / name
        (bundle / "Contents").mkdir(parents=True)
        return bundle

    def test_input_setting_is_applied_only_to_supported_rosemary_launches(self):
        for app_type in ("player", "studio"):
            with self.subTest(app_type=app_type), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                bundle = self._bundle(root, app_type)
                executable = root / "rosemary"
                executable.write_bytes(b"ELF\0" + PLAIN_CTRL_CLICK_VARIABLE.encode() + b"\0")
                executable.chmod(0o755)
                record = {"app_type": app_type, "bundle_path": str(bundle)}
                fake_process = subprocess.CompletedProcess([], 0)
                fake_process.pid = 23456
                fake_process.stdout = None
                fake_process.stderr = None
                fake_process.poll = lambda: None
                original_value = os.environ.get(PLAIN_CTRL_CLICK_VARIABLE)
                with patch.dict(os.environ, {"ROSEBLOX_TEST_ENV": "preserved", PLAIN_CTRL_CLICK_VARIABLE: "0"}):
                    with patch("roseblox.backend.rosemary.subprocess.Popen", return_value=fake_process) as popen:
                        session = launch_application(
                            record, RosemaryStatus(str(executable)),
                            guest_arguments=("roblox-player:1+launchmode:play",),
                            log_directory=root / "logs",
                        )
                environment = popen.call_args.kwargs["env"]
                self.assertEqual("1", environment[PLAIN_CTRL_CLICK_VARIABLE])
                self.assertEqual("preserved", environment["ROSEBLOX_TEST_ENV"])
                self.assertEqual(original_value, os.environ.get(PLAIN_CTRL_CLICK_VARIABLE))
                self.assertTrue(session.plain_ctrl_click_active)
                self.assertEqual(
                    "roblox-player:1+launchmode:play", popen.call_args.args[0][-1]
                )

    def test_turning_the_input_setting_off_keeps_rosemary_default_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self._bundle(root, "player")
            executable = root / "rosemary"
            executable.write_bytes(b"ELF\0" + PLAIN_CTRL_CLICK_VARIABLE.encode() + b"\0")
            executable.chmod(0o755)
            fake_process = subprocess.CompletedProcess([], 0)
            fake_process.pid = 23458
            fake_process.poll = lambda: None
            with patch.dict(os.environ, {PLAIN_CTRL_CLICK_VARIABLE: "1"}):
                with patch("roseblox.backend.rosemary.subprocess.Popen", return_value=fake_process) as popen:
                    session = launch_application(
                        {"app_type": "player", "bundle_path": str(bundle)},
                        RosemaryStatus(str(executable)),
                        log_directory=root / "logs",
                        plain_ctrl_click=False,
                    )
            self.assertNotIn(PLAIN_CTRL_CLICK_VARIABLE, popen.call_args.kwargs["env"])
            self.assertFalse(session.plain_ctrl_click_active)

    def test_unsupported_rosemary_launch_does_not_claim_fix_is_active(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self._bundle(root, "player")
            executable = root / "rosemary"
            executable.write_bytes(b"ELF\0old build\0")
            executable.chmod(0o755)
            record = {"app_type": "player", "bundle_path": str(bundle)}
            fake_process = subprocess.CompletedProcess([], 0)
            fake_process.pid = 23457
            fake_process.stdout = None
            fake_process.stderr = None
            fake_process.poll = lambda: None
            with patch.dict(os.environ, {}, clear=True):
                with patch("roseblox.backend.rosemary.subprocess.Popen", return_value=fake_process) as popen:
                    session = launch_application(record, RosemaryStatus(str(executable)), log_directory=root / "logs")
            self.assertNotIn(PLAIN_CTRL_CLICK_VARIABLE, popen.call_args.kwargs["env"])
            self.assertFalse(session.plain_ctrl_click_active)
            self.assertFalse(rosemary_supports_plain_ctrl_click(executable))

    def test_release_launcher_reports_the_runtime_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bin").mkdir()
            (root / "libexec").mkdir()
            launcher = root / "bin" / "rosemary"
            launcher.write_bytes(b"ELF\0static launcher\0")
            launcher.chmod(0o755)
            (root / "AppRun").symlink_to("bin/rosemary")
            runtime = root / "libexec" / "rosemary"
            runtime.write_bytes(b"ELF\0old build\0")
            self.assertFalse(rosemary_supports_plain_ctrl_click(root / "AppRun"))
            runtime.write_bytes(b"ELF\0newer build\0" + PLAIN_CTRL_CLICK_VARIABLE.encode() + b"\0")
            self.assertTrue(rosemary_supports_plain_ctrl_click(root / "AppRun"))
            self.assertTrue(rosemary_supports_plain_ctrl_click(launcher))

    def test_configure_and_restore_rosemary_path_preserves_other_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "rosemary"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
            config = root / "settings.json"
            config.write_text(json.dumps({"other": "kept"}), encoding="utf-8")
            save_rosemary_executable(executable, config)
            self.assertEqual(str(executable), detect_rosemary(config).executable)
            save_rosemary_executable(None, config)
            self.assertEqual({"other": "kept"}, json.loads(config.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
