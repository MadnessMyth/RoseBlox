import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from roseblox import __version__
from roseblox.backend import self_update
from roseblox.backend.self_update import Release, SelfUpdateError

APPIMAGE_URL = "https://github.com/MadnessMyth/RoseBlox/releases/download/v9.0.0/Roseblox-x86_64.AppImage"
SUMS_URL = "https://github.com/MadnessMyth/RoseBlox/releases/download/v9.0.0/SHA256SUMS"


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def _opener(responses: dict[str, bytes]):
    def open_url(request, timeout):
        if request.full_url not in responses:
            raise OSError(f"unexpected {request.full_url}")
        return _Response(responses[request.full_url])

    return open_url


def _release_document(tag="v9.0.0"):
    return {
        "tag_name": tag,
        "html_url": f"https://github.com/MadnessMyth/RoseBlox/releases/tag/{tag}",
        "assets": [
            {"name": "Roseblox-x86_64.AppImage", "browser_download_url": APPIMAGE_URL, "size": 6},
            {"name": "SHA256SUMS", "browser_download_url": SUMS_URL, "size": 100},
            {"name": "roseblox_9.0.0_all.deb", "browser_download_url": "https://github.com/x.deb"},
        ],
    }


class VersionTests(unittest.TestCase):
    def test_versions_compare_numerically(self):
        self.assertTrue(self_update.is_newer("0.10.0", "0.9.9"))
        self.assertTrue(self_update.is_newer("v1.0.0", "0.9.9"))
        self.assertFalse(self_update.is_newer("0.5.0", "0.5.0"))
        self.assertFalse(self_update.is_newer("0.4.9", "0.5.0"))
        self.assertFalse(self_update.is_newer("nightly", "0.5.0"))

    def test_current_version_is_parseable(self):
        self.assertIsNotNone(self_update.parse_version(__version__))


class ReleaseTests(unittest.TestCase):
    def test_release_lists_appimage_and_checksums(self):
        release = self_update.parse_release(_release_document())
        self.assertEqual("9.0.0", release.version)
        self.assertEqual(APPIMAGE_URL, release.appimage_url)
        self.assertEqual(6, release.appimage_size)
        self.assertEqual(SUMS_URL, release.checksums_url)

    def test_assets_outside_github_are_ignored(self):
        document = _release_document()
        document["assets"][0]["browser_download_url"] = "https://example.com/Roseblox-x86_64.AppImage"
        document["html_url"] = "https://example.com/"
        release = self_update.parse_release(document)
        self.assertIsNone(release.appimage_url)
        self.assertEqual(self_update.RELEASES_URL, release.page_url)

    def test_invalid_release_is_rejected(self):
        for document in ([], {"tag_name": "latest"}, {"tag_name": 5}):
            with self.subTest(document=document), self.assertRaises(SelfUpdateError):
                self_update.parse_release(document)

    def test_fetch_reads_the_latest_release(self):
        body = json.dumps(_release_document()).encode()
        release = self_update.fetch_latest_release(_opener({self_update.LATEST_RELEASE_API: body}))
        self.assertEqual("9.0.0", release.version)

    def test_fetch_reports_network_and_format_errors(self):
        with self.assertRaisesRegex(SelfUpdateError, "Could not reach GitHub"):
            self_update.fetch_latest_release(_opener({}))
        with self.assertRaisesRegex(SelfUpdateError, "not valid JSON"):
            self_update.fetch_latest_release(_opener({self_update.LATEST_RELEASE_API: b"<html>"}))

    def test_checksum_line_is_found(self):
        digest = "a" * 64
        sums = f"{'b' * 64}  roseblox_9.0.0_all.deb\n{digest} *Roseblox-x86_64.AppImage\n"
        self.assertEqual(digest, self_update.expected_checksum(sums))
        self.assertIsNone(self_update.expected_checksum("nothing here"))


class InstallKindTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_appimage_needs_the_package_inside_the_mount(self):
        appdir = self.root / "mount"
        package = appdir / "usr" / "share" / "roseblox" / "roseblox"
        package.mkdir(parents=True)
        appimage = self.root / "Roseblox-x86_64.AppImage"
        appimage.write_bytes(b"")
        environ = {"ROSEBLOX_APPIMAGE": str(appimage), "ROSEBLOX_APPDIR": str(appdir)}
        self.assertEqual(self_update.INSTALL_APPIMAGE, self_update.install_kind(environ, package))
        elsewhere = self.root / "src" / "roseblox"
        elsewhere.mkdir(parents=True)
        self.assertEqual(self_update.INSTALL_SOURCE, self_update.install_kind(environ, elsewhere))

    def test_install_script_marker(self):
        package = self.root / "lib" / "roseblox" / "roseblox"
        package.mkdir(parents=True)
        (package.parent / ".installed-by-roseblox").write_text("")
        self.assertEqual(self_update.INSTALL_SCRIPT, self_update.install_kind({}, package))

    def test_system_packages(self):
        listing = self.root / "roseblox.list"
        package = Path("/usr/lib/python3/dist-packages/roseblox")
        self.assertEqual(self_update.INSTALL_PACKAGE, self_update.install_kind({}, package, listing))
        listing.write_text("")
        self.assertEqual(self_update.INSTALL_DEB, self_update.install_kind({}, package, listing))


class AppImageUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.target = Path(self.temporary.name) / "Roseblox-x86_64.AppImage"
        self.target.write_bytes(b"old")
        self.target.chmod(0o755)
        self.release = self_update.parse_release(_release_document())

    def _responses(self, data=b"new!!!", listed=None):
        digest = listed or hashlib.sha256(data).hexdigest()
        return {APPIMAGE_URL: data, SUMS_URL: f"{digest}  Roseblox-x86_64.AppImage\n".encode()}

    def test_verified_download_replaces_the_appimage(self):
        progress = []
        self_update.update_appimage(
            self.release, self.target, lambda received, total: progress.append((received, total)),
            opener=_opener(self._responses()),
        )
        self.assertEqual(b"new!!!", self.target.read_bytes())
        self.assertTrue(os.access(self.target, os.X_OK))
        self.assertEqual((6, 6), progress[-1])
        self.assertEqual(["Roseblox-x86_64.AppImage"], os.listdir(self.target.parent))

    def test_checksum_mismatch_keeps_the_old_file(self):
        with self.assertRaisesRegex(SelfUpdateError, "does not match"):
            self_update.update_appimage(self.release, self.target, opener=_opener(self._responses(listed="0" * 64)))
        self.assertEqual(b"old", self.target.read_bytes())
        self.assertEqual(["Roseblox-x86_64.AppImage"], os.listdir(self.target.parent))

    def test_cancel_keeps_the_old_file(self):
        with self.assertRaisesRegex(SelfUpdateError, "cancelled"):
            self_update.update_appimage(
                self.release, self.target, cancelled=lambda: True, opener=_opener(self._responses()),
            )
        self.assertEqual(b"old", self.target.read_bytes())
        self.assertEqual(["Roseblox-x86_64.AppImage"], os.listdir(self.target.parent))

    def test_release_without_appimage_or_checksums_is_refused(self):
        for release in (
            Release("9.0.0", self_update.RELEASES_URL),
            Release("9.0.0", self_update.RELEASES_URL, APPIMAGE_URL, 6, None),
        ):
            with self.subTest(release=release), self.assertRaises(SelfUpdateError):
                self_update.update_appimage(release, self.target, opener=_opener(self._responses()))
        with self.assertRaisesRegex(SelfUpdateError, "does not list a checksum"):
            responses = self._responses()
            responses[SUMS_URL] = b"nothing"
            self_update.update_appimage(self.release, self.target, opener=_opener(responses))


if __name__ == "__main__":
    unittest.main()
