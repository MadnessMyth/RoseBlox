import io
import json
import os
import signal
import socket
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from roseblox.activity_service import ActivityWatcher, watcher_arguments
from roseblox.backend.activity import ActivityTracker, GameJoined, GameLeft, LogFollower, find_roblox_log, public_address
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
            [GameJoined(1234567890, "11111111-2222-4333-8444-555555555555", "128.116.0.10", False), GameLeft(True)],
            events,
        )

    def test_a_join_is_reported_once_connected_with_the_public_address(self):
        tracker = ActivityTracker()
        self.assertEqual([], feed(tracker, JOIN, PUBLIC))
        [joined] = feed(tracker, CONNECTED)
        self.assertEqual(GameJoined(2753915549, JOB, "128.116.44.3", False), joined)
        self.assertEqual(
            f"https://www.roblox.com/games/start?placeId=2753915549&gameInstanceId={JOB}", joined.join_url
        )
        self.assertEqual("https://www.roblox.com/games/2753915549", joined.game_url)
        self.assertTrue(tracker.in_game)

    def test_private_servers_have_no_join_link(self):
        [joined] = feed(ActivityTracker(), JOIN, PRIVATE, CONNECTED)
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

    def _overlay_watcher(self, **overrides):
        from roseblox.backend.game_overlay import HIDDEN, GameOverlay

        overlay = GameOverlay.create(Path(self.directory.name) / "overlay", HIDDEN)
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
        self.assertIn("custom_text_center=Server: Frankfurt am Main, Hessen, Germany\n", overlay.path.read_text())
        self.now += BANNER_SECONDS - 1
        watcher.poll()
        self.assertIn("custom_text_center=", overlay.path.read_text())
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
        self.assertIn("custom_text_center=Server: Frankfurt am Main, Hessen, Germany · 24 ms\n", overlay.path.read_text())
        self.now += BANNER_SECONDS
        watcher.poll()
        self.assertIn("text_color=FDF909\ncustom_text=PING\nexec=echo 24 ms\n", overlay.path.read_text())
        self.now += 20
        watcher.poll()
        self.assertIn("text_color=FDF909\ncustom_text=PING\nexec=echo 31 ms\n", overlay.path.read_text())
        self.now += 30
        watcher.poll()
        self.assertIn("text_color=FDF909\ncustom_text=PING\nexec=echo 19 ms\n", overlay.path.read_text())
        self._write(DISCONNECT)
        watcher.poll()
        self.assertEqual(HIDDEN, overlay.path.read_text())

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


if __name__ == "__main__":
    unittest.main()
