import io
import json
import os
import signal
import socket
import struct
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from roseblox.activity_service import ActivityWatcher, _log, watcher_arguments
from roseblox.backend.activity import (
    ActivityTracker,
    GameJoined,
    GameJoining,
    GameLeft,
    LogFollower,
    find_roblox_log,
    public_address,
)
from roseblox.backend.discord_rpc import DiscordError, DiscordPresence, build_activity, socket_candidates
from roseblox.backend.game_info import GameDetails, GameInfoError, fetch_game, locate_server, ping_server
from roseblox.backend.microphone_sounds import MicrophoneSounds
from roseblox.backend.setup import SetupSettingsError, SetupStore, WhilePlaying

JOB = "e42cc7b0-9b0a-4b7e-8f35-1c2d3e4f5a6b"
JOIN = f"2026-10-18T13:49:33.349Z,1.2,abc,6 [FLog::Output] ! Joining game '{JOB}' place 2753915549 at 10.9.7.196"
PUBLIC = "2026-10-18T13:49:33.350Z,1.2,abc,7 [FLog::Network] Replicator created for player 128.116.44.3|53452"
CONNECTED = "2026-10-18T13:49:33.351Z,1.2,abc,7 [FLog::Network] Replicator created: 0x7f278ba47e40"
PRIVATE = "[FLog::GameJoinLoadTime] Report game_join_loadtime: referral_page:RequestPrivateGame,foo"
HOME = "2026-10-24T10:47:44.680Z,34.0,abc,6 [FLog::SingleSurfaceApp] returnToLuaApp: (stage:UGCGame)."
DISCONNECT = "[FLog::Network] Time to disconnect replication data: 0.01"


def feed(tracker, *lines):
    events = []
    for line in lines:
        events.extend(tracker.feed(line))
    return events


class TrackerTests(unittest.TestCase):
    def test_a_mac_player_log_from_rosemary(self):
        lines = [
            "2026-11-07T20:44:39.893Z,0.893042,e8cc0000,6 [FLog::SingleSurfaceApp] returnToLuaApp: (stage:InitializedLuaApp).",
            "2026-11-07T20:45:20.612Z,41.612572,e9ef0000,6 [FLog::Output] ! Joining game "
            "'11111111-2222-4333-8444-555555555555' place 1234567890 at 10.1.2.3",
            "2026-11-07T20:45:20.616Z,41.616253,e9ef0000,7 [FLog::Network] UDMUX Address = 128.116.0.10, Port = 53033 "
            "| RCC Server Address = 10.1.2.3, Port = 53033",
            "2026-11-07T20:45:20.766Z,41.766312,e9ef0000,7 [FLog::Network] Replicator created for player 128.116.0.10|53033",
            "2026-11-07T20:45:20.766Z,41.766319,e9ef0000,7 [FLog::Network] Replicator created: 0x7f921f78eb70",
            "2026-11-07T20:45:23.951Z,44.951740,e8cc0000,6 [FLog::SingleSurfaceApp] returnToLuaApp: (stage:UGCGame).",
            "2026-11-07T20:45:23.961Z,44.961334,ea990000,7 [FLog::Network] Time to disconnect replication data: 0.072429",
        ]
        tracker = ActivityTracker()
        events = [event for line in lines for event in tracker.feed(line)]
        self.assertEqual(
            [
                GameJoining(1234567890, "11111111-2222-4333-8444-555555555555"),
                GameJoined(1234567890, "11111111-2222-4333-8444-555555555555", "128.116.0.10", False),
                GameLeft(True),
            ],
            events,
        )

    def test_a_join_is_reported_once_connected_with_the_public_address(self):
        tracker = ActivityTracker()
        self.assertEqual([GameJoining(2753915549, JOB)], feed(tracker, JOIN, PUBLIC))
        [joined] = feed(tracker, CONNECTED)
        self.assertEqual(GameJoined(2753915549, JOB, "128.116.44.3", False), joined)
        self.assertEqual(
            f"https://www.roblox.com/games/start?placeId=2753915549&gameInstanceId={JOB}", joined.join_url
        )
        self.assertEqual("https://www.roblox.com/games/2753915549", joined.game_url)
        self.assertTrue(tracker.in_game)

    def test_private_servers_have_no_join_link(self):
        [_joining, joined] = feed(ActivityTracker(), JOIN, PRIVATE, CONNECTED)
        self.assertTrue(joined.private)
        self.assertIsNone(joined.join_url)
        self.assertIsNone(joined.server_address)

    def test_leaving_is_told_apart_from_returning_home(self):
        tracker = ActivityTracker()
        feed(tracker, JOIN, CONNECTED)
        self.assertEqual([GameLeft(False)], feed(tracker, DISCONNECT))
        self.assertEqual([GameLeft(True)], feed(tracker, HOME))
        self.assertEqual([], feed(tracker, DISCONNECT))
        self.assertEqual([], feed(ActivityTracker(), "returnToLuaApp: (stage:InitializedLuaApp)."))

    def test_a_late_disconnect_from_the_last_server_does_not_cancel_the_next_join(self):
        second_job = "22222222-3333-4444-8555-666666666666"
        teleport = [
            f"2026-11-11T06:01:10.100Z,50.100000,e9ef0000,6 [FLog::Output] ! Joining game '{second_job}' "
            "place 920587237 at 10.4.5.6",
            "2026-11-11T06:01:10.104Z,50.104000,ea990000,7 [FLog::Network] Time to disconnect replication data: 0.0041",
            "2026-11-11T06:01:10.110Z,50.110000,e9ef0000,7 [FLog::Network] UDMUX Address = 128.116.50.7, Port = 50001 "
            "| RCC Server Address = 10.4.5.6, Port = 50001",
            "2026-11-11T06:01:10.190Z,50.190000,e9ef0000,7 [FLog::Network] Replicator created: 0x7f921f78eb71",
        ]
        tracker = ActivityTracker()
        feed(tracker, JOIN, PUBLIC, CONNECTED)
        self.assertEqual(
            [
                GameJoining(920587237, second_job),
                GameLeft(False),
                GameJoined(920587237, second_job, "128.116.50.7", False),
            ],
            feed(tracker, *teleport),
        )
        self.assertTrue(tracker.in_game)
        self.assertEqual(40, tracker.last_join_latency_ms)
        feed(tracker, HOME)
        self.assertEqual(
            [GameJoining(920587237, second_job), GameJoined(920587237, second_job, "128.116.50.7", False)],
            feed(tracker, *teleport),
        )

    def test_leaving_while_joining_still_cancels_the_join(self):
        tracker = ActivityTracker()
        self.assertEqual(
            [GameJoining(2753915549, JOB)],
            feed(tracker, JOIN, "[FLog::SingleSurfaceApp] leaveUGCGameInternal", CONNECTED),
        )
        self.assertFalse(tracker.in_game)

    def test_malformed_lines_are_ignored(self):
        tracker = ActivityTracker()
        self.assertEqual([], feed(tracker, "! Joining game '' place  at 1.2.3.4", CONNECTED, "[FLog::Output] hello"))
        self.assertEqual([], feed(tracker, "! Joining game 'not-a-uuid' place 1 at 128.116.0.1", CONNECTED))

    def test_only_public_addresses_are_kept(self):
        self.assertEqual("128.116.44.3", public_address("128.116.44.3"))
        for address in ("10.0.0.1", "127.0.0.1", "192.168.1.2", "0.0.0.0", "::1", "fe80::1", "nonsense"):
            self.assertIsNone(public_address(address), address)


class LogFollowerTests(unittest.TestCase):
    def test_lines_are_read_as_they_are_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "player.log"
            follower = LogFollower(path)
            self.assertEqual([], follower.read_lines())
            path.write_bytes(b"first\r\nsec")
            self.assertEqual(["first"], follower.read_lines())
            with path.open("ab") as stream:
                stream.write(b"ond \xff\nthird")
            self.assertEqual(["second \ufffd"], follower.read_lines())
            path.write_bytes(b"new\n")
            self.assertEqual(["new"], follower.read_lines())


class FakeDiscord:
    """A Unix socket server speaking Discord's IPC framing."""

    def __init__(self, directory: Path, reply_error: bool = False) -> None:
        self.path = directory / "discord-ipc-0"
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.path))
        self.server.listen(1)
        self.frames = []
        self.reply_error = reply_error
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _read(self, connection, size):
        data = b""
        while len(data) < size:
            chunk = connection.recv(size - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    def _send(self, connection, opcode, payload):
        data = json.dumps(payload).encode()
        connection.sendall(struct.pack("<II", opcode, len(data)) + data)

    def _serve(self):
        connection, _ = self.server.accept()
        with connection:
            try:
                while True:
                    opcode, length = struct.unpack("<II", self._read(connection, 8))
                    payload = json.loads(self._read(connection, length))
                    self.frames.append((opcode, payload))
                    if opcode == 0:
                        if self.reply_error:
                            self._send(connection, 2, {"code": 4000, "message": "Invalid Client ID"})
                            return
                        self._send(connection, 1, {"cmd": "DISPATCH", "evt": "READY", "data": {}})
                    else:
                        self._send(connection, 1, {"cmd": payload["cmd"], "nonce": payload["nonce"], "data": {}})
            except EOFError:
                pass

    def close(self):
        self.server.close()


class DiscordTests(unittest.TestCase):
    def test_presence_handshakes_and_sets_the_activity(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeDiscord(Path(directory))
            presence = DiscordPresence("123456789012345678", env={"XDG_RUNTIME_DIR": directory})
            activity = build_activity(
                game_name="Blox Fruits", creator="Gamer Robot Inc", started_at=1000,
                icon_url="https://tr.rbxcdn.com/icon", buttons=[("Join Server", "https://www.roblox.com/games/start?placeId=1")],
            )
            presence.set_activity(4242, activity)
            presence.set_activity(4242, None)
            presence.close()
            fake.thread.join(timeout=5)
            fake.close()
        self.assertEqual((0, {"v": 1, "client_id": "123456789012345678"}), fake.frames[0])
        opcode, payload = fake.frames[1]
        self.assertEqual(1, opcode)
        self.assertEqual("SET_ACTIVITY", payload["cmd"])
        self.assertEqual(4242, payload["args"]["pid"])
        self.assertEqual("Blox Fruits", payload["args"]["activity"]["details"])
        self.assertEqual("by Gamer Robot Inc", payload["args"]["activity"]["state"])
        self.assertEqual("https://tr.rbxcdn.com/icon", payload["args"]["activity"]["assets"]["large_image"])
        self.assertIsNone(fake.frames[2][1]["args"]["activity"])

    def test_a_refused_application_id_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeDiscord(Path(directory), reply_error=True)
            presence = DiscordPresence("123456789012345678", env={"XDG_RUNTIME_DIR": directory})
            with self.assertRaisesRegex(DiscordError, "Invalid Client ID"):
                presence.connect()
            fake.close()

    def test_missing_discord_and_bad_ids_raise(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(DiscordError, "not running"):
                DiscordPresence("123456789012345678", env={"XDG_RUNTIME_DIR": directory, "TMPDIR": directory}).connect()
        with self.assertRaises(DiscordError):
            DiscordPresence("roseblox")

    def test_flatpak_and_snap_sockets_are_searched(self):
        candidates = [str(path) for path in socket_candidates({"XDG_RUNTIME_DIR": "/run/user/1000"})]
        self.assertIn("/run/user/1000/discord-ipc-0", candidates)
        self.assertIn("/run/user/1000/app/com.discordapp.Discord/discord-ipc-0", candidates)
        self.assertIn("/run/user/1000/snap.discord/discord-ipc-9", candidates)

    def test_activity_text_and_buttons_stay_within_discord_limits(self):
        activity = build_activity(
            game_name="x" * 300, creator=None, started_at=1, icon_url="http://insecure", buttons=[
                ("A very long button label that goes on", "https://a"), ("b", "roblox://x"), ("c", "https://c"),
            ],
        )
        self.assertEqual(128, len(activity["details"]))
        self.assertNotIn("state", activity)
        self.assertNotIn("large_image", activity["assets"])
        self.assertEqual(["https://a"], [button["url"] for button in activity["buttons"]])
        self.assertLessEqual(len(activity["buttons"][0]["label"]), 32)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def opener_for(routes):
    requested = []

    def opener(request, timeout):
        requested.append(request.full_url)
        for prefix, payload in routes.items():
            if request.full_url.startswith(prefix):
                if isinstance(payload, Exception):
                    raise payload
                return FakeResponse(json.dumps(payload).encode())
        raise OSError("no route")

    opener.requested = requested
    return opener


class LookupTests(unittest.TestCase):
    def test_game_details_come_from_public_endpoints(self):
        opener = opener_for({
            "https://apis.roblox.com/universes/v1/places/2753915549/universe": {"universeId": 994732206},
            "https://games.roblox.com/v1/games?universeIds=994732206": {"data": [
                {"name": " Blox Fruits ", "creator": {"name": "Gamer Robot Inc"}}]},
            "https://thumbnails.roblox.com/v1/games/icons?universeIds=994732206": {"data": [
                {"state": "Completed", "imageUrl": "https://tr.rbxcdn.com/icon"}]},
        })
        self.assertEqual(
            GameDetails(2753915549, 994732206, "Blox Fruits", "Gamer Robot Inc", "https://tr.rbxcdn.com/icon"),
            fetch_game(2753915549, opener),
        )

    def test_a_missing_icon_does_not_fail_the_lookup(self):
        opener = opener_for({
            "https://apis.roblox.com/": {"universeId": 5},
            "https://games.roblox.com/": {"data": [{"name": "Game"}]},
            "https://thumbnails.roblox.com/": OSError("down"),
        })
        self.assertEqual(GameDetails(1, 5, "Game", None, None), fetch_game(1, opener))
        with self.assertRaises(GameInfoError):
            fetch_game(1, opener_for({"https://apis.roblox.com/": {"errors": []}}))

    def test_server_location_is_looked_up_only_for_public_addresses(self):
        opener = opener_for({"https://ipwho.is/128.116.44.3": {
            "success": True, "city": "Frankfurt am Main", "region": "Hessen", "country": "Germany"}})
        self.assertEqual("Frankfurt am Main, Hessen, Germany", locate_server("128.116.44.3", opener))
        with self.assertRaises(GameInfoError):
            locate_server("192.168.1.5", opener)
        self.assertEqual(["https://ipwho.is/128.116.44.3"], opener.requested)
        with self.assertRaisesRegex(GameInfoError, "could not locate"):
            locate_server("128.116.0.1", opener_for({"https://ipwho.is/": {"success": False, "message": "Reserved"}}))

    def test_server_ping_is_measured_only_for_public_addresses(self):
        calls = []

        def fake_run(cmd, **_kwargs):
            calls.append(cmd)
            return SimpleNamespace(stdout="64 bytes from 128.116.44.3: icmp_seq=1 ttl=54 time=23.6 ms\n")

        self.assertEqual(24, ping_server("128.116.44.3", fake_run))
        self.assertIsNone(ping_server("10.0.0.1", fake_run))
        self.assertEqual(1, len(calls))
        self.assertIsNone(ping_server("128.116.44.3", lambda *a, **k: SimpleNamespace(stdout="100% packet loss")))

        # When a UDMUX host drops ICMP, the subnet .3 border router or TTL hop is used.
        def router_run(cmd, **_kwargs):
            if cmd[-1] == "128.116.44.3":
                return SimpleNamespace(stdout="64 bytes from 128.116.44.3: icmp_seq=1 ttl=54 time=19.2 ms\n")
            return SimpleNamespace(stdout="100% packet loss")

        self.assertEqual(19, ping_server("128.116.44.10", router_run))


class WhilePlayingSettingsTests(unittest.TestCase):
    def test_settings_round_trip_and_reject_bad_values(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SetupStore(Path(directory) / "setup.json")
            self.assertEqual(WhilePlaying(), store.load().while_playing)
            saved = WhilePlaying(True, False, "123456789012345678", True, True)
            self.assertEqual(saved, store.save_while_playing(saved).while_playing)
            with self.assertRaises(ValueError):
                store.save_while_playing(WhilePlaying(discord_application_id="abc"))
            document = json.loads(store.path.read_text())
            document["while_playing"]["close_on_leave"] = "yes"
            store.path.write_text(json.dumps(document))
            with self.assertRaises(SetupSettingsError):
                store.load()

    def test_ping_switches_round_trip_and_are_off_until_turned_on(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SetupStore(Path(directory) / "setup.json")
            defaults = store.load().while_playing
            self.assertEqual(
                (False, False, False),
                (defaults.server_ping, defaults.server_ping_constant, defaults.server_ping_yellow),
            )
            saved = WhilePlaying(server_ping=True, server_ping_constant=True, server_ping_yellow=True)
            self.assertEqual(saved, store.save_while_playing(saved).while_playing)
            self.assertEqual(saved, store.load().while_playing)
            with self.assertRaises(ValueError):
                store.save_while_playing(WhilePlaying(server_ping_yellow="yes"))
            # Settings saved before the yellow row existed.
            document = json.loads(store.path.read_text())
            del document["while_playing"]["server_ping_yellow"]
            store.path.write_text(json.dumps(document))
            self.assertFalse(store.load().while_playing.server_ping_yellow)

    def test_microphone_settings_round_trip_and_reject_bad_values(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SetupStore(Path(directory) / "setup.json")
            defaults = store.load().while_playing
            self.assertEqual((False, "toggle", 250), (
                defaults.microphone_toggle, defaults.microphone_mode, defaults.microphone_release_delay_ms,
            ))
            self.assertFalse(defaults.any_enabled)
            self.assertEqual(MicrophoneSounds(), defaults.sounds)
            saved = WhilePlaying(
                microphone_toggle=True, microphone_mode="hold", microphone_release_delay_ms=400, microphone_inverted=True,
                microphone_sounds=True, microphone_sound_volume=25, microphone_sound_off="/tmp/off.ogg",
            )
            self.assertTrue(saved.any_enabled)
            self.assertEqual(saved, store.save_while_playing(saved).while_playing)
            for bad in (
                WhilePlaying(microphone_mode="sometimes"),
                WhilePlaying(microphone_release_delay_ms=-5),
                WhilePlaying(microphone_release_delay_ms=True),
                WhilePlaying(microphone_toggle="yes"),
                WhilePlaying(microphone_inverted=1),
                WhilePlaying(microphone_sounds="on"),
                WhilePlaying(microphone_sound_volume=101),
                WhilePlaying(microphone_sound_volume=True),
                WhilePlaying(microphone_sound_on="relative.wav"),
            ):
                with self.assertRaises(ValueError):
                    store.save_while_playing(bad)
            document = json.loads(store.path.read_text())
            document["while_playing"]["microphone_release_delay_ms"] = 1e9
            store.path.write_text(json.dumps(document))
            with self.assertRaises(SetupSettingsError):
                store.load()


class FakePresence:
    instances = []

    def __init__(self, application_id):
        self.application_id = application_id
        self.activities = []
        self.closed = False
        FakePresence.instances.append(self)

    def set_activity(self, pid, activity):
        self.activities.append((pid, activity))

    def close(self):
        self.closed = True


class FakePingPlayer:
    name = "roseblox.instance7"

    def __init__(self, on_bus=True):
        self.on_bus = on_bus
        self.reads = 0
        self.titles = []
        self.hidden = 0
        self.stopped = False

    def show(self, title):
        if self.on_bus:
            self.titles.append(title)
        return self.on_bus

    def hide(self):
        self.hidden += 1

    def stop(self):
        self.stopped = True


YELLOW_ROW = (
    "media_player\nmedia_player_name=roseblox.instance7\nmedia_player_color=FDF909\n"
    "media_player_format={title}\nfont_scale_media_player=1\n"
)


class WatcherTests(unittest.TestCase):
    def setUp(self):
        FakePresence.instances = []
        self.directory = tempfile.TemporaryDirectory()
        self.log = Path(self.directory.name) / "player.log"
        self.log.write_text("")
        self.settings = WhilePlaying(True, True, "123456789012345678", True, True)
        self.start_time = 777
        self.kills = []
        self.notifications = []
        self.lookups = []

    def tearDown(self):
        self.directory.cleanup()

    def _watcher(self, **overrides):
        def lookup(place_id):
            self.lookups.append(place_id)
            return GameDetails(place_id, 9, "Blox Fruits", "Gamer Robot Inc", "https://tr.rbxcdn.com/icon")

        options = dict(
            settings=lambda: self.settings,
            presence_factory=FakePresence,
            game_lookup=lookup,
            locate=lambda address: "Frankfurt am Main, Hessen, Germany",
            notify=lambda summary, body: self.notifications.append((summary, body)),
            clock=lambda: 1000.0,
            sleep=lambda _seconds: None,
            process_snapshot=lambda pid: SimpleNamespace(start_time=self.start_time),
            kill=lambda pid, sig: self.kills.append((pid, sig)),
            find_log=lambda *_args: None,
            started_at=lambda _start_time: None,
        )
        options.update(overrides)
        return ActivityWatcher(4242, 777, self.log, **options)

    def _write(self, *lines):
        with self.log.open("a") as stream:
            stream.write("\n".join(lines) + "\n")

    def test_joining_sets_presence_and_notifies_the_location(self):
        watcher = self._watcher()
        self._write(JOIN, PUBLIC, CONNECTED)
        watcher.poll()
        [presence] = FakePresence.instances
        self.assertEqual("123456789012345678", presence.application_id)
        pid, activity = presence.activities[-1]
        self.assertEqual(4242, pid)
        self.assertEqual("Blox Fruits", activity["details"])
        self.assertEqual(1000, activity["timestamps"]["start"])
        self.assertEqual(["Join Server", "View Game"], [button["label"] for button in activity["buttons"]])
        self.assertEqual(
            [("Connected to a Roblox server", "The server is in Frankfurt am Main, Hessen, Germany.")],
            self.notifications,
        )
        self._write(DISCONNECT, JOIN, CONNECTED)
        watcher.poll()
        self.assertEqual([2753915549], self.lookups)

    def test_join_button_can_be_turned_off_and_private_servers_never_get_one(self):
        self.settings = WhilePlaying(True, False, "123456789012345678", False, False)
        watcher = self._watcher()
        self._write(JOIN, CONNECTED)
        watcher.poll()
        self.assertEqual(["View Game"], [b["label"] for b in FakePresence.instances[0].activities[-1][1]["buttons"]])
        self.assertEqual([], self.notifications)
        self.settings = WhilePlaying(True, True, "123456789012345678", False, False)
        self._write(DISCONNECT, JOIN, PRIVATE, CONNECTED)
        watcher.poll()
        self.assertEqual(["View Game"], [b["label"] for b in FakePresence.instances[-1].activities[-1][1]["buttons"]])

    def _overlay_watcher(self, resting=None, **overrides):
        from roseblox.backend.game_overlay import HIDDEN, GameOverlay

        overlay = GameOverlay.create(Path(self.directory.name) / "overlay", HIDDEN if resting is None else resting)
        self.now = 1000.0
        watcher = self._watcher(overlay=overlay, clock=lambda: self.now, **overrides)
        return watcher, overlay

    def test_server_location_banner_shows_inside_the_game_then_hides(self):
        from roseblox.backend.game_overlay import BANNER_SECONDS, HIDDEN

        self.settings = WhilePlaying(server_location=True, server_location_in_game=True)
        watcher, overlay = self._overlay_watcher()
        self._write(JOIN, PUBLIC, CONNECTED)
        watcher.poll()
        self.assertEqual([], self.notifications)
        self.assertIn("time_format=Server: Frankfurt am Main, Hessen, Germany\n", overlay.path.read_text())
        self.now += BANNER_SECONDS - 1
        watcher.poll()
        self.assertIn("position=top-center\n", overlay.path.read_text())
        self.now += 1
        watcher.poll()
        self.assertEqual(HIDDEN, overlay.path.read_text())

    def test_constant_ping_overlay_updates_every_30_seconds_and_hides_on_leave(self):
        from roseblox.backend.game_overlay import BANNER_SECONDS, HIDDEN

        pings = iter([24, 31, 19])
        self.settings = WhilePlaying(
            server_location=True,
            server_location_in_game=True,
            server_ping=True,
            server_ping_constant=True,
        )
        watcher, overlay = self._overlay_watcher(ping=lambda _addr: next(pings))
        self._write(JOIN, PUBLIC, CONNECTED)
        watcher.poll()
        self.assertIn("position=top-center\n", overlay.path.read_text())
        self.assertIn("time_format=Server: Frankfurt am Main, Hessen, Germany · 24 ms\n", overlay.path.read_text())
        self.now += BANNER_SECONDS
        watcher.poll()
        self.assertIn("text_color=FDF909\n", overlay.path.read_text())
        self.assertIn("time_format=PING 24 ms\n", overlay.path.read_text())
        self.now += 20
        watcher.poll()
        self.assertIn("time_format=PING 31 ms\n", overlay.path.read_text())
        self.now += 30
        watcher.poll()
        self.assertIn("time_format=PING 19 ms\n", overlay.path.read_text())
        self._write(DISCONNECT)
        watcher.poll()
        self.assertEqual(HIDDEN, overlay.path.read_text())

    def test_constant_ping_is_one_row_of_the_users_hud_and_leaves_its_colors_alone(self):
        from roseblox.backend.game_overlay import BANNER_SECONDS

        own = "fps\ntext_color=FFFFFF\ngpu_stats\n"
        pings = iter([24, 31])
        self.settings = WhilePlaying(
            server_location=True,
            server_location_in_game=True,
            server_ping=True,
            server_ping_constant=True,
        )
        watcher, overlay = self._overlay_watcher(resting=own, ping=lambda _addr: next(pings))
        self._write(JOIN, PUBLIC, CONNECTED)
        watcher.poll()
        self.assertIn("time_format=Server: Frankfurt am Main, Hessen, Germany · 24 ms\n", overlay.path.read_text())
        self.now += BANNER_SECONDS
        watcher.poll()
        self.assertEqual(f"time\ntime_no_label\ntime_format=PING 24 ms\n{own}", overlay.path.read_text())
        self.now += 30
        watcher.poll()
        self.assertEqual(f"time\ntime_no_label\ntime_format=PING 31 ms\n{own}", overlay.path.read_text())
        self._write(DISCONNECT)
        watcher.poll()
        self.assertEqual(own, overlay.path.read_text())

    def test_without_the_location_the_ping_row_is_added_without_a_banner(self):
        self.settings = WhilePlaying(server_ping=True, server_ping_constant=True)
        watcher, overlay = self._overlay_watcher(resting="", ping=lambda _addr: 24)
        self._write(JOIN, PUBLIC, CONNECTED)
        watcher.poll()
        self.assertEqual("time\ntime_no_label\ntime_format=PING 24 ms\n", overlay.path.read_text())
        self.assertEqual([], self.notifications)

    def _yellow_watcher(self, resting, pings=(24, 31, 19), player=None, **settings):
        values = iter(pings)
        self.settings = WhilePlaying(server_ping=True, server_ping_constant=True, server_ping_yellow=True, **settings)
        players = []

        def factory(is_game):
            players.append(player or FakePingPlayer())
            players[-1].is_game = is_game
            return players[-1]

        watcher, overlay = self._overlay_watcher(
            resting=resting, ping=lambda _addr: next(values), ping_player_factory=factory,
        )
        return watcher, overlay, players

    def test_the_yellow_ping_row_is_a_media_player_whose_title_is_the_ping(self):
        from roseblox.backend.game_overlay import BANNER_SECONDS

        own = "fps\ntext_color=FFFFFF\ngpu_stats\n"
        watcher, overlay, players = self._yellow_watcher(own, server_location=True)
        self._write(JOIN, PUBLIC, CONNECTED)
        watcher.poll()
        self.assertIn("time_format=Server: Frankfurt am Main, Hessen, Germany · 24 ms\n", overlay.path.read_text())
        self.assertEqual([], players)
        self.now += BANNER_SECONDS
        watcher.poll()
        [player] = players
        self.assertEqual(["PING 24 ms"], player.titles)
        self.assertEqual(own + YELLOW_ROW, overlay.path.read_text())
        self.assertEqual(watcher._is_game, player.is_game)
        # MangoHud loads the row and asks the player for its title.
        player.reads += 1
        os.utime(overlay.path, ns=(1, 1))
        self.now += 30
        watcher.poll()
        self.assertEqual(["PING 24 ms", "PING 31 ms"], player.titles)
        self.assertEqual(1, overlay.path.stat().st_mtime_ns, "MangoHud reloads its HUD when the file is rewritten")
        for _poll in range(20):
            watcher.poll()
        self.assertEqual(own + YELLOW_ROW, overlay.path.read_text())
        self.assertEqual(0, player.hidden)
        self._write(DISCONNECT)
        watcher.poll()
        self.assertEqual(own, overlay.path.read_text())
        self.assertEqual(1, player.hidden)
        self.assertFalse(player.stopped)
        self.start_time = 999
        self.assertEqual(0, watcher.run())
        self.assertTrue(player.stopped)

    def test_the_game_is_what_this_launch_started(self):
        import subprocess
        import sys

        from roseblox.activity_service import _descends_from
        from roseblox.backend.game_overlay import CONFIG_VARIABLE

        watcher, overlay = self._overlay_watcher(resting="fps\n")

        def process(**environment):
            started = subprocess.Popen(
                [sys.executable, "-c", "import sys; print('started', flush=True); sys.stdin.read()"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, env={**os.environ, **environment},
            )
            self.addCleanup(started.wait, 10)
            self.addCleanup(started.stdout.close)
            self.addCleanup(started.stdin.close)
            self.assertEqual(b"started\n", started.stdout.readline())
            return started.pid

        watcher.pid = process()
        other = process()
        self.assertFalse(watcher._is_game(other))
        # MangoHud is told its config by the environment the Player was started with.
        self.assertTrue(watcher._is_game(process(**{CONFIG_VARIABLE: str(overlay.path)})))
        self.assertFalse(watcher._is_game(process(**{CONFIG_VARIABLE: str(overlay.resting_path)})))
        # A Player that changed its environment is still the process this watcher follows, or a child of it.
        self.assertTrue(watcher._is_game(watcher.pid))
        watcher.pid = os.getpid()
        self.assertTrue(watcher._is_game(other))
        self.assertTrue(_descends_from(os.getpid(), os.getppid()))
        self.assertFalse(_descends_from(os.getppid(), os.getpid()))

    def test_the_banner_of_the_next_server_takes_the_player_off_the_bus_until_the_row_is_back(self):
        from roseblox.backend.game_overlay import BANNER_SECONDS

        watcher, overlay, players = self._yellow_watcher("fps\n", server_location=True)
        self._write(JOIN, PUBLIC, CONNECTED)
        watcher.poll()
        self.now += BANNER_SECONDS
        watcher.poll()
        [player] = players
        player.reads += 1
        self._write(JOIN, PUBLIC, CONNECTED)
        watcher.poll()
        self.assertIn("time_format=Server: Frankfurt am Main, Hessen, Germany · 31 ms\n", overlay.path.read_text())
        self.assertEqual(1, player.hidden)
        self.now += BANNER_SECONDS
        watcher.poll()
        self.assertEqual("fps\n" + YELLOW_ROW, overlay.path.read_text())
        self.assertEqual(["PING 24 ms", "PING 31 ms"], player.titles)
        self.assertEqual(1, len(players))

    def test_a_mangohud_that_never_asks_for_the_title_gets_the_row_in_its_own_color(self):
        from roseblox.activity_service import PLAYER_ROW_POLLS

        watcher, overlay, players = self._yellow_watcher("fps\n")
        self._write(JOIN, PUBLIC, CONNECTED)
        with patch("sys.stderr"):
            for _poll in range(PLAYER_ROW_POLLS - 1):
                watcher.poll()
                self.assertEqual("fps\n" + YELLOW_ROW, overlay.path.read_text())
            [player] = players
            self.assertEqual(0, player.hidden)
            watcher.poll()
        self.assertEqual("time\ntime_no_label\ntime_format=PING 24 ms\nfps\n", overlay.path.read_text())
        self.assertEqual(1, player.hidden)
        self.now += 30
        watcher.poll()
        self.assertEqual("time\ntime_no_label\ntime_format=PING 31 ms\nfps\n", overlay.path.read_text())
        self.assertEqual(["PING 24 ms"], player.titles)
        self.assertEqual(1, len(players))

    def test_without_a_session_bus_the_row_is_in_the_huds_own_color(self):
        watcher, overlay, players = self._yellow_watcher("fps\n", player=FakePingPlayer(on_bus=False))
        self._write(JOIN, PUBLIC, CONNECTED)
        watcher.poll()
        self.assertEqual("time\ntime_no_label\ntime_format=PING 24 ms\nfps\n", overlay.path.read_text())
        self.now += 30
        watcher.poll()
        self.assertEqual("time\ntime_no_label\ntime_format=PING 31 ms\nfps\n", overlay.path.read_text())
        self.assertEqual(1, len(players))

    def test_the_yellow_row_needs_its_switch_and_a_hud_of_the_users_that_shows_no_media_player(self):
        from dataclasses import replace

        from roseblox.backend.game_overlay import HIDDEN

        for resting, expected in (
            (HIDDEN, "legacy_layout=0\ntext_color=FDF909\ntable_columns=1\nwidth=136\ntime\ntime_no_label\n"
                     "time_format=PING 24 ms\n"),
            ("fps\nmedia_player\n", "time\ntime_no_label\ntime_format=PING 24 ms\nfps\nmedia_player\n"),
        ):
            watcher, overlay, players = self._yellow_watcher(resting)
            self._write(JOIN, PUBLIC, CONNECTED)
            watcher.poll()
            self.assertEqual(expected, overlay.path.read_text())
            self.assertEqual([], players)
            self.log.write_text("")

        watcher, overlay, players = self._yellow_watcher("fps\n")
        self.settings = replace(self.settings, server_ping_yellow=False)
        self._write(JOIN, PUBLIC, CONNECTED)
        watcher.poll()
        self.assertEqual("time\ntime_no_label\ntime_format=PING 24 ms\nfps\n", overlay.path.read_text())
        self.assertEqual([], players)
        # The switch works in the middle of a game, at the next ping.
        self.settings = replace(self.settings, server_ping_yellow=True)
        self.now += 30
        watcher.poll()
        self.assertEqual("fps\n" + YELLOW_ROW, overlay.path.read_text())
        [player] = players
        player.reads += 1
        self.settings = replace(self.settings, server_ping_yellow=False)
        self.now += 30
        watcher.poll()
        self.assertEqual("time\ntime_no_label\ntime_format=PING 19 ms\nfps\n", overlay.path.read_text())
        self.assertEqual(1, player.hidden)

    def test_with_the_in_game_switch_off_the_location_is_a_desktop_notification(self):
        from roseblox.backend.game_overlay import HIDDEN

        self.assertTrue(WhilePlaying().server_location_in_game)
        self.settings = WhilePlaying(server_location=True, server_location_in_game=False)
        watcher, overlay = self._overlay_watcher()
        self._write(JOIN, PUBLIC, CONNECTED)
        watcher.poll()
        self.assertEqual(1, len(self.notifications))
        self.assertEqual(HIDDEN, overlay.path.read_text())

    def test_a_broken_banner_falls_back_to_a_notification(self):
        self.settings = WhilePlaying(server_location=True, server_location_in_game=True)
        watcher, overlay = self._overlay_watcher()
        overlay.path.chmod(0o400)
        self._write(JOIN, PUBLIC, CONNECTED)
        try:
            with patch("sys.stderr"):
                watcher.poll()
        finally:
            overlay.path.chmod(0o600)
        self.assertEqual(1, len(self.notifications))

    def test_banner_file_is_removed_only_after_roblox_exits(self):
        from roseblox.backend.game_overlay import HIDDEN

        self.settings = WhilePlaying(server_location=True, server_location_in_game=True)
        watcher, overlay = self._overlay_watcher()
        self._write(JOIN, PUBLIC, CONNECTED)
        watcher.poll()
        watcher._finish_overlay()
        self.assertEqual(HIDDEN, overlay.path.read_text())
        self.start_time = 1
        watcher._finish_overlay()
        self.assertFalse(overlay.path.exists())
        self.assertFalse(overlay.resting_path.exists())

    def _roblox_log(self, *lines):
        path = Path(self.directory.name) / "0.747.0.7471481_20261111T152214Z_Player_1cfb7_last.log"
        with path.open("a") as stream:
            stream.write("".join(f"{line}\n" for line in lines))
        return path

    def test_once_mangohud_blacklists_the_player_its_config_is_never_changed_again(self):
        from roseblox.backend.game_overlay import BANNER_SECONDS

        own = "fps\ngpu_stats\n"
        self.settings = WhilePlaying(
            server_location=True, server_location_in_game=True, server_ping=True, server_ping_constant=True,
        )
        roblox_log = self._roblox_log()
        watcher, overlay = self._overlay_watcher(
            resting=own, ping=lambda _addr: 24, find_log=lambda *_args: roblox_log,
        )
        with redirect_stderr(io.StringIO()) as stderr:
            self._roblox_log(JOIN, PUBLIC, CONNECTED)
            watcher.poll()
            self.now += BANNER_SECONDS
            watcher.poll()
            self.assertEqual(f"time\ntime_no_label\ntime_format=PING 24 ms\n{own}", overlay.path.read_text())
            self._roblox_log(HOME)
            watcher.poll()
            self.assertEqual(own, overlay.path.read_text())
            # Rosemary opened the server list, a GTK window inside the Player.
            self._write("[2026-11-11 18:23:44.460] [MANGOHUD] [info] [blacklist.cpp:86] "
                        "process 'rosemary' is blacklisted in MangoHud")
            watcher.poll()
            self._roblox_log(JOIN, PUBLIC, CONNECTED)
            watcher.poll()
            self.now += BANNER_SECONDS + 30
            watcher.poll()
            self._roblox_log(HOME)
            watcher.poll()
            watcher._finish_overlay()
        self.assertEqual(1, stderr.getvalue().count("MangoHud stopped taking config changes in this Player"))
        self.assertNotIn("unexpected error", stderr.getvalue())
        self.assertEqual(
            [("Connected to a Roblox server", "The server is in Frankfurt am Main, Hessen, Germany (24 ms).")],
            self.notifications,
        )
        self.assertEqual(own, overlay.path.read_text())
        # A process whose environment cannot be read still counts as the game for the yellow row's player.
        self.assertTrue(watcher._is_game(0))
        self.start_time = 1
        watcher._finish_overlay()
        self.assertFalse(overlay.path.exists())
        self.assertFalse(overlay.resting_path.exists())

    def test_a_banner_on_screen_when_mangohud_blacklists_the_player_stays_as_it_is(self):
        from roseblox.backend.game_overlay import BANNER_SECONDS

        self.settings = WhilePlaying(server_location=True, server_location_in_game=True)
        roblox_log = self._roblox_log()
        watcher, overlay = self._overlay_watcher(resting="", find_log=lambda *_args: roblox_log)
        self._roblox_log(JOIN, PUBLIC, CONNECTED)
        watcher.poll()
        banner = overlay.path.read_text()
        self.assertIn("time_format=Server: Frankfurt am Main, Hessen, Germany\n", banner)
        self._write("process 'rosemary' is blacklisted in MangoHud")
        with redirect_stderr(io.StringIO()) as stderr:
            watcher.poll()
            self.now += BANNER_SECONDS
            watcher.poll()
        self.assertEqual(banner, overlay.path.read_text())
        self.assertNotIn("unexpected error", stderr.getvalue())

    def test_overlay_path_is_passed_to_the_watcher(self):
        from roseblox.activity_service import main, watcher_arguments

        arguments = watcher_arguments(1, 2, Path("/tmp/l"), Path("/tmp/r"), overlay=Path("/run/o.conf"))
        self.assertEqual(["/tmp/l", "/tmp/r", "--overlay", "/run/o.conf"], arguments[-4:])
        with patch("roseblox.activity_service.ActivityWatcher") as watcher, patch("signal.signal"):
            main(["roseblox", "--activity-watch", "1", "2", "/tmp/l", "--overlay", "/run/o.conf"])
        self.assertEqual(Path("/run/o.conf"), watcher.call_args.kwargs["overlay"].path)
        self.assertIsNone(watcher.call_args.args[3])
        with patch("sys.stderr"):
            self.assertEqual(2, main(["roseblox", "--activity-watch", "1", "2", "/tmp/l", "--overlay"]))

    def test_returning_home_closes_roblox_only_when_asked(self):
        watcher = self._watcher()
        self._write(JOIN, CONNECTED, DISCONNECT)
        watcher.poll()
        self.assertEqual([], self.kills)
        self._write(HOME)
        watcher.poll()
        self.assertEqual([(4242, signal.SIGTERM)], self.kills)
        self.assertTrue(FakePresence.instances[0].closed)
        self.settings = WhilePlaying(close_on_leave=False)
        self._write(JOIN, CONNECTED, HOME)
        watcher.poll()
        self.assertEqual(1, len(self.kills))

    def test_the_microphone_toggle_follows_its_setting_and_stops_with_roblox(self):
        created = []

        class FakeMicrophone:
            def __init__(self, mode, delay, inverted, sounds):
                self.settings = [(mode, delay, inverted)]
                self.sounds = [sounds]
                self.started = self.stopped = False
                created.append(self)

            def start(self):
                self.started = True

            def configure(self, mode, delay, inverted, sounds):
                self.settings.append((mode, delay, inverted))
                self.sounds.append(sounds)

            def stop(self):
                self.stopped = True

        self.settings = WhilePlaying()
        watcher = self._watcher(microphone_factory=FakeMicrophone)
        watcher.poll()
        self.assertEqual([], created)
        self.settings = WhilePlaying(microphone_toggle=True)
        watcher.poll()
        watcher.poll()
        [first] = created
        self.assertTrue(first.started)
        self.assertEqual([("toggle", 250, False)], first.settings)
        self.settings = WhilePlaying(
            microphone_toggle=True, microphone_mode="hold", microphone_release_delay_ms=500, microphone_inverted=True,
        )
        watcher.poll()
        self.assertEqual([("toggle", 250, False), ("hold", 500, True)], first.settings)
        self.settings = WhilePlaying(
            microphone_toggle=True, microphone_mode="hold", microphone_release_delay_ms=500, microphone_inverted=True,
            microphone_sounds=True, microphone_sound_volume=30,
        )
        watcher.poll()
        self.assertEqual(MicrophoneSounds(True, 30), first.sounds[-1])
        self.settings = WhilePlaying()
        watcher.poll()
        self.assertTrue(first.stopped)
        self.assertIsNone(watcher.microphone)

        self.settings = WhilePlaying(microphone_toggle=True)
        watcher.poll()
        second = created[-1]
        self.assertTrue(second.started)
        self.start_time = 999
        self.assertEqual(0, watcher.run())
        self.assertTrue(second.stopped)
        self.assertEqual(2, len(created))

    def test_a_reused_pid_is_never_signaled(self):
        self.start_time = 999
        watcher = self._watcher()
        self._write(JOIN, CONNECTED, HOME)
        watcher.poll()
        self.assertEqual([], self.kills)
        self.assertEqual(0, watcher.run())

    def test_presence_uses_roseblox_application_unless_another_is_set(self):
        self.settings = WhilePlaying(discord_presence=True)
        watcher = self._watcher()
        self._write(JOIN, CONNECTED)
        watcher.poll()
        self.assertEqual("1568683661321052170", FakePresence.instances[0].application_id)
        self.settings = WhilePlaying(discord_presence=False)
        self._write(DISCONNECT, JOIN, CONNECTED)
        watcher.poll()
        self.assertEqual(1, len(FakePresence.instances))

    def test_a_discord_failure_is_retried_later(self):
        attempts = []

        def failing(application_id):
            attempts.append(application_id)
            raise DiscordError("Discord is not running.")

        now = [1000.0]
        watcher = self._watcher(presence_factory=failing, clock=lambda: now[0])
        self._write(JOIN, CONNECTED)
        watcher.poll()
        watcher.poll()
        self.assertEqual(1, len(attempts))
        now[0] += 31
        watcher.poll()
        self.assertEqual(2, len(attempts))

    def test_the_watcher_runs_as_a_roseblox_subcommand(self):
        arguments = watcher_arguments(4242, 777, Path("/tmp/player.log"))
        self.assertEqual(["-m", "roseblox", "--activity-watch", "4242", "777", "/tmp/player.log"], arguments[1:])
        arguments = watcher_arguments(4242, 777, Path("/tmp/player.log"), Path("/p/Logs/Roblox"))
        self.assertEqual("/p/Logs/Roblox", arguments[-1])

    def test_the_roblox_log_is_followed_once_found_instead_of_the_launch_log(self):
        roblox_log = Path(self.directory.name) / "0.700_Player_ab12_last.log"
        roblox_log.write_text("")
        searches = []
        now = [1000.0]

        def find(pid, directory, since):
            searches.append((pid, directory, since))
            return roblox_log if len(searches) > 1 else None

        watcher = self._watcher(find_log=find, roblox_logs=Path("/logs"), clock=lambda: now[0])
        watcher.poll()
        watcher.poll()
        self.assertEqual([(4242, Path("/logs"), 995.0)], searches)
        now[0] += 6
        self._write(JOIN, CONNECTED)
        with roblox_log.open("a") as stream:
            stream.write("\n".join((JOIN, CONNECTED)) + "\n")
        watcher.poll()
        self.assertEqual(2, len(searches))
        self.assertEqual(1, len(FakePresence.instances[0].activities))
        watcher.poll()
        self.assertEqual(2, len(searches))

    def test_logs_are_looked_for_from_when_the_player_process_started(self):
        searches = []
        watcher = self._watcher(
            find_log=lambda pid, directory, since: searches.append(since),
            roblox_logs=Path("/logs"),
            started_at=lambda start_time: 990.0 + start_time / 100,
        )
        watcher.poll()
        self.assertEqual([995.77], [round(since, 2) for since in searches])

    def test_a_log_picked_by_its_age_gives_way_to_the_one_the_player_has_open(self):
        # A Player that quit just before this one started had written to its log moments earlier, and this
        # Player had not opened its own yet when the watcher first looked.
        directory = Path(self.directory.name)
        previous = directory / "previous_Player_last.log"
        previous.write_text("\n".join((JOIN, PUBLIC, CONNECTED)) + "\n")
        own = directory / "own_Player_last.log"
        own.write_text("")
        held = directory / "fd-7"
        held.symlink_to(own)
        searches = []
        now = [1000.0]

        def find(pid, logs, since):
            searches.append(logs)
            if logs is not None:
                return previous
            return held if len(searches) > 2 else None

        watcher = self._watcher(find_log=find, roblox_logs=Path("/logs"), clock=lambda: now[0])
        with redirect_stderr(io.StringIO()) as errors:
            watcher.poll()
            now[0] += 6
            watcher.poll()
            with own.open("a") as stream:
                stream.write("\n".join((JOIN.replace("2753915549", "606849621"), PUBLIC, CONNECTED)) + "\n")
            now[0] += 6
            watcher.poll()
            now[0] += 6
            watcher.poll()
        self.assertEqual([Path("/logs"), None, None], searches)
        self.assertIn(f"switching to Roblox's log {own}, which the Player has open", errors.getvalue())
        self.assertIn("left the game", errors.getvalue())
        self.assertEqual([2753915549, 606849621], self.lookups)

    def test_a_roblox_log_that_is_not_found_is_reported_once(self):
        now = [1000.0]
        watcher = self._watcher(find_log=lambda *_: None, roblox_logs=Path("/logs"), clock=lambda: now[0])
        with redirect_stderr(io.StringIO()) as errors:
            watcher.poll()
            now[0] += 55
            watcher.poll()
            self.assertEqual("", errors.getvalue())
            for _ in range(3):
                now[0] += 6
                watcher.poll()
        self.assertEqual(1, errors.getvalue().count("no Roblox log found yet"))
        self.assertIn("among the Player's open files or in /logs;", errors.getvalue())

    def test_an_unexpected_error_in_one_join_does_not_lose_the_next(self):
        located = []

        def locate(address):
            located.append(address)
            if len(located) == 1:
                raise ValueError("unexpected reply")
            return "Frankfurt am Main, Hessen, Germany"

        watcher = self._watcher(locate=locate)
        self._write(JOIN, PUBLIC, CONNECTED, DISCONNECT, JOIN, PUBLIC, CONNECTED)
        with redirect_stderr(io.StringIO()) as errors:
            watcher.poll()
        self.assertEqual(2, len(located))
        self.assertIn("unexpected error, still watching", errors.getvalue())
        self.assertIn("ValueError: unexpected reply", errors.getvalue())
        self.assertEqual(
            [("Connected to a Roblox server", "The server is in Frankfurt am Main, Hessen, Germany.")],
            self.notifications,
        )
        self.assertEqual("Blox Fruits", FakePresence.instances[-1].activities[-1][1]["details"])

    def test_a_failing_poll_is_logged_once_and_the_watcher_keeps_running(self):
        alive = iter([True, True, True])
        watcher = self._watcher(
            process_snapshot=lambda pid: SimpleNamespace(start_time=777) if next(alive, False) else None
        )
        polls = []

        def poll():
            polls.append(len(polls))
            if len(polls) < 3:
                raise RuntimeError("no such file")

        watcher.poll = poll
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(0, watcher.run())
        self.assertEqual(4, len(polls))
        self.assertEqual(1, errors.getvalue().count("unexpected error, still watching"))
        self.assertIn("Roblox Player process 4242 has ended; stopped watching", errors.getvalue())

    def test_log_lines_carry_the_local_time(self):
        with redirect_stderr(io.StringIO()) as errors:
            _log("joined place 1")
        self.assertRegex(
            errors.getvalue(), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d roseblox activity: joined place 1\n$"
        )


class FindRobloxLogTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.proc = self.root / "proc"

    def tearDown(self):
        self.directory.cleanup()

    def _fd(self, pid, number, target):
        fd = self.proc / str(pid) / "fd"
        fd.mkdir(parents=True, exist_ok=True)
        (fd / str(number)).symlink_to(target)

    def test_the_log_a_player_has_open_is_found_through_its_process_tree(self):
        self._fd(10, 1, "/home/me/.local/state/roseblox/logs/player.log")
        self._fd(11, 2, "/Users/me/Library/Logs/Roblox/0.746_20261107T204438Z_Player_bc545_CrashHandler_last.log")
        self._fd(11, 3, "/Users/me/Library/Logs/Roblox/old_Player_last.log (deleted)")
        self._fd(11, 7, "/Users/me/Library/Logs/Roblox/0.700_20261108T040000Z_Player_AB12_last.log")
        self._fd(12, 4, "/Users/me/Library/Logs/Roblox/other_Player_last.log")
        found = find_roblox_log(10, None, 0, proc=self.proc, tree=lambda pid, proc: [10, 11])
        self.assertEqual(self.proc / "11" / "fd" / "7", found)

    def test_without_an_open_log_the_newest_player_log_since_launch_is_used(self):
        logs = self.root / "Logs" / "Roblox"
        logs.mkdir(parents=True)
        for name, modified in (("a_Player_old.log", 100), ("b_Player_new.log", 300), ("c_Studio_new.log", 400),
                               ("d_Player_mid.log", 250), ("e_Player_bc545_CrashHandler_last.log", 500)):
            (logs / name).write_text("")
            os.utime(logs / name, (modified, modified))
        tree = lambda pid, proc: [10]
        self.assertEqual(logs / "b_Player_new.log", find_roblox_log(10, logs, 200, proc=self.proc, tree=tree))
        self.assertIsNone(find_roblox_log(10, logs, 350, proc=self.proc, tree=tree))
        self.assertIsNone(find_roblox_log(10, self.root / "missing", 0, proc=self.proc, tree=tree))

    def test_a_log_roblox_started_before_the_launch_is_not_taken_for_this_players(self):
        logs = self.root / "Logs" / "Roblox"
        logs.mkdir(parents=True)
        launched = datetime(2026, 11, 11, 9, 37, 34, tzinfo=timezone.utc).timestamp()
        quit_just_before = logs / "0.747.0.7471481_20261111T093730Z_Player_d4f06_last.log"
        quit_just_before.write_text("")
        os.utime(quit_just_before, (launched + 3, launched + 3))
        tree = lambda pid, proc: [10]
        self.assertIsNone(find_roblox_log(10, logs, launched, proc=self.proc, tree=tree))
        own = logs / "0.747.0.7471481_20261111T093736Z_Player_9f731_last.log"
        own.write_text("")
        os.utime(own, (launched + 2, launched + 2))
        self.assertEqual(own, find_roblox_log(10, logs, launched, proc=self.proc, tree=tree))


if __name__ == "__main__":
    unittest.main()
