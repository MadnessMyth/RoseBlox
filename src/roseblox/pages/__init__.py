"""Page definitions for the Roseblox window."""

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from .manager_page import InstallationPage, SettingsPage

PAGE_DEFINITIONS = (
    ("home", "Home", "go-home-symbolic", "A quick overview of your Roblox installations."),
    ("player", "Player", "applications-games-symbolic", "Manage and launch Roblox Player."),
    ("studio", "Studio", "applications-graphics-symbolic", "Manage and launch Roblox Studio."),
    ("library", "Library", "folder-symbolic", "View your registered Player and Studio installations."),
    ("settings", "Settings", "emblem-system-symbolic", "Configure Roseblox and its Rosemary integration."),
)


def create_page(
    page_id: str,
    title: str,
    icon_name: str,
    description: str,
    *,
    window: Gtk.Window,
    store,
    rosemary_status,
    library_error: str | None,
) -> Gtk.Widget:
    """Build a page, retaining the original Home landing view."""
    if page_id in ("player", "studio", "library"):
        app_type = page_id if page_id in ("player", "studio") else None
        return InstallationPage(
            title,
            icon_name,
            app_type,
            window=window,
            store=store,
            rosemary_status=rosemary_status,
            library_error=library_error,
        )
    if page_id == "settings":
        return SettingsPage(rosemary_status)

    page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    page.set_halign(Gtk.Align.CENTER)
    page.set_valign(Gtk.Align.CENTER)
    page.set_hexpand(True)
    page.set_vexpand(True)
    page.set_margin_top(40)
    page.set_margin_bottom(40)
    page.set_margin_start(40)
    page.set_margin_end(40)
    page.add_css_class("page-content")
    page.set_name(f"page-{page_id}")

    icon = Gtk.Image.new_from_icon_name(icon_name)
    icon.set_pixel_size(48)
    icon.add_css_class("page-icon")
    page.append(icon)

    heading = Gtk.Label(label=title)
    heading.add_css_class("title-1")
    page.append(heading)

    detail = Gtk.Label(label=description)
    detail.add_css_class("dim-label")
    detail.set_wrap(True)
    detail.set_justify(Gtk.Justification.CENTER)
    detail.set_max_width_chars(52)
    page.append(detail)
    return page
