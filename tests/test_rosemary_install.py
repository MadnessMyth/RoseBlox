import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from roseblox.backend.rosemary_install import (
    INSTALL_METHOD_APPIMAGE,
    INSTALL_METHOD_AUR,
    INSTALL_METHOD_MANUAL,
    ROSEMARY_AUR_PACKAGE,
    ROSEMARY_INSTALL_PACKAGES,
    RosemaryInstallError,
    appimage_search_directories,
    build_install_command,
    find_aur_helper,
    find_rosemary_appimage,
    find_terminal,
    install_method,
    plan_rosemary_install,
    prepare_appimage,
    start_rosemary_install,
)
from roseblox.backend.system import SystemInfo


def lookup_for(*available):
    paths = {name: f"/usr/bin/{name}" for name in available}
    return paths.get


class RosemaryInstallPlanTests(unittest.TestCase):
    def test_prefers_paru_then_yay(self):
        self.assertEqual("/usr/bin/paru", find_aur_helper(lookup_for("yay", "paru")))
        self.assertEqual("/usr/bin/yay", find_aur_helper(lookup_for("yay")))
        self.assertIsNone(find_aur_helper(lookup_for("pacman")))

    def test_terminal_from_environment_is_preferred_when_supported(self):
        lookup = lookup_for("konsole", "kitty", "xterm")
        self.assertEqual(("/usr/bin/kitty", ()), find_terminal(lookup, {"TERMINAL": "/usr/local/bin/kitty"}))
        self.assertEqual(("/usr/bin/konsole", ("-e",)), find_terminal(lookup, {"TERMINAL": "unknown-term"}))
        self.assertIsNone(find_terminal(lookup_for("paru"), {}))

    def test_install_command_passes_helper_and_packages_as_arguments(self):
        command = build_install_command("/usr/bin/gnome-terminal", ("--wait", "--"), "/usr/bin/paru")
        self.assertEqual(("/usr/bin/gnome-terminal", "--wait", "--", "/bin/sh", "-c"), command[:5])
        self.assertEqual(("/usr/bin/paru", *ROSEMARY_INSTALL_PACKAGES), command[-4:])
        self.assertEqual((ROSEMARY_AUR_PACKAGE, "sdl3", "vulkan-icd-loader"), ROSEMARY_INSTALL_PACKAGES)
        self.assertNotIn("paru", command[5])

    def _stub(self, directory, name, body):
        path = Path(directory) / name
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_install_script_installs_packages_then_prepares_the_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = self._stub(directory, "paru", 'echo "helper: $*"\nexit "$STUB_STATUS"')
            self._stub(directory, "rosemary", 'echo "rosemary: $* setup=$ROSEMARY_PREFIX_SETUP"\nexit "$PREFIX_STATUS"')
            cases = (
                (0, 0, "Rosemary is installed. You can return", True),
                (0, 1, "its prefix is not set up yet", True),
                (3, 0, "exit status 3", False),
            )
            for status, prefix_status, outcome, prepares_prefix in cases:
                with self.subTest(status=status, prefix_status=prefix_status):
                    environment = (
                        f"PATH={directory}:/usr/bin:/bin",
                        f"STUB_STATUS={status}",
                        f"PREFIX_STATUS={prefix_status}",
                    )
                    command = build_install_command("/usr/bin/env", environment, str(helper))
                    result = subprocess.run(command, input="\n", capture_output=True, text=True)
                    self.assertEqual(status, result.returncode)
                    self.assertIn("helper: -S --needed rosemary sdl3 vulkan-icd-loader", result.stdout)
                    self.assertIn(outcome, result.stdout)
                    self.assertEqual(
                        prepares_prefix, "rosemary: prefix create setup=download" in result.stdout
                    )

    def test_plan_requires_helper_and_terminal(self):
        plan = plan_rosemary_install(lookup_for("pacman", "yay", "foot"), {})
        self.assertEqual("yay", plan.helper_name)
        self.assertEqual("yay -S --needed rosemary sdl3 vulkan-icd-loader", plan.display_command)
        self.assertEqual("/usr/bin/foot", plan.command[0])

        with self.assertRaisesRegex(RosemaryInstallError, "Arch-based"):
            plan_rosemary_install(lookup_for("foot"), {})
        with self.assertRaisesRegex(RosemaryInstallError, "No AUR helper"):
            plan_rosemary_install(lookup_for("pacman", "foot"), {})
        with self.assertRaisesRegex(RosemaryInstallError, "paru -S --needed rosemary sdl3"):
            plan_rosemary_install(lookup_for("pacman", "paru"), {})

    def test_start_opens_terminal_without_a_shell_string(self):
        plan = plan_rosemary_install(lookup_for("pacman", "paru", "xterm"), {})
        popen = Mock(return_value="process")
        self.assertEqual("process", start_rosemary_install(plan, popen))
        arguments, options = popen.call_args
        self.assertEqual(list(plan.command), arguments[0])
        self.assertNotIn("shell", options)
        self.assertTrue(options["start_new_session"])

        failing = Mock(side_effect=FileNotFoundError("xterm"))
        with self.assertRaisesRegex(RosemaryInstallError, "Could not open a terminal"):
            start_rosemary_install(plan, failing)


class RosemaryAppImageTests(unittest.TestCase):
    def test_install_method_by_system(self):
        self.assertEqual(INSTALL_METHOD_AUR, install_method(SystemInfo("cachyos", ("arch",), machine="x86_64")))
        self.assertEqual(INSTALL_METHOD_AUR, install_method(SystemInfo("arch", machine="aarch64")))
        self.assertEqual(INSTALL_METHOD_APPIMAGE, install_method(SystemInfo("ubuntu", ("debian",), machine="x86_64")))
        self.assertEqual(INSTALL_METHOD_APPIMAGE, install_method(SystemInfo("nixos", machine="x86_64")))
        self.assertEqual(INSTALL_METHOD_MANUAL, install_method(SystemInfo("fedora", machine="aarch64")))

    def test_search_directories_follow_the_translated_downloads_folder(self):
        with tempfile.TemporaryDirectory() as home:
            environ = {"HOME": home}
            self.assertEqual(
                [Path(home, "Downloads"), Path(home, "Applications"), Path(home, ".local/bin"), Path(home, "bin")],
                appimage_search_directories(environ),
            )
            config = Path(home, ".config")
            config.mkdir()
            (config / "user-dirs.dirs").write_text('XDG_DOWNLOAD_DIR="$HOME/Téléchargements"\n', encoding="utf-8")
            self.assertEqual(Path(home, "Téléchargements"), appimage_search_directories(environ)[0])

    def test_finds_the_newest_rosemary_appimage_only(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory, "a"), Path(directory, "b")
            first.mkdir()
            second.mkdir()
            self.assertIsNone(find_rosemary_appimage([first, second, Path(directory, "missing")]))
            (first / "Rosemary-0.4.1-x86_64.AppImage").write_text("", encoding="utf-8")
            (first / "Other-9.9.9-x86_64.AppImage").write_text("", encoding="utf-8")
            (first / "Rosemary-0.10.0-x86_64.AppImage").mkdir()
            (second / "rosemary-0.5.0-x86_64.appimage").write_text("", encoding="utf-8")
            self.assertEqual(second / "rosemary-0.5.0-x86_64.appimage", find_rosemary_appimage([first, second]))

    def test_prepare_appimage_makes_only_appimages_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            appimage = Path(directory, "Rosemary-0.5.0-x86_64.AppImage")
            appimage.write_text("", encoding="utf-8")
            appimage.chmod(0o644)
            self.assertEqual(str(appimage), prepare_appimage(appimage))
            self.assertEqual(0o744, appimage.stat().st_mode & 0o777)

            other = Path(directory, "rosemary")
            other.write_text("", encoding="utf-8")
            other.chmod(0o644)
            self.assertEqual(str(other), prepare_appimage(other))
            self.assertEqual(0o644, other.stat().st_mode & 0o777)


if __name__ == "__main__":
    unittest.main()
