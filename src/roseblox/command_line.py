"""Command-line parsing shared by the window and the background update checker."""

from __future__ import annotations

import sys
from dataclasses import dataclass

from .backend.setup import APP_TYPES
from .backend.url_handlers import parse_roblox_url

USAGE = "Usage: roseblox [[--roblox-url] URI | --install-update player|studio | --show player|studio]"


@dataclass(frozen=True)
class WindowRequest:
    uri: str | None = None
    install_update: str | None = None
    show: str | None = None


def print_error(command_line, message: str) -> None:
    """Print on the invoking terminal; g_application_command_line_printerr is variadic."""
    printerr = getattr(command_line, "printerr_literal", None)
    if printerr is not None:
        printerr(message)
    else:
        sys.stderr.write(message)


def roblox_url_from_arguments(arguments: list[str]) -> str | None:
    """Return the Roblox link to open, or raise ValueError with a message for the user.

    The app's desktop entry passes links as a bare %u (as "Open with" dialogs do);
    the URL-handler entry passes them after --roblox-url.
    """
    explicit = arguments[:1] == ["--roblox-url"]
    if explicit:
        arguments = arguments[1:]
    if not arguments and not explicit:
        return None
    if len(arguments) != 1 or arguments[0].startswith("-"):
        raise ValueError(USAGE)
    try:
        return parse_roblox_url(arguments[0]).uri
    except ValueError as error:
        raise ValueError(f"Invalid Roblox link: {error}") from error


def parse_window_arguments(arguments: list[str]) -> WindowRequest:
    """``--install-update`` and ``--show`` come from update notifications."""
    if arguments[:1] in (["--install-update"], ["--show"]):
        if len(arguments) != 2 or arguments[1] not in APP_TYPES:
            raise ValueError(USAGE)
        if arguments[0] == "--install-update":
            return WindowRequest(install_update=arguments[1])
        return WindowRequest(show=arguments[1])
    return WindowRequest(uri=roblox_url_from_arguments(arguments))
