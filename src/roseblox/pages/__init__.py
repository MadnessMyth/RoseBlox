"""Page definitions for the Roseblox window."""

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from .home import HomePage
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
    installer,
    rosemary_status,
    library_error: str | None,
    version_settings,
) -> Gtk.Widget:
    """Build a page, retaining the original Home landing view."""
    if page_id == "home":
        return HomePage(window)
    if page_id in ("player", "studio", "library"):
        app_type = page_id if page_id in ("player", "studio") else None
        return InstallationPage(
            title,
            icon_name,
            app_type,
            window=window,
            store=store,
            installer=installer,
            rosemary_status=rosemary_status,
            library_error=library_error,
        )
    if page_id == "settings":
        settings_page = SettingsPage(
            rosemary_status,
            window=window,
            version_settings=version_settings,
            version_client=installer.version_client,
        )
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(settings_page)
        scroller.set_hexpand(True)
        scroller.set_vexpand(True)
        return scroller

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
