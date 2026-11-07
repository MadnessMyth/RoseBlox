import io
import json
import signal
import socket
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from roseblox.activity_service import ActivityWatcher, watcher_arguments
from roseblox.backend.activity import ActivityTracker, GameJoined, GameLeft, LogFollower, public_address
from roseblox.backend.discord_rpc import DiscordError, DiscordPresence, build_activity, socket_candidates
from roseblox.backend.game_info import GameDetails, GameInfoError, fetch_game, locate_server
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
        self.assertEqual("roseblox", activity["assets"]["large_image"])
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

    def test_a_reused_pid_is_never_signaled(self):
        self.start_time = 999
        watcher = self._watcher()
        self._write(JOIN, CONNECTED, HOME)
        watcher.poll()
        self.assertEqual([], self.kills)
        self.assertEqual(0, watcher.run())

    def test_presence_is_off_without_the_switch_or_an_id(self):
        self.settings = WhilePlaying(discord_presence=True, discord_application_id="")
        watcher = self._watcher()
        self._write(JOIN, CONNECTED)
        watcher.poll()
        self.assertEqual([], FakePresence.instances)

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


if __name__ == "__main__":
    unittest.main()
