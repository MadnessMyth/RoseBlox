import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from roseblox.backend.version_settings import (
    VersionSettingsError,
    VersionSettingsStore,
    VersionSource,
    default_version_source,
    parse_json_pointer,
    validate_endpoint,
)


class VersionSettingsTests(unittest.TestCase):
    def test_defaults_are_existing_endpoints_and_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = VersionSettingsStore(Path(directory) / "sources.json")
            defaults = store.load()
            self.assertEqual(default_version_source("player"), defaults["player"])
            self.assertEqual(default_version_source("studio"), defaults["studio"])
            custom = VersionSource("https://example.org/player", "/release/0/version")
            store.save("player", custom)
            self.assertEqual(custom, store.load()["player"])
            self.assertEqual(default_version_source("studio"), store.load()["studio"])

    def test_restore_defaults_only_changes_selected_application(self):
        with tempfile.TemporaryDirectory() as directory:
            store = VersionSettingsStore(Path(directory) / "sources.json")
            store.save("player", VersionSource("https://example.org/p", "/version"))
            store.save("studio", VersionSource("https://example.org/s", "/build/version"))
            store.reset("player")
            current = store.load()
            self.assertEqual(default_version_source("player"), current["player"])
            self.assertEqual(VersionSource("https://example.org/s", "/build/version"), current["studio"])

    def test_malformed_settings_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sources.json"
            original = b"{broken"
            path.write_bytes(original)
            with self.assertRaises(VersionSettingsError):
                VersionSettingsStore(path).save("player", default_version_source("player"))
            self.assertEqual(original, path.read_bytes())

    def test_failed_atomic_persistence_preserves_prior_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sources.json"
            store = VersionSettingsStore(path)
            store.save("player", VersionSource("https://old.example/version", "/version"))
            original_bytes = path.read_bytes()
            with patch("roseblox.backend.version_settings.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(VersionSettingsError):
                    store.save("player", VersionSource("https://new.example/version", "/version"))
            self.assertEqual(original_bytes, path.read_bytes())

    def test_rejects_http_endpoint_and_invalid_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            store = VersionSettingsStore(Path(directory) / "sources.json")
            for source in (
                VersionSource("http://example.org/version", "/version"),
                VersionSource("https://user:pass@example.org/version", "/version"),
                VersionSource("https://example.org/version", "data.version"),
                VersionSource("https://example.org/version", "/data/~2value"),
            ):
                with self.subTest(source=source), self.assertRaises(VersionSettingsError):
                    store.save("player", source)

    def test_endpoint_validation_rejects_malformed_authorities_and_controls(self):
        invalid = (
            "https://example.org/path\nnext",
            "https://example.org/path\tvalue",
            "https://bad_host.example/version",
            "https:///missing-host",
            "https://example.org:443:443/version",
            "ftp://example.org/version",
            "https://user@example.org/version",
            "https://example.org:444/version",
            "https://example.org:bad/version",
            "https://example.org/%not-encoded",
        )
        for endpoint in invalid:
            with self.subTest(endpoint=endpoint), self.assertRaises(VersionSettingsError):
                validate_endpoint(endpoint)

    def test_endpoint_validation_accepts_https_path_query_ipv4_ipv6_and_idn(self):
        valid = (
            "https://example.org/version/v2?channel=stable&platform=mac",
            "HTTPS://EXAMPLE.ORG:443/version?key=a%20b",
            "https://192.0.2.10/version",
            "https://[2001:db8::1]:443/version",
            "https://例え.テスト/version",
        )
        for endpoint in valid:
            with self.subTest(endpoint=endpoint):
                self.assertEqual(endpoint, validate_endpoint(endpoint))

    def test_invalid_player_record_does_not_block_studio_and_can_be_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sources.json"
            studio = VersionSource("https://studio.example/version", "/data/studio")
            path.write_text(json.dumps({
                "version": 1,
                "applications": {
                    "player": {"endpoint": "http://invalid.example/version", "json_pointer": "/version"},
                    "studio": {"endpoint": studio.endpoint, "json_pointer": studio.json_pointer},
                },
            }), encoding="utf-8")
            store = VersionSettingsStore(path)

            values, errors = store.load_independent()
            self.assertIn("player", errors)
            self.assertEqual(studio, values["studio"])
            self.assertEqual(studio, store.load_for("studio"))
            with self.assertRaises(VersionSettingsError):
                store.load_for("player")

            store.reset("player")
            self.assertEqual(default_version_source("player"), store.load_for("player"))
            self.assertEqual(studio, store.load_for("studio"))

    def test_invalid_studio_record_does_not_block_player_save_and_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sources.json"
            player = VersionSource("https://player.example/version", "/release/version")
            path.write_text(json.dumps({
                "version": 1,
                "applications": {
                    "player": {"endpoint": player.endpoint, "json_pointer": player.json_pointer},
                    "studio": {"endpoint": "not a url", "json_pointer": "/version"},
                },
            }), encoding="utf-8")
            store = VersionSettingsStore(path)

            self.assertEqual(player, store.load_for("player"))
            replacement = VersionSource("https://player.example/new", "/payload/version")
            store.save("player", replacement)
            self.assertEqual(replacement, store.load_for("player"))
            with path.open(encoding="utf-8") as stream:
                document = json.load(stream)
            self.assertEqual("not a url", document["applications"]["studio"]["endpoint"])

            store.reset("studio")
            self.assertEqual(default_version_source("studio"), store.load_for("studio"))
            self.assertEqual(replacement, store.load_for("player"))

    def test_json_pointer_supports_nested_objects_array_indexes_and_escaping(self):
        document = {"nested": {"items": [{"version": "v1"}], "a/b": {"~key": 7}}}
        self.assertEqual("v1", parse_json_pointer(document, "/nested/items/0/version"))
        self.assertEqual(7, parse_json_pointer(document, "/nested/a~1b/~0key"))
        with self.assertRaises(VersionSettingsError):
            parse_json_pointer(document, "/nested/items/01/version")


if __name__ == "__main__":
    unittest.main()
