import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from roseblox.backend.download_state import DownloadJobStore, DownloadStateError


VERSION = "version-0123456789abcdef"


class DownloadJobStoreTests(unittest.TestCase):
    def test_recovers_in_progress_job_from_partial_file_size(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "roseblox"
            store = DownloadJobStore(root)
            archive_dir = root / "archives" / "studio"
            archive_dir.mkdir(parents=True)
            job = store.create("studio", VERSION, "studio-job", archive_dir / "version.zip")
            partial = Path(job.temporary_path)
            partial.write_bytes(b"partial")

            [recovered] = store.load("studio")

            self.assertEqual("interrupted", recovered.state)
            self.assertEqual(7, recovered.downloaded)
            self.assertEqual(VERSION, recovered.version)
            self.assertEqual("studio", recovered.app_type)
            self.assertEqual(job.temporary_path, recovered.temporary_path)
            self.assertEqual(job.archive_destination, recovered.archive_destination)

    def test_discard_only_removes_managed_interrupted_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "roseblox"
            store = DownloadJobStore(root)
            archive_dir = root / "archives" / "player"
            archive_dir.mkdir(parents=True)
            job = store.create("player", VERSION, "player-job", archive_dir / "version.zip")
            partial = Path(job.temporary_path)
            partial.write_bytes(b"partial")
            store.load()

            store.discard(job.id)

            self.assertFalse(partial.exists())
            self.assertEqual([], store.load())

    def test_malformed_job_file_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "roseblox"
            root.mkdir()
            metadata = root / "downloads.json"
            original = b'{"downloads": [broken]'
            metadata.write_bytes(original)
            store = DownloadJobStore(root)

            with self.assertRaises(DownloadStateError):
                store.load()

            self.assertEqual(original, metadata.read_bytes())

    def test_recovers_completed_archive_promoted_before_state_update(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "roseblox"
            store = DownloadJobStore(root)
            archive_dir = root / "archives" / "player"
            archive_dir.mkdir(parents=True)
            destination = archive_dir / "complete.zip"
            job = store.create("player", VERSION, "promoted-job", destination)
            with zipfile.ZipFile(destination, "w") as archive:
                archive.writestr("RobloxPlayer.app/Contents/Info.plist", "plist")
            size = destination.stat().st_size
            store.update(job.id, total=size, downloaded=size)

            [recovered] = store.load()

            self.assertEqual("downloaded", recovered.state)
            self.assertEqual(size, recovered.downloaded)

            with self.assertRaisesRegex(DownloadStateError, "(?i)completed archive is retained"):
                store.discard(job.id)
            self.assertTrue(destination.is_file())

    def test_rejects_persisted_paths_outside_managed_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "roseblox"
            root.mkdir()
            metadata = root / "downloads.json"
            record = {
                "id": "unsafe",
                "app_type": "player",
                "version": VERSION,
                "url": f"https://setup.rbxcdn.com/mac/{VERSION}-RobloxPlayer.zip",
                "archive_destination": str(Path(directory) / "outside.zip"),
                "temporary_path": str(root / ".staging/downloads/unsafe.part"),
                "state": "interrupted",
                "downloaded": 1,
                "total": 10,
            }
            original = json.dumps({"version": 1, "downloads": [record]}).encode()
            metadata.write_bytes(original)
            store = DownloadJobStore(root)

            with self.assertRaises(DownloadStateError):
                store.load()

            self.assertEqual(original, metadata.read_bytes())


if __name__ == "__main__":
    unittest.main()
