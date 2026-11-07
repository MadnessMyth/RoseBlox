import unittest

from roseblox.backend.flag_catalog import BUILT_IN_ALLOWLIST, PROFILE_EXCLUDED_CATEGORIES, categorize
from roseblox.backend.launch_config import AppLaunchConfig
from roseblox.backend.presets import CUSTOM, PRESET_FLAGS, PRESETS, matching_preset, preset


class PresetTests(unittest.TestCase):
    def test_defaults_are_the_balanced_preset(self):
        self.assertEqual("balanced", matching_preset(AppLaunchConfig()))

    def test_each_preset_is_recognized_after_it_is_applied(self):
        for candidate in PRESETS:
            with self.subTest(preset=candidate.id):
                applied = candidate.apply(AppLaunchConfig())
                self.assertEqual(candidate.id, matching_preset(applied))
                self.assertEqual([], applied.problems())

    def test_presets_keep_everything_they_do_not_list(self):
        config = AppLaunchConfig(
            rosemary_environment={"ROSEMARY_SHADER_CACHE": "off"},
            guest_environment={"A": "1"},
            arguments=("-task",),
            fast_flags={"FFlagDebugSkyGray": "True", "FIntDebugForceMSAASamples": "8"},
            refresh_rate=144,
            gpu="0000:01:00.0",
        )
        applied = preset("performance").apply(config)
        self.assertEqual(config.rosemary_environment, applied.rosemary_environment)
        self.assertEqual(config.guest_environment, applied.guest_environment)
        self.assertEqual(config.arguments, applied.arguments)
        self.assertEqual(144, applied.refresh_rate)
        self.assertEqual("0000:01:00.0", applied.gpu)
        self.assertEqual("True", applied.fast_flags["FFlagDebugSkyGray"])
        self.assertEqual("1", applied.fast_flags["FIntDebugForceMSAASamples"])
        back = preset("balanced").apply(applied)
        self.assertEqual({"FFlagDebugSkyGray": "True"}, back.fast_flags)
        self.assertIsNone(back.presentation_mode)
        self.assertFalse(back.gamemode)

    def test_gamemode_is_only_turned_on_when_it_is_installed(self):
        self.assertTrue(preset("performance").apply(AppLaunchConfig()).gamemode)
        without = preset("performance").apply(AppLaunchConfig(), gamemode_available=False)
        self.assertFalse(without.gamemode)
        self.assertEqual("performance", matching_preset(without, gamemode_available=False))

    def test_edited_settings_are_custom(self):
        edited = preset("visual").apply(AppLaunchConfig())
        edited = AppLaunchConfig(**{**edited.__dict__, "presentation_mode": "immediate"})
        self.assertEqual(CUSTOM, matching_preset(edited))

    def test_presets_only_use_allowlisted_rendering_flags(self):
        for candidate in PRESETS:
            for name in candidate.flags:
                with self.subTest(preset=candidate.id, flag=name):
                    self.assertIn(name, PRESET_FLAGS)
                    self.assertIn(name, BUILT_IN_ALLOWLIST)
                    self.assertNotIn(categorize(name), PROFILE_EXCLUDED_CATEGORIES)
        with self.assertRaises(ValueError):
            preset("unknown")


if __name__ == "__main__":
    unittest.main()
