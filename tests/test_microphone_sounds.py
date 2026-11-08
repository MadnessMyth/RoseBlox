import io
import tempfile
import unittest
import wave
from pathlib import Path

from roseblox.backend.microphone_sounds import (
    OFF,
    ON,
    MicrophoneSoundError,
    MicrophoneSounds,
    SoundPlayer,
    check_sound_file,
    default_sound,
    play_command,
    store_sound,
    tone,
)


class SoundTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.spawned = []
        self.messages = []

    def tearDown(self):
        self.temporary.cleanup()

    def player(self, available=("pw-play", "paplay")):
        return SoundPlayer(
            directory=self.root / "sounds",
            which=lambda name: f"/usr/bin/{name}" if name in available else None,
            spawn=self.spawned.append,
            log=self.messages.append,
        )

    def test_roseblox_sounds_are_short_wav_files_that_differ(self):
        for kind in (ON, OFF):
            with wave.open(io.BytesIO(tone(kind))) as sound:
                self.assertEqual((1, 2, 48000), (sound.getnchannels(), sound.getsampwidth(), sound.getframerate()))
                self.assertLess(sound.getnframes() / sound.getframerate(), 0.2)
        self.assertNotEqual(tone(ON), tone(OFF))
        path = default_sound(ON, self.root)
        self.assertEqual(".wav", check_sound_file(path))
        self.assertEqual(path, default_sound(ON, self.root))

    def test_only_playable_small_files_are_accepted(self):
        cases = {
            "a.wav": b"RIFF\x00\x00\x00\x00WAVEfmt ",
            "a.ogg": b"OggS\x00\x02" + b"\x00" * 10,
            "a.flac": b"fLaC\x00\x00\x00\x22" + b"\x00" * 8,
        }
        for name, data in cases.items():
            path = self.root / name
            path.write_bytes(data)
            self.assertEqual(Path(name).suffix, check_sound_file(path))
        bad = self.root / "song.mp3"
        bad.write_bytes(b"ID3\x04" + b"\x00" * 20)
        with self.assertRaises(MicrophoneSoundError):
            check_sound_file(bad)
        large = self.root / "large.wav"
        large.write_bytes(b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * (3 * 1024 * 1024))
        with self.assertRaises(MicrophoneSoundError):
            check_sound_file(large)
        with self.assertRaises(MicrophoneSoundError):
            check_sound_file(self.root / "missing.wav")

    def test_a_chosen_sound_is_copied_and_replaces_the_previous_one(self):
        ogg = self.root / "beep.ogg"
        ogg.write_bytes(b"OggS" + b"\x00" * 20)
        wav = self.root / "boop.wav"
        wav.write_bytes(b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 20)
        directory = self.root / "store"
        first = store_sound(ON, ogg, directory)
        self.assertEqual(directory / "microphone-on.ogg", first)
        second = store_sound(ON, wav, directory)
        self.assertEqual(directory / "microphone-on.wav", second)
        self.assertFalse(first.exists())
        ogg.unlink()
        self.assertEqual(wav.read_bytes(), second.read_bytes())
        self.assertEqual([second], sorted(directory.iterdir()))

    def test_play_command_prefers_pipewire(self):
        path = Path("/tmp/x.wav")
        self.assertEqual(["pw-play", "--volume=0.60", str(path)], play_command(path, 60, lambda n: n))
        self.assertEqual(
            ["paplay", "--volume=32768", str(path)], play_command(path, 50, lambda n: n if n == "paplay" else None)
        )
        self.assertIsNone(play_command(path, 50, lambda _n: None))

    def test_player_plays_only_when_enabled(self):
        player = self.player()
        self.assertFalse(player.play(MicrophoneSounds(), ON))
        self.assertFalse(player.play(MicrophoneSounds(True, 0), ON))
        self.assertTrue(player.play(MicrophoneSounds(True, 40), OFF))
        self.assertEqual("pw-play", self.spawned[-1][0])
        self.assertTrue(self.spawned[-1][-1].endswith("roseblox-microphone-off.wav"))
        self.assertTrue(player.play(MicrophoneSounds(False, 40), ON, force=True))

    def test_custom_sound_and_missing_file_fallback(self):
        custom = self.root / "click.wav"
        custom.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
        player = self.player()
        player.play(MicrophoneSounds(True, 50, on=str(custom)), ON)
        self.assertEqual(str(custom), self.spawned[-1][-1])
        player.play(MicrophoneSounds(True, 50, on=str(self.root / "gone.wav")), ON)
        self.assertTrue(self.spawned[-1][-1].endswith("roseblox-microphone-on.wav"))
        self.assertTrue(any("missing" in message for message in self.messages))

    def test_without_a_player_program_it_only_logs(self):
        player = self.player(available=())
        self.assertFalse(player.play(MicrophoneSounds(True), ON))
        self.assertEqual([], self.spawned)
        self.assertTrue(any("pw-play" in message for message in self.messages))


if __name__ == "__main__":
    unittest.main()
