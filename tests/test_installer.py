import threading
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from roseblox.backend.archive import ArchiveTypeUndetermined
from roseblox.backend.downloader import DownloadCancelled, DownloadError
from roseblox.backend.installer import InstallationConflictError, InstallerService
from roseblox.backend.library import LibraryStore
from roseblox.backend.versions import VersionCache, VersionInfo
from roseblox.backend.version_settings import VersionSource
from roseblox.backend.version_settings import VersionSettingsStore


VERSION = "version-0123456789abcdef"


class FakeVersionClient:
    def __init__(self):
        self.last_source = None

    def fetch(self, app_type, source=None):
        self.last_source = source
        return VersionInfo(VERSION, "2026-11-05T00:00:00+00:00")


def write_player_archive(path: Path, *, valid: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        member = "RobloxPlayer.app/Contents/Info.plist" if valid else "RobloxPlayer.app/readme.txt"
        archive.writestr(member, "plist" if valid else "not a bundle")


class InstallerTests(unittest.TestCase):
    def make_service(self, root: Path) -> InstallerService:
        data = root / "data"
        library = LibraryStore(data / "library.json")
        cache = VersionCache(data / "versions.json")
        settings = VersionSettingsStore(root / "config" / "version_sources.json")
        return InstallerService(
            library,
            version_client=FakeVersionClient(),
            version_cache=cache,
            version_settings=settings,
            data_directory=data,
        )

    def test_archive_import_registers_managed_version_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / f"{VERSION}-RobloxPlayer.zip"
            write_player_archive(archive)
            original = archive.read_bytes()
            service = self.make_service(root)

            result = service.import_archive(archive)

            self.assertEqual(VERSION, result.record.version)
            self.assertTrue(result.record.managed)
            self.assertEqual("archive_import", result.record.source)
            self.assertEqual(str(archive.absolute()), result.record.archive_path)
            self.assertEqual(original, archive.read_bytes())
            self.assertTrue(Path(result.record.bundle_path).is_dir())
            self.assertIn(VERSION, Path(result.record.bundle_path).parts)
            self.assertEqual(result.record, service.library.load()[0])

    def test_archive_import_without_recognizable_type_requires_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "opaque.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("bundle/Contents/Info.plist", "plist")
            service = self.make_service(root)
            with self.assertRaises(ArchiveTypeUndetermined):
                service.import_archive(archive)
            self.assertEqual([], service.library.load())

    def test_download_install_preserves_archive_after_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_archive = root / "source.zip"
            write_player_archive(source_archive)
            service = self.make_service(root)
            captured_urls = []

            def fake_download(url, temp_dir, cancel_event, callback, *, partial_path, **_kwargs):
                captured_urls.append(url)
                destination = partial_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source_archive.read_bytes())
                callback(type("Progress", (), {"downloaded": destination.stat().st_size, "total": destination.stat().st_size})())
                return destination

            progress = []
            with patch("roseblox.backend.installer.download_archive", side_effect=fake_download):
                result = service.download_latest("player", threading.Event(), progress.append)

            self.assertEqual(
                f"https://setup.rbxcdn.com/mac/{VERSION}-RobloxPlayer.zip",
                captured_urls[0],
            )
            self.assertTrue(result.record.managed)
            self.assertEqual("download", result.record.source)
            self.assertEqual(VERSION, result.record.version)
            self.assertTrue(result.archive_path and result.archive_path.is_file())
            self.assertEqual(source_archive.read_bytes(), result.archive_path.read_bytes())
            self.assertEqual(
                "https://clientsettingscdn.roblox.com/v2/client-version/MacPlayer",
                service.version_client.last_source.endpoint,
            )
            self.assertIn("extracting", [event.phase for event in progress])
            self.assertIn("complete", [event.phase for event in progress])

            with patch("roseblox.backend.installer.download_archive") as downloader:
                repeated = service.download_latest("player", threading.Event())
            downloader.assert_not_called()
            self.assertTrue(repeated.already_installed)

    def test_download_uses_saved_per_application_version_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.make_service(root)
            source = VersionSource("https://versions.example/player.json", "/data/0/upload")
            service.version_settings.save("player", source)
            archive = root / "source.zip"
            write_player_archive(archive)

            def fake_download(_url, _temp_dir, _cancel, _callback, *, partial_path, **_kwargs):
                partial_path.parent.mkdir(parents=True, exist_ok=True)
                partial_path.write_bytes(archive.read_bytes())
                return partial_path

            with patch("roseblox.backend.installer.download_archive", side_effect=fake_download):
                service.download_latest("player", threading.Event())
            self.assertEqual(source, service.version_client.last_source)

    def test_existing_managed_destination_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.make_service(root)
            existing = service.install_root / "player" / VERSION
            existing.mkdir(parents=True)
            marker = existing / "keep.txt"
            marker.write_text("preserve", encoding="utf-8")
            with patch("roseblox.backend.installer.download_archive") as downloader:
                with self.assertRaises(InstallationConflictError):
                    service.download_latest("player", threading.Event())
            downloader.assert_not_called()
            self.assertEqual("preserve", marker.read_text(encoding="utf-8"))

    def test_existing_recovery_job_prevents_duplicate_latest_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.make_service(root)
            archive_root = root / "data" / "archives" / "player"
            archive_root.mkdir(parents=True)
            job = service.download_jobs.create("player", VERSION, "recovery-job", archive_root / "saved.zip")
            Path(job.temporary_path).write_bytes(b"partial")

            with patch("roseblox.backend.installer.download_archive") as downloader:
                with self.assertRaisesRegex(InstallationConflictError, "Resume or install"):
                    service.download_latest("player", threading.Event())

            downloader.assert_not_called()

    def test_non_zip_transfer_is_retained_as_interrupted_not_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.make_service(root)

            def bad_transfer(_url, _temp_dir, _cancel, _callback, *, partial_path, **_kwargs):
                partial_path.parent.mkdir(parents=True, exist_ok=True)
                partial_path.write_bytes(b"truncated archive")
                return partial_path

            with patch("roseblox.backend.installer.download_archive", side_effect=bad_transfer):
                with self.assertRaisesRegex(RuntimeError, "not a readable ZIP"):
                    service.download_latest("player", threading.Event())

            [job] = service.recoverable_downloads("player")
            self.assertEqual("interrupted", job.state)
            self.assertEqual(b"truncated archive", Path(job.temporary_path).read_bytes())

    def test_cancelled_transfer_keeps_partial_for_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "complete.zip"
            write_player_archive(source)
            service = self.make_service(root)
            cancelled = threading.Event()

            def cancelled_transfer(_url, _temp_dir, _cancel, _callback, *, partial_path, **_kwargs):
                partial_path.parent.mkdir(parents=True, exist_ok=True)
                partial_path.write_bytes(source.read_bytes()[:20])
                cancelled.set()
                raise DownloadCancelled("Download cancelled.")

            with patch("roseblox.backend.installer.download_archive", side_effect=cancelled_transfer):
                with self.assertRaises(DownloadCancelled):
                    service.download_latest("player", cancelled)

            [job] = service.recoverable_downloads("player")
            self.assertEqual("interrupted", job.state)
            self.assertEqual(source.read_bytes()[:20], Path(job.temporary_path).read_bytes())

    def test_completed_download_is_preserved_when_archive_validation_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid_archive = root / "invalid.zip"
            write_player_archive(invalid_archive, valid=False)
            service = self.make_service(root)

            def fake_download(_url, temp_dir, _cancel, _callback, *, partial_path, **_kwargs):
                result = partial_path
                result.parent.mkdir(parents=True, exist_ok=True)
                result.write_bytes(invalid_archive.read_bytes())
                return result

            with patch("roseblox.backend.installer.download_archive", side_effect=fake_download):
                with self.assertRaises(Exception):
                    service.download_latest("player", threading.Event())

            preserved = list((root / "data" / "archives" / "player").glob("*.zip"))
            self.assertEqual(1, len(preserved))
            self.assertEqual(invalid_archive.read_bytes(), preserved[0].read_bytes())
            self.assertEqual([], service.library.load())

    def test_cancel_before_install_does_not_download(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(Path(directory))
            cancelled = threading.Event()
            cancelled.set()
            with patch("roseblox.backend.installer.download_archive") as downloader:
                with self.assertRaises(DownloadCancelled):
                    service.download_latest("player", cancelled)
            downloader.assert_not_called()

    def test_interrupted_download_is_persisted_then_resumed_and_installed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "complete.zip"
            write_player_archive(archive)
            service = self.make_service(root)

            def interrupted(_url, _temp_dir, _cancel, _callback, *, partial_path, metadata_callback, **_kwargs):
                partial_path.parent.mkdir(parents=True, exist_ok=True)
                partial_path.write_bytes(b"partial-bytes")
                metadata_callback('"archive-v1"', None, 100)
                raise DownloadError("Connection interrupted.")

            with patch("roseblox.backend.installer.download_archive", side_effect=interrupted):
                with self.assertRaises(DownloadError):
                    service.download_latest("player", threading.Event())

            [job] = service.recoverable_downloads("player")
            self.assertEqual("interrupted", job.state)
            self.assertEqual(len(b"partial-bytes"), job.downloaded)
            saved = json_read(service.download_jobs.path)
            self.assertEqual("player", saved["downloads"][0]["app_type"])
            self.assertEqual(VERSION, saved["downloads"][0]["version"])
            self.assertTrue(Path(job.temporary_path).is_file())

            def resumed(url, temp_dir, _cancel, callback, *, partial_path, resume, etag, **_kwargs):
                self.assertEqual(service.download_jobs.get(job.id).url, url)
                self.assertTrue(resume)
                self.assertEqual('"archive-v1"', etag)
                partial_path.write_bytes(archive.read_bytes())
                callback(type("Progress", (), {"downloaded": partial_path.stat().st_size, "total": partial_path.stat().st_size})())
                return partial_path

            with patch("roseblox.backend.installer.download_archive", side_effect=resumed):
                result = service.retry_download(job.id, threading.Event())
            self.assertTrue(result.record.managed)
            self.assertTrue(result.archive_path.is_file())
            self.assertEqual("installed", service.download_jobs.get(job.id).state)


def json_read(path: Path):
    import json

    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
