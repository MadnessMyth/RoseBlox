import shutil
import time
import unittest


def _wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


@unittest.skipUnless(shutil.which("dbus-daemon"), "needs dbus-daemon")
class PingPlayerTests(unittest.TestCase):
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

    def _player(self, **options):
        from roseblox.backend.ping_player import PingPlayer

        player = PingPlayer(log=self.messages.append, connection_factory=self._connect, **options)
        self.addCleanup(player.stop)
        return player

    def _names(self):
        from gi.repository import GLib

        reply = self.connection.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "ListNames",
            None, GLib.VariantType.new("(as)"), 0, 3000, None,
        )
        return [name for name in reply.unpack()[0] if name.startswith("org.mpris.")]

    def _properties(self, player, method, *arguments):
        from gi.repository import GLib

        signature = "(" + "s" * len(arguments) + ")"
        return self.connection.call_sync(
            player.bus_name, "/org/mpris/MediaPlayer2", "org.freedesktop.DBus.Properties", method,
            GLib.Variant(signature, arguments), None, 0, 3000, None,
        ).unpack()[0]

    def test_the_ping_is_the_title_of_a_player_that_mangohud_can_ask_for_by_name(self):
        import os

        player = self._player()
        self.assertEqual(f"roseblox.instance{os.getpid()}", player.name)
        self.assertEqual([], self._names())
        self.assertTrue(player.show("PING 24 ms"))
        self.assertEqual([f"org.mpris.MediaPlayer2.{player.name}"], self._names())
        # What MangoHud reads when it loads the row.
        self.assertEqual(0, player.reads)
        metadata = self._properties(player, "Get", "org.mpris.MediaPlayer2.Player", "Metadata")
        self.assertEqual("PING 24 ms", metadata["xesam:title"])
        self.assertEqual("Playing", self._properties(player, "Get", "org.mpris.MediaPlayer2.Player", "PlaybackStatus"))
        self.assertEqual(1, player.reads)

    def test_there_is_nothing_for_a_desktop_to_list_or_control(self):
        from gi.repository import GLib

        player = self._player()
        self.assertTrue(player.show("PING 24 ms"))
        everything = self._properties(player, "GetAll", "org.mpris.MediaPlayer2.Player")
        for name in ("CanControl", "CanPlay", "CanPause", "CanSeek", "CanGoNext", "CanGoPrevious"):
            self.assertIs(False, everything[name], name)
        # KDE Plasma drops a player whose name it cannot read from this interface.
        with self.assertRaises(GLib.Error):
            self._properties(player, "GetAll", "org.mpris.MediaPlayer2")
        with self.assertRaises(GLib.Error):
            self.connection.call_sync(
                player.bus_name, "/org/mpris/MediaPlayer2", "org.mpris.MediaPlayer2.Player", "PlayPause",
                None, None, 0, 3000, None,
            )

    def test_a_new_ping_reaches_mangohud_as_a_changed_title(self):
        from gi.repository import Gio, GLib

        player = self._player()
        self.assertTrue(player.show("PING 24 ms"))
        changes = []
        self.connection.signal_subscribe(
            None, "org.freedesktop.DBus.Properties", "PropertiesChanged", "/org/mpris/MediaPlayer2", None,
            Gio.DBusSignalFlags.NONE,
            lambda _connection, _sender, _path, _interface, _signal, parameters: changes.append(parameters.unpack()),
        )
        context = GLib.MainContext.default()

        def received(count):
            context.iteration(False)
            return len(changes) >= count

        self.assertTrue(player.show("PING 24 ms"))
        self.assertTrue(player.show("PING 31 ms"))
        self.assertTrue(_wait_for(lambda: received(1)))
        interface, changed, invalidated = changes[0]
        self.assertEqual(("org.mpris.MediaPlayer2.Player", []), (interface, invalidated))
        self.assertEqual("PING 31 ms", changed["Metadata"]["xesam:title"])
        # The same ping again is not a change.
        self.assertFalse(_wait_for(lambda: received(2), timeout=0.3))

    def test_it_leaves_the_bus_a_moment_after_the_row_is_gone(self):
        player = self._player(release_delay_ms=150)
        self.assertTrue(player.show("PING 24 ms"))
        player.hide()
        # Still there while MangoHud re-reads its config.
        self.assertEqual([player.bus_name], self._names())
        self.assertTrue(_wait_for(lambda: self._names() == []))
        self.assertTrue(player.show("PING 40 ms"))
        self.assertEqual([player.bus_name], self._names())

    def test_showing_again_keeps_it_on_the_bus(self):
        player = self._player(release_delay_ms=150)
        self.assertTrue(player.show("PING 24 ms"))
        player.hide()
        self.assertTrue(player.show("PING 25 ms"))
        time.sleep(0.4)
        self.assertEqual([player.bus_name], self._names())

    def test_stopping_takes_it_off_the_bus_at_once(self):
        player = self._player()
        self.assertTrue(player.show("PING 24 ms"))
        player.stop()
        self.assertFalse(player.thread.is_alive())
        self.assertEqual([], self._names())
        self.assertFalse(player.show("PING 24 ms"))

    def test_a_taken_name_means_no_player(self):
        first = self._player()
        second = self._player()
        self.assertTrue(first.show("PING 24 ms"))
        self.assertFalse(second.show("PING 99 ms"))
        self.assertIn(f"{second.bus_name} is taken", self.messages)

    def test_without_a_session_bus_there_is_no_player(self):
        from gi.repository import Gio, GLib

        from roseblox.backend.ping_player import PingPlayer

        def no_bus():
            raise GLib.Error.new_literal(Gio.io_error_quark(), "Could not connect", Gio.IOErrorEnum.FAILED)

        player = PingPlayer(log=self.messages.append, connection_factory=no_bus)
        self.assertFalse(player.show("PING 24 ms"))
        self.assertEqual(["no session bus: Could not connect"], self.messages)
        player.hide()
        player.stop()
        self.assertFalse(player.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
