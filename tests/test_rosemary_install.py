import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from roseblox.backend.rosemary_install import (
    ROSEMARY_AUR_PACKAGE,
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

    def test_install_command_passes_helper_and_package_as_arguments(self):
        command = build_install_command("/usr/bin/gnome-terminal", ("--wait", "--"), "/usr/bin/paru")
        self.assertEqual(("/usr/bin/gnome-terminal", "--wait", "--", "/bin/sh", "-c"), command[:5])
        self.assertEqual(("/usr/bin/paru", ROSEMARY_AUR_PACKAGE), command[-2:])
        self.assertNotIn("paru", command[5])

    def test_install_script_runs_helper_and_preserves_exit_status(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / "paru"
            helper.write_text('#!/bin/sh\necho "helper: $*"\nexit "$STUB_STATUS"\n', encoding="utf-8")
            helper.chmod(0o755)
            for status, outcome in ((0, "Rosemary is installed"), (3, "exit status 3")):
                with self.subTest(status=status):
                    command = build_install_command("/usr/bin/env", (f"STUB_STATUS={status}",), str(helper))
                    result = subprocess.run(command, input="\n", capture_output=True, text=True)
                    self.assertEqual(status, result.returncode)
                    self.assertIn(f"helper: -S --needed {ROSEMARY_AUR_PACKAGE}", result.stdout)
                    self.assertIn(outcome, result.stdout)

    def test_plan_requires_helper_and_terminal(self):
        plan = plan_rosemary_install(lookup_for("pacman", "yay", "foot"), {})
        self.assertEqual("yay", plan.helper_name)
        self.assertEqual("yay -S --needed rosemary", plan.display_command)
        self.assertEqual("/usr/bin/foot", plan.command[0])

        with self.assertRaisesRegex(RosemaryInstallError, "Arch-based"):
            plan_rosemary_install(lookup_for("foot"), {})
        with self.assertRaisesRegex(RosemaryInstallError, "No AUR helper"):
            plan_rosemary_install(lookup_for("pacman", "foot"), {})
        with self.assertRaisesRegex(RosemaryInstallError, "paru -S --needed rosemary"):
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
