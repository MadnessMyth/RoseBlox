"""Measure the running application and compare saved measurements."""

from __future__ import annotations

import threading
import time

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gtk, Pango

from ..backend import diagnostics
from ..backend.measure import (
    DURATIONS,
    MAX_NOTES,
    MeasureError,
    MeasurementStore,
    comparison_rows,
    gpu_reader,
    measure,
    new_record,
)
from ..backend.rosemary import read_process_snapshot
from ..backend.transfer_rate import format_size
from .sections import CollapsibleSection

APP_TITLES = {"player": "Roblox Player", "studio": "Roblox Studio"}
RECORDS_SHOWN = 20


def describe_duration(seconds: int) -> str:
    return f"{seconds} seconds" if seconds < 60 else f"{seconds // 60} minute{'s' if seconds >= 120 else ''}"


def summary(metrics) -> str:
    parts = [f"CPU {metrics.cpu_average:g}%", f"busiest thread {metrics.busiest_thread:g}%"]
    if metrics.memory_average is not None:
        parts.append(f"memory {format_size(metrics.memory_average)}")
    if metrics.gpu_busy_average is not None:
        parts.append(f"GPU {metrics.gpu_busy_average:g}%")
    return " · ".join(parts)


class MeasureSection(CollapsibleSection):
    """Records CPU, memory and GPU load while the user plays, then compares two runs.

    ``context`` returns ``(app_type, settings)`` describing the configuration in use;
    ``window.running_session(app_type)`` returns the running ``(record, session)``.
    """

    def __init__(self, window, context, *, store: MeasurementStore | None = None, gpu=gpu_reader) -> None:
        super().__init__(
            "measure",
            "Measure & compare",
            "Record CPU, memory and GPU load while you play, then compare two configurations.",
            expanded=False,
        )
        self.window = window
        self.context = context
        self.store = store or MeasurementStore()
        self.gpu = gpu
        self.records = []
        self.selected: list[str] = []
        self.running = False
        self._cancel: threading.Event | None = None

        intro = Gtk.Label(xalign=0, wrap=True)
        intro.set_text(
            "Start the application, go to the same place in the same game for each test, then measure. "
            "Roblox does not report its frame rate; turn on the MangoHud overlay to see FPS and frame times. "
            "GPU load covers the whole GPU and is shown only where its driver reports it (amdgpu, or nvidia-smi)."
        )
        intro.add_css_class("dim-label")
        self.add(intro)

        group = Adw.PreferencesGroup.new()
        self.duration_row = Adw.ComboRow.new()
        self.duration_row.set_title("Duration")
        self.duration_row.set_model(Gtk.StringList.new([describe_duration(seconds) for seconds in DURATIONS]))
        self.duration_row.set_selected(1)
        group.add(self.duration_row)
        self.notes_row = Adw.EntryRow.new()
        self.notes_row.set_title("Notes, such as the game and what you did")
        group.add(self.notes_row)
        self.add(group)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.start_button = Gtk.Button(label="Start Measuring")
        self.start_button.add_css_class("suggested-action")
        self.start_button.connect("clicked", self._start_clicked)
        actions.append(self.start_button)
        self.compare_button = Gtk.Button(label="Compare Selected")
        self.compare_button.set_tooltip_text("Select two measurements below")
        self.compare_button.set_sensitive(False)
        self.compare_button.connect("clicked", lambda _button: self.compare())
        actions.append(self.compare_button)
        self.add(actions)
        self.progress = Gtk.ProgressBar(visible=False, show_text=True)
        self.add(self.progress)
        self.message = Gtk.Label(xalign=0, wrap=True, visible=False, selectable=True)
        self.add(self.message)

        self.records_group = Adw.PreferencesGroup.new()
        self.records_group.set_title("Saved measurements")
        self._record_rows: list[Gtk.Widget] = []
        self.add(self.records_group)
        self.load()

    def load(self) -> None:
        try:
            self.records = self.store.load()
        except MeasureError as error:
            self.records = []
            self._say(str(error), error=True)
        self._show_records()

    def _say(self, text: str, *, error: bool = False) -> None:
        self.message.set_text(text)
        self.message.set_visible(bool(text))
        if error:
            self.message.add_css_class("error")
        else:
            self.message.remove_css_class("error")

    def _start_clicked(self, _button) -> None:
        if self.running:
            if self._cancel is not None:
                self._cancel.set()
            return
        app_type, settings = self.context()
        running = self.window.running_session(app_type) if hasattr(self.window, "running_session") else None
        if running is None:
            self._say(f"Start {APP_TITLES[app_type]} from Roseblox first, then measure while it runs.", error=True)
            return
        record, session = running
        settings = dict(settings)
        if getattr(record, "version", None):
            settings["Roblox version"] = record.version
        duration = DURATIONS[self.duration_row.get_selected()]
        notes = self.notes_row.get_text()[:MAX_NOTES]
        self.running = True
        self._cancel = threading.Event()
        self.start_button.set_label("Stop")
        self.start_button.remove_css_class("suggested-action")
        self.progress.set_fraction(0)
        self.progress.set_text(f"Measuring {APP_TITLES[app_type]}…")
        self.progress.set_visible(True)
        self._say("")
        executable = getattr(getattr(self.window, "rosemary_status", None), "executable", None)
        threading.Thread(
            target=self._worker,
            args=(app_type, settings, session, duration, notes, executable, self._cancel),
            daemon=True,
        ).start()

    def _worker(self, app_type, settings, session, duration, notes, executable, cancel) -> None:
        pid = session.process.pid

        def alive() -> bool:
            if session.process.poll() is not None:
                return False
            if session.start_time is None:
                return True
            snapshot = read_process_snapshot(pid)
            return snapshot is not None and snapshot.start_time == session.start_time

        try:
            settings["Rosemary"] = diagnostics.rosemary_version(executable)
            metrics = measure(
                pid,
                duration,
                alive=alive,
                cancelled=cancel,
                progress=lambda fraction: GLib.idle_add(self.progress.set_fraction, fraction),
                gpu=self.gpu() if callable(self.gpu) else self.gpu,
            )
            records = self.store.add(new_record(app_type, notes, settings, metrics))
            GLib.idle_add(self._finished, records, None)
        except (MeasureError, OSError) as error:
            GLib.idle_add(self._finished, None, str(error))

    def _finished(self, records, error: str | None) -> bool:
        self.running = False
        self._cancel = None
        self.start_button.set_label("Start Measuring")
        self.start_button.add_css_class("suggested-action")
        self.progress.set_visible(False)
        if error is not None:
            self._say(error, error=True)
        else:
            self.records = records
            newest = records[0].metrics
            self._say(f"Saved: {summary(newest)}. {newest.limit_hint}")
            self._show_records()
        return GLib.SOURCE_REMOVE

    def _show_records(self) -> None:
        for row in self._record_rows:
            self.records_group.remove(row)
        self._record_rows = []
        known = {record.id for record in self.records}
        self.selected = [record_id for record_id in self.selected if record_id in known]
        if not self.records:
            row = Adw.ActionRow.new()
            row.set_title("No measurements yet")
            row.set_subtitle("Each measurement is saved here with the settings it was taken with.")
            self.records_group.add(row)
            self._record_rows.append(row)
        for record in self.records[:RECORDS_SHOWN]:
            row = Adw.ActionRow.new()
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(record.created))
            preset = record.context.get("Preset", "")
            title = " · ".join(part for part in (when, APP_TITLES.get(record.app_type, record.app_type), preset) if part)
            row.set_title(GLib.markup_escape_text(title))
            subtitle = summary(record.metrics)
            if record.notes:
                subtitle = f"{record.notes}\n{subtitle}"
            row.set_subtitle(GLib.markup_escape_text(subtitle))
            check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
            check.set_tooltip_text("Select for comparison")
            check.set_active(record.id in self.selected)
            check.connect("toggled", self._selection_toggled, record.id)
            row.add_prefix(check)
            row.set_activatable_widget(check)
            delete = Gtk.Button.new_from_icon_name("user-trash-symbolic")
            delete.add_css_class("flat")
            delete.set_valign(Gtk.Align.CENTER)
            delete.set_tooltip_text("Delete this measurement")
            delete.connect("clicked", lambda _button, record_id=record.id: self.delete(record_id))
            row.add_suffix(delete)
            row.check = check
            row.record_id = record.id
            self.records_group.add(row)
            self._record_rows.append(row)
        self.compare_button.set_sensitive(len(self.selected) == 2)

    def _selection_toggled(self, check, record_id: str) -> None:
        if check.get_active():
            if record_id not in self.selected:
                self.selected.append(record_id)
            while len(self.selected) > 2:
                dropped = self.selected.pop(0)
                for row in self._record_rows:
                    if getattr(row, "record_id", None) == dropped:
                        row.check.set_active(False)
        elif record_id in self.selected:
            self.selected.remove(record_id)
        self.compare_button.set_sensitive(len(self.selected) == 2)

    def delete(self, record_id: str) -> None:
        try:
            self.records = self.store.delete(record_id)
        except (MeasureError, OSError) as error:
            self._say(f"Could not delete the measurement: {error}", error=True)
            return
        self._show_records()

    def compare(self) -> Gtk.Grid | None:
        by_id = {record.id: record for record in self.records}
        chosen = [by_id[record_id] for record_id in self.selected if record_id in by_id]
        if len(chosen) != 2:
            return None
        first, second = sorted(chosen, key=lambda record: record.created)
        grid = Gtk.Grid(column_spacing=18, row_spacing=6)
        for column, text in enumerate(("", "Older", "Newer")):
            label = Gtk.Label(label=text, xalign=0)
            label.add_css_class("heading")
            grid.attach(label, column, 0, 1, 1)
        for index, (name, older, newer) in enumerate(comparison_rows(first, second), start=1):
            for column, text in enumerate((name, older, newer)):
                label = Gtk.Label(label=text, xalign=0, yalign=0, wrap=True)
                label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
                label.set_max_width_chars(34 if column else 24)
                if column == 0:
                    label.add_css_class("dim-label")
                elif older != newer:
                    label.add_css_class("accent")
                grid.attach(label, column, index, 1, 1)
        scroller = Gtk.ScrolledWindow(min_content_height=420, min_content_width=640)
        scroller.set_child(grid)
        parent = self.window if isinstance(self.window, Gtk.Window) else self.get_root()
        dialog = Adw.MessageDialog.new(
            parent if isinstance(parent, Gtk.Window) else None,
            "Compare measurements",
            "Differences are highlighted. Measurements vary from run to run, so compare runs of the same "
            "length in the same place, and repeat a test before drawing conclusions.",
        )
        dialog.set_extra_child(scroller)
        dialog.add_response("close", "Close")
        dialog.present()
        return grid
