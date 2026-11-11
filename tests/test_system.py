import tempfile
import unittest
from pathlib import Path

from roseblox.backend.system import SystemInfo, detect_system, install_command, install_hint, parse_os_release


def _lookup(*available):
    return {name: f"/usr/bin/{name}" for name in available}.get


class SystemTests(unittest.TestCase):
    def _detect(self, text, *available, machine="x86_64"):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "os-release"
            path.write_text(text, encoding="utf-8")
            return detect_system((path,), _lookup(*available), machine)

    def test_parses_quoted_and_unquoted_values(self):
        values = parse_os_release('# comment\nNAME="Fedora Linux"\nID=fedora\nID_LIKE=\'rhel centos\'\n\nbroken line\n')
        self.assertEqual({"NAME": "Fedora Linux", "ID": "fedora", "ID_LIKE": "rhel centos"}, values)

    def test_distribution_families(self):
        cases = (
            ('ID=cachyos\nID_LIKE=arch\nPRETTY_NAME="CachyOS"\n', "pacman", True),
            ("ID=arch\n", "pacman", True),
            ('ID=linuxmint\nID_LIKE="ubuntu debian"\n', "apt", False),
            ("ID=debian\n", "apt", False),
            ("ID=fedora\n", "dnf", False),
            ('ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n', "zypper", False),
            ("ID=nixos\n", None, False),
        )
        for text, manager, arch_based in cases:
            with self.subTest(text=text):
                info = self._detect(text)
                self.assertEqual(manager, info.package_manager)
                self.assertEqual(arch_based, info.arch_based)

    def test_unknown_system_with_pacman_counts_as_arch_based(self):
        self.assertTrue(self._detect("ID=custom\n", "pacman").arch_based)
        self.assertTrue(detect_system((Path("/nonexistent/os-release"),), _lookup("pacman"), "x86_64").arch_based)
        self.assertFalse(detect_system((Path("/nonexistent/os-release"),), _lookup(), "x86_64").arch_based)

    def test_name_and_machine(self):
        info = self._detect('NAME=Ubuntu\nPRETTY_NAME="Ubuntu 24.04.5 LTS"\nID=ubuntu\n', machine="aarch64")
        self.assertEqual("Ubuntu 24.04.5 LTS", info.name)
        self.assertEqual("aarch64", info.machine)

    def test_install_commands_use_each_distributions_package_name(self):
        self.assertEqual("sudo pacman -S --needed libarchive", install_command("bsdtar", SystemInfo("arch")))
        self.assertEqual("sudo apt install libarchive-tools", install_command("bsdtar", SystemInfo("ubuntu", ("debian",))))
        self.assertEqual("sudo dnf install bsdtar", install_command("bsdtar", SystemInfo("fedora")))
        self.assertEqual("sudo zypper install xdg-utils", install_command("xdg-mime", SystemInfo("opensuse-leap", ("suse",))))
        self.assertIsNone(install_command("bsdtar", SystemInfo("nixos")))
        self.assertIsNone(install_command("unknown", SystemInfo("arch")))
        self.assertEqual("sudo pacman -S --needed gtk4-layer-shell", install_command("gtk4-layer-shell", SystemInfo("arch")))
        self.assertEqual(
            "sudo apt install gir1.2-gtk4layershell-1.0", install_command("gtk4-layer-shell", SystemInfo("debian"))
        )
        self.assertIsNone(install_command("gtk4-layer-shell", SystemInfo("opensuse-leap", ("suse",))))

    def test_install_hint_falls_back_when_the_command_is_unknown(self):
        self.assertEqual("Install it with “sudo dnf install gamemode”.", install_hint("gamemoded", "x", SystemInfo("fedora")))
        self.assertEqual("Use your package manager.", install_hint("gamemoded", "Use your package manager.", SystemInfo("nixos")))


if __name__ == "__main__":
    unittest.main()
