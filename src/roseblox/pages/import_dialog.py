"""Import settings from another launcher: pick the app, preview what carries over, then apply the ticked rows."""

from __future__ import annotations

import threading
from pathlib import Path

import gi
gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gtk

from ..backend import importers
from ..backend.importers import planner
from ..backend.importers.model import INVALID, SKIPPED, TARGET_TITLES, UNSUPPORTED, ImportSourceError

PROBLEM_TITLES = {
    INVALID: ("Invalid", "Values Roseblox rejected, with the reason."),
    UNSUPPORTED: ("Unsupported", "Settings Roseblox has no equivalent for."),
    SKIPPED: ("Skipped", "Settings the source app itself does not use, or that another entry replaces."),
}


def _escape(text: object) -> str:
    return GLib.markup_escape_text(str(text))


def _display_path(path: Path) -> str:
    home = Path.home()
    return f"~/{path.relative_to(home)}" if path.is_relative_to(home) and path != home else str(path)


def _shorten(value: object, limit: int = 80) -> str:
    text = "on" if value is True else "off" if value is False else "not set" if value in (None, "") else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class ImportWindow(Adw.Window):
    def __init__(self, parent, *, stores: planner.Stores | None = None, environ=None, on_imported=None) -> None:
        super().__init__(title="Import from Another App", modal=True, transient_for=parent)
        self.set_default_size(640, 720)
        self.stores = stores or planner.Stores.default()
        self.environ = environ
        self.on_imported = on_imported
        self.plan: planner.ImportPlan | None = None
        self._checks: list[tuple[Gtk.CheckButton, planner.PlannedChange]] = []
        self._busy = False

        self.navigation = Adw.NavigationView()
        self.set_content(self.navigation)
        self.sources_page = self._build_sources_page()
        self.navigation.add(self.sources_page)
        self.detect()

    def _page(self, title: str, tag: str, content: Gtk.Widget, bottom: Gtk.Widget | None = None) -> Adw.NavigationPage:
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        clamp = Adw.Clamp(maximum_size=600)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for margin in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{margin}")(18)
        box.append(content)
        clamp.set_child(box)
        scroller.set_child(clamp)
        toolbar.set_content(scroller)
        if bottom is not None:
            toolbar.add_bottom_bar(bottom)
        return Adw.NavigationPage(title=title, tag=tag, child=toolbar)

    # Sources

    def _build_sources_page(self) -> Adw.NavigationPage:
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        intro = Gtk.Label(
            label="Bring FastFlags and settings over from a launcher you used before. Its files are only read, "
            "never changed, and you see everything before Roseblox saves anything.",
            wrap=True, xalign=0,
        )
        intro.add_css_class("dim-label")
        content.append(intro)
        self.sources_group = Adw.PreferencesGroup(title="Apps")
        content.append(self.sources_group)
        self.sources_spinner = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER)
        content.append(self.sources_spinner)
        self.sources_error = Gtk.Label(wrap=True, xalign=0, visible=False, selectable=True)
        self.sources_error.add_css_class("error")
        content.append(self.sources_error)
        self._source_rows: list[Gtk.Widget] = []
        return self._page("Import from Another App", "sources", content)

    def detect(self) -> None:
        threading.Thread(target=self._detect_worker, daemon=True).start()

    def _detect_worker(self) -> None:
        detections = importers.detect_all(self.environ)
        GLib.idle_add(self._show_sources, detections)

    def _show_sources(self, detections: list[importers.Detection]) -> bool:
        self.sources_spinner.set_visible(False)
        for row in self._source_rows:
            self.sources_group.remove(row)
        self._source_rows = []
        for detection in sorted(detections, key=lambda d: not d.locations):
            row = Adw.ActionRow(title=_escape(detection.importer.title))
            row.set_subtitle_selectable(True)
            if detection.locations:
                found = detection.locations[0]
                more = len(detection.locations) - 1
                row.set_subtitle(
                    _escape(f"Found {_display_path(found)}" + (f" (and {more} more; use Choose… for another)" if more else ""))
                    + "\n" + _escape(detection.importer.description)
                )
                preview = Gtk.Button(label="Preview", valign=Gtk.Align.CENTER)
                preview.add_css_class("suggested-action")
                preview.connect("clicked", lambda _b, d=detection, p=found: self.read(d.importer, p))
                row.add_suffix(preview)
                row.add_prefix(Gtk.Image.new_from_icon_name("object-select-symbolic"))
            else:
                where = "in a Wine, Bottles, Lutris or Steam prefix" if detection.importer.windows else "on this computer"
                hint = "" if detection.importer.id == "client_settings" else f"Not found {where}. "
                row.set_subtitle(
                    _escape(f"{hint}Choose {detection.importer.browse_hint}.")
                    + "\n" + _escape(detection.importer.description)
                )
                row.add_prefix(Gtk.Image.new_from_icon_name("folder-symbolic"))
            browse = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
            browse.set_tooltip_text(f"Choose {detection.importer.browse_hint}")
            browse.connect("clicked", self._browse_clicked, detection.importer)
            row.add_suffix(browse)
            self.sources_group.add(row)
            self._source_rows.append(row)
        return GLib.SOURCE_REMOVE

    def _browse_clicked(self, _button, importer) -> None:
        dialog = Gtk.FileDialog(title=f"Choose {importer.browse_hint}", modal=True)
        if importer.windows or importer.id == "quadra":
            dialog.select_folder(self, None, self._browsed_folder, importer)
        else:
            dialog.open(self, None, self._browsed_file, importer)

    def _browsed_file(self, dialog, result, importer) -> None:
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return
        self.read(importer, Path(file.get_path()))

    def _browsed_folder(self, dialog, result, importer) -> None:
        try:
            file = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        self.read(importer, Path(file.get_path()))

    def read(self, importer, path: Path) -> None:
        if self._busy:
            return
        self._busy = True
        self.sources_error.set_visible(False)
        self.sources_spinner.set_visible(True)
        threading.Thread(target=self._read_worker, args=(importer, path), daemon=True).start()

    def _read_worker(self, importer, path: Path) -> None:
        try:
            result = importers.read_source(importer.id, path)
            outcome, error = planner.plan(result, self.stores), None
        except ImportSourceError as failure:
            outcome, error = None, str(failure)
        except Exception as failure:  # a damaged Roseblox settings file must not leave the window stuck
            outcome, error = None, f"Could not compare with Roseblox's settings: {failure}"
        GLib.idle_add(self._read_finished, outcome, error)

    def _read_finished(self, outcome: planner.ImportPlan | None, error: str | None) -> bool:
        self._busy = False
        self.sources_spinner.set_visible(False)
        if error:
            self.sources_error.set_text(error)
            self.sources_error.set_visible(True)
            return GLib.SOURCE_REMOVE
        self.plan = outcome
        self.navigation.replace([self.sources_page, self._build_preview_page(outcome)])
        return GLib.SOURCE_REMOVE

    # Preview

    def _build_preview_page(self, plan: planner.ImportPlan) -> Adw.NavigationPage:
        self._checks = []
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        source = Adw.PreferencesGroup(title=_escape(plan.result.source))
        source.set_description(_escape(_display_path(plan.result.location)))
        for note in plan.result.notes:
            label = Gtk.Label(label=note, wrap=True, xalign=0)
            label.add_css_class("dim-label")
            source.add(label)
        content.append(source)

        actionable = plan.actionable
        if not actionable:
            empty = Adw.StatusPage(
                icon_name="object-select-symbolic",
                title="Nothing new to import",
                description="Roseblox already has every setting it can take from this app."
                if plan.changes else "This app has nothing Roseblox can use. See below for why.",
            )
            content.append(empty)
        for target, title in TARGET_TITLES.items():
            changes = [change for change in actionable if change.item.target == target]
            if not changes:
                continue
            group = Adw.PreferencesGroup(title=title)
            replacing = sum(1 for change in changes if change.status == planner.REPLACE)
            if replacing:
                group.set_description(
                    f"{replacing} would replace a setting you already have; those are not ticked."
                )
            for change in sorted(changes, key=lambda c: (not c.item.is_flag, c.item.label.lower())):
                group.add(self._change_row(change))
            content.append(group)

        if plan.unchanged:
            group = Adw.PreferencesGroup()
            expander = Adw.ExpanderRow(
                title="Already the same",
                subtitle=f"{len(plan.unchanged)} setting{'s' if len(plan.unchanged) != 1 else ''} Roseblox already has.",
            )
            for change in plan.unchanged:
                row = Adw.ActionRow(
                    title=_escape(change.item.label),
                    subtitle=_escape(f"{TARGET_TITLES[change.item.target]} · {_shorten(change.item.value)}"),
                )
                expander.add_row(row)
            group.add(expander)
            content.append(group)

        if plan.problems:
            group = Adw.PreferencesGroup(title="Not imported")
            for status, (title, description) in PROBLEM_TITLES.items():
                entries = [problem for problem in plan.problems if problem.status == status]
                if not entries:
                    continue
                expander = Adw.ExpanderRow(title=f"{title} ({len(entries)})", subtitle=description)
                for problem in entries:
                    row = Adw.ActionRow(title=_escape(problem.source_key), subtitle=_escape(problem.reason))
                    row.set_subtitle_selectable(True)
                    expander.add_row(row)
                group.add(expander)
            content.append(group)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        for margin in ("top", "bottom", "start", "end"):
            getattr(bar, f"set_margin_{margin}")(12)
        self.selection_label = Gtk.Label(xalign=0, hexpand=True, wrap=True)
        self.selection_label.add_css_class("dim-label")
        bar.append(self.selection_label)
        self.import_button = Gtk.Button(label="Import")
        self.import_button.add_css_class("suggested-action")
        self.import_button.add_css_class("pill")
        self.import_button.connect("clicked", self._import_clicked)
        bar.append(self.import_button)
        self.preview_error = Gtk.Label(wrap=True, xalign=0, visible=False, selectable=True)
        self.preview_error.add_css_class("error")
        content.append(self.preview_error)
        self._selection_changed()
        return self._page("Preview", "preview", content, bar)

    def _change_row(self, change: planner.PlannedChange) -> Adw.ActionRow:
        row = Adw.ActionRow(title=_escape(change.item.label))
        check = Gtk.CheckButton(active=change.selected, valign=Gtk.Align.CENTER)
        check.connect("toggled", lambda *_: self._selection_changed())
        row.add_prefix(check)
        row.set_activatable_widget(check)
        if change.status == planner.REPLACE:
            value = f"{_shorten(change.current)} → {_shorten(change.item.value)} (replaces your setting)"
        else:
            value = _shorten(change.item.value)
        lines = [_escape(value)]
        if change.item.note:
            lines.append(f"<small>{_escape(change.item.note)}</small>")
        row.set_subtitle("\n".join(lines))
        if change.status == planner.REPLACE:
            row.add_suffix(Gtk.Image(icon_name="dialog-warning-symbolic", tooltip_text="Replaces a setting you have"))
        self._checks.append((check, change))
        return row

    def _chosen(self) -> list[planner.PlannedChange]:
        return [change for check, change in self._checks if check.get_active()]

    def _selection_changed(self) -> None:
        chosen = self._chosen()
        replacing = sum(1 for change in chosen if change.status == planner.REPLACE)
        text = f"{len(chosen)} of {len(self._checks)} selected"
        if replacing:
            text += f", {replacing} replacing your settings"
        self.selection_label.set_text(text + ".")
        self.import_button.set_sensitive(bool(chosen) and not self._busy)

    def _import_clicked(self, _button) -> None:
        chosen = self._chosen()
        replacing = [change for change in chosen if change.status == planner.REPLACE]
        if not replacing:
            self._apply(chosen)
            return
        lines = "\n".join(
            f"• {TARGET_TITLES[c.item.target]}: {c.item.label}: {_shorten(c.current, 40)} → {_shorten(c.item.value, 40)}"
            for c in replacing[:12]
        )
        if len(replacing) > 12:
            lines += f"\n• and {len(replacing) - 12} more"
        dialog = Adw.MessageDialog.new(
            self,
            f"Replace {len(replacing)} of your setting{'s' if len(replacing) != 1 else ''}?",
            f"{lines}\n\nRoseblox keeps a backup of the Player and Studio configuration before it changes them.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("replace", "Replace and Import")
        dialog.set_response_appearance("replace", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, response: response == "replace" and self._apply(chosen))
        dialog.present()

    def _apply(self, chosen: list[planner.PlannedChange]) -> None:
        self._busy = True
        self.import_button.set_sensitive(False)
        self.preview_error.set_visible(False)
        source = self.plan.result.source
        threading.Thread(target=self._apply_worker, args=(chosen, source), daemon=True).start()

    def _apply_worker(self, chosen, source: str) -> None:
        try:
            written, error = planner.apply(chosen, self.stores, source), None
        except planner.ImportApplyError as failure:
            written, error = 0, str(failure)
        GLib.idle_add(self._applied, written, error, source)

    def _applied(self, written: int, error: str | None, source: str) -> bool:
        self._busy = False
        if error:
            self.preview_error.set_text(error)
            self.preview_error.set_visible(True)
            self._selection_changed()
            return GLib.SOURCE_REMOVE
        if self.on_imported is not None:
            self.on_imported()
        done = Adw.StatusPage(
            icon_name="object-select-symbolic",
            title=f"Imported {written} setting{'s' if written != 1 else ''}",
            description=f"From {source}. FastFlags and launch options apply the next time Roblox starts. "
            "Importing again later only offers what changed.",
        )
        close = Gtk.Button(label="Done", halign=Gtk.Align.CENTER)
        close.add_css_class("suggested-action")
        close.add_css_class("pill")
        close.connect("clicked", lambda *_: self.close())
        done.set_child(close)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(done)
        self.navigation.push(Adw.NavigationPage(title="Imported", tag="done", child=toolbar))
        return GLib.SOURCE_REMOVE
