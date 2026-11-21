"""GTK-dependent tests for the Roblox Player vs Roblox Updater launch feature.

These exercise the pieces that import PyGObject (``launch_application`` override
forwarding in ``rosemary`` and the variant prompt routing in the manager page),
so they follow the same unguarded-import convention as ``tests/test_page_launch``
and ``tests/test_launch_config`` and run wherever GTK/libadwaita is installed.
The GTK-free backend logic is covered separately in ``tests/test_launch_variants``.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch

from roseblox.backend.launch_config import AppLaunchConfig
from roseblox.backend.library import BundleValidationError, LaunchVariant
from roseblox.backend.rosemary import (
    RosemaryStatus,
    RosemaryUnavailableError,
    launch_application,
)
from roseblox.pages.manager_page import InstallationPage


def _make_bundle(parent: Path, name: str) -> Path:
    bundle = parent / name
    (bundle / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    return bundle


class _FakeMessageDialog:
    """Records the responses/config of an Adw.MessageDialog and replays one."""

    def __init__(self, title: str, body: str) -> None:
        self.title = title
        self.body = body
        self.responses: list[tuple[str, str]] = []
        self.default_response: str | None = None
        self.close_response: str | None = None
        self._handler = None
        self._handler_args: tuple = ()

    def add_response(self, response_id: str, label: str) -> None:
        self.responses.append((response_id, label))

    def set_default_response(self, response_id: str) -> None:
        self.default_response = response_id

    def set_close_response(self, response_id: str) -> None:
        self.close_response = response_id

    def connect(self, signal: str, handler, *args) -> None:
        assert signal == "response"
        self._handler = handler
        self._handler_args = args

    def present(self) -> None:
        pass

    def emit_response(self, response: str) -> None:
        assert self._handler is not None
        self._handler(self, response, *self._handler_args)


class LaunchApplicationOverrideTests(unittest.TestCase):
    """The bundle_path_override starts the chosen sibling bundle directly."""

    def _launch(self, root: Path, *, bundle_path_override: str | None, create_updater: bool = True) -> object:
        # The registered bundle is always RobloxPlayer.app; the override, when
        # set, points at the sibling Roblox.app updater. ``create_updater``
        # controls whether that sibling actually exists on disk.
        _make_bundle(root, "RobloxPlayer.app")
        if create_updater:
            _make_bundle(root, "Roblox.app")
        executable = root / "rosemary"
        executable.write_bytes(b"ELF\0rosemary build\0")
        executable.chmod(0o755)
        fake_process = subprocess.CompletedProcess([], 0)
        fake_process.pid = 34567
        fake_process.poll = lambda: None
        with patch.dict(os.environ, {"HOME": str(root)}, clear=True), patch(
            "roseblox.backend.rosemary.subprocess.Popen", return_value=fake_process
        ) as popen:
            session = launch_application(
                {"app_type": "player", "bundle_path": str(root / "RobloxPlayer.app")},
                RosemaryStatus(str(executable)),
                launch_config=AppLaunchConfig(),
                log_directory=root / "logs",
                flag_backup_directory=root / "backups",
                bundle_path_override=bundle_path_override,
            )
        return session, popen

    def test_override_targets_the_updater_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            updater = _make_bundle(root, "Roblox.app")
            session, _ = self._launch(root, bundle_path_override=str(updater))
            target = str(session.launch_target).rstrip(os.sep)
            self.assertTrue(target.endswith("Roblox.app"), target)
            self.assertNotIn("RobloxPlayer.app", target)

    def test_no_override_targets_the_registered_player_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session, _ = self._launch(root, bundle_path_override=None, create_updater=False)
            target = str(session.launch_target).rstrip(os.sep)
            self.assertTrue(target.endswith("RobloxPlayer.app"), target)

    def test_override_pointing_at_missing_bundle_fails_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Only the registered player bundle exists; the override references
            # an updater bundle that was never installed, so validation must fail.
            _make_bundle(root, "RobloxPlayer.app")
            with self.assertRaises((BundleValidationError, RosemaryUnavailableError)):
                self._launch(root, bundle_path_override=str(root / "Roblox.app"), create_updater=False)


class LaunchWorkerOverrideTests(unittest.TestCase):
    """The launch worker forwards the chosen bundle path to launch_application."""

    def _page(self) -> SimpleNamespace:
        page = SimpleNamespace(
            rosemary_status=object(),
            window=SimpleNamespace(setup_state=SimpleNamespace(plain_ctrl_click=True)),
            _launch_ready=Mock(),
        )
        page._multiple_allowed = MethodType(InstallationPage._multiple_allowed, page)
        return page

    def test_worker_forwards_override_to_launch_application(self):
        page = self._page()
        record = SimpleNamespace(id="r1", app_type="player", bundle_path="/apps/RobloxPlayer.app")
        with patch("roseblox.pages.manager_page.launch_application") as launch, patch(
            "roseblox.pages.manager_page.LaunchConfigStore"
        ), patch(
            "roseblox.pages.manager_page.prepare_mods", return_value=None
        ), patch("roseblox.pages.manager_page.wait_for_running"), patch(
            "roseblox.pages.manager_page.GLib.idle_add"
        ):
            InstallationPage._launch_worker(page, "r1", record, (), None, "/apps/Roblox.app")
        self.assertEqual("/apps/Roblox.app", launch.call_args.kwargs["bundle_path_override"])

    def test_worker_defaults_override_to_none_when_not_provided(self):
        page = self._page()
        record = SimpleNamespace(id="r1", app_type="player", bundle_path="/apps/RobloxPlayer.app")
        with patch("roseblox.pages.manager_page.launch_application") as launch, patch(
            "roseblox.pages.manager_page.LaunchConfigStore"
        ), patch(
            "roseblox.pages.manager_page.prepare_mods", return_value=None
        ), patch("roseblox.pages.manager_page.wait_for_running"), patch(
            "roseblox.pages.manager_page.GLib.idle_add"
        ):
            InstallationPage._launch_worker(page, "r1", record)
        self.assertIsNone(launch.call_args.kwargs["bundle_path_override"])


class LaunchVariantPromptRoutingTests(unittest.TestCase):
    """request_launch_toggle routes to the prompt or a direct launch correctly."""

    def _bind_page(self) -> SimpleNamespace:
        page = SimpleNamespace(
            rosemary_status=object(),
            window=SimpleNamespace(),
            _sessions={},
            _launching_ids=set(),
            _stopping_ids=set(),
            _refresh_all=Mock(),
            _begin_launch=Mock(),
            _prompt_launch_variant=Mock(),
            _stop_worker=Mock(),
        )
        page.request_launch_toggle = MethodType(InstallationPage.request_launch_toggle, page)
        page._single_variant_override = MethodType(InstallationPage._single_variant_override, page)
        return page

    def _record(self) -> SimpleNamespace:
        return SimpleNamespace(id="r1", app_type="player", bundle_path="/apps/RobloxPlayer.app")

    def test_two_variants_show_prompt_without_launching(self):
        page = self._bind_page()
        record = self._record()
        variants = [
            LaunchVariant("player", Path("/apps/RobloxPlayer.app")),
            LaunchVariant("updater", Path("/apps/Roblox.app")),
        ]
        with patch("roseblox.pages.manager_page.discover_launch_variants", return_value=variants):
            page.request_launch_toggle(record)
        page._prompt_launch_variant.assert_called_once_with(record, variants)
        page._begin_launch.assert_not_called()

    def test_single_player_variant_launches_directly_without_prompt(self):
        page = self._bind_page()
        record = self._record()
        with patch(
            "roseblox.pages.manager_page.discover_launch_variants",
            return_value=[LaunchVariant("player", Path("/apps/RobloxPlayer.app"))],
        ):
            page.request_launch_toggle(record)
        page._prompt_launch_variant.assert_not_called()
        page._begin_launch.assert_called_once_with(record, bundle_path_override=None)

    def test_single_updater_variant_launches_it_directly_with_override(self):
        page = self._bind_page()
        record = self._record()
        # Only the updater was discovered: launch the only available option.
        with patch(
            "roseblox.pages.manager_page.discover_launch_variants",
            return_value=[LaunchVariant("updater", Path("/apps/Roblox.app"))],
        ):
            page.request_launch_toggle(record)
        page._prompt_launch_variant.assert_not_called()
        page._begin_launch.assert_called_once_with(record, bundle_path_override="/apps/Roblox.app")

    def test_no_variants_falls_back_to_the_registered_bundle(self):
        page = self._bind_page()
        record = self._record()
        with patch("roseblox.pages.manager_page.discover_launch_variants", return_value=[]):
            page.request_launch_toggle(record)
        page._prompt_launch_variant.assert_not_called()
        page._begin_launch.assert_called_once_with(record, bundle_path_override=None)

    def test_running_session_toggles_stop_without_considering_variants(self):
        page = self._bind_page()
        page._sessions = {"r1": object()}
        with patch("roseblox.pages.manager_page.threading.Thread") as thread, patch(
            "roseblox.pages.manager_page.discover_launch_variants"
        ) as discover:
            thread.return_value.start = Mock()
            page.request_launch_toggle(self._record())
        discover.assert_not_called()
        page._prompt_launch_variant.assert_not_called()
        page._begin_launch.assert_not_called()
        self.assertIn("r1", page._stopping_ids)


class LaunchVariantDialogTests(unittest.TestCase):
    """The prompt dialog offers both variants, defaults to Player, and routes."""

    def _bind_page(self) -> tuple[SimpleNamespace, dict]:
        page = SimpleNamespace(window=SimpleNamespace(), _begin_launch=Mock())
        page._prompt_launch_variant = MethodType(InstallationPage._prompt_launch_variant, page)
        page._on_launch_variant_response = MethodType(
            InstallationPage._on_launch_variant_response, page
        )
        holder: dict = {}
        fake_adw = Mock()

        def make_dialog(_window, title, body):
            dialog = _FakeMessageDialog(title, body)
            holder["dialog"] = dialog
            return dialog

        fake_adw.MessageDialog.new.side_effect = make_dialog
        return page, (fake_adw, holder)

    def _variants(self) -> list[LaunchVariant]:
        return [
            LaunchVariant("player", Path("/apps/RobloxPlayer.app")),
            LaunchVariant("updater", Path("/apps/Roblox.app")),
        ]

    def test_dialog_offers_both_variants_and_defaults_to_player(self):
        page, (fake_adw, holder) = self._bind_page()
        record = SimpleNamespace(id="r1", bundle_path="/apps/RobloxPlayer.app")
        with patch("roseblox.pages.manager_page.Adw", fake_adw):
            page._prompt_launch_variant(record, self._variants())
        dialog = holder["dialog"]
        self.assertEqual("Launch Roblox Player or Roblox Updater?", dialog.title)
        self.assertEqual("cancel", dialog.close_response)
        self.assertEqual(
            [("player", "Roblox Player"), ("updater", "Roblox Updater")], dialog.responses
        )
        self.assertEqual("player", dialog.default_response)

    def test_choosing_updater_launches_the_updater_bundle(self):
        page, (fake_adw, holder) = self._bind_page()
        record = SimpleNamespace(id="r1", bundle_path="/apps/RobloxPlayer.app")
        with patch("roseblox.pages.manager_page.Adw", fake_adw):
            page._prompt_launch_variant(record, self._variants())
        holder["dialog"].emit_response("updater")
        page._begin_launch.assert_called_once_with(record, bundle_path_override="/apps/Roblox.app")

    def test_choosing_player_launches_without_an_override(self):
        page, (fake_adw, holder) = self._bind_page()
        record = SimpleNamespace(id="r1", bundle_path="/apps/RobloxPlayer.app")
        with patch("roseblox.pages.manager_page.Adw", fake_adw):
            page._prompt_launch_variant(record, self._variants())
        holder["dialog"].emit_response("player")
        page._begin_launch.assert_called_once_with(record, bundle_path_override=None)

    def test_cancelling_does_not_launch(self):
        page, (fake_adw, holder) = self._bind_page()
        record = SimpleNamespace(id="r1", bundle_path="/apps/RobloxPlayer.app")
        with patch("roseblox.pages.manager_page.Adw", fake_adw):
            page._prompt_launch_variant(record, self._variants())
        holder["dialog"].emit_response("cancel")
        page._begin_launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
