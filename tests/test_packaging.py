import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = ROOT / "packaging" / "generic" / "install.sh"


class GenericInstallTests(unittest.TestCase):
    def _run(self, home, *arguments):
        environment = {
            "HOME": str(home),
            "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
            "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
            "ALLOW_ROOT": "1",
        }
        return subprocess.run(
            ["/bin/sh", str(INSTALL_SCRIPT), *arguments], env=environment, capture_output=True, text=True, timeout=60
        )

    def test_installs_a_working_launcher_and_uninstalls_it(self):
        try:
            import gi

            gi.require_version("Gtk", "4.0")
            gi.require_version("Adw", "1")
        except (ImportError, ValueError):
            self.skipTest("needs PyGObject with GTK 4 and libadwaita")
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            result = self._run(home)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            local = home / ".local"
            launcher = local / "bin" / "roseblox"
            desktop = local / "share" / "applications" / "io.github.roseblox.Roseblox.desktop"
            self.assertTrue((local / "lib" / "roseblox" / "roseblox" / "__main__.py").is_file())
            self.assertFalse(list((local / "lib" / "roseblox").rglob("__pycache__")))
            self.assertTrue((local / "share" / "icons" / "hicolor" / "scalable" / "apps" / "io.github.roseblox.Roseblox.svg").is_file())
            self.assertIn(f'Exec="{launcher}" %u', desktop.read_text(encoding="utf-8"))
            self.assertIn("is not in your PATH", result.stdout)

            usage = subprocess.run(
                [str(launcher), "--activity-watch"], env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(2, usage.returncode)
            self.assertIn("Usage: roseblox --activity-watch", usage.stderr)

            self.assertEqual(0, self._run(home).returncode)
            result = self._run(home, "--uninstall")
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertFalse(launcher.exists())
            self.assertFalse(desktop.exists())
            self.assertFalse((local / "lib" / "roseblox").exists())

    def test_refuses_to_replace_files_it_did_not_install(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".local" / "lib" / "roseblox").mkdir(parents=True)
            result = self._run(home, "--uninstall")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("was not installed by this script", result.stderr)
            self.assertTrue((home / ".local" / "lib" / "roseblox").is_dir())


class ArchPackagingTests(unittest.TestCase):
    def test_recipe_uses_the_versioned_archive_and_real_runtime_dependencies(self):
        recipe = (ROOT / "packaging" / "arch" / "PKGBUILD").read_text(encoding="utf-8")
        self.assertIn(
            "https://github.com/MadnessMyth/RoseBlox/archive/refs/tags/v${pkgver}.tar.gz",
            recipe,
        )
        self.assertIn("7ebbb72730fd384b946900615626c22d1889cf7ae8c4642d90cc7e3f380bd512", recipe)
        self.assertNotIn("makedepends=('git'", recipe)
        self.assertIn("depends=('gtk4' 'libadwaita' 'python' 'python-gobject' 'xdg-utils')", recipe)
        self.assertIn("libarchive", recipe)
        self.assertIn("python -m build --wheel --no-isolation", recipe)
        self.assertIn("python -m installer --destdir=", recipe)
        self.assertNotRegex(recipe, r"(?m)^(depends|optdepends)=.*rosemary")
        self.assertNotIn("pip install", recipe)

    def test_git_recipe_tracks_master_and_replaces_the_release_package(self):
        recipe = (ROOT / "packaging" / "arch-git" / "PKGBUILD").read_text(encoding="utf-8")
        release = (ROOT / "packaging" / "arch" / "PKGBUILD").read_text(encoding="utf-8")
        self.assertIn("pkgname=roseblox-git", recipe)
        self.assertIn("git+https://github.com/MadnessMyth/RoseBlox.git#branch=master", recipe)
        self.assertIn("makedepends=('git'", recipe)
        self.assertIn('provides=("$_pkgname")', recipe)
        self.assertIn('conflicts=("$_pkgname")', recipe)
        self.assertIn("pkgver() {", recipe)
        runtime = re.search(r"(?m)^depends=.*$", release).group(0)
        self.assertIn(runtime, recipe)
        self.assertNotRegex(recipe, r"(?m)^(depends|optdepends)=.*rosemary")
        for asset in ("io.github.roseblox.Roseblox.desktop", "io.github.roseblox.Roseblox.svg"):
            self.assertIn(f"packaging/arch/{asset}", recipe)
            self.assertTrue((ROOT / "packaging" / "arch" / asset).is_file())

    def test_desktop_entry_references_the_packaged_hicolor_icon(self):
        desktop = (ROOT / "packaging" / "arch" / "io.github.roseblox.Roseblox.desktop").read_text(
            encoding="utf-8"
        )
        self.assertIn("Type=Application", desktop)
        self.assertIn("Exec=roseblox %u\n", desktop)
        self.assertNotIn("MimeType=", desktop)
        self.assertIn("TryExec=roseblox", desktop)
        self.assertIn("StartupWMClass=io.github.roseblox.Roseblox\n", desktop)
        self.assertIn("Icon=io.github.roseblox.Roseblox", desktop)
        icon = ROOT / "packaging" / "arch" / "io.github.roseblox.Roseblox.svg"
        self.assertTrue(icon.is_file())
        self.assertIn("/usr/share/icons/hicolor/scalable/apps/", (ROOT / "packaging" / "arch" / "PKGBUILD").read_text(encoding="utf-8"))

    def test_release_docs_are_explicit_about_unpublished_and_license_state(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        checklist = (ROOT / "packaging" / "RELEASE.md").read_text(encoding="utf-8")
        self.assertIn("AUR package has not been published yet", readme)
        self.assertIn("MIT License", readme)
        self.assertIn("license=('MIT')", (ROOT / "packaging" / "arch" / "PKGBUILD").read_text(encoding="utf-8"))
        self.assertIn("Copyright (c) 2026 MadnessMyth", (ROOT / "LICENSE").read_text(encoding="utf-8"))
        self.assertIn("/usr/share/licenses/$pkgname/LICENSE", (ROOT / "packaging" / "arch" / "PKGBUILD").read_text(encoding="utf-8"))
        self.assertRegex(checklist, r"(?m)^## Release notes — v0\.1\.0$")


if __name__ == "__main__":
    unittest.main()
