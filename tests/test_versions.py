import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from roseblox.backend.versions import (
    VersionCache,
    VersionCacheError,
    VersionClient,
    VersionLookupError,
    VersionInfo,
    archive_url,
    validate_version,
)
from roseblox.backend.version_settings import VersionSource


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, headers=None):
        super().__init__(body)
        self.headers = headers or {}
        self.status = 200

    def getcode(self):
        return self.status


class VersionTests(unittest.TestCase):
    def test_parses_player_and_studio_json_and_exact_endpoints(self):
        from roseblox.backend.versions import VERSION_ENDPOINTS

        for app_type, endpoint in (
            ("player", "MacPlayer"),
            ("studio", "MacStudio"),
        ):
            response = FakeResponse(b'{"clientVersionUpload":"version-0123456789abcdef"}')
            with patch("roseblox.backend.versions.urlopen", return_value=response) as opener:
                info = VersionClient().fetch(app_type)
            self.assertEqual("version-0123456789abcdef", info.version)
            self.assertTrue(info.checked_at)
            self.assertEqual(
                f"https://clientsettingscdn.roblox.com/v2/client-version/{endpoint}",
                VERSION_ENDPOINTS[app_type],
            )
            self.assertEqual(endpoint, opener.call_args.args[0].full_url.rsplit("/", 1)[-1])

    def test_constructs_only_established_archive_urls(self):
        self.assertEqual(
            "https://setup.rbxcdn.com/mac/version-0123456789abcdef-RobloxPlayer.zip",
            archive_url("player", "version-0123456789abcdef"),
        )
        self.assertEqual(
            "https://setup.rbxcdn.com/mac/version-0123456789abcdef-RobloxStudioApp.zip",
            archive_url("studio", "version-0123456789abcdef"),
        )

    def test_rejects_invalid_versions_and_unsupported_type(self):
        for value in (None, "", "version-xyz", "../version-0123456789abcdef", "version-0123"):
            with self.subTest(value=value), self.assertRaises(VersionLookupError):
                validate_version(value)
        with self.assertRaises(ValueError):
            archive_url("other", "version-0123456789abcdef")

    def test_handles_invalid_shapes_http_errors_and_network_failures(self):
        for body in (b"[]", b"{}", b'{"clientVersionUpload":4}', b"invalid"):
            with self.subTest(body=body):
                with patch("roseblox.backend.versions.urlopen", return_value=FakeResponse(body)):
                    with self.assertRaises(VersionLookupError):
                        VersionClient().fetch("player")
        with patch(
            "roseblox.backend.versions.urlopen",
            side_effect=HTTPError("url", 503, "unavailable", {}, None),
        ):
            with self.assertRaisesRegex(VersionLookupError, "HTTP 503"):
                VersionClient().fetch("player")
        with patch("roseblox.backend.versions.urlopen", side_effect=URLError("offline")):
            with self.assertRaises(VersionLookupError):
                VersionClient().fetch("studio")

    def test_version_cache_is_separate_and_keeps_last_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = VersionCache(root / "versions.json")
            old = VersionInfo("version-0123456789abcdef", "2026-01-01T00:00:00+00:00")
            cache.update("player", old)
            library_file = root / "library.json"
            library_file.write_text('{"installations": []}', encoding="utf-8")

            with patch("roseblox.backend.versions.urlopen", side_effect=URLError("offline")):
                with self.assertRaises(VersionLookupError):
                    VersionClient().fetch("player")

            self.assertEqual(old, cache.load()["player"])
            self.assertTrue(library_file.exists())
            self.assertNotEqual(cache.path, library_file)

    def test_malformed_cache_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "versions.json"
            original = b"{broken"
            path.write_bytes(original)
            cache = VersionCache(path)
            with self.assertRaises(VersionCacheError):
                cache.update("player", VersionInfo("version-0123456789abcdef", "now"))
            self.assertEqual(original, path.read_bytes())

    def test_custom_json_pointer_and_diagnostic_fields(self):
        source = VersionSource("https://example.org/version", "/result/releases/0/upload")
        body = b'{"result":{"releases":[{"upload":"version-0123456789abcdef"}]}}'
        with patch("roseblox.backend.versions.urlopen", side_effect=lambda *_args, **_kwargs: FakeResponse(body)):
            result = VersionClient().test_configuration(source)
            info = VersionClient().fetch("player", source)
        self.assertTrue(result.valid)
        self.assertEqual("Connected", result.connection_status)
        self.assertEqual(200, result.http_status)
        self.assertEqual("Valid JSON", result.json_result)
        self.assertEqual("version-0123456789abcdef", result.extracted_version)
        self.assertEqual("Valid", result.validation_result)
        self.assertEqual("version-0123456789abcdef", info.version)

    def test_test_configuration_reports_http_failure_and_response_limit(self):
        source = VersionSource("https://example.org/version", "/clientVersionUpload")
        with patch(
            "roseblox.backend.versions.urlopen",
            side_effect=HTTPError("url", 503, "unavailable", {}, None),
        ):
            result = VersionClient().test_configuration(source)
        self.assertEqual(503, result.http_status)
        self.assertFalse(result.valid)
        with patch(
            "roseblox.backend.versions.urlopen",
            return_value=FakeResponse(b" " * (1024 * 1024 + 1)),
        ):
            result = VersionClient().test_configuration(source)
        self.assertIn("exceeds 1 MiB", result.json_result)

    def test_test_configuration_converts_unexpected_request_and_parser_errors(self):
        source = VersionSource("https://example.org/version", "/clientVersionUpload")
        with patch("roseblox.backend.versions.urlopen", side_effect=ValueError("malformed request target")):
            result = VersionClient().test_configuration(source)
        self.assertFalse(result.valid)
        self.assertEqual("Failed", result.connection_status)
        self.assertIn("malformed request target", result.error)

        with patch("roseblox.backend.versions.urlopen", return_value=FakeResponse(b"{}")):
            with patch("roseblox.backend.versions.json.loads", side_effect=RecursionError("too deeply nested")):
                result = VersionClient().test_configuration(source)
        self.assertFalse(result.valid)
        self.assertIn("could not be parsed", result.error)

    def test_non_string_extracted_values_are_reported_without_repr_dump(self):
        source = VersionSource("https://example.org/version", "/value")
        body = json.dumps({"value": {"secret": "x" * 10000}}).encode()
        with patch("roseblox.backend.versions.urlopen", return_value=FakeResponse(body)):
            result = VersionClient().test_configuration(source)
        self.assertEqual("<dict value>", result.extracted_version)
        self.assertEqual("Invalid", result.validation_result)


if __name__ == "__main__":
    unittest.main()
