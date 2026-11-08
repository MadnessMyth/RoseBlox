import json
import tempfile
import unittest
from pathlib import Path

from roseblox.backend import importers
from roseblox.backend.importers import planner
from roseblox.backend.importers.model import INVALID, PLAYER, ROSEBLOX, SKIPPED, STUDIO, UNSUPPORTED, ImportSourceError
from roseblox.backend.launch_config import AppLaunchConfig, LaunchConfigStore
from roseblox.backend.mods import ModsStore
from roseblox.backend.setup import SetupStore, WhilePlaying

TTF = b"\x00\x01\x00\x00" + b"\x00" * 64


def items(result, target=None):
    return {item.key: item for item in result.items if target is None or item.target == target}


def problems(result, status):
    return {problem.source_key: problem.reason for problem in result.problems if problem.status == status}


class ImporterTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.environ = {"HOME": str(self.home)}

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, relative, text):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8")
        return path

    def detected(self, importer_id):
        return next(d.locations for d in importers.detect_all(self.environ) if d.importer.id == importer_id)

    def stores(self):
        return planner.Stores(
            LaunchConfigStore(self.root / "roseblox" / "launch-config.json"),
            SetupStore(self.root / "roseblox" / "setup.json"),
            ModsStore(self.root / "roseblox" / "mods.json", assets_directory=self.root / "roseblox" / "assets"),
        )


class RoselineTests(ImporterTestCase):
    SETTINGS = """
runner = "/opt/rosemary"
flags = { FIntDebugForceMSAASamples = "4", FFlagDebugSkyGray = "True", FIntSomethingElse = "1" }

[user_flags]
FFlagDebugSkyGray = "False"
DFIntTaskSchedulerTargetFps = "144"
NotAFlag = "1"

[graphics]
backend = "opengl"
device = "0000:03:00.0"

[environment]
guest = ["MY_GUEST=1", "BARE"]
runner = ["LD_PRELOAD=/tmp/x.so", "RADV_PERFTEST=gpl"]

[behavior]
close_on_leave = true
gamemode = true

[discord]
enabled = true
app_id = "1561415907023265872"
join_button = false
show_game = true
show_avatar = true
details = "Roblox"
state = "via Roséline"

[server_location]
enabled = true

[avatar_background]
old = true

[mystery]
value = 1
"""

    def test_reads_what_roseblox_can_represent(self):
        path = self.write(".config/roseline/settings.toml", self.SETTINGS)
        self.assertEqual(self.detected("roseline"), (path,))
        result = importers.read_source("roseline", path)
        player, roseblox = items(result, PLAYER), items(result, ROSEBLOX)
        self.assertEqual(player["fast_flags.FIntDebugForceMSAASamples"].value, "4")
        self.assertEqual(player["fast_flags.FFlagDebugSkyGray"].value, "False")
        self.assertEqual(player["fast_flags.DFIntTaskSchedulerTargetFps"].value, "144")
        self.assertEqual(player["gpu"].value, "0000:03:00.0")
        self.assertEqual(player["guest_environment.MY_GUEST"].value, "1")
        self.assertEqual(player["rosemary_environment.RADV_PERFTEST"].value, "gpl")
        self.assertIs(player["gamemode"].value, True)
        self.assertIs(player["mods.classic_avatar_background"].value, True)
        self.assertIs(roseblox["close_on_leave"].value, True)
        self.assertIs(roseblox["discord_presence"].value, True)
        self.assertIs(roseblox["discord_join_button"].value, False)
        self.assertIs(roseblox["server_location"].value, True)
        self.assertNotIn("discord_application_id", roseblox)

        skipped, unsupported, invalid = (problems(result, s) for s in (SKIPPED, UNSUPPORTED, INVALID))
        self.assertIn("flags.FFlagDebugSkyGray", skipped)
        self.assertIn("flags.FIntSomethingElse", skipped)
        self.assertIn("discord.app_id", skipped)
        self.assertIn("graphics.backend", unsupported)
        self.assertIn("environment.guest[1]", unsupported)
        self.assertIn("discord.show_avatar", unsupported)
        self.assertNotIn("discord.show_game", unsupported)
        self.assertIn("runner", unsupported)
        self.assertIn("mystery", unsupported)
        self.assertIn("user_flags.NotAFlag", invalid)
        self.assertIn("environment.runner[0]", invalid)

    def test_malformed_file_is_reported(self):
        path = self.write(".config/roseline/settings.toml", "flags = {")
        with self.assertRaises(ImportSourceError):
            importers.read_source("roseline", path)

    def test_wrong_types_are_invalid_not_fatal(self):
        path = self.write(".config/roseline/settings.toml", 'behavior = "yes"\n[discord]\nenabled = "on"\n')
        result = importers.read_source("roseline", path)
        invalid = problems(result, INVALID)
        self.assertIn("behavior", invalid)
        self.assertIn("discord.enabled", invalid)

    def test_browsing_to_the_folder_finds_the_file(self):
        self.write(".config/roseline/settings.toml", "[behavior]\ngamemode = true\n")
        result = importers.read_source("roseline", self.home / ".config/roseline")
        self.assertIn("gamemode", items(result, PLAYER))


class QuadraTests(ImporterTestCase):
    def setUp(self):
        super().setUp()
        self.font = self.write("fonts/Comic.ttf", TTF)
        self.folder = self.home / ".config/quadrahq"
        self.write(".config/quadrahq/fastflags.json", json.dumps({"FIntTargetFps": 120, "BadName": 1}))
        self.write(".config/quadrahq/settings.json", json.dumps({
            "gpu": "pci:0000:01:00.0",
            "studio_gpu": "",
            "studio_gamemode": True,
            "studio_fflags": {"FFlagStudioThing": True},
            "mangohud": True,
            "rosemary_presentation_mode": "fifo",
            "rosemary_refresh_rate": "59.94",
            "rosemary_decorations": "server",
            "rosemary_command_key": "ctrl",
            "rosemary_plain_ctrl_click": False,
            "discord_rpc": True,
            "server_location_lookup": True,
            "server_location_notify": True,
            "mod_death_sound": "classic_oof",
            "mod_custom_font": str(self.font),
            "rosemary_force_opengl": False,
            "playtime_seconds": 1234,
        }))

    def test_reads_player_and_studio_separately(self):
        self.assertEqual(self.detected("quadra"), (self.folder,))
        result = importers.read_source("quadra", self.folder)
        player, studio, roseblox = items(result, PLAYER), items(result, STUDIO), items(result, ROSEBLOX)
        self.assertEqual(player["fast_flags.FIntTargetFps"].value, "120")
        self.assertEqual(studio["fast_flags.FFlagStudioThing"].value, "True")
        self.assertNotIn("fast_flags.FFlagStudioThing", player)
        self.assertEqual(player["gpu"].value, "0000:01:00.0")
        self.assertNotIn("gpu", studio)
        self.assertIs(studio["gamemode"].value, True)
        self.assertEqual(player["presentation_mode"].value, "fifo")
        self.assertEqual(player["decorations"].value, "server")
        self.assertEqual(player["command_key"].value, "ctrl")
        self.assertIs(player["mods.classic_death_sound"].value, True)
        self.assertEqual(player["mods.font"].value, str(self.font))
        self.assertIs(roseblox["plain_ctrl_click"].value, False)
        self.assertIs(roseblox["server_location"].value, True)
        invalid, unsupported = problems(result, INVALID), problems(result, UNSUPPORTED)
        self.assertIn("rosemary_refresh_rate", invalid)
        self.assertIn("fastflags.json.BadName", invalid)
        self.assertIn("playtime_seconds", unsupported)
        self.assertNotIn("rosemary_force_opengl", unsupported)
        self.assertTrue(all("1234" not in reason for reason in unsupported.values()))

    def test_malformed_flags_do_not_stop_the_settings(self):
        self.write(".config/quadrahq/fastflags.json", "{nope")
        result = importers.read_source("quadra", self.folder / "settings.json")
        self.assertIn("fastflags.json", problems(result, INVALID))
        self.assertIn("mangohud", items(result, PLAYER))

    def test_never_reads_the_web_folder(self):
        self.write(".config/quadrahq/web/Cookies", "secret")
        result = importers.read_source("quadra", self.folder)
        self.assertTrue(all("web" not in problem.source_key for problem in result.problems))


class SoberTests(ImporterTestCase):
    def test_reads_the_documented_keys(self):
        path = self.write(".var/app/org.vinegarhq.Sober/config/sober/config.json", json.dumps({
            "fflags": {"FFlagDebugSkyGray": True},
            "discord_rpc_enabled": True,
            "discord_rpc_show_join_button": False,
            "server_location_indicator_enabled": True,
            "close_on_leave": True,
            "enable_gamemode": True,
            "bring_back_oof": True,
            "use_opengl": True,
            "touch_mode": "off",
            "something_new": 1,
        }))
        self.assertEqual(self.detected("sober"), (path,))
        result = importers.read_source("sober", path)
        player, roseblox = items(result, PLAYER), items(result, ROSEBLOX)
        self.assertIn("Android", player["fast_flags.FFlagDebugSkyGray"].note)
        self.assertIs(player["gamemode"].value, True)
        self.assertIs(player["mods.classic_death_sound"].value, True)
        self.assertIs(roseblox["discord_join_button"].value, False)
        self.assertIs(roseblox["close_on_leave"].value, True)
        unsupported = problems(result, UNSUPPORTED)
        for key in ("use_opengl", "touch_mode", "something_new"):
            self.assertIn(key, unsupported)

    def test_not_an_object(self):
        path = self.write("config.json", "[]")
        with self.assertRaises(ImportSourceError):
            importers.read_source("sober", path)


class VinegarTests(ImporterTestCase):
    def test_reads_studio_settings_only(self):
        path = self.write(".config/vinegar/config.toml", """
debug = true
[studio]
gamemode = false
gpu = "0000:01:00.0"
renderer = "DXVK"
fflags = { DFIntTaskSchedulerTargetFps = 240 }
[studio.env]
WINEFSYNC = "1"
""")
        self.assertEqual(self.detected("vinegar"), (path,))
        result = importers.read_source("vinegar", path)
        studio = items(result, STUDIO)
        self.assertEqual(items(result, PLAYER), {})
        self.assertEqual(studio["fast_flags.DFIntTaskSchedulerTargetFps"].value, "240")
        self.assertEqual(studio["gpu"].value, "0000:01:00.0")
        self.assertIs(studio["gamemode"].value, False)
        unsupported = problems(result, UNSUPPORTED)
        self.assertIn("studio.env", unsupported)
        self.assertIn("studio.renderer", unsupported)


class WindowsAppTests(ImporterTestCase):
    def prefix_file(self, relative, text, prefix=".wine", user="me"):
        return self.write(f"{prefix}/drive_c/users/{user}/{relative}", text)

    def test_bloxstrap(self):
        self.prefix_file("AppData/Local/Bloxstrap/Settings.json", json.dumps({
            "UseFastFlagManager": False,
            "EnableActivityTracking": True,
            "UseDiscordRichPresence": True,
            "HideRPCButtons": True,
            "ShowServerDetails": False,
            "Theme": 1,
        }))
        self.prefix_file(
            "AppData/Local/Bloxstrap/Modifications/ClientSettings/ClientAppSettings.json",
            json.dumps({"FFlagDebugSkyGray": "True"}),
        )
        self.prefix_file("AppData/Local/Bloxstrap/Settings.json", "{}", user="Public")
        folder = self.home / ".wine/drive_c/users/me/AppData/Local/Bloxstrap"
        self.assertEqual(self.detected("bloxstrap"), (folder,))
        result = importers.read_source("bloxstrap", self.home / ".wine")
        player, studio, roseblox = items(result, PLAYER), items(result, STUDIO), items(result, ROSEBLOX)
        self.assertFalse(player["fast_flags.FFlagDebugSkyGray"].selected)
        self.assertFalse(studio["fast_flags.FFlagDebugSkyGray"].selected)
        self.assertIs(roseblox["discord_presence"].value, True)
        self.assertIs(roseblox["discord_join_button"].value, False)
        self.assertIn("Theme", problems(result, UNSUPPORTED))

    def test_bloxstrap_flags_file_alone(self):
        path = self.prefix_file(
            "AppData/Local/Bloxstrap/Modifications/ClientSettings/ClientAppSettings.json",
            json.dumps({"FFlagDebugSkyGray": "True"}),
        )
        result = importers.read_source("bloxstrap", path)
        self.assertTrue(items(result, PLAYER)["fast_flags.FFlagDebugSkyGray"].selected)
        self.assertFalse(items(result, STUDIO)["fast_flags.FFlagDebugSkyGray"].selected)

    def test_bloxstrap_in_a_bottle(self):
        self.prefix_file(
            "AppData/Local/Bloxstrap/Settings.json", "{}", prefix=".local/share/bottles/bottles/Roblox", user="steve"
        )
        self.assertEqual(len(self.detected("bloxstrap")), 1)

    def test_fastflag_manager(self):
        path = self.prefix_file(".FFlagManager/user_flags.json", json.dumps([
            {"name": "DFIntTaskSchedulerTargetFps", "value": "240", "type": "int", "enabled": True},
            {"name": "FFlagOff", "value": "True", "type": "bool", "enabled": False},
            {"name": "EnablePerFrameSampling", "value": "true", "type": "bool"},
            {"name": "SomeFloat", "value": "0.5", "type": "float"},
            {"name": "FFlagHotkey", "value": "True", "type": "bool", "bind": "F6"},
            "garbage",
        ]))
        self.assertEqual(self.detected("fastflag_manager"), (path,))
        result = importers.read_source("fastflag_manager", path)
        player = items(result, PLAYER)
        self.assertEqual(player["fast_flags.DFIntTaskSchedulerTargetFps"].value, "240")
        guessed = player["fast_flags.FFlagEnablePerFrameSampling"]
        self.assertFalse(guessed.selected)
        self.assertIn("guessed", guessed.note)
        self.assertIn("FFlagOff", problems(result, SKIPPED))
        invalid = problems(result, INVALID)
        self.assertIn("SomeFloat", invalid)
        self.assertIn("[5]", invalid)
        self.assertIn("FFlagHotkey.bind", problems(result, UNSUPPORTED))

    def test_fastflag_manager_not_a_list(self):
        path = self.prefix_file(".FFlagManager/user_flags.json", "{}")
        with self.assertRaises(ImportSourceError):
            importers.read_source("fastflag_manager", path)

    def test_roblox_manager_reads_flags_only(self):
        path = self.prefix_file("AppData/Roaming/RM/config.json", json.dumps({
            "roblox_fast_flags": {"FIntTargetFps": "60"},
            "custom_game_args": "--foo",
            "accounts_path": "C:\\Users\\me\\AppData\\Roaming\\RM\\accounts.dat",
            "auto_launch_account_id": 123456,
        }))
        self.prefix_file("AppData/Roaming/RM/client-launch-settings.json", "{}")
        self.prefix_file("AppData/Roaming/RM/accounts.dat", "secret")
        self.assertEqual(self.detected("roblox_manager"), (path,))
        result = importers.read_source("roblox_manager", path.parent)
        self.assertEqual(items(result, PLAYER)["fast_flags.FIntTargetFps"].value, "60")
        unsupported = problems(result, UNSUPPORTED)
        self.assertIn("custom_game_args", unsupported)
        self.assertIn("client-launch-settings.json", unsupported)
        mentioned = " ".join(p.source_key + p.reason for p in result.problems)
        self.assertNotIn("account_id", mentioned)
        self.assertNotIn("123456", mentioned)
        with self.assertRaises(ImportSourceError):
            importers.read_source("roblox_manager", path.parent / "accounts.dat")

    def test_generic_flag_file(self):
        path = self.write("Downloads/flags.json", json.dumps({"FFlagA": True, "DFIntB": "7", "Nope": 1}))
        result = importers.read_source("client_settings", path)
        self.assertTrue(items(result, PLAYER)["fast_flags.FFlagA"].selected)
        self.assertFalse(items(result, STUDIO)["fast_flags.DFIntB"].selected)
        self.assertEqual(list(problems(result, INVALID)), ["flags.json.Nope"])
        with self.assertRaises(ImportSourceError):
            importers.read_source("client_settings", self.write("list.json", "[1]"))


class PlannerTests(ImporterTestCase):
    def setUp(self):
        super().setUp()
        self.font = self.write("fonts/Comic.ttf", TTF)
        self.source = self.write(".config/roseline/settings.toml", f"""
user_flags = {{ FFlagDebugSkyGray = "True", DFIntTaskSchedulerTargetFps = "240" }}
[behavior]
gamemode = true
close_on_leave = true
[fonts]
font = "{self.font}"
""")
        self.store = self.stores()
        self.store.launch.save(PLAYER, AppLaunchConfig(fast_flags={"DFIntTaskSchedulerTargetFps": "60"}), "test")

    def plan(self):
        return planner.plan(importers.read_source("roseline", self.source), self.store)

    def test_conflicts_need_ticking(self):
        plan = self.plan()
        by_key = {change.item.key: change for change in plan.changes}
        self.assertEqual(by_key["fast_flags.FFlagDebugSkyGray"].status, planner.ADD)
        self.assertTrue(by_key["fast_flags.FFlagDebugSkyGray"].selected)
        conflict = by_key["fast_flags.DFIntTaskSchedulerTargetFps"]
        self.assertEqual((conflict.status, conflict.current, conflict.selected), (planner.REPLACE, "60", False))
        self.assertEqual(by_key["close_on_leave"].status, planner.ADD)

    def test_a_changed_roseblox_setting_is_a_conflict(self):
        self.source.write_text("[behavior]\nclose_on_leave = false\n", encoding="utf-8")
        self.store.setup.save_while_playing(WhilePlaying(close_on_leave=True))
        change = self.plan().changes[0]
        self.assertEqual((change.status, change.current, change.selected), (planner.REPLACE, True, False))

    def test_apply_writes_only_chosen_and_is_idempotent(self):
        before = self.source.read_bytes()
        plan = self.plan()
        written = planner.apply([c for c in plan.changes if c.selected], self.store, "Roséline")
        self.assertEqual(written, 4)
        config = self.store.launch.load(PLAYER)
        self.assertEqual(config.fast_flags, {"DFIntTaskSchedulerTargetFps": "60", "FFlagDebugSkyGray": "True"})
        self.assertTrue(config.gamemode)
        self.assertTrue(self.store.setup.load().while_playing.close_on_leave)
        stored_font = Path(self.store.mods.load(PLAYER).font)
        self.assertEqual(stored_font.read_bytes(), TTF)
        self.assertNotEqual(stored_font, self.font)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertTrue(self.store.launch.backups())

        again = self.plan()
        self.assertEqual([c.item.key for c in again.actionable], ["fast_flags.DFIntTaskSchedulerTargetFps"])
        self.assertEqual(planner.apply([c for c in again.changes if c.selected], self.store, "Roséline"), 0)

        planner.apply(again.actionable, self.store, "Roséline")
        self.assertEqual(self.store.launch.load(PLAYER).fast_flags["DFIntTaskSchedulerTargetFps"], "240")
        self.assertEqual(self.plan().actionable, [])

    def test_validation_happens_before_anything_is_written(self):
        self.store.launch.save(
            PLAYER, AppLaunchConfig(fast_flags={f"FIntFlag{n}": "1" for n in range(499)}), "test"
        )
        plan = self.plan()
        with self.assertRaises(planner.ImportApplyError) as raised:
            planner.apply(plan.actionable, self.store, "Roséline")
        self.assertIn("500", str(raised.exception))
        self.assertFalse(self.store.launch.load(PLAYER).gamemode)
        self.assertFalse(self.store.setup.load().while_playing.close_on_leave)
        self.assertIsNone(self.store.mods.load(PLAYER).font)

    def test_bad_font_is_invalid(self):
        self.font.write_bytes(b"not a font")
        plan = self.plan()
        self.assertIn("fonts.font", {problem.source_key for problem in plan.problems})
        self.assertNotIn("mods.font", {change.item.key for change in plan.changes})


if __name__ == "__main__":
    unittest.main()
