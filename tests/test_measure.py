import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib

from roseblox.backend import measure as measure_module
from roseblox.backend.measure import (
    GpuReading,
    MeasureError,
    MeasurementStore,
    Metrics,
    amdgpu_reader,
    comparison_rows,
    measure,
    new_record,
    nvidia_reader,
    process_memory,
    process_tree,
    thread_cpu_ticks,
)
from roseblox.pages.measure_section import MeasureSection

TICKS = measure_module._CLOCK_TICKS


class FakeProc:
    def __init__(self, root: Path):
        self.root = root

    def process(self, pid: int, parent: int, threads: dict[int, int], pss_kib: int | None = None) -> None:
        directory = self.root / str(pid)
        (directory / "task").mkdir(parents=True, exist_ok=True)
        (directory / "stat").write_text(f"{pid} (Roblox Player) S {parent} 1 1 0 -1 0 0 0 0 0 0 0 0 0\n")
        for tid, ticks in threads.items():
            (directory / "task" / str(tid)).mkdir(exist_ok=True)
            fields = ["S", str(parent)] + ["0"] * 9 + [str(ticks), "0"]
            (directory / "task" / str(tid) / "stat").write_text(f"{tid} (a thread) {' '.join(fields)}\n")
        if pss_kib is not None:
            (directory / "smaps_rollup").write_text(f"Rss:  999 kB\nPss:  {pss_kib} kB\n")


def metrics(**overrides) -> Metrics:
    values = dict(
        duration=60.0, samples=60, cpu_average=120.0, cpu_peak=150.0, busiest_thread=50.0,
        memory_average=2 << 30, memory_peak=3 << 30,
    )
    values.update(overrides)
    return Metrics(**values)


class ProcReadingTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.proc = FakeProc(Path(self._directory.name))

    def tearDown(self):
        self._directory.cleanup()

    def test_tree_threads_and_memory(self):
        self.proc.process(100, 1, {100: 10}, pss_kib=1000)
        self.proc.process(101, 100, {101: 5, 102: 7}, pss_kib=500)
        self.proc.process(200, 1, {200: 1})
        root = self.proc.root
        self.assertEqual({100, 101}, set(process_tree(100, root)))
        self.assertEqual([], process_tree(999, root))
        self.assertEqual({(101, 101): 5, (101, 102): 7}, thread_cpu_ticks(101, root))
        self.assertEqual(500 * 1024, process_memory(101, root))
        self.assertIsNone(process_memory(200, root))

    def test_measure_reports_cpu_threads_memory_and_gpu(self):
        self.proc.process(100, 1, {100: 0, 103: 0}, pss_kib=1024)
        self.proc.process(101, 100, {101: 0}, pss_kib=1024)
        now = [0.0]
        step = [0]

        def sleep(_seconds):
            step[0] += 1
            now[0] += 1.0
            self.proc.process(100, 1, {100: TICKS * step[0], 103: TICKS * step[0] // 2}, pss_kib=1024 * (1 + step[0]))
            self.proc.process(101, 100, {101: 0}, pss_kib=1024)

        fractions = []
        result = measure(
            100, 3, proc=self.proc.root, clock=lambda: now[0], sleep=sleep, progress=fractions.append,
            gpu=("test", lambda: GpuReading(95.0, 1 << 30)),
        )
        self.assertEqual(3, result.samples)
        self.assertEqual(150.0, result.cpu_average)
        self.assertEqual(100.0, result.busiest_thread)
        self.assertEqual((1024 * 4 + 1024) * 1024, result.memory_peak)
        self.assertEqual(95.0, result.gpu_busy_average)
        self.assertEqual("test", result.gpu_source)
        self.assertEqual(1.0, fractions[-1])
        self.assertIn("GPU", result.limit_hint)

    def test_measure_stops_when_cancelled_closed_or_missing(self):
        with self.assertRaises(MeasureError):
            measure(100, 3, proc=self.proc.root, sleep=lambda _s: None)
        self.proc.process(100, 1, {100: 0})
        cancel = threading.Event()
        cancel.set()
        with self.assertRaisesRegex(MeasureError, "stopped"):
            measure(100, 3, proc=self.proc.root, sleep=lambda _s: None, cancelled=cancel)
        with self.assertRaisesRegex(MeasureError, "closed"):
            measure(100, 3, proc=self.proc.root, sleep=lambda _s: None, alive=lambda: False)


class GpuReaderTests(unittest.TestCase):
    def test_amdgpu_sysfs(self):
        with tempfile.TemporaryDirectory() as directory:
            drm = Path(directory)
            self.assertIsNone(amdgpu_reader(drm))
            device = drm / "card1" / "device"
            device.mkdir(parents=True)
            (device / "gpu_busy_percent").write_text("42\n")
            (device / "mem_info_vram_used").write_text("1048576\n")
            (drm / "card1-DP-1").mkdir()
            self.assertEqual(GpuReading(42.0, 1048576), amdgpu_reader(drm)())

    def test_nvidia_smi(self):
        self.assertIsNone(nvidia_reader(which=lambda _name: None))
        run = lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout="37, 2048\n", stderr="")
        reader = nvidia_reader(which=lambda _name: "/usr/bin/nvidia-smi", run=run)
        self.assertEqual(GpuReading(37.0, 2048 * 1024 * 1024), reader())
        failing = nvidia_reader(which=lambda _name: "/x", run=lambda *a, **k: (_ for _ in ()).throw(OSError("gone")))
        self.assertEqual(GpuReading(None, None), failing())


class MetricsTests(unittest.TestCase):
    def test_limit_hints_are_cautious(self):
        self.assertIn("GPU was busy", metrics(gpu_busy_average=97.0).limit_hint)
        self.assertIn("One CPU thread", metrics(busiest_thread=99.0, gpu_busy_average=40.0).limit_hint)
        self.assertIn("not reported", metrics().limit_hint)
        self.assertIn("frame rate cap or V-Sync", metrics(gpu_busy_average=40.0).limit_hint)


class StoreTests(unittest.TestCase):
    def test_records_are_saved_newest_first_capped_and_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MeasurementStore(Path(directory) / "state" / "measurements.json")
            self.assertEqual([], store.load())
            first = new_record("player", "  Spawn area  ", {"Preset": "Balanced"}, metrics(), now=1.0)
            second = new_record("player", "", {"Preset": "Performance"}, metrics(cpu_average=90.0), now=2.0)
            store.add(first)
            records = store.add(second)
            self.assertEqual([second.id, first.id], [record.id for record in records])
            self.assertEqual("Spawn area", store.load()[1].notes)
            self.assertEqual(metrics(), store.load()[1].metrics)
            document = json.loads(store.path.read_text())
            document["measurements"].append({"id": "broken"})
            store.path.write_text(json.dumps(document))
            self.assertEqual(2, len(store.load()))
            self.assertEqual([second.id], [record.id for record in store.delete(first.id)])
            for index in range(measure_module.MAX_RECORDS + 5):
                store.add(new_record("studio", "", {}, metrics(), now=10.0 + index))
            self.assertEqual(measure_module.MAX_RECORDS, len(store.load()))
            store.path.write_text("{broken")
            with self.assertRaises(MeasureError):
                store.load()

    def test_comparison_lists_settings_and_metrics(self):
        older = new_record("player", "a", {"Preset": "Balanced"}, metrics(), now=1.0)
        newer = new_record("player", "b", {"Preset": "Performance", "MangoHud": "On"}, metrics(gpu_busy_average=50.0), now=2.0)
        rows = {label: (a, b) for label, a, b in comparison_rows(older, newer)}
        self.assertEqual(("Balanced", "Performance"), rows["Preset"])
        self.assertEqual(("—", "On"), rows["MangoHud"])
        self.assertEqual(("120%", "120%"), rows["CPU, average (100% = one core)"])
        self.assertEqual(("Not reported", "50%"), rows["GPU busy, average"])


class MeasureSectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Adw.init()

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.store = MeasurementStore(Path(self._directory.name) / "measurements.json")
        self.running = None
        self.window = SimpleNamespace(
            running_session=lambda app_type: self.running, rosemary_status=SimpleNamespace(executable=None)
        )
        self.section = MeasureSection(
            self.window, lambda: ("player", {"Preset": "Balanced"}), store=self.store, gpu=lambda: None
        )

    def tearDown(self):
        self._directory.cleanup()

    def _wait(self, condition):
        context = GLib.MainContext.default()
        for _ in range(500):
            while context.pending():
                context.iteration(False)
            if condition():
                return True
            threading.Event().wait(0.01)
        return False

    def test_measuring_needs_a_running_application(self):
        self.section.start_button.emit("clicked")
        self.assertIn("Start Roblox Player from Roseblox first", self.section.message.get_text())
        self.assertFalse(self.section.running)

    def test_a_measurement_is_saved_and_two_can_be_compared(self):
        process = SimpleNamespace(pid=4321, poll=lambda: None)
        self.running = (SimpleNamespace(version="version-abc"), SimpleNamespace(process=process, start_time=None))
        self.section.notes_row.set_text("Crossroads spawn")
        with patch("roseblox.pages.measure_section.measure", return_value=metrics()) as fake:
            self.section.start_button.emit("clicked")
            self.assertTrue(self.section.running)
            self.assertEqual("Stop", self.section.start_button.get_label())
            self.assertTrue(self._wait(lambda: not self.section.running))
            self.assertEqual(4321, fake.call_args.args[0])
            self.assertEqual(60, fake.call_args.args[1])
            self.section.start_button.emit("clicked")
            self.assertTrue(self._wait(lambda: not self.section.running and len(self.section.records) == 2))
        record = self.store.load()[0]
        self.assertEqual("Crossroads spawn", record.notes)
        self.assertEqual("version-abc", record.context["Roblox version"])
        self.assertEqual("Balanced", record.context["Preset"])
        self.assertIn("Saved: CPU 120%", self.section.message.get_text())
        self.assertFalse(self.section.compare_button.get_sensitive())
        rows = [row for row in self.section._record_rows if hasattr(row, "check")]
        rows[0].check.set_active(True)
        rows[1].check.set_active(True)
        self.assertTrue(self.section.compare_button.get_sensitive())
        self.assertIsNotNone(self.section.compare())
        self.section.delete(record.id)
        self.assertEqual(1, len(self.store.load()))
        self.assertFalse(self.section.compare_button.get_sensitive())

    def test_a_failed_measurement_is_reported(self):
        process = SimpleNamespace(pid=4321, poll=lambda: None)
        self.running = (SimpleNamespace(version=None), SimpleNamespace(process=process, start_time=None))
        with patch("roseblox.pages.measure_section.measure", side_effect=MeasureError("The application closed")):
            self.section.start_button.emit("clicked")
            self.assertTrue(self._wait(lambda: not self.section.running))
        self.assertEqual("The application closed", self.section.message.get_text())
        self.assertEqual([], self.store.load())


class RunningSessionTests(unittest.TestCase):
    def test_only_a_running_session_of_the_requested_app_is_returned(self):
        from roseblox.window import RosebloxWindow

        player = SimpleNamespace(id="p", app_type="player")
        studio = SimpleNamespace(id="s", app_type="studio")
        running = SimpleNamespace(process=SimpleNamespace(poll=lambda: None))
        finished = SimpleNamespace(process=SimpleNamespace(poll=lambda: 0))
        window = SimpleNamespace(
            library_records=[player, studio],
            pages={"player": SimpleNamespace(_sessions={"p": running}), "studio": SimpleNamespace(_sessions={"s": finished}),
                   "home": SimpleNamespace()},
        )
        self.assertEqual((player, running), RosebloxWindow.running_session(window, "player"))
        self.assertIsNone(RosebloxWindow.running_session(window, "studio"))


if __name__ == "__main__":
    unittest.main()
