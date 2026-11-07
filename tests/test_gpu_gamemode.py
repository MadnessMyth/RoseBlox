import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import gi

gi.require_version("Gio", "2.0")
from gi.repository import GLib

from roseblox.backend import gamemode
from roseblox.backend.gpu import Gpu, find_gpu, gpu_name, list_gpus
from roseblox.backend.launch_config import AppLaunchConfig, describe_changes, effective_environment
from roseblox.backend.rosemary import RosemaryStatus, launch_application

PCI_IDS = """\
# comment
10de  NVIDIA Corporation
\t2782  AD104 [GeForce RTX 4070 Ti]
\t\t1043 8888  Some subsystem
8086  Intel Corporation
\t4680  AlderLake-S GT1
1af4  Red Hat, Inc.
C 00  Unclassified device
"""


def _card(drm: Path, name: str, slot: str, vendor: str, device: str, driver: str | None, boot: bool) -> None:
    devices = drm.parent / "devices"
    target = devices / slot
    target.mkdir(parents=True)
    (target / "vendor").write_text(f"0x{vendor}\n")
    (target / "device").write_text(f"0x{device}\n")
    (target / "boot_vga").write_text("1\n" if boot else "0\n")
    if driver:
        driver_dir = drm.parent / "drivers" / driver
        driver_dir.mkdir(parents=True, exist_ok=True)
        (target / "driver").symlink_to(driver_dir)
    (drm / name).mkdir(parents=True)
    (drm / name / "device").symlink_to(target)


class GpuTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        root = Path(self._directory.name)
        self.drm = root / "drm"
        self.drm.mkdir()
        self.pci_ids = root / "pci.ids"
        self.pci_ids.write_text(PCI_IDS)

    def tearDown(self):
        self._directory.cleanup()

    def test_cards_are_listed_with_names_and_the_boot_card_first(self):
        _card(self.drm, "card1", "0000:01:00.0", "10de", "2782", "nvidia", False)
        _card(self.drm, "card0", "0000:00:02.0", "8086", "4680", "i915", True)
        (self.drm / "card0-DP-1").mkdir()
        (self.drm / "renderD128").mkdir()
        gpus = list_gpus(self.drm, self.pci_ids)
        self.assertEqual(["0000:00:02.0", "0000:01:00.0"], [gpu.slot for gpu in gpus])
        self.assertEqual("Intel AlderLake-S GT1", gpus[0].name)
        self.assertEqual("NVIDIA AD104 [GeForce RTX 4070 Ti]", gpus[1].name)
        self.assertEqual("nvidia", gpus[1].driver)
        self.assertEqual("10de:2782", gpus[1].ids)
        self.assertEqual(gpus[1], find_gpu("0000:01:00.0", gpus))
        self.assertIsNone(find_gpu("0000:09:00.0", gpus))
        self.assertIsNone(find_gpu(None, gpus))

    def test_unknown_cards_still_get_a_name(self):
        self.assertEqual("NVIDIA device 1234", gpu_name(0x10DE, 0x1234, self.pci_ids))
        self.assertEqual("Red Hat, Inc. device 1050", gpu_name(0x1AF4, 0x1050, self.pci_ids))
        self.assertEqual("Vendor abcd device 0001", gpu_name(0xABCD, 0x0001, self.pci_ids))

    def test_missing_drm_means_no_cards(self):
        self.assertEqual([], list_gpus(self.drm / "missing", self.pci_ids))

    def test_vulkan_variables_pin_the_card(self):
        nvidia = Gpu("0000:01:00.0", 0x10DE, 0x2782, "nvidia", "NVIDIA")
        intel = Gpu("0000:00:02.0", 0x8086, 0x4680, "i915", "Intel")
        self.assertEqual(
            {
                "MESA_VK_DEVICE_SELECT": "10de:2782",
                "MESA_VK_DEVICE_SELECT_FORCE_DEFAULT_DEVICE": "1",
                "__VK_LAYER_NV_optimus": "NVIDIA_only",
            },
            nvidia.vulkan_environment(),
        )
        self.assertEqual("non_NVIDIA_only", intel.vulkan_environment()["__VK_LAYER_NV_optimus"])

        config = AppLaunchConfig(gpu=nvidia.slot)
        self.assertEqual("10de:2782", effective_environment(config, {}, [intel, nvidia])["MESA_VK_DEVICE_SELECT"])
        self.assertNotIn("MESA_VK_DEVICE_SELECT", effective_environment(config, {}, [intel]))
        manual = AppLaunchConfig(gpu=nvidia.slot, rosemary_environment={"MESA_VK_DEVICE_SELECT": "8086:4680"})
        self.assertEqual("8086:4680", effective_environment(manual, {}, [nvidia])["MESA_VK_DEVICE_SELECT"])

    def test_gpu_and_gamemode_choices_are_validated_and_described(self):
        self.assertEqual([], AppLaunchConfig(gpu="0000:01:00.0", gamemode=True).problems())
        self.assertEqual(1, len(AppLaunchConfig(gpu="card1").problems()))
        self.assertEqual(1, len(AppLaunchConfig(gamemode="yes").problems()))
        self.assertEqual(
            ["GPU: chosen by Rosemary → NVIDIA", "GameMode: off → on"],
            describe_changes(AppLaunchConfig(), AppLaunchConfig(gpu="0000:01:00.0", gamemode=True), {"0000:01:00.0": "NVIDIA"}),
        )
        document = AppLaunchConfig(gpu="0000:01:00.0", gamemode=True).to_document()
        self.assertEqual(AppLaunchConfig(gpu="0000:01:00.0", gamemode=True), AppLaunchConfig.from_document(document))
        self.assertFalse(AppLaunchConfig.from_document({}).gamemode)


class MangoHudTests(unittest.TestCase):
    def test_the_layer_is_found_from_its_manifest(self):
        from roseblox.backend import mangohud

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {"HOME": directory, "XDG_DATA_DIRS": str(root / "share")}
            self.assertFalse(mangohud.layer_installed(env))
            layers = root / "share" / "vulkan" / "implicit_layer.d"
            layers.mkdir(parents=True)
            (layers / "MangoHud.x86_64.json").write_text("not json")
            self.assertFalse(mangohud.layer_installed(env))
            (layers / "MangoHud.x86_64.json").write_text(
                '{"file_format_version": "1.0.0", "layer": {"name": "VK_LAYER_MANGOHUD_overlay_x86_64"}}'
            )
            self.assertTrue(mangohud.layer_installed(env))

    def test_the_overlay_is_enabled_per_application(self):
        self.assertEqual("1", effective_environment(AppLaunchConfig(mangohud=True), {})["MANGOHUD"])
        self.assertNotIn("MANGOHUD", effective_environment(AppLaunchConfig(), {}))
        self.assertEqual(1, len(AppLaunchConfig(mangohud="on").problems()))
        self.assertEqual(1, len(AppLaunchConfig(rosemary_environment={"MANGOHUD": "1"}).problems()))
        self.assertEqual(["MangoHud: off → on"], describe_changes(AppLaunchConfig(), AppLaunchConfig(mangohud=True)))


class _Reply:
    def __init__(self, value):
        self.value = value

    def unpack(self):
        return self.value


class _Bus:
    def __init__(self, replies=None, error=None):
        self.calls = []
        self.replies = replies or {}
        self.error = error

    def call_sync(self, name, path, interface, method, parameters, reply_type, flags, timeout, cancellable):
        self.calls.append((name, method, parameters.unpack() if parameters is not None else None, timeout))
        if self.error is not None:
            raise self.error
        return _Reply(self.replies[method])


class GameModeTests(unittest.TestCase):
    def test_registration_sends_the_process_id(self):
        bus = _Bus({"RegisterGame": (0,), "UnregisterGame": (0,)})
        gamemode.register(4242, bus)
        gamemode.unregister(4242, bus)
        self.assertEqual(
            [(gamemode.BUS_NAME, "RegisterGame", (4242,), 2000), (gamemode.BUS_NAME, "UnregisterGame", (4242,), 2000)],
            bus.calls,
        )

    def test_refusals_and_missing_daemons_are_reported(self):
        with self.assertRaises(gamemode.GameModeError):
            gamemode.register(1, _Bus({"RegisterGame": (-1,)}))
        with self.assertRaises(gamemode.GameModeError):
            gamemode.register(1, _Bus(error=GLib.Error("The name is not activatable")))

    def test_availability_checks_running_and_activatable_daemons(self):
        self.assertTrue(gamemode.available(_Bus({"NameHasOwner": (True,)})))
        self.assertTrue(
            gamemode.available(_Bus({"NameHasOwner": (False,), "ListActivatableNames": ([gamemode.BUS_NAME],)}))
        )
        self.assertFalse(gamemode.available(_Bus({"NameHasOwner": (False,), "ListActivatableNames": (["other"],)})))
        self.assertFalse(gamemode.available(_Bus(error=GLib.Error("no bus"))))

    def test_no_session_bus_is_not_autolaunched(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(gamemode.Gio, "bus_get_sync") as bus_get:
            self.assertFalse(gamemode.available())
            with self.assertRaises(gamemode.GameModeError):
                gamemode.register(1)
        bus_get.assert_not_called()

    def test_launch_registers_rosemary_and_reports_failures_without_stopping(self):
        for failure in (None, gamemode.GameModeError("GameMode is not running")):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                bundle = root / "RobloxPlayer.app"
                (bundle / "Contents" / "MacOS").mkdir(parents=True)
                executable = root / "rosemary"
                executable.write_bytes(b"ELF\0")
                executable.chmod(0o755)
                fake_process = subprocess.CompletedProcess([], 0)
                fake_process.pid = 5150
                fake_process.poll = lambda: None
                with patch("roseblox.backend.rosemary.subprocess.Popen", return_value=fake_process), patch(
                    "roseblox.backend.rosemary.gamemode.register", side_effect=failure
                ) as register:
                    session = launch_application(
                        {"app_type": "player", "bundle_path": str(bundle)},
                        RosemaryStatus(str(executable)),
                        log_directory=root / "logs",
                        launch_config=AppLaunchConfig(gamemode=True),
                        flag_backup_directory=root / "backups",
                    )
                register.assert_called_once_with(5150)
                self.assertEqual(None if failure is None else "GameMode is not running", session.gamemode_error)


if __name__ == "__main__":
    unittest.main()
