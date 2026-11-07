"""Measure a running Roblox session and keep the results for comparison.

Only values the kernel or a GPU driver reports are recorded. Frame rate is not
measured: Roblox does not expose it, and MangoHud's overlay shows it instead.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

MAX_RECORDS = 50
MAX_NOTES = 200
DURATIONS = (30, 60, 120, 300)
SATURATED = 90.0

_CLOCK_TICKS = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


class MeasureError(RuntimeError):
    pass


def _stat_fields(path: Path) -> list[str] | None:
    """Fields of a /proc stat file after the command name, which may contain spaces."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    end = text.rfind(")")
    return text[end + 2 :].split() if end != -1 else None


def process_tree(pid: int, proc: Path = Path("/proc")) -> list[int]:
    """The process and all of its descendants that are still running."""
    children: dict[int, list[int]] = {}
    try:
        entries = [entry for entry in os.listdir(proc) if entry.isdigit()]
    except OSError:
        return []
    for entry in entries:
        fields = _stat_fields(proc / entry / "stat")
        if fields and len(fields) > 1 and fields[1].isdigit():
            children.setdefault(int(fields[1]), []).append(int(entry))
    if not (proc / str(pid)).exists():
        return []
    found, pending = [], [pid]
    while pending and len(found) < 4096:
        current = pending.pop()
        found.append(current)
        pending.extend(children.get(current, ()))
    return found


def thread_cpu_ticks(pid: int, proc: Path = Path("/proc")) -> dict[tuple[int, int], int]:
    """User plus system CPU ticks of every thread of a process."""
    ticks = {}
    try:
        tasks = os.listdir(proc / str(pid) / "task")
    except OSError:
        return ticks
    for task in tasks:
        fields = _stat_fields(proc / str(pid) / "task" / task / "stat")
        if fields and len(fields) > 12 and fields[11].isdigit() and fields[12].isdigit():
            ticks[(pid, int(task))] = int(fields[11]) + int(fields[12])
    return ticks


def process_memory(pid: int, proc: Path = Path("/proc")) -> int | None:
    """Proportional set size in bytes, so memory shared between the processes is counted once."""
    try:
        for line in (proc / str(pid) / "smaps_rollup").read_text(encoding="utf-8").splitlines():
            if line.startswith("Pss:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        return int((proc / str(pid) / "statm").read_text(encoding="utf-8").split()[1]) * _PAGE_SIZE
    except (OSError, ValueError, IndexError):
        return None


@dataclass(frozen=True)
class GpuReading:
    busy_percent: float | None
    memory_bytes: int | None


def amdgpu_reader(drm: Path = Path("/sys/class/drm")) -> Callable[[], GpuReading] | None:
    """Read the amdgpu driver's busy percentage and VRAM use, when such a card exists."""
    cards = []
    try:
        for card in sorted(drm.glob("card[0-9]*")):
            device = card / "device"
            if "-" not in card.name and (device / "gpu_busy_percent").is_file():
                cards.append(device)
    except OSError:
        return None
    if not cards:
        return None

    def read() -> GpuReading:
        busy, memory = [], []
        for device in cards:
            try:
                busy.append(float((device / "gpu_busy_percent").read_text().strip()))
            except (OSError, ValueError):
                pass
            try:
                memory.append(int((device / "mem_info_vram_used").read_text().strip()))
            except (OSError, ValueError):
                pass
        return GpuReading(max(busy) if busy else None, sum(memory) if memory else None)

    return read


def nvidia_reader(which=shutil.which, run=subprocess.run) -> Callable[[], GpuReading] | None:
    """Read utilization and memory use from nvidia-smi, when it is installed."""
    executable = which("nvidia-smi")
    if executable is None:
        return None

    def read() -> GpuReading:
        try:
            result = run(
                [executable, "--query-gpu=utilization.gpu,memory.used", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=5,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            return GpuReading(None, None)
        busy, memory = [], []
        for line in result.stdout.splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) == 2:
                try:
                    busy.append(float(parts[0]))
                    memory.append(int(float(parts[1]) * 1024 * 1024))
                except ValueError:
                    continue
        return GpuReading(max(busy) if busy else None, sum(memory) if memory else None)

    return read


def gpu_reader() -> tuple[str, Callable[[], GpuReading]] | None:
    reader = amdgpu_reader()
    if reader is not None:
        return "amdgpu", reader
    reader = nvidia_reader()
    if reader is not None:
        return "nvidia-smi", reader
    return None


@dataclass(frozen=True)
class Metrics:
    duration: float
    samples: int
    cpu_average: float
    cpu_peak: float
    busiest_thread: float
    memory_average: int | None
    memory_peak: int | None
    gpu_source: str | None = None
    gpu_busy_average: float | None = None
    gpu_busy_peak: float | None = None
    gpu_memory_peak: int | None = None

    @property
    def limit_hint(self) -> str:
        """A cautious reading of what limited the frame rate during the measurement."""
        gpu = self.gpu_busy_average
        if gpu is not None and gpu >= SATURATED:
            return "The GPU was busy almost all the time, so it is probably the limit."
        if self.busiest_thread >= SATURATED:
            return "One CPU thread was busy almost all the time, so the CPU is probably the limit."
        if gpu is None:
            return (
                "No CPU thread was saturated. GPU load is not reported for this GPU, so the GPU, "
                "a frame rate cap or V-Sync may be the limit."
            )
        return "Neither the CPU nor the GPU was saturated, so a frame rate cap or V-Sync is probably the limit."


def measure(
    pid: int,
    duration: float,
    *,
    alive: Callable[[], bool] = lambda: True,
    cancelled: threading.Event | None = None,
    progress: Callable[[float], None] = lambda _fraction: None,
    gpu: tuple[str, Callable[[], GpuReading]] | None = None,
    proc: Path = Path("/proc"),
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    interval: float = 1.0,
) -> Metrics:
    """Sample the process tree once per ``interval`` for ``duration`` seconds."""
    cancelled = cancelled or threading.Event()

    def read_ticks() -> dict[tuple[int, int], int]:
        ticks = {}
        for member in process_tree(pid, proc):
            ticks.update(thread_cpu_ticks(member, proc))
        return ticks

    start = clock()
    first = previous = read_ticks()
    if not previous:
        raise MeasureError("The application is not running.")
    previous_time = start
    cpu_samples, memory_samples, busy_samples, gpu_memory = [], [], [], []
    while True:
        sleep(interval)
        now = clock()
        if cancelled.is_set():
            raise MeasureError("Measuring was stopped.")
        if not alive():
            raise MeasureError("The application closed while it was being measured.")
        current = read_ticks()
        elapsed = max(now - previous_time, 1e-6)
        used = sum(max(ticks - previous.get(key, ticks), 0) for key, ticks in current.items())
        cpu_samples.append(100.0 * used / _CLOCK_TICKS / elapsed)
        memories = [process_memory(member, proc) for member in process_tree(pid, proc)]
        known = [memory for memory in memories if memory is not None]
        if known:
            memory_samples.append(sum(known))
        if gpu is not None:
            reading = gpu[1]()
            if reading.busy_percent is not None:
                busy_samples.append(reading.busy_percent)
            if reading.memory_bytes is not None:
                gpu_memory.append(reading.memory_bytes)
        previous, previous_time = current, now
        progress(min((now - start) / duration, 1.0))
        if now - start >= duration:
            break
    total_time = max(previous_time - start, 1e-6)
    per_thread = [ticks - first.get(key, ticks) for key, ticks in previous.items()]
    return Metrics(
        duration=round(total_time, 1),
        samples=len(cpu_samples),
        cpu_average=round(sum(cpu_samples) / len(cpu_samples), 1),
        cpu_peak=round(max(cpu_samples), 1),
        busiest_thread=round(100.0 * max(per_thread, default=0) / _CLOCK_TICKS / total_time, 1),
        memory_average=int(sum(memory_samples) / len(memory_samples)) if memory_samples else None,
        memory_peak=max(memory_samples) if memory_samples else None,
        gpu_source=gpu[0] if gpu is not None else None,
        gpu_busy_average=round(sum(busy_samples) / len(busy_samples), 1) if busy_samples else None,
        gpu_busy_peak=max(busy_samples) if busy_samples else None,
        gpu_memory_peak=max(gpu_memory) if gpu_memory else None,
    )


@dataclass(frozen=True)
class MeasurementRecord:
    id: str
    created: float
    app_type: str
    notes: str
    context: dict[str, str]
    metrics: Metrics

    def to_document(self) -> dict:
        return {
            "id": self.id,
            "created": self.created,
            "app_type": self.app_type,
            "notes": self.notes,
            "context": dict(self.context),
            "metrics": asdict(self.metrics),
        }

    @classmethod
    def from_document(cls, document: object) -> "MeasurementRecord":
        if not isinstance(document, dict):
            raise ValueError("not an object")
        context = document.get("context", {})
        if not isinstance(context, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in context.items()
        ):
            raise ValueError("bad context")
        metrics = document.get("metrics")
        if not isinstance(metrics, dict):
            raise ValueError("bad metrics")
        known = Metrics.__dataclass_fields__
        return cls(
            id=str(document["id"]),
            created=float(document["created"]),
            app_type=str(document["app_type"]),
            notes=str(document.get("notes", ""))[:MAX_NOTES],
            context=context,
            metrics=Metrics(**{key: value for key, value in metrics.items() if key in known}),
        )


def new_record(app_type: str, notes: str, context: Mapping[str, str], metrics: Metrics, now: float | None = None):
    return MeasurementRecord(
        id=uuid.uuid4().hex,
        created=time.time() if now is None else now,
        app_type=app_type,
        notes=notes.strip()[:MAX_NOTES],
        context=dict(context),
        metrics=metrics,
    )


def default_measurements_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    state = env.get("XDG_STATE_HOME") or str(Path(env.get("HOME") or Path.home()) / ".local" / "state")
    return Path(state) / "roseblox" / "measurements.json"


@dataclass
class MeasurementStore:
    path: Path = field(default_factory=default_measurements_path)

    def load(self) -> list[MeasurementRecord]:
        """Saved measurements, newest first; unreadable entries are skipped."""
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as error:
            raise MeasureError(f"Could not read {self.path}: {error}") from error
        records = []
        for entry in document.get("measurements", []) if isinstance(document, dict) else []:
            try:
                records.append(MeasurementRecord.from_document(entry))
            except (KeyError, TypeError, ValueError):
                continue
        return sorted(records, key=lambda record: record.created, reverse=True)

    def add(self, record: MeasurementRecord) -> list[MeasurementRecord]:
        records = [record, *(existing for existing in self.load() if existing.id != record.id)][:MAX_RECORDS]
        self._write(records)
        return records

    def delete(self, record_id: str) -> list[MeasurementRecord]:
        records = [record for record in self.load() if record.id != record_id]
        self._write(records)
        return records

    def _write(self, records: list[MeasurementRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        document = {"version": 1, "measurements": [record.to_document() for record in records]}
        descriptor, temporary = tempfile.mkstemp(prefix=".measurements-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise


def comparison_rows(first: MeasurementRecord, second: MeasurementRecord) -> list[tuple[str, str, str]]:
    """Label and both values for every setting and metric, for a side-by-side view."""
    from .transfer_rate import format_size

    def percent(value: float | None) -> str:
        return "Not reported" if value is None else f"{value:g}%"

    def size(value: int | None) -> str:
        return "Not reported" if value is None else format_size(value)

    rows = [
        ("Date", time.strftime("%Y-%m-%d %H:%M", time.localtime(first.created)),
         time.strftime("%Y-%m-%d %H:%M", time.localtime(second.created))),
        ("Notes", first.notes or "—", second.notes or "—"),
    ]
    a, b = first.metrics, second.metrics
    rows += [
        ("Duration", f"{a.duration:g} s", f"{b.duration:g} s"),
        ("CPU, average (100% = one core)", percent(a.cpu_average), percent(b.cpu_average)),
        ("CPU, peak", percent(a.cpu_peak), percent(b.cpu_peak)),
        ("Busiest thread", percent(a.busiest_thread), percent(b.busiest_thread)),
        ("Memory, average", size(a.memory_average), size(b.memory_average)),
        ("Memory, peak", size(a.memory_peak), size(b.memory_peak)),
        ("GPU busy, average", percent(a.gpu_busy_average), percent(b.gpu_busy_average)),
        ("GPU busy, peak", percent(a.gpu_busy_peak), percent(b.gpu_busy_peak)),
        ("GPU memory, peak", size(a.gpu_memory_peak), size(b.gpu_memory_peak)),
        ("What limited it", a.limit_hint, b.limit_hint),
    ]
    for key in sorted(set(first.context) | set(second.context)):
        rows.append((key, first.context.get(key, "—"), second.context.get(key, "—")))
    return rows
