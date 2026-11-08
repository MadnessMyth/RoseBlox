"""Wording for the Rosemary rows in setup and Settings, by how this system gets Rosemary."""

from ..backend.rosemary_install import (
    INSTALL_METHOD_APPIMAGE,
    INSTALL_METHOD_AUR,
    INSTALL_METHOD_MANUAL,
    ROSEMARY_NIX_COMMAND,
)

INSTALL_ROW_TITLES = {
    INSTALL_METHOD_AUR: "Install from the AUR",
    INSTALL_METHOD_APPIMAGE: "Download the AppImage",
    INSTALL_METHOD_MANUAL: "Install Rosemary",
}

INSTALL_ROW_SUBTITLES = {
    INSTALL_METHOD_AUR: "Builds the “rosemary” package with paru or yay in a terminal, where you can review it "
    "and enter your password.",
    INSTALL_METHOD_APPIMAGE: "Opens Rosemary's official releases page. Download the file ending in "
    "“x86_64.AppImage”; Roseblox finds it in your Downloads folder.",
    INSTALL_METHOD_MANUAL: "",
}

INSTALL_BUTTON_LABELS = {
    INSTALL_METHOD_AUR: "Install Rosemary",
    INSTALL_METHOD_APPIMAGE: "Open Releases Page",
    INSTALL_METHOD_MANUAL: "Install Rosemary",
}

ABOUT_ROW_SUBTITLES = {
    INSTALL_METHOD_AUR: "Project page and other installation methods",
    INSTALL_METHOD_APPIMAGE: f"Project page and other installation methods. With Nix: {ROSEMARY_NIX_COMMAND}",
    INSTALL_METHOD_MANUAL: "Rosemary publishes AppImages for x86-64 only. On this computer, install it with Nix "
    f"({ROSEMARY_NIX_COMMAND}) or build it from source.",
}


def rosemary_install_method(window) -> str:
    return getattr(window, "rosemary_install_method", INSTALL_METHOD_AUR)
