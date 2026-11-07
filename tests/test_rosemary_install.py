import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from roseblox.backend.rosemary_install import (
    ROSEMARY_AUR_PACKAGE,
    ROSEMARY_INSTALL_PACKAGES,
    RosemaryInstallError,
    build_install_command,
    find_aur_helper,
    find_terminal,
    plan_rosemary_install,
    start_rosemary_install,
)


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


if __name__ == "__main__":
    unittest.main()
