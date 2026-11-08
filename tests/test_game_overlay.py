import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from roseblox.backend import game_overlay
from roseblox.backend.game_overlay import (
    CONFIG_VARIABLE,
    HIDDEN,
    NO_MANGOHUD,
    GameOverlay,
    banner_config,
    clean_text,
    prepare_launch,
)


class OverlayTextTests(unittest.TestCase):
    def test_text_is_one_parsable_line(self):
        self.assertEqual("Server: a b c", clean_text("Server: a\nb#c"))
        self.assertEqual("x y", clean_text("x = y"))
        self.assertEqual("São Paulo, Brazil", clean_text("São Paulo,\tBrazil"))
        long = clean_text("y" * 500)
        self.assertEqual(game_overlay.MAX_TEXT, len(long))
        self.assertTrue(long.endswith("…"))

    def test_banner_shows_only_the_text_at_the_top(self):
        config = banner_config("Server: Frankfurt, Germany")
        self.assertIn("legacy_layout=0\n", config)
        self.assertIn("position=top-center\n", config)
        self.assertTrue(config.endswith("custom_text=Server: Frankfurt, Germany\n"))
        self.assertNotIn("no_display", config)


class PrepareLaunchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.env = {"HOME": str(self.root), "XDG_RUNTIME_DIR": str(self.root)}
        self.enterContext(patch.object(game_overlay.mangohud, "layer_installed", lambda env: True))

    def test_without_mangohud_there_is_no_overlay(self):
        with patch.object(game_overlay.mangohud, "layer_installed", lambda env: False):
            self.assertEqual((None, {}, NO_MANGOHUD), prepare_launch(self.env, "rosemary"))
        self.assertFalse((self.root / "roseblox").exists())

    def test_hidden_until_shown_and_mangohud_is_loaded(self):
        overlay, changes, problem = prepare_launch(self.env, "rosemary")
        self.assertIsNone(problem)
        self.assertEqual({"MANGOHUD": "1", CONFIG_VARIABLE: str(overlay.path)}, changes)
        self.assertEqual(self.root / "roseblox" / "overlay", overlay.path.parent)
        self.assertEqual(HIDDEN, overlay.path.read_text())
        self.assertEqual(0o600, overlay.path.stat().st_mode & 0o777)
        overlay.show("Server: Tokyo, Japan")
        self.assertIn("custom_text=Server: Tokyo, Japan", overlay.path.read_text())
        GameOverlay(overlay.path).hide()
        self.assertEqual(HIDDEN, overlay.path.read_text())
        overlay.remove()
        self.assertEqual([], list(overlay.path.parent.iterdir()))

    def test_the_users_own_mangohud_setup_is_what_the_banner_returns_to(self):
        config = self.root / ".config" / "MangoHud"
        config.mkdir(parents=True)
        (config / "MangoHud.conf").write_text("fps\ngpu_stats\n")
        env = {**self.env, "MANGOHUD": "1"}
        overlay, _changes, _problem = prepare_launch(env, "rosemary")
        self.assertEqual("fps\ngpu_stats\n", overlay.path.read_text())
        (config / "rosemary.conf").write_text("fps_only\n")
        overlay, _changes, _problem = prepare_launch(env, "rosemary")
        self.assertEqual("fps_only\n", overlay.path.read_text())
        own = self.root / "mine.conf"
        own.write_text("cpu_stats\n")
        overlay, changes, _problem = prepare_launch({**env, CONFIG_VARIABLE: str(own)}, "rosemary")
        self.assertEqual("cpu_stats\n", overlay.path.read_text())
        self.assertEqual(str(overlay.path), changes[CONFIG_VARIABLE])
        overlay.show("Server: Paris, France")
        overlay.hide()
        self.assertEqual("cpu_stats\n", overlay.path.read_text())
        self.assertEqual("cpu_stats\n", own.read_text())

    def test_an_empty_user_setup_means_mangohud_defaults(self):
        overlay, _changes, _problem = prepare_launch({**self.env, "MANGOHUD": "1"}, "rosemary")
        self.assertEqual("", overlay.path.read_text())

    def test_old_files_from_crashed_launches_are_cleaned_up(self):
        directory = self.root / "roseblox" / "overlay"
        directory.mkdir(parents=True)
        old = directory / "player-old.conf"
        old.write_text(HIDDEN)
        past = time.time() - game_overlay.STALE_SECONDS - 60
        os.utime(old, (past, past))
        recent = directory / "player-recent.conf"
        recent.write_text(HIDDEN)
        prepare_launch(self.env, "rosemary")
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())


if __name__ == "__main__":
    unittest.main()
