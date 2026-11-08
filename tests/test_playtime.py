import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from roseblox.backend.playtime import (  # noqa: E402
    Playtime,
    PlaytimeError,
    PlaytimeStore,
    default_playtime_path,
    describe,
    format_duration,
)
from roseblox import playtime_service  # noqa: E402
from roseblox.playtime_service import PlaytimeTracker, main, tracker_arguments  # noqa: E402

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "roseblox" / "playtime.json"
        self.store = PlaytimeStore(self.path)

    def test_nothing_played_yet_reads_as_zero(self):
        self.assertEqual({"player": Playtime(), "studio": Playtime()}, self.store.load())

    def test_sessions_and_seconds_add_up_per_application(self):
        self.store.start_session("player", NOW)
        self.store.add_seconds("player", 59.9, NOW)
        self.store.add_seconds("player", 61, NOW + timedelta(minutes=2))
        self.store.start_session("studio", NOW)
        self.store.add_seconds("studio", 0.4, NOW)
        self.assertEqual(Playtime(120, 1, NOW + timedelta(minutes=2)), self.store.get("player"))
        self.assertEqual(Playtime(0, 1, NOW), self.store.get("studio"))

    def test_reset_clears_only_that_application(self):
        self.store.start_session("player", NOW)
        self.store.start_session("studio", NOW)
        self.store.reset("player")
        self.assertEqual(Playtime(), self.store.get("player"))
        self.assertEqual(1, self.store.get("studio").sessions)

    def test_a_damaged_file_is_reported_and_replaced_on_the_next_save(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(PlaytimeError):
            self.store.load()
        self.store.add_seconds("player", 30, NOW)
        self.assertEqual(30, self.store.get("player").seconds)

    def test_bad_values_read_as_zero(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            json.dumps({"player": {"seconds": -5, "sessions": True, "last_played": "yesterday"}, "studio": 3}),
            encoding="utf-8",
        )
        self.assertEqual({"player": Playtime(), "studio": Playtime()}, self.store.load())

    def test_unknown_applications_are_refused(self):
        with self.assertRaises(ValueError):
            self.store.start_session("roblox")

    def test_the_file_lives_in_the_data_folder(self):
        self.assertEqual(
            Path("/data/roseblox/playtime.json"), default_playtime_path({"XDG_DATA_HOME": "/data", "HOME": "/h"})
        )
        self.assertEqual(Path("/h/.local/share/roseblox/playtime.json"), default_playtime_path({"HOME": "/h"}))


class TextTests(unittest.TestCase):
    def test_durations_round_down_to_minutes(self):
        self.assertEqual("less than a minute", format_duration(59))
        self.assertEqual("1 min", format_duration(60))
        self.assertEqual("2 h", format_duration(7200))
        self.assertEqual("125 h 3 min", format_duration(125 * 3600 + 3 * 60 + 59))

    def test_description(self):
        self.assertEqual("You have not played Roblox Player from Roseblox yet.", describe(Playtime(), "Roblox Player"))
        self.assertEqual(
            "Played for 1 h 30 min in 1 session. Last played today.",
            describe(Playtime(5400, 1, NOW), "Roblox Player", NOW),
        )
        self.assertEqual(
            "Played for 5 min in 3 sessions. Last played yesterday.",
            describe(Playtime(300, 3, NOW - timedelta(days=1)), "Roblox Studio", NOW),
        )
        self.assertIn("Last played 3 days ago.", describe(Playtime(60, 1, NOW - timedelta(days=3)), "X", NOW))
        self.assertIn("Last played on ", describe(Playtime(60, 1, NOW - timedelta(days=30)), "X", NOW))


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = PlaytimeStore(Path(self.temporary.name) / "playtime.json")
        self.now = 1000.0

    def _tracker(self, alive_checks, start_time=777):
        checks = iter(alive_checks)

        def snapshot(pid):
            self.assertEqual(4242, pid)
            return SimpleNamespace(start_time=777) if next(checks, False) else None

        def sleep(seconds):
            self.now += seconds

        return PlaytimeTracker(
            "studio", 4242, start_time, store=self.store, clock=lambda: self.now, sleep=sleep,
            process_snapshot=snapshot,
        )

    def test_counts_one_session_and_the_time_it_ran(self):
        saved = []
        original = self.store.add_seconds
        with patch.object(self.store, "add_seconds", side_effect=lambda *a: (saved.append(a[1]), original(*a))):
            # Alive at the start and for 25 polls of 5 seconds, then gone.
            self._tracker([True] * 26).run()
        self.assertEqual(Playtime(125, 1, self.store.get("studio").last_played), self.store.get("studio"))
        self.assertEqual([60.0, 60.0, 5.0], saved)

    def test_a_process_that_already_exited_is_not_counted(self):
        self._tracker([]).run()
        self.assertEqual(Playtime(), self.store.get("studio"))

    def test_a_reused_process_id_is_not_counted(self):
        self._tracker([True], start_time=1).run()
        self.assertEqual(Playtime(), self.store.get("studio"))

    def test_time_that_could_not_be_saved_is_kept_for_the_next_save(self):
        tracker = self._tracker([True])
        tracker._counted_until = self.now
        self.now += 90
        with patch.object(self.store, "add_seconds", side_effect=PlaytimeError("disk full")), patch.object(
            playtime_service, "_log"
        ):
            tracker.save()
        self.now += 10
        tracker.save()
        self.assertEqual(100, self.store.get("studio").seconds)

    def test_command_line(self):
        with patch.object(playtime_service, "PlaytimeTracker") as tracker, patch.object(
            playtime_service.signal, "signal"
        ):
            tracker.return_value.run.return_value = 0
            self.assertEqual(0, main(["roseblox", "--playtime", "player", "1", "2"]))
            tracker.assert_called_once_with("player", 1, 2)
        with patch("sys.stderr"):
            self.assertEqual(2, main(["roseblox", "--playtime", "roblox", "1", "2"]))
            self.assertEqual(2, main(["roseblox", "--playtime", "player", "x", "2"]))
            self.assertEqual(2, main(["roseblox", "--playtime", "player", "1"]))

    def test_tracker_arguments(self):
        with patch.object(playtime_service, "appimage_path", return_value=None):
            arguments = tracker_arguments("player", 4242, 777)
        self.assertEqual(["-m", "roseblox", "--playtime", "player", "4242", "777"], arguments[1:])
        with patch.object(playtime_service, "appimage_path", return_value="/apps/Roseblox.AppImage"):
            self.assertEqual(
                ["/apps/Roseblox.AppImage", "--playtime", "studio", "5", "6"], tracker_arguments("studio", 5, 6)
            )


if __name__ == "__main__":
    unittest.main()
