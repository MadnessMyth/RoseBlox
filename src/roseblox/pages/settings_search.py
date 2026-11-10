"""Search for the Settings page: shows only the sections, groups and rows that match what was typed."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gtk

from .sections import CollapsibleSection


def _rows(widget: Gtk.Widget) -> Iterator[Adw.PreferencesRow]:
    """The rows of a preferences group in order, without the rows inside its expander rows."""
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Adw.PreferencesRow):
            yield child
        else:
            yield from _rows(child)
        child = child.get_next_sibling()


def _row_text(row: Adw.PreferencesRow) -> str:
    subtitle = row.get_subtitle() if hasattr(row, "get_subtitle") else ""
    return f"{row.get_title() or ''} {subtitle or ''}"


class SettingsSearch:
    """Filters the Settings page down to what a search matches.

    A row matches when each word typed starts a word of its title or subtitle, or of the section, group or
    expander row around it, the way GNOME's own settings searches match. Rows the page hides stay hidden; when one
    of them matches, the row whose switch shows it is listed instead, with a hint naming the hidden row, so
    "yellow" finds Server ping until Server ping is on. Rows are left out through their list's filter rather than
    hidden, so the page keeps showing and hiding rows as before while a search is open, and once listed, a row stays
    listed until the search changes.
    """

    def __init__(
        self,
        sections: Sequence[CollapsibleSection],
        *,
        expander_rows: Mapping[Adw.ExpanderRow, Sequence[Adw.PreferencesRow]],
        reveals: Mapping[Adw.PreferencesRow, Sequence[Adw.PreferencesRow]],
        empty: Gtk.Widget,
        hint: Gtk.Label,
    ) -> None:
        self.sections = list(sections)
        self.expander_rows = expander_rows
        self.reveals = reveals
        self.empty = empty
        self.hint = hint
        self.query = ""
        self._shown: set[Gtk.Widget] = set()
        self._hidden: set[Gtk.Widget] = set()
        self._opened: set[Adw.ExpanderRow] = set()
        self._expanded_before: dict[str, bool] | None = None
        self._refresh_pending = False
        self._index: list[tuple[CollapsibleSection, list[tuple[Adw.PreferencesGroup, list[Adw.PreferencesRow]]]]] = []
        self._lists: dict[Gtk.ListBox, None] = {}
        for section in self.sections:
            groups = []
            child = section.content.get_first_child()
            while child is not None:
                if isinstance(child, Adw.PreferencesGroup):
                    rows = list(_rows(child))
                    groups.append((child, rows))
                    for row in rows:
                        self._watch(row)
                        for inner in self.expander_rows.get(row, ()):
                            self._watch(inner)
                child = child.get_next_sibling()
            self._index.append((section, groups))

    def set_query(self, text: str) -> None:
        query = " ".join(text.split())
        if query == self.query:
            return
        if query and self._expanded_before is None:
            self._expanded_before = {section.section_id: section.expanded for section in self.sections}
        self.query = query
        self._shown = set()
        self._apply()
        if not query and self._expanded_before is not None:
            for section in self.sections:
                section.set_expanded(self._expanded_before.get(section.section_id, section.expanded))
            self._expanded_before = None

    def remember_expanded(self, section_id: str, expanded: bool) -> bool:
        """Keeps a section's open or closed state for when the search is cleared; True while searching."""
        if self._expanded_before is None:
            return False
        self._expanded_before[section_id] = expanded
        return True

    def _watch(self, row: Adw.PreferencesRow) -> None:
        listbox = row.get_parent()
        if isinstance(listbox, Gtk.ListBox) and listbox not in self._lists:
            listbox.set_filter_func(self._filter)
            self._lists[listbox] = None
        row.connect("notify::visible", self._visibility_changed)

    def _filter(self, row: Gtk.ListBoxRow) -> bool:
        return not self.query or row in self._shown

    def _visibility_changed(self, _row, _pspec) -> None:
        if self.query and not self._refresh_pending:
            self._refresh_pending = True
            GLib.idle_add(self._refresh)

    def _refresh(self) -> bool:
        self._refresh_pending = False
        if self.query:
            self._apply()
        return GLib.SOURCE_REMOVE

    def _apply(self) -> None:
        shown = set(self._shown)
        opened: set[Adw.ExpanderRow] = set()
        under: dict[str, None] = {}
        found = False
        for section, groups in self._index:
            section_found = False
            for group, rows in groups:
                context = " ".join(
                    (section.title_label.get_label(), group.get_title() or "", group.get_description() or "")
                )
                group_found = False
                for row in rows:
                    if not self.query or not row.get_visible():
                        continue
                    found_in = self._collect(row, context, shown, under)
                    if found_in is None and row not in shown:
                        continue
                    group_found = True
                    if row in self.expander_rows and self._opens(row, found_in):
                        opened.add(row)
                self._show(group, group_found or not self.query)
                section_found = section_found or group_found
            self._show(section, section_found or not self.query)
            if section_found:
                section.set_expanded(True)
            found = found or section_found
        for expander in self._opened - opened:
            expander.set_expanded(False)
        for expander in opened - self._opened:
            expander.set_expanded(True)
        self._opened = opened
        self._shown = shown
        for listbox in self._lists:
            listbox.invalidate_filter()
        self.empty.set_visible(bool(self.query) and not found)
        self.hint.set_label("\n".join(under))
        self.hint.set_visible(bool(under))

    def _show(self, widget: Gtk.Widget, listed: bool) -> None:
        """Hides a group or section that has no results, and shows again only what the search hid."""
        if not listed and widget.get_visible():
            widget.set_visible(False)
            self._hidden.add(widget)
        elif listed and widget in self._hidden:
            self._hidden.discard(widget)
            widget.set_visible(True)

    def _collect(self, row: Adw.PreferencesRow, context: str, shown: set, under: dict[str, None]) -> str | None:
        """Adds a matching row to ``shown``, and says whether the row itself ("row") or only rows inside it
        ("inner") matched. An expander row that matches brings every row inside it; otherwise only those that do.
        A hidden row that matched is named in ``under``."""
        inner = [child for child in self.expander_rows.get(row, ()) if child.get_visible()]
        if self._found(row, context, under):
            shown.add(row)
            shown.update(inner)
            return "row"
        context = f"{context} {_row_text(row)}"
        matching = [child for child in inner if self._found(child, context, under)]
        if not matching:
            return None
        shown.add(row)
        shown.update(matching)
        return "inner"

    def _found(self, row: Adw.PreferencesRow, context: str, under: dict[str, None]) -> bool:
        match = self._match(row, context)
        if match is not None and match is not row:
            under[f"“{match.get_title()}” is under “{row.get_title()}”."] = None
        return match is not None

    def _match(self, row: Adw.PreferencesRow, context: str) -> Adw.PreferencesRow | None:
        """The row the search matches: this one, or a hidden row that its switch shows."""
        if GLib.str_match_string(self.query, f"{context} {_row_text(row)}", True):
            return row
        for hidden in self.reveals.get(row, ()):
            if not hidden.get_visible():
                match = self._match(hidden, context)
                if match is not None:
                    return match
        return None

    def _opens(self, expander: Adw.ExpanderRow, found_in: str | None) -> bool:
        """Whether the search keeps this expander row open, or opens it, to show the matching rows inside.

        One that was already open is left to the user, and one whose switch is off cannot open.
        """
        if expander in self._opened:
            return True
        return found_in == "inner" and expander.get_enable_expansion() and not expander.get_expanded()
