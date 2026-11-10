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
    media_row_fits,
    ping_config,
    prepare_launch,
)

YELLOW_ROW = (
    "media_player\nmedia_player_name=roseblox.instance7\nmedia_player_color=FDF909\n"
    "media_player_format={title}\nfont_scale_media_player=1\n"
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

    def test_ping_as_the_media_player_line_is_the_only_yellow_row(self):
        self.assertEqual(YELLOW_ROW, ping_config("", 31, "roseblox.instance7"))
        own = "fps\n# my colors\ntext_color=FFFFFF\ngpu_stats"
        config = ping_config(own, 18, "roseblox.instance7")
        self.assertEqual(f"{own}\n{YELLOW_ROW}", config)
        # The ping is the player's title, so the config does not change with it.
        self.assertEqual(config, ping_config(own, 250, "roseblox.instance7"))
        for changed in ("text_color=FDF909", "legacy_layout", "font_size", "custom_text", "exec", "time"):
            self.assertNotIn(changed, ping_config("fps\ngpu_stats\n", 18, "roseblox.instance7"))

    def test_media_player_settings_of_a_hud_that_shows_none_give_way_to_the_row(self):
        own = (
            "fps\nmedia_player=0\nmedia_player_name = spotify\nmedia_player_color=00FF00\n"
            "font_scale_media_player=0.4  # small\nmedia_player_format={artist};{title}\nram\n"
        )
        self.assertEqual(f"fps\nram\n{YELLOW_ROW}", ping_config(own, 18, "roseblox.instance7"))

    def test_a_hud_that_shows_the_users_music_keeps_the_ping_on_the_clock_line(self):
        for own in ("fps\nmedia_player\n", "media_player=1\n", "full\n", "media_player=0\nmedia_player\n"):
            self.assertFalse(media_row_fits(own), own)
            self.assertEqual(ping_config(own, 40), ping_config(own, 40, "roseblox.instance7"), own)
        for own in ("", "fps\n", "media_player=0\n", "#media_player\n", "full\nmedia_player=0\n", "media_player=true\n"):
            self.assertTrue(media_row_fits(own), own)

    def test_ping_on_its_own_is_yellow_without_a_media_player(self):
        self.assertFalse(media_row_fits(HIDDEN))
        self.assertEqual(ping_config(HIDDEN, 24), ping_config(HIDDEN, 24, "roseblox.instance7"))


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

    def test_the_file_is_rewritten_only_when_the_hud_changes(self):
        config = self.root / ".config" / "MangoHud"
        config.mkdir(parents=True)
        (config / "MangoHud.conf").write_text("fps\n")
        overlay, _changes, _problem = prepare_launch({**self.env, "MANGOHUD": "1"}, "rosemary")
        self.assertTrue(overlay.media_row_fits())

        def rewritten(change, *arguments):
            os.utime(overlay.path, ns=(1, 1))
            change(*arguments)
            return overlay.path.stat().st_mtime_ns != 1

        self.assertTrue(rewritten(overlay.show_ping, 24, "roseblox.instance7"))
        self.assertEqual(f"fps\n{YELLOW_ROW}", overlay.path.read_text())
        # MangoHud re-reads the file whenever it is written; a new ping reaches it as the player's title.
        self.assertFalse(rewritten(overlay.show_ping, 31, "roseblox.instance7"))
        self.assertTrue(rewritten(overlay.show_ping, 31))
        self.assertFalse(rewritten(overlay.show_ping, 31))
        self.assertTrue(rewritten(overlay.show_ping, 32))
        self.assertTrue(rewritten(overlay.hide))
        self.assertFalse(rewritten(overlay.hide))
        self.assertEqual("fps\n", overlay.path.read_text())

    def test_the_game_is_a_process_started_with_the_overlay_file_as_its_mangohud_config(self):
        import subprocess
        import sys

        overlay, changes, _problem = prepare_launch(self.env, "rosemary")
        other, _changes, _problem = prepare_launch(self.env, "rosemary")
        game = subprocess.Popen(
            [sys.executable, "-c", "import sys; print('started', flush=True); sys.stdin.read()"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, env={**os.environ, **changes},
        )
        try:
            # A process has no environment to read until it has been loaded.
            self.assertEqual(b"started\n", game.stdout.readline())
            self.assertTrue(overlay.configures(game.pid))
            self.assertFalse(other.configures(game.pid))
            self.assertFalse(overlay.configures(os.getpid()))
        finally:
            game.stdin.close()
            game.stdout.close()
            game.wait(10)
        # A process that cannot be looked into is not known to be another program.
        self.assertTrue(overlay.configures(game.pid))

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
