import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from roseblox.activity_service import watcher_arguments
from roseblox.backend import appimage


class AppImageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.appdir = self.root / "mount"
        self.package = self.appdir / "usr" / "share" / "roseblox" / "roseblox"
        self.package.mkdir(parents=True)
        self.file = self.root / "Applications" / "Roseblox-x86_64.AppImage"
        self.file.parent.mkdir()
        self.file.write_bytes(b"")
        self.environ = {
            "HOME": str(self.root),
            "XDG_DATA_HOME": str(self.root / "data"),
            "ROSEBLOX_APPIMAGE": str(self.file),
            "ROSEBLOX_APPDIR": str(self.appdir),
        }

    def _inside(self):
        return patch.object(appimage, "_PACKAGE", self.package)

    def test_path_needs_the_package_inside_the_appdir(self):
        self.assertEqual(str(self.file), appimage.appimage_path(self.environ, self.package))
        self.assertIsNone(appimage.appimage_path(self.environ, self.root / "src" / "roseblox"))
        self.assertIsNone(appimage.appimage_path({}, self.package))

    def test_restore_gives_children_the_host_environment(self):
        environ = {
            "ROSEBLOX_HOST_VARS": "LD_LIBRARY_PATH PYTHONHOME XDG_DATA_DIRS",
            "LD_LIBRARY_PATH": "/mount/usr/lib/libs:/host/lib",
            "ROSEBLOX_HOST_LD_LIBRARY_PATH": "/host/lib",
            "PYTHONHOME": "/mount/usr",
            "XDG_DATA_DIRS": "/mount/usr/share:/usr/share",
            "ROSEBLOX_HOST_XDG_DATA_DIRS": "/usr/share",
            "APPIMAGE": "/x.AppImage",
            "APPDIR": "/mount",
            "ROSEBLOX_APPIMAGE": "/x.AppImage",
        }
        appimage.restore_host_environment(environ)
        self.assertEqual(
            {"LD_LIBRARY_PATH": "/host/lib", "XDG_DATA_DIRS": "/usr/share", "ROSEBLOX_APPIMAGE": "/x.AppImage"},
            environ,
        )
        appimage.restore_host_environment(environ)
        self.assertEqual("/host/lib", environ["LD_LIBRARY_PATH"])

    def test_integration_adds_and_refreshes_the_menu_entry(self):
        desktop = self.root / "data" / "applications" / appimage.DESKTOP_ID
        icon = self.root / "data" / "icons" / "hicolor" / "scalable" / "apps" / f"{appimage.APP_ID}.svg"
        with self._inside():
            self.assertTrue(appimage.integrate(self.environ))
            self.assertFalse(appimage.integrate(self.environ))
            self.assertIn(f'Exec="{self.file}" %u', desktop.read_text())
            self.assertIn("StartupWMClass=io.github.roseblox.Roseblox", desktop.read_text())
            self.assertTrue(icon.read_bytes().startswith(b"<"))

            moved = self.root / "Roseblox moved.AppImage"
            self.file.rename(moved)
            self.assertTrue(appimage.integrate({**self.environ, "ROSEBLOX_APPIMAGE": str(moved)}))
            self.assertIn(f'Exec="{moved}" %u', desktop.read_text())

            appimage.remove_integration(self.environ)
        self.assertFalse(desktop.exists())
        self.assertFalse(icon.exists())

    def test_integration_leaves_other_installations_alone(self):
        desktop = self.root / "data" / "applications" / appimage.DESKTOP_ID
        desktop.parent.mkdir(parents=True)
        desktop.write_text("[Desktop Entry]\nExec=/home/user/.local/bin/roseblox %u\n")
        with self._inside():
            self.assertFalse(appimage.integrate(self.environ))
            appimage.remove_integration(self.environ)
        self.assertIn(".local/bin/roseblox", desktop.read_text())

    def test_quoting_keeps_exec_paths_literal(self):
        entry = appimage.desktop_entry('/home/a "b"/$x.AppImage')
        self.assertIn('Exec="/home/a \\"b\\"/\\$x.AppImage" %u', entry)

    def test_background_programs_start_the_appimage_file(self):
        with self._inside(), patch.dict("os.environ", self.environ, clear=True):
            self.assertEqual(str(self.file), appimage.which("roseblox"))
            arguments = watcher_arguments(5, 6, Path("/log"))
        self.assertEqual([str(self.file), "--activity-watch", "5", "6", "/log"], arguments)

    def test_bundled_tools_are_only_used_inside_the_appimage(self):
        tool = self.appdir / "usr" / "bin" / "bsdtar"
        tool.parent.mkdir(parents=True)
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755)
        with self._inside():
            self.assertEqual(str(tool), appimage.bundled_tool("bsdtar", self.environ))
            self.assertIsNone(appimage.bundled_tool("bsdtar", {}))


if __name__ == "__main__":
    unittest.main()
