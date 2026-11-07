import hashlib
import io
import json
import os
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from roseblox.backend import mods
from roseblox.backend.mods import (
    AVATAR_BACKGROUND,
    CURSORS,
    DEATH_SOUND,
    FAMILIES,
    ModsError,
    ModSettings,
    ModsStore,
    apply_mods,
    applied_files,
    check_relative,
    custom_font_files,
    desired_files,
    fetch_avatar_background,
    folder_files,
    prepare_launch,
)

TTF = b"\x00\x01\x00\x00" + b"font" * 10
OTF = b"OTTO" + b"font" * 10


def png(width=32, height=32):
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class ModsTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.bundle = self.root / "installations" / "player-version-1" / "RobloxPlayer.app"
        resources = self.bundle / "Contents" / "Resources" / "content"
        (resources / "fonts" / "families").mkdir(parents=True)
        (resources / "sounds").mkdir()
        (resources / "textures" / "Cursors" / "KeyboardMouse").mkdir(parents=True)
        (resources / "sounds" / "ouch.ogg").write_bytes(b"new death sound")
        (resources / "sounds" / "oof.ogg").write_bytes(b"classic oof")
        (resources / "textures" / "Cursors" / "KeyboardMouse" / "ArrowCursor.png").write_bytes(b"stock arrow")
        for name, path in (("BuilderSans", "BuilderSans-Regular.otf"), ("RobotoMono", "RobotoMono-Regular.ttf")):
            (resources / "fonts" / "families" / f"{name}.json").write_text(json.dumps({"name": name, "faces": [
                {"name": "Regular", "weight": 400, "style": "normal", "assetId": f"rbxasset://fonts/{path}"},
                {"name": "Bold", "weight": 700, "style": "normal", "assetId": "rbxassetid://123"},
            ]}))
        (self.bundle.parent.parent / "external").mkdir()

    def tearDown(self):
        self.directory.cleanup()

    def read(self, relative):
        return (self.bundle / relative).read_bytes()


class PathRuleTests(unittest.TestCase):
    def test_only_media_under_resources_can_be_replaced(self):
        for relative in (
            "Contents/Resources/content/textures/sky.png",
            "Contents/Resources/content/sounds/ouch.ogg",
            "Contents/Resources/content/fonts/families/Roboto.json",
            AVATAR_BACKGROUND,
        ):
            self.assertEqual(relative, check_relative(relative))
        for relative in (
            "Contents/MacOS/RobloxPlayer",
            "Contents/Resources/content/avatar/scripts/humanoidAnimateR15.lua",
            "Contents/Resources/content/models/thing.rbxm",
            "Contents/Resources/content/configs/settings.json",
            "Contents/Resources/../MacOS/x.png",
            "Contents/Frameworks/lib.dylib",
            "/etc/passwd.png",
        ):
            with self.assertRaises(ModsError, msg=relative):
                check_relative(relative)


class FolderTests(ModsTestCase):
    def test_folder_files_mirror_resources_and_explain_refusals(self):
        folder = self.root / "mods" / "player"
        (folder / "content" / "sounds").mkdir(parents=True)
        (folder / "content" / "sounds" / "ouch.ogg").write_bytes(b"x")
        (folder / "content" / "avatar").mkdir()
        (folder / "content" / "avatar" / "script.lua").write_text("print('hi')")
        os.symlink("/etc/hostname", folder / "content" / "sounds" / "link.ogg")
        found = {item.relative: item.problem for item in folder_files(folder)}
        self.assertIsNone(found["Contents/Resources/content/sounds/ouch.ogg"])
        self.assertIn("only images", found["Contents/Resources/content/avatar/script.lua"])
        self.assertEqual("Links are not followed.", found["Contents/Resources/content/sounds/link.ogg"])
        self.assertEqual([], folder_files(self.root / "missing"))


class ApplyTests(ModsTestCase):
    def test_replaced_files_are_backed_up_and_restored(self):
        source = self.root / "my-ouch.ogg"
        source.write_bytes(b"my sound")
        result = apply_mods(self.bundle, {DEATH_SOUND: source, "Contents/Resources/content/sounds/new.ogg": b"extra"})
        self.assertEqual(2, len(result.written))
        self.assertEqual(b"my sound", self.read(DEATH_SOUND))
        self.assertEqual(b"new death sound", (self.bundle.parent / ".roseblox-originals" / DEATH_SOUND).read_bytes())
        self.assertEqual(
            {DEATH_SOUND, "Contents/Resources/content/sounds/new.ogg"}, set(applied_files(self.bundle))
        )
        again = apply_mods(self.bundle, {DEATH_SOUND: source, "Contents/Resources/content/sounds/new.ogg": b"extra"})
        self.assertEqual(([], 2), (again.written, again.unchanged))

        restored = apply_mods(self.bundle, {})
        self.assertEqual(2, len(restored.restored))
        self.assertEqual(b"new death sound", self.read(DEATH_SOUND))
        self.assertFalse((self.bundle / "Contents/Resources/content/sounds/new.ogg").exists())
        self.assertEqual({}, applied_files(self.bundle))

    def test_a_file_changed_by_someone_else_is_not_overwritten_on_restore(self):
        apply_mods(self.bundle, {DEATH_SOUND: b"mine"})
        (self.bundle / DEATH_SOUND).write_bytes(b"edited by hand")
        apply_mods(self.bundle, {})
        self.assertEqual(b"edited by hand", self.read(DEATH_SOUND))

    def test_links_inside_the_bundle_are_refused(self):
        sounds = self.bundle / "Contents/Resources/content/sounds"
        os.symlink(self.root / "elsewhere.ogg", sounds / "linked.ogg")
        with self.assertRaisesRegex(ModsError, "link"):
            apply_mods(self.bundle, {"Contents/Resources/content/sounds/linked.ogg": b"x"})
        with self.assertRaises(ModsError):
            apply_mods(self.bundle, {"Contents/MacOS/RobloxPlayer": b"x"})


class OptionTests(ModsTestCase):
    def test_a_custom_font_replaces_every_family_except_kept_ones(self):
        font = self.root / "font.otf"
        font.write_bytes(OTF)
        files = custom_font_files(self.bundle, font)
        self.assertIs(font, files["Contents/Resources/content/fonts/RosebloxCustomFont.otf"])
        builder = json.loads(files[f"{FAMILIES}/BuilderSans.json"])
        self.assertEqual(
            {"rbxasset://fonts/RosebloxCustomFont.otf"}, {face["assetId"] for face in builder["faces"]}
        )
        self.assertNotIn(f"{FAMILIES}/RobotoMono.json", files)
        apply_mods(self.bundle, files)
        # The second time the family is read from the backup, so it still lists Roblox's faces.
        again = custom_font_files(self.bundle, font)
        self.assertEqual(files[f"{FAMILIES}/BuilderSans.json"], again[f"{FAMILIES}/BuilderSans.json"])

    def test_classic_death_sound_and_cursor(self):
        cursor = self.root / "cursor.png"
        cursor.write_bytes(png())
        settings = ModSettings(cursor=str(cursor), classic_death_sound=True)
        files, problems = desired_files(self.bundle, "player", settings, None)
        self.assertEqual([], problems)
        apply_mods(self.bundle, files)
        self.assertEqual(b"classic oof", self.read(DEATH_SOUND))
        for relative in CURSORS:
            self.assertEqual(png(), self.read(relative))
        studio_files, _ = desired_files(self.bundle, "studio", settings, None)
        self.assertNotIn(DEATH_SOUND, studio_files)

    def test_avatar_background_is_used_only_after_verification(self):
        places = self.bundle / "Contents/Resources/ExtraContent/places"
        places.mkdir(parents=True)
        (places / "Mobile.rbxl").write_bytes(b"stock scene")
        cache = self.root / "cache"
        data = b"<roblox!reviewed scene"
        with mock.patch.object(mods, "AVATAR_BACKGROUND_SHA256", hashlib.sha256(data).hexdigest()):
            files, problems = desired_files(
                self.bundle, "player", ModSettings(classic_avatar_background=True), None, cache_directory=cache
            )
            self.assertNotIn(AVATAR_BACKGROUND, files)
            self.assertIn("not downloaded", problems[0])
            with self.assertRaisesRegex(ModsError, "did not match"):
                fetch_avatar_background(cache, opener=lambda request, timeout: FakeResponse(b"tampered"))
            path = fetch_avatar_background(cache, opener=lambda request, timeout: FakeResponse(data))
            self.assertEqual(data, path.read_bytes())
            self.assertEqual(path, fetch_avatar_background(cache, opener=lambda *_: self.fail("downloaded twice")))
            files, problems = desired_files(
                self.bundle, "player", ModSettings(classic_avatar_background=True), None, cache_directory=cache
            )
        self.assertEqual(path, files[AVATAR_BACKGROUND])
        self.assertEqual([], problems)


class StoreTests(ModsTestCase):
    def test_chosen_files_are_validated_and_copied(self):
        store = ModsStore(self.root / "mods.json", assets_directory=self.root / "assets")
        font = self.root / "font.ttf"
        font.write_bytes(TTF)
        settings = store.set_font("player", font)
        font.unlink()
        self.assertEqual(TTF, Path(settings.font).read_bytes())
        self.assertEqual("font.ttf", Path(settings.font).name)
        other = self.root / "Other Font.otf"
        other.write_bytes(OTF)
        settings = store.set_font("player", other)
        self.assertEqual(["Other Font.otf"], [path.name for path in Path(settings.font).parent.iterdir()])
        bad = self.root / "bad.ttf"
        bad.write_bytes(b"not a font")
        with self.assertRaisesRegex(ModsError, "TrueType"):
            store.set_font("player", bad)
        big = self.root / "big.png"
        big.write_bytes(png(1024, 1024))
        with self.assertRaisesRegex(ModsError, "256 pixels"):
            store.set_cursor("player", big)
        self.assertIsNone(store.set_font("player", None).font)
        self.assertEqual(ModSettings(), store.load("studio"))


class PrepareLaunchTests(ModsTestCase):
    def test_mods_apply_to_managed_installations_only(self):
        store = ModsStore(self.root / "mods.json", assets_directory=self.root / "assets")
        store.save("player", ModSettings(classic_death_sound=True))
        env = {"XDG_DATA_HOME": str(self.root / "data")}
        external = SimpleNamespace(app_type="player", bundle_path=str(self.bundle), managed=False)
        self.assertIn("only to installations Roseblox installed", prepare_launch(external, store=store, env=env))
        self.assertEqual(b"new death sound", self.read(DEATH_SOUND))
        managed = SimpleNamespace(app_type="player", bundle_path=str(self.bundle), managed=True)
        self.assertIsNone(prepare_launch(managed, store=store, env=env))
        self.assertEqual(b"classic oof", self.read(DEATH_SOUND))
        store.save("player", ModSettings())
        self.assertIsNone(prepare_launch(managed, store=store, env=env))
        self.assertEqual(b"new death sound", self.read(DEATH_SOUND))


if __name__ == "__main__":
    unittest.main()
