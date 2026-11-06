import shutil
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from roseblox.backend.archive import (
    ArchiveExtractionError,
    ArchiveValidationError,
    extract_archive,
    inspect_archive,
)


def add_symlink(archive: zipfile.ZipFile, path: str, target: str) -> None:
    info = zipfile.ZipInfo(path)
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    archive.writestr(info, target.encode("utf-8"))


class ArchiveTests(unittest.TestCase):
    def test_detects_bundle_type_from_member_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "player.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("payload/RobloxPlayer.app/Contents/Info.plist", "plist")
            inspection = inspect_archive(archive_path)
            self.assertEqual(frozenset({"player"}), inspection.app_types)
            self.assertEqual("player", inspection.detected_type)

    @unittest.skipUnless(shutil.which("bsdtar"), "bsdtar is required for extraction test")
    def test_selects_outer_studio_bundle_over_nested_application_bundles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "nested-studio.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("RobloxStudio.app/Contents/Info.plist", "outer")
                archive.writestr(
                    "RobloxStudio.app/Contents/MacOS/RobloxStudio.app/Contents/Info.plist",
                    "nested executable bundle",
                )
                archive.writestr(
                    "RobloxStudio.app/Contents/MacOS/RobloxStudioInstaller.app/Contents/Info.plist",
                    "installer helper",
                )

            bundle = extract_archive(archive_path, root / "stage", "studio")
            self.assertEqual(root / "stage/RobloxStudio.app", bundle)

    @unittest.skipUnless(shutil.which("bsdtar"), "bsdtar is required for extraction test")
    def test_still_rejects_independent_expected_bundles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "ambiguous.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("one/RobloxStudio.app/Contents/Info.plist", "one")
                archive.writestr("two/RobloxStudio.app/Contents/Info.plist", "two")

            with self.assertRaisesRegex(ArchiveValidationError, "multiple"):
                extract_archive(archive_path, root / "stage", "studio")

    def test_rejects_path_traversal_and_absolute_paths(self):
        for filename in ("../../outside", "/outside", "App/../outside"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as directory:
                archive_path = Path(directory) / "bad.zip"
                with zipfile.ZipFile(archive_path, "w") as archive:
                    archive.writestr(filename, "bad")
                with self.assertRaises(ArchiveValidationError):
                    inspect_archive(archive_path)

    def test_rejects_escaping_symlinks_and_members_nested_under_links(self):
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "bad-link.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                add_symlink(archive, "App/Contents/link", "../../../outside")
            with self.assertRaisesRegex(ArchiveValidationError, "escapes"):
                inspect_archive(archive_path)

            archive_path = Path(directory) / "nested-link.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                add_symlink(archive, "App/Contents/alias", "Frameworks")
                archive.writestr("App/Contents/alias/payload", "unsafe")
            with self.assertRaisesRegex(ArchiveValidationError, "nested under symbolic link"):
                inspect_archive(archive_path)

    def test_reports_missing_bsdtar_without_installing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "player.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("RobloxPlayer.app/Contents/Info.plist", "plist")
            with patch("roseblox.backend.archive.shutil.which", return_value=None):
                with self.assertRaisesRegex(ArchiveExtractionError, "bsdtar is required"):
                    extract_archive(archive_path, Path(directory) / "stage", "player")

    @unittest.skipUnless(shutil.which("bsdtar"), "bsdtar is required for extraction test")
    def test_bsdtar_preserves_framework_symbolic_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "studio.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("RobloxStudio.app/Contents/Info.plist", "plist")
                archive.writestr(
                    "RobloxStudio.app/Contents/Frameworks/QtCore.framework/Versions/A/QtCore",
                    "framework-binary",
                )
                add_symlink(
                    archive,
                    "RobloxStudio.app/Contents/Frameworks/QtCore.framework/Versions/Current",
                    "A",
                )
                add_symlink(
                    archive,
                    "RobloxStudio.app/Contents/Frameworks/QtCore.framework/QtCore",
                    "Versions/Current/QtCore",
                )

            stage = root / "stage"
            bundle = extract_archive(archive_path, stage, "studio")
            framework = bundle / "Contents/Frameworks/QtCore.framework"
            self.assertEqual("A", (framework / "Versions/Current").readlink().as_posix())
            self.assertTrue((framework / "QtCore").is_symlink())
            self.assertEqual("framework-binary", (framework / "QtCore").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
