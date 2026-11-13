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


class ReleasePackageTests(unittest.TestCase):
    def test_release_attaches_what_the_updater_downloads(self):
        from roseblox.backend import self_update

        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        for name in (self_update.APPIMAGE_ASSET, self_update.CHECKSUMS_ASSET, f"{self_update.APPIMAGE_ASSET}.zsync"):
            self.assertIn(f"dist/{name}", workflow)
        self.assertIn("sha256sum roseblox_*_all.deb Roseblox-x86_64.AppImage > SHA256SUMS", workflow)
        self.assertIn("runs-on: ubuntu-24.04", workflow)

        builder = (ROOT / "packaging" / "appimage" / "build_appimage.py").read_text(encoding="utf-8")
        self.assertIn(f'OUTPUT_NAME = "{self_update.APPIMAGE_ASSET}"', builder)
        self.assertIn(f"|{self_update.REPOSITORY.replace('/', '|')}|latest|", builder)

    def test_apprun_saves_every_variable_it_changes(self):
        apprun = (ROOT / "packaging" / "appimage" / "AppRun").read_text(encoding="utf-8")
        self.assertEqual(0, subprocess.run(["/bin/sh", "-n", str(ROOT / "packaging" / "appimage" / "AppRun")]).returncode)
        saved = set(re.search(r'ROSEBLOX_HOST_VARS="([^"]+)"', apprun).group(1).replace("\\\n", " ").split())
        changed = set(re.findall(r"^\s*export ([A-Z_]+)=", apprun, re.MULTILINE))
        changed -= {"ROSEBLOX_APPDIR", "ROSEBLOX_APPIMAGE"}
        self.assertTrue(changed)
        self.assertLessEqual(changed, saved)

    def test_deb_package_installs_into_the_system_python(self):
        if subprocess.run(["sh", "-c", "command -v dpkg-deb"], capture_output=True).returncode != 0:
            self.skipTest("needs dpkg-deb")
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                ["/bin/sh", str(ROOT / "packaging" / "debian" / "build-deb.sh"), directory],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            package = Path(result.stdout.strip())
            contents = subprocess.run(["dpkg-deb", "--contents", str(package)], capture_output=True, text=True).stdout
            fields = subprocess.run(["dpkg-deb", "--field", str(package)], capture_output=True, text=True).stdout
        self.assertIn("./usr/lib/python3/dist-packages/roseblox/__main__.py", contents)
        self.assertIn("./usr/share/applications/io.github.roseblox.Roseblox.desktop", contents)
        self.assertNotIn("__pycache__", contents)
        self.assertIn("gir1.2-adw-1 (>= 1.4)", fields)


class ArchPackagingTests(unittest.TestCase):
    def test_recipe_uses_the_versioned_archive_and_real_runtime_dependencies(self):
        recipe = (ROOT / "packaging" / "arch" / "PKGBUILD").read_text(encoding="utf-8")
        self.assertIn(
            "https://github.com/MadnessMyth/RoseBlox/archive/refs/tags/v${pkgver}.tar.gz",
            recipe,
        )
        self.assertIn("79bfcd2403d3b19340ba618de08c99968fcd4f5d7a6855840ea75238d429deac", recipe)
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

    def test_srcinfo_lists_what_each_recipe_declares(self):
        # The AUR reads only .SRCINFO, so a PKGBUILD change without `makepkg --printsrcinfo` goes unseen there.
        for directory in ("arch", "arch-git"):
            recipe = (ROOT / "packaging" / directory / "PKGBUILD").read_text(encoding="utf-8")
            srcinfo = (ROOT / "packaging" / directory / ".SRCINFO").read_text(encoding="utf-8")
            for field in ("depends", "makedepends", "optdepends", "sha256sums"):
                declared = re.findall(r"'([^']*)'", re.search(rf"(?ms)^{field}=\((.*?)\)$", recipe).group(1))
                self.assertEqual(declared, re.findall(rf"(?m)^\t{field} = (.*)$", srcinfo), f"{directory}: {field}")
            pkgver = re.search(r"(?m)^pkgver=(.*)$", recipe).group(1)
            self.assertEqual([pkgver], re.findall(r"(?m)^\tpkgver = (.*)$", srcinfo), directory)

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
