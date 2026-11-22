"""Networking: a monitor of Roblox servers joined, their regions and ping.

Connections are recorded by the activity watcher into NetworkHistoryStore; this tab only
reads that store. The Refresh button re-pings every known server and fills in any region
the watcher did not look up. Each server row can be removed, and Clear History wipes the
lot. Nothing here launches or changes a game.
"""

from __future__ import annotations

import threading
from datetime import datetime

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..backend.game_info import GameInfoError, locate_server, ping_server  # noqa: E402
from ..backend.network_history import ConnectionRecord, NetworkHistoryStore, RegionStat  # noqa: E402
from ..backend.setup import NetworkSettings  # noqa: E402


class NetworkingPage(Gtk.Box):
    """Server-connection monitor: best regions up top, then every IP connected to."""

    def __init__(
        self,
        window,
        *,
        store: NetworkHistoryStore | None = None,
        ping=ping_server,
        locate=locate_server,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        self.set_hexpand(True)
        self.set_margin_top(28)
        self.set_margin_bottom(28)
        self.set_margin_start(28)
        self.set_margin_end(28)
        self.window = window
        self.store = store or NetworkHistoryStore()
        self.ping = ping
        self.locate = locate
        self._refreshing = False
        self._region_rows: list[Adw.ActionRow] = []
        self._server_rows: list[Adw.ActionRow] = []
        self._server_delete_buttons: list[Gtk.Button] = []

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        heading = Gtk.Label(label="Networking", xalign=0, hexpand=True)
        heading.add_css_class("title-1")
        header.append(heading)
        self.clear_button = Gtk.Button(label="Clear History")
        self.clear_button.add_css_class("destructive-action")
        self.clear_button.set_valign(Gtk.Align.CENTER)
        self.clear_button.set_tooltip_text("Remove every recorded server")
        self.clear_button.connect("clicked", self._on_clear_clicked)
        header.append(self.clear_button)
        self.refresh_button = Gtk.Button(label="Refresh")
        self.refresh_button.add_css_class("suggested-action")
        self.refresh_button.set_valign(Gtk.Align.CENTER)
        self.refresh_button.set_tooltip_text("Re-ping every recorded server and fill in missing regions")
        self.refresh_button.connect("clicked", self._on_refresh)
        header.append(self.refresh_button)
        self.append(header)

        self.summary_label = Gtk.Label(xalign=0, wrap=True)
        self.summary_label.add_css_class("title-3")
        self.append(self.summary_label)

        self.regions_list = self._boxed_list()
        self.append(self._section("Best regions", "Regions you have connected to, ranked by their best ping.", self.regions_list))

        self.servers_list = self._boxed_list()
        self.append(self._section(
            "Servers you connected to",
            "Each public Roblox server IP the watcher recorded, with its last measured ping. "
            "Use the trash button to remove one.",
            self.servers_list,
        ))

        self.empty_label = Gtk.Label(xalign=0, wrap=True)
        self.empty_label.add_css_class("dim-label")
        self.append(self.empty_label)

        self.refresh_view()

    def refresh_view(self) -> None:
        """Re-read the store and redraw the lists. Safe to call from the UI thread."""
        settings = self._network_settings()
        records = self.store.load(retention_days=settings.retention_days)
        regions = self.store.best_regions(retention_days=settings.retention_days)
        self._render_summary(regions)
        self._render_regions(regions)
        self._render_servers(records)
        has_data = bool(records)
        self.regions_list.get_parent().set_visible(has_data)
        self.servers_list.get_parent().set_visible(has_data)
        self.empty_label.set_text(self._empty_text(settings))
        self.empty_label.set_visible(not has_data)

    def _network_settings(self) -> NetworkSettings:
        state = getattr(self.window, "setup_state", None)
        settings = getattr(state, "network_settings", None)
        if isinstance(settings, NetworkSettings):
            return settings
        return NetworkSettings()

    def _empty_text(self, settings: NetworkSettings) -> str:
        base = "No servers recorded yet. Launch a Roblox game from Roseblox and the servers you join will appear here."
        if not settings.record_connections:
            return base + " Recording is turned off in Settings, so new joins will not be stored."
        return base

    def _boxed_list(self) -> Gtk.ListBox:
        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        return listbox

    def _section(self, title: str, description: str, listbox: Gtk.ListBox) -> Gtk.Box:
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        heading = Gtk.Label(label=title, xalign=0)
        heading.add_css_class("title-4")
        column.append(heading)
        detail = Gtk.Label(label=description, xalign=0, wrap=True)
        detail.add_css_class("dim-label")
        column.append(detail)
        column.append(listbox)
        return column

    def _render_summary(self, regions: list[RegionStat]) -> None:
        if not regions:
            self.summary_label.set_text("No connections recorded yet")
            return
        best = regions[0]
        ping = f"{best.best_ping_ms} ms" if best.best_ping_ms is not None else "ping unknown"
        self.summary_label.set_text(f"Best region: {best.region} · {ping}")

    def _render_regions(self, regions: list[RegionStat]) -> None:
        self._clear_list(self.regions_list, self._region_rows)
        for stat in regions:
            row = Adw.ActionRow.new()
            row.set_title(stat.region)
            row.set_subtitle(self._region_subtitle(stat))
            self.regions_list.append(row)
            self._region_rows.append(row)

    def _render_servers(self, records: list[ConnectionRecord]) -> None:
        self._clear_list(self.servers_list, self._server_rows)
        self._server_delete_buttons.clear()
        for record in records:
            row = Adw.ActionRow.new()
            row.set_title(record.address)
            row.set_subtitle_selectable(True)
            row.set_subtitle(self._server_subtitle(record))
            delete = Gtk.Button.new_from_icon_name("user-trash-symbolic")
            delete.set_tooltip_text("Remove this server")
            delete.set_valign(Gtk.Align.CENTER)
            delete.add_css_class("destructive-action")
            delete.connect("clicked", self._on_delete_clicked, record.address)
            row.add_suffix(delete)
            self.servers_list.append(row)
            self._server_rows.append(row)
            self._server_delete_buttons.append(delete)

    def _clear_list(self, listbox: Gtk.ListBox, rows: list) -> None:
        for row in rows:
            listbox.remove(row)
        rows.clear()

    def _region_subtitle(self, stat: RegionStat) -> str:
        ping = f"{stat.best_ping_ms} ms" if stat.best_ping_ms is not None else "ping unknown"
        when = self._format_time(stat.last_seen)
        noun = "connection" if stat.connections == 1 else "connections"
        return f"best {ping} · {stat.connections} {noun} · last {when}"

    def _server_subtitle(self, record: ConnectionRecord) -> str:
        parts: list[str] = []
        if record.region:
            parts.append(record.region)
        if record.last_ping_ms is not None:
            parts.append(f"{record.last_ping_ms} ms")
        elif record.best_ping_ms is not None:
            parts.append(f"best {record.best_ping_ms} ms")
        else:
            parts.append("ping unknown")
        if record.place_id is not None:
            parts.append(f"place {record.place_id}")
        parts.append(f"last {self._format_time(record.last_seen)}")
        parts.append(f"×{record.connections}")
        return " · ".join(parts)

    def _format_time(self, timestamp: float) -> str:
        if not timestamp:
            return "unknown"
        try:
            return datetime.fromtimestamp(timestamp).astimezone().strftime("%Y-%m-%d %H:%M")
        except (OSError, ValueError, OverflowError):
            return "unknown"

    def _on_delete_clicked(self, _button, address: str) -> None:
        try:
            self.store.delete(address)
        except OSError:
            return
        self.refresh_view()

    def _on_clear_clicked(self, _button) -> None:
        if not self.store.load():
            return
        dialog = Adw.MessageDialog.new(
            self.window,
            "Clear server history?",
            "All recorded servers will be removed. New joins will be recorded again.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("clear", "Clear")
        dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._on_clear_response)
        dialog.present()

    def _on_clear_response(self, dialog, response: str) -> None:
        dialog.destroy()
        if response != "clear":
            return
        try:
            self.store.clear()
        except OSError:
            return
        self.refresh_view()

    def _on_refresh(self, _button) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        self.refresh_button.set_sensitive(False)
        self.refresh_button.set_label("Refreshing…")
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self) -> None:
        """Re-ping every recorded server off the UI thread; locate any unknown regions."""
        settings = self._network_settings()
        for record in self.store.load(retention_days=settings.retention_days):
            region = record.region
            if region is None:
                try:
                    region = self.locate(record.address)
                except GameInfoError:
                    region = None
            ping_ms = self.ping(record.address)
            if ping_ms is not None or region is not None:
                try:
                    self.store.update_ping(record.address, region=region, ping_ms=ping_ms)
                except OSError:
                    pass
        GLib.idle_add(self._refresh_finished)

    def _refresh_finished(self) -> bool:
        self._refreshing = False
        self.refresh_button.set_sensitive(True)
        self.refresh_button.set_label("Refresh")
        self.refresh_view()
        return GLib.SOURCE_REMOVE
