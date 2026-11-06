import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from roseblox.backend.library import InstallationRecord, LibraryStore
from roseblox.backend.preferences import (
    PreferencesError,
    PreferencesStore,
    PreferredInstallations,
    default_preferences_path,
    resolve_preferred_installation,
)


def bundle(root: Path, app_type: str) -> Path:
    name = "RobloxPlayer.app" if app_type == "player" else "RobloxStudio.app"
    path = root / name
    (path / "Contents").mkdir(parents=True, exist_ok=True)
    return path


class PreferencesStoreTests(unittest.TestCase):
    def test_default_path_uses_xdg_config_home(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}):
                self.assertEqual(Path(directory) / "roseblox" / "preferences.json", default_preferences_path())

    def test_missing_file_returns_independent_empty_preferences(self):
        with tempfile.TemporaryDirectory() as directory:
            state = PreferencesStore(Path(directory) / "preferences.json").load()
        self.assertEqual(PreferredInstallations(), state)

    def test_player_and_studio_preferences_persist_independently(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = LibraryStore(root / "library.json")
            player = library.add("player", bundle(root / "player", "player"), "Player")
            studio = library.add("studio", bundle(root / "studio", "studio"), "Studio")
            store = PreferencesStore(root / "preferences.json")
            store.set_preferred("player", player.id, library)
            store.set_preferred("studio", studio.id, library)
            self.assertEqual(
                PreferredInstallations(player.id, studio.id),
                PreferencesStore(store.path).load(),
            )
            store.clear("player")
            self.assertEqual(PreferredInstallations(None, studio.id), store.load())

    def test_set_preference_requires_registered_matching_valid_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = LibraryStore(root / "library.json")
            player = library.add("player", bundle(root / "player", "player"), "Player")
            store = PreferencesStore(root / "preferences.json")
            with self.assertRaises(PreferencesError):
                store.set_preferred("studio", player.id, library)
            with self.assertRaises(PreferencesError):
                store.set_preferred("player", "missing-id", library)
            (Path(player.bundle_path) / "Contents").rmdir()
            with self.assertRaises(PreferencesError):
                store.set_preferred("player", player.id, library)
            self.assertIsNone(store.load().player_id)

    def test_malformed_json_falls_back_without_overwriting_user_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            original = b"{broken"
            path.write_bytes(original)
            store = PreferencesStore(path)
            state = store.load()
            self.assertIsNone(state.player_id)
            self.assertIn("player", state.errors)
            self.assertIn("studio", state.errors)
            self.assertEqual(original, path.read_bytes())
            with self.assertRaises(PreferencesError):
                store.clear("player")
            self.assertEqual(original, path.read_bytes())

    def test_invalid_app_entry_does_not_discard_other_preference(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            path.write_text(
                json.dumps({
                    "version": 1,
                    "preferred_installations": {"player": ["invalid"], "studio": "studio-id"},
                }),
                encoding="utf-8",
            )
            state = PreferencesStore(path).load()
        self.assertIsNone(state.player_id)
        self.assertEqual("studio-id", state.studio_id)
        self.assertIn("player", state.errors)

    def test_failed_atomic_write_preserves_previous_preferences(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            original = b'{"version": 1, "preferred_installations": {"player": "old"}}\n'
            path.write_bytes(original)
            store = PreferencesStore(path)
            with patch("roseblox.backend.preferences.os.replace", side_effect=OSError("read-only filesystem")):
                with self.assertRaises(PreferencesError):
                    store.clear("player")
            self.assertEqual(original, path.read_bytes())

    def test_removing_matching_preference_preserves_other_application(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "preferences.json"
            store = PreferencesStore(path)
            document = {"version": 1, "preferred_installations": {"player": "p", "studio": "s"}}
            path.write_text(json.dumps(document), encoding="utf-8")
            self.assertTrue(store.clear_if_matches("player", "p"))
            self.assertFalse(store.clear_if_matches("player", "other"))
            self.assertEqual(PreferredInstallations(None, "s"), store.load())

    def test_stale_or_invalid_preference_uses_first_valid_without_rewriting(self):
        records = [
            InstallationRecord("broken", "Broken", "player", "/missing/RobloxPlayer.app", "date", "valid"),
            InstallationRecord("valid", "Valid", "player", "/ok/RobloxPlayer.app", "date", "valid"),
            InstallationRecord("studio", "Studio", "studio", "/ok/RobloxStudio.app", "date", "valid"),
        ]
        preferences = PreferredInstallations("broken", "removed")
        validation = {"broken": "Missing or invalid", "valid": "Valid", "studio": "Valid"}
        self.assertEqual("valid", resolve_preferred_installation("player", preferences, records, validation))
        self.assertEqual("studio", resolve_preferred_installation("studio", preferences, records, validation))
        self.assertEqual("broken", preferences.player_id)
        self.assertEqual("removed", preferences.studio_id)

    def test_no_valid_bundle_has_no_fallback(self):
        records = [
            InstallationRecord("bad", "Broken", "player", "/missing/RobloxPlayer.app", "date", "valid")
        ]
        self.assertIsNone(
            resolve_preferred_installation(
                "player", PreferredInstallations("bad"), records, {"bad": "Missing or invalid"}
            )
        )


if __name__ == "__main__":
    unittest.main()
