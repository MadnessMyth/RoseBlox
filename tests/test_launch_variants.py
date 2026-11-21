"""Tests for the Roblox Player vs Roblox Updater launch variant feature.

These cover the pure, GTK-free backend logic in ``roseblox.backend.library``:
the updater bundle name registry, launch-variant discovery beside a registered
bundle, and the relaxed ``allow_updater`` validation that lets the launch path
start the ``Roblox.app`` updater chosen from the variant prompt while keeping
registration strict.

The GTK-dependent pieces (``launch_application`` forwarding the override, and
the manager-page prompt routing) live in ``tests/test_launch_variant_prompt.py``
because they import PyGObject, which is not available in every environment.
"""

import tempfile
import unittest
from pathlib import Path

from roseblox.backend.library import (
    APP_BUNDLE_NAMES,
    APP_UPDATER_BUNDLE_NAMES,
    BundleValidationError,
    LAUNCH_VARIANT_LABELS,
    LaunchVariant,
    allowed_bundle_names,
    discover_launch_variants,
    validate_bundle,
)


def _make_bundle(parent: Path, name: str) -> Path:
    """Create a minimal but Contents-valid .app bundle directory."""
    bundle = parent / name
    (bundle / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    return bundle


class AllowedBundleNamesTests(unittest.TestCase):
    def test_player_has_primary_and_updater(self):
        self.assertEqual(["RobloxPlayer.app", "Roblox.app"], allowed_bundle_names("player"))

    def test_studio_has_only_primary(self):
        self.assertEqual(["RobloxStudio.app"], allowed_bundle_names("studio"))

    def test_unknown_type_has_no_allowed_names(self):
        self.assertEqual([], allowed_bundle_names("unknown"))

    def test_registry_constants_are_consistent(self):
        self.assertEqual("RobloxPlayer.app", APP_BUNDLE_NAMES["player"])
        self.assertEqual("Roblox.app", APP_UPDATER_BUNDLE_NAMES["player"])
        self.assertNotIn("studio", APP_UPDATER_BUNDLE_NAMES)
        self.assertEqual("Roblox Player", LAUNCH_VARIANT_LABELS["player"])
        self.assertEqual("Roblox Updater", LAUNCH_VARIANT_LABELS["updater"])


class DiscoverLaunchVariantsTests(unittest.TestCase):
    def test_both_bundles_present_returns_player_first(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            player = _make_bundle(parent, "RobloxPlayer.app")
            updater = _make_bundle(parent, "Roblox.app")
            variants = discover_launch_variants(player, "player")
            self.assertEqual(2, len(variants))
            self.assertEqual(["player", "updater"], [v.role for v in variants])
            self.assertEqual(player, variants[0].bundle_path)
            self.assertEqual(updater, variants[1].bundle_path)

    def test_only_player_present_returns_single_player_variant(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            player = _make_bundle(parent, "RobloxPlayer.app")
            variants = discover_launch_variants(player, "player")
            self.assertEqual([LaunchVariant("player", player)], variants)

    def test_only_updater_present_returns_single_updater_variant(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            # The discovery key is the parent directory, so the passed bundle
            # path may itself be the updater; the player is simply absent.
            updater = _make_bundle(parent, "Roblox.app")
            variants = discover_launch_variants(updater, "player")
            self.assertEqual([LaunchVariant("updater", updater)], variants)

    def test_bundle_without_contents_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            player = _make_bundle(parent, "RobloxPlayer.app")
            # An updater directory without Contents is not a real bundle.
            (parent / "Roblox.app").mkdir()
            variants = discover_launch_variants(player, "player")
            self.assertEqual(["player"], [v.role for v in variants])

    def test_studio_has_no_updater_variant(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            studio = _make_bundle(parent, "RobloxStudio.app")
            # Even if a Roblox.app happens to sit beside Studio, it is not a
            # Studio updater and must not be offered as a Studio launch variant.
            _make_bundle(parent, "Roblox.app")
            variants = discover_launch_variants(studio, "studio")
            self.assertEqual([LaunchVariant("player", studio)], variants)

    def test_returns_absolute_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            player = _make_bundle(parent, "RobloxPlayer.app")
            variants = discover_launch_variants(str(player) + "/.", "player")
            self.assertTrue(variants[0].bundle_path.is_absolute())


class ValidateBundleUpdaterTests(unittest.TestCase):
    def test_default_rejects_updater_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            updater = _make_bundle(parent, "Roblox.app")
            with self.assertRaises(BundleValidationError):
                validate_bundle(updater, "player")

    def test_allow_updater_accepts_updater_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            updater = _make_bundle(parent, "Roblox.app")
            validated = validate_bundle(updater, "player", allow_updater=True)
            self.assertEqual(updater.resolve(), validated.resolve())

    def test_primary_accepted_with_and_without_allow_updater(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            player = _make_bundle(parent, "RobloxPlayer.app")
            self.assertEqual(player.resolve(), validate_bundle(player, "player").resolve())
            self.assertEqual(
                player.resolve(),
                validate_bundle(player, "player", allow_updater=True).resolve(),
            )

    def test_allow_updater_still_requires_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            updater = parent / "Roblox.app"
            updater.mkdir()  # no Contents directory
            with self.assertRaises(BundleValidationError):
                validate_bundle(updater, "player", allow_updater=True)

    def test_allow_updater_rejects_unrelated_name(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            other = _make_bundle(parent, "SomeOther.app")
            with self.assertRaises(BundleValidationError):
                validate_bundle(other, "player", allow_updater=True)

    def test_allow_updater_does_not_weaken_studio(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            other = _make_bundle(parent, "Roblox.app")
            # Studio has no updater variant, so Roblox.app must still be rejected.
            with self.assertRaises(BundleValidationError):
                validate_bundle(other, "studio", allow_updater=True)


if __name__ == "__main__":
    unittest.main()
