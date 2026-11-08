import io
import json
import shutil
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

from roseblox.backend.microphone import (
    MODE_HOLD,
    MODE_TOGGLE,
    NO_MUTE_TOOLS,
    NO_SOUND_SERVER,
    CaptureStream,
    MicrophoneError,
    MicrophoneGate,
    PipeWireBackend,
    PulseBackend,
    detect_backend,
    is_roblox_stream,
    microphone_problem,
    parse_pw_dump,
    parse_source_outputs,
)
from roseblox.backend.microphone_sounds import sound_problem

SOURCE_OUTPUTS = """Source Output #8733
\tDriver: PipeWire
\tMute: no
\tProperties:
\t\tapplication.name = "WEBRTC VoiceEngine"
\t\tapplication.process.binary = "Discord"
\t\tmedia.name = "recStream"
\t\tmedia.class = "Stream/Input/Audio"

Source Output #32976
\tDriver: PipeWire
\tOwner Module: n/a
\tClient: 32975
\tSource: 188
\tCorked: no
\tMute: no
\tVolume: mono: 65536 / 100% / 0.00 dB
\t        balance 0.00
\tProperties:
\t\tmedia.type = "Audio"
\t\tmedia.category = "Capture"
\t\tmedia.role = "game"
\t\tapplication.name = "Roblox"
\t\tapplication.id = "com.roblox.RobloxPlayer"
\t\tnode.name = "Roblox"
\t\tmedia.class = "Stream/Input/Audio"
\t\tobject.serial = "32976"

Source Output #40000
\tMute: yes
\tProperties:
\t\tapplication.name = "Roblox"
\t\tapplication.id = "com.roblox.RobloxStudio"
"""


class FakeBackend:
    def __init__(self, streams):
        self.list = list(streams)
        self.calls = []
        self.fail_listing = False

    def streams(self):
        if self.fail_listing:
            raise MicrophoneError("pactl: Connection refused")
        return list(self.list)

    def set_muted(self, stream, muted):
        self.calls.append((stream.index, muted))
        self.list = [CaptureStream(item.index, muted, item.properties) if item.index == stream.index else item
                     for item in self.list]


ROBLOX = {"application.id": "com.roblox.RobloxPlayer", "application.name": "Roblox", "media.class": "Stream/Input/Audio"}
DISCORD = {"application.name": "WEBRTC VoiceEngine", "media.class": "Stream/Input/Audio"}


class Timers:
    def __init__(self):
        self.pending = {}
        self.next = 0

    def schedule(self, milliseconds, callback):
        self.next += 1
        self.pending[self.next] = (milliseconds, callback)
        return self.next

    def cancel(self, handle):
        self.pending.pop(handle, None)

    def fire_all(self):
        for handle in list(self.pending):
            _delay, callback = self.pending.pop(handle)
            callback()


def gate(backend, mode=MODE_TOGGLE, delay=250, timers=None):
    timers = timers or Timers()
    return MicrophoneGate(backend, mode, delay, schedule=timers.schedule, cancel=timers.cancel), timers


class ParsingTests(unittest.TestCase):
    def test_pactl_output_is_parsed_and_only_roblox_player_matches(self):
        streams = parse_source_outputs(SOURCE_OUTPUTS)
        self.assertEqual(["8733", "32976", "40000"], [stream.index for stream in streams])
        self.assertEqual([False, False, True], [stream.muted for stream in streams])
        self.assertEqual("com.roblox.RobloxPlayer", streams[1].properties["application.id"])
        self.assertEqual([False, True, False], [is_roblox_stream(stream.properties) for stream in streams])

    def test_a_stream_named_roblox_without_an_application_id_matches(self):
        self.assertTrue(is_roblox_stream({"application.name": "Roblox"}))
        self.assertFalse(is_roblox_stream({"application.name": "Roblox", "media.class": "Stream/Output/Audio"}))
        self.assertFalse(is_roblox_stream({"application.name": "Discord"}))

    def test_pw_dump_capture_nodes_are_read_with_their_mute(self):
        document = [
            {"id": 264, "type": "PipeWire:Interface:Node", "info": {
                "props": {**ROBLOX, "object.serial": 32976},
                "params": {"Props": [{"volume": 1.0}, {"mute": True}]},
            }},
            {"id": 319, "type": "PipeWire:Interface:Node", "info": {
                "props": {"application.name": "Roblox", "media.class": "Stream/Output/Audio"},
            }},
            {"id": 5, "type": "PipeWire:Interface:Client", "info": {"props": {}}},
        ]
        streams = parse_pw_dump(json.dumps(document))
        self.assertEqual([CaptureStream("264", True, {**ROBLOX, "object.serial": "32976"})], streams)
        with self.assertRaises(MicrophoneError):
            parse_pw_dump("not json")

    def test_backends_run_the_expected_commands(self):
        calls = []

        def runner(arguments):
            calls.append(arguments)
            return "[]" if arguments[0] == "pw-dump" else ""

        stream = CaptureStream("7", False)
        PulseBackend(runner).set_muted(stream, True)
        PipeWireBackend(runner).set_muted(stream, False)
        PulseBackend(runner).streams()
        PipeWireBackend(runner).streams()
        self.assertEqual(
            [
                ["pactl", "set-source-output-mute", "7", "1"],
                ["wpctl", "set-mute", "7", "0"],
                ["pactl", "list", "source-outputs"],
                ["pw-dump"],
            ],
            calls,
        )
        self.assertTrue(PulseBackend.relevant("Event 'new' on source-output #33475"))
        self.assertFalse(PulseBackend.relevant("Event 'change' on sink #56"))

    def test_pactl_is_preferred_and_pipewire_tools_are_the_fallback(self):
        self.assertIsInstance(detect_backend(lambda name: f"/usr/bin/{name}"), PulseBackend)
        self.assertIsInstance(
            detect_backend(lambda name: None if name == "pactl" else f"/usr/bin/{name}"), PipeWireBackend
        )
        self.assertIsNone(detect_backend(lambda name: None))

    def test_problems_name_a_missing_sound_server_or_missing_tools(self):
        import tempfile
        from pathlib import Path

        everything = lambda name: f"/usr/bin/{name}"  # noqa: E731
        with tempfile.TemporaryDirectory() as runtime:
            environ = {"XDG_RUNTIME_DIR": runtime}
            self.assertEqual(NO_SOUND_SERVER, microphone_problem(everything, environ))
            self.assertEqual(NO_SOUND_SERVER, microphone_problem(everything, {}))
            (Path(runtime) / "pipewire-0").touch()
            self.assertIsNone(microphone_problem(everything, environ))
            self.assertEqual(NO_MUTE_TOOLS, microphone_problem(lambda name: None, environ))
            self.assertIsNone(microphone_problem(lambda name: None if name == "pactl" else name, environ))
            self.assertEqual(NO_MUTE_TOOLS, microphone_problem(lambda name: name if name == "wpctl" else None, environ))
        self.assertIsNone(microphone_problem(everything, {"PULSE_SERVER": "tcp:host"}))
        self.assertIsNone(sound_problem(lambda name: name if name == "paplay" else None))
        self.assertIn("pw-play", sound_problem(lambda name: None))


class GateTests(unittest.TestCase):
    def test_roblox_starts_muted_and_other_apps_are_never_touched(self):
        backend = FakeBackend([CaptureStream("1", False, DISCORD), CaptureStream("2", False, ROBLOX)])
        microphone, _timers = gate(backend)
        microphone.sync()
        self.assertEqual([("2", True)], backend.calls)
        microphone.sync()
        self.assertEqual([("2", True)], backend.calls)

    def test_toggle_mode_flips_on_each_press_and_ignores_release(self):
        backend = FakeBackend([CaptureStream("2", True, ROBLOX)])
        microphone, _timers = gate(backend)
        microphone.press()
        microphone.release()
        self.assertTrue(microphone.open)
        self.assertEqual([("2", False)], backend.calls)
        microphone.press()
        self.assertFalse(microphone.open)
        self.assertEqual([("2", False), ("2", True)], backend.calls)

    def test_hold_mode_mutes_after_the_release_delay(self):
        backend = FakeBackend([CaptureStream("2", True, ROBLOX)])
        microphone, timers = gate(backend, MODE_HOLD, 300)
        microphone.press()
        self.assertTrue(microphone.open)
        microphone.release()
        self.assertTrue(microphone.open)
        self.assertEqual([300], [delay for delay, _callback in timers.pending.values()])
        timers.fire_all()
        self.assertFalse(microphone.open)
        self.assertEqual([("2", False), ("2", True)], backend.calls)

    def test_pressing_again_during_the_release_delay_keeps_the_microphone_open(self):
        backend = FakeBackend([CaptureStream("2", True, ROBLOX)])
        microphone, timers = gate(backend, MODE_HOLD, 300)
        microphone.press()
        microphone.release()
        microphone.press()
        self.assertEqual({}, timers.pending)
        self.assertTrue(microphone.open)

    def test_hold_mode_without_a_delay_mutes_on_release(self):
        backend = FakeBackend([CaptureStream("2", True, ROBLOX)])
        microphone, timers = gate(backend, MODE_HOLD, 0)
        microphone.press()
        microphone.release()
        self.assertFalse(microphone.open)
        self.assertEqual({}, timers.pending)

    def test_inverted_hold_mutes_while_held_and_reopens_after_the_delay(self):
        backend = FakeBackend([CaptureStream("2", True, ROBLOX)])
        timers = Timers()
        microphone = MicrophoneGate(
            backend, MODE_HOLD, 200, True, schedule=timers.schedule, cancel=timers.cancel,
        )
        self.assertTrue(microphone.open)
        microphone.sync()
        self.assertEqual([("2", False)], backend.calls)
        microphone.press()
        self.assertFalse(microphone.open)
        microphone.release()
        self.assertFalse(microphone.open)
        timers.fire_all()
        self.assertTrue(microphone.open)
        self.assertEqual([("2", False), ("2", True), ("2", False)], backend.calls)

    def test_switching_inverted_returns_to_the_new_resting_state(self):
        backend = FakeBackend([CaptureStream("2", True, ROBLOX)])
        microphone, _timers = gate(backend, MODE_HOLD, 0)
        microphone.configure(MODE_HOLD, 0, True)
        self.assertTrue(microphone.open)
        microphone.configure(MODE_TOGGLE, 0, True)
        self.assertFalse(microphone.open)
        microphone.press()
        self.assertTrue(microphone.open)
        with self.assertRaises(ValueError):
            microphone.configure(MODE_HOLD, 0, "yes")

    def test_a_replacement_stream_gets_the_current_state(self):
        backend = FakeBackend([CaptureStream("2", True, ROBLOX)])
        microphone, _timers = gate(backend)
        microphone.toggle()
        backend.list = [CaptureStream("3", True, ROBLOX)]
        microphone.sync()
        self.assertEqual([("2", False), ("3", False)], backend.calls)

    def test_stopping_opens_every_roblox_stream(self):
        backend = FakeBackend([CaptureStream("2", True, ROBLOX), CaptureStream("1", True, DISCORD)])
        microphone, timers = gate(backend, MODE_HOLD, 300)
        microphone.press()
        microphone.release()
        backend.list = [CaptureStream("3", True, ROBLOX), CaptureStream("1", True, DISCORD)]
        microphone.stop()
        self.assertEqual({}, timers.pending)
        self.assertEqual([("2", False), ("3", False)], backend.calls)
        self.assertFalse(microphone.active)

    def test_changing_mode_mutes_and_validates(self):
        backend = FakeBackend([CaptureStream("2", True, ROBLOX)])
        microphone, _timers = gate(backend)
        microphone.toggle()
        microphone.configure(MODE_HOLD, 100)
        self.assertFalse(microphone.open)
        self.assertEqual(100, microphone.release_delay_ms)
        with self.assertRaises(ValueError):
            microphone.configure("sometimes", 100)
        with self.assertRaises(ValueError):
            microphone.configure(MODE_HOLD, -1)

    def test_sound_server_errors_are_logged_not_raised(self):
        messages = []
        backend = FakeBackend([CaptureStream("2", False, ROBLOX)])
        backend.fail_listing = True
        microphone = MicrophoneGate(backend, schedule=lambda *_: None, cancel=lambda _: None, log=messages.append)
        microphone.sync()
        self.assertEqual(["pactl: Connection refused"], messages)


class ServiceBackend(FakeBackend):
    name = "fake"
    monitor_command = ["sh", "-c", "read line; echo \"Event 'new' on source-output #$line\"; sleep 60"]

    @staticmethod
    def relevant(line):
        return "source-output" in line


class FailingShortcuts:
    def __init__(self, _connection, **_handlers):
        self.closed = False

    def start(self, _shortcuts, done):
        done(None, "This desktop has no global shortcut service.")

    def close(self):
        self.closed = True


def _wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


@unittest.skipUnless(shutil.which("dbus-daemon"), "needs dbus-daemon")
class ServiceTests(unittest.TestCase):
    def setUp(self):
        from gi.repository import Gio

        self.bus = Gio.TestDBus.new(Gio.TestDBusFlags.NONE)
        self.bus.up()
        self.addCleanup(self.bus.down)
        self.messages = []
        self.connection = self._connect()
        self.addCleanup(self.connection.close_sync, None)

    def _connect(self):
        from gi.repository import Gio

        return Gio.DBusConnection.new_for_address_sync(
            self.bus.get_bus_address(),
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None,
            None,
        )

    def _service(self, backend, mode=MODE_HOLD, delay=100, sounds=None, player=None):
        from roseblox.microphone_service import MicrophoneService

        service = MicrophoneService(
            mode, delay, False, sounds, log=self.messages.append, backend_factory=lambda: backend,
            shortcuts_factory=FailingShortcuts, connection_factory=self._connect, player=player,
        )
        service.start()
        self.addCleanup(service.stop)
        return service

    def _command(self, *arguments):
        from roseblox.microphone_service import command_main

        error = io.StringIO()
        output = io.StringIO()
        with redirect_stderr(error), redirect_stdout(output):
            status = command_main(["roseblox", "--mic", *arguments], self.connection)
        return status, output.getvalue().strip(), error.getvalue().strip()

    def test_the_command_explains_why_the_toggle_cannot_run(self):
        from unittest.mock import patch

        from roseblox.microphone_service import NOT_RUNNING

        with patch("roseblox.microphone_service.microphone_problem", return_value="No sound server."):
            status, output, error = self._command("status")
        self.assertEqual((1, "", f"{NOT_RUNNING}\nNo sound server."), (status, output, error))
        with patch("roseblox.microphone_service.microphone_problem", return_value=None):
            self.assertEqual(NOT_RUNNING, self._command("toggle")[2])

    def test_sounds_play_when_the_microphone_opens_and_mutes(self):
        from roseblox.backend.microphone_sounds import MicrophoneSounds

        played = []

        class FakePlayer:
            def play(self, sounds, kind):
                played.append((sounds.enabled, kind))

        backend = ServiceBackend([CaptureStream("2", False, ROBLOX)])
        service = self._service(backend, MODE_TOGGLE, sounds=MicrophoneSounds(True, 50), player=FakePlayer())
        self.assertTrue(_wait_for(lambda: backend.calls == [("2", True)]))
        self.assertEqual([], played)
        self.assertEqual(0, self._command("toggle")[0])
        self.assertTrue(_wait_for(lambda: played == [(True, "on")]))
        service.configure(MODE_TOGGLE, 100, False, MicrophoneSounds(False, 50))
        self.assertEqual(0, self._command("toggle")[0])
        self.assertTrue(_wait_for(lambda: played == [(True, "on"), (False, "off")]))

    def test_one_gate_is_in_charge_and_the_command_drives_it(self):
        first_backend = ServiceBackend([CaptureStream("2", False, ROBLOX)])
        first = self._service(first_backend)
        self.assertTrue(_wait_for(lambda: first_backend.calls == [("2", True)]))
        self.assertTrue(any("roseblox --mic toggle" in message for message in self.messages))

        second_backend = ServiceBackend([CaptureStream("2", True, ROBLOX)])
        second = self._service(second_backend, MODE_TOGGLE)
        self.assertIsNone(second.gate)

        self.assertEqual((0, "muted (hold mode, hotkey: none)", ""), self._command("status"))
        self.assertEqual(0, self._command("press")[0])
        self.assertTrue(_wait_for(lambda: first_backend.calls[-1] == ("2", False)))
        self.assertEqual(0, self._command("release")[0])
        self.assertTrue(_wait_for(lambda: first_backend.calls[-1] == ("2", True)))

        first.stop()
        self.assertFalse(first.thread.is_alive())
        self.assertEqual(("2", False), first_backend.calls[-1])
        self.assertTrue(_wait_for(lambda: second.gate is not None))
        self.assertEqual((0, "muted (toggle mode, hotkey: none)", ""), self._command("status"))
        self.assertEqual(0, self._command("on")[0])
        self.assertTrue(_wait_for(lambda: second.gate.open))
        second.configure(MODE_HOLD, 0, True)
        self.assertTrue(_wait_for(lambda: second.gate.inverted))
        self.assertEqual((0, "open (hold to mute mode, hotkey: none)", ""), self._command("status"))

    def test_the_command_explains_when_nothing_is_running_and_rejects_unknown_actions(self):
        status, _output, error = self._command("toggle")
        self.assertEqual(1, status)
        self.assertIn("Microphone toggle turned on", error)
        self.assertEqual(2, self._command("loud")[0])


if __name__ == "__main__":
    unittest.main()
