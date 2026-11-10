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
    ping_config,
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

    def test_banner_shows_only_the_text_at_the_top_center(self):
        config = banner_config("Server: Frankfurt, Germany")
        self.assertIn("legacy_layout=0\n", config)
        self.assertIn("position=top-center\n", config)
        self.assertIn("font_size=36\n", config)
        self.assertIn("background_alpha=0.7\n", config)
        self.assertTrue(config.endswith("custom_text_center=Server: Frankfurt, Germany\n"))
        self.assertNotIn("no_display", config)

    def test_a_long_banner_is_wide_enough_for_its_text(self):
        line = "Server: Santa Clara, California, United States of America"
        config = banner_config(line)
        width = int(config.split("width=")[1].split("\n")[0])
        self.assertGreater(width, 600)
        self.assertLess(width, 900)

    def test_ping_on_its_own_is_yellow_at_stat_size(self):
        self.assertEqual(
            "legacy_layout=0\ntext_color=FDF909\nfont_size=44\nwidth=160\ncustom_text=PING 24 ms\n",
            ping_config(HIDDEN, 24),
        )
        self.assertIn("width=186\ncustom_text=PING 1234 ms\n", ping_config(HIDDEN, 1234))
        self.assertIn("custom_text=PING 1 ms\n", ping_config(HIDDEN, 0))

    def test_ping_in_the_users_hud_is_a_first_row_that_changes_nothing_else(self):
        self.assertEqual("time\ntime_no_label\ntime_format=PING 31 ms\n", ping_config("", 31))
        own = "fps\n# my colors\ntext_color=FFFFFF\ngpu_stats"
        self.assertEqual(f"time\ntime_no_label\ntime_format=PING 18 ms\n{own}\n", ping_config(own, 18))
        # Neither the whole HUD's text color, its layout nor its fonts, and no shell inside the game.
        for changed in ("FDF909", "legacy_layout", "font_size", "custom_text", "exec"):
            self.assertNotIn(changed, ping_config("fps\ngpu_stats\n", 18))

    def test_ping_joins_a_clock_the_user_already_shows(self):
        self.assertEqual(
            "time\ntime_no_label\ntime_format=%H:%M  PING 40 ms\nfps\nram\n",
            ping_config("fps\ntime\ntime_format = %H:%M  # short\nram\n", 40),
        )
        for clock in ("time=1\n", "full\n", "time=0\ntime\n"):
            self.assertIn("time_format=%T  PING 40 ms\n", ping_config(clock, 40), clock)
        # MangoHud reads switches as numbers; a format alone does not show the clock.
        for no_clock in ("time=0\n", "#time\n", "time=true\n", "full\ntime=0\n", "time_format=%H:%M\ntime_no_label\n"):
            config = ping_config(no_clock, 40)
            self.assertIn("time_format=PING 40 ms\n", config, no_clock)
            self.assertEqual(1, config.splitlines().count("time"), no_clock)
            self.assertEqual(1, config.count("time_format="), no_clock)


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
        self.assertIn("custom_text_center=Server: Tokyo, Japan", overlay.path.read_text())
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
