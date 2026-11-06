import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from roseblox.backend.downloader import (
    DownloadCancelled,
    DownloadError,
    DownloadProgress,
    download_archive,
)


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, content_length: str | None = None, *, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = dict(headers or {})
        if content_length is not None:
            self.headers["Content-Length"] = content_length


class CancelAfterFirstRead(FakeResponse):
    def __init__(self, body: bytes, cancel_event: threading.Event):
        super().__init__(body, str(len(body)))
        self.cancel_event = cancel_event

    def read(self, size=-1):
        chunk = super().read(size)
        if chunk:
            self.cancel_event.set()
        return chunk


class DownloaderTests(unittest.TestCase):
    def test_streams_download_and_reports_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            progress = []
            event = threading.Event()
            with patch(
                "roseblox.backend.downloader.urlopen",
                return_value=FakeResponse(b"archive-data", "12"),
            ):
                downloaded = download_archive(
                    "https://example.invalid/file.zip",
                    Path(directory),
                    event,
                    progress.append,
                    chunk_size=4,
                )
            self.assertEqual(b"archive-data", downloaded.read_bytes())
            self.assertEqual(
                [DownloadProgress(0, 12), DownloadProgress(4, 12), DownloadProgress(8, 12), DownloadProgress(12, 12)],
                progress,
            )

    def test_cancellation_removes_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            event = threading.Event()
            response = CancelAfterFirstRead(b"partial-content", event)
            with patch("roseblox.backend.downloader.urlopen", return_value=response):
                with self.assertRaises(DownloadCancelled):
                    download_archive("https://example.invalid/file.zip", Path(directory), event)
            self.assertEqual([], list(Path(directory).iterdir()))

    def test_http_network_and_truncated_downloads_fail_and_clean_up(self):
        with tempfile.TemporaryDirectory() as directory:
            for error in (
                HTTPError("url", 404, "missing", {}, None),
                URLError("offline"),
            ):
                with patch("roseblox.backend.downloader.urlopen", side_effect=error):
                    with self.assertRaises(DownloadError):
                        download_archive("https://example.invalid/file.zip", Path(directory), threading.Event())
            with patch(
                "roseblox.backend.downloader.urlopen",
                return_value=FakeResponse(b"short", "100"),
            ):
                with self.assertRaisesRegex(DownloadError, "incomplete"):
                    download_archive("https://example.invalid/file.zip", Path(directory), threading.Event())
            self.assertEqual([], list(Path(directory).iterdir()))

    def test_resumes_with_valid_range_and_retains_partial_on_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            partial = root / "download.part"
            partial.write_bytes(b"first-")
            event = threading.Event()
            response = FakeResponse(
                b"second",
                "6",
                status=206,
                headers={"Content-Range": "bytes 6-11/12", "ETag": '"v1"'},
            )
            progress = []
            with patch("roseblox.backend.downloader.urlopen", return_value=response) as urlopen_mock:
                result = download_archive(
                    "https://example.invalid/archive.zip",
                    root,
                    event,
                    progress.append,
                    partial_path=partial,
                    resume=True,
                    etag='"v1"',
                    preserve_partial=True,
                    chunk_size=3,
                )
            request = urlopen_mock.call_args.args[0]
            self.assertEqual("bytes=6-", request.get_header("Range"))
            self.assertEqual(b"first-second", result.read_bytes())
            self.assertEqual(DownloadProgress(12, 12), progress[-1])

    def test_server_ignoring_range_restarts_from_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            partial = root / "download.part"
            partial.write_bytes(b"stale")
            response = FakeResponse(b"fresh-full", "10", status=200)
            with patch("roseblox.backend.downloader.urlopen", return_value=response):
                result = download_archive(
                    "https://example.invalid/archive.zip",
                    root,
                    threading.Event(),
                    partial_path=partial,
                    resume=True,
                    preserve_partial=True,
                )
            self.assertEqual(b"fresh-full", result.read_bytes())

    def test_invalid_content_range_restarts_without_using_saved_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            partial = root / "download.part"
            partial.write_bytes(b"stale")
            wrong_range = FakeResponse(
                b"ignored",
                "7",
                status=206,
                headers={"Content-Range": "bytes 99-105/106"},
            )
            fresh = FakeResponse(b"fresh-full", "10", status=200)
            with patch("roseblox.backend.downloader.urlopen", side_effect=[wrong_range, fresh]) as urlopen_mock:
                result = download_archive(
                    "https://example.invalid/archive.zip",
                    root,
                    threading.Event(),
                    partial_path=partial,
                    resume=True,
                    preserve_partial=True,
                )
            self.assertEqual(2, urlopen_mock.call_count)
            self.assertEqual(b"fresh-full", result.read_bytes())


if __name__ == "__main__":
    unittest.main()
