"""Running from a Roseblox AppImage: which file to start again, the environment programs
Roseblox starts get, and the app menu entry.

An AppImage's files disappear when the process that mounted it exits, so anything that
must outlive the window (the update checker, the activity watcher, Roblox links) starts
the AppImage file itself rather than the Python inside it.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Mapping

APP_ID = "io.github.roseblox.Roseblox"
DESKTOP_ID = f"{APP_ID}.desktop"
MARKER = "X-Roseblox-AppImage=true"
_PACKAGE = Path(__file__).resolve().parent.parent
_ICON = _PACKAGE / "resources" / "roseblox-icon.svg"
# Set by the AppImage runtime; programs Roseblox starts must not see them.
_RUNTIME_VARIABLES = ("APPDIR", "APPIMAGE", "ARGV0", "OWD")


def appimage_path(environ: Mapping[str, str] | None = None, package: Path | None = None) -> str | None:
    """The Roseblox AppImage file this code runs from, or None outside one.

    AppRun copies the runtime's variables to ROSEBLOX_APPIMAGE and ROSEBLOX_APPDIR. They
    only count when this package really lives inside that AppDir, so a value inherited
    by an unrelated Roseblox installation is ignored.
    """
    environ = os.environ if environ is None else environ
    path, appdir = environ.get("ROSEBLOX_APPIMAGE"), environ.get("ROSEBLOX_APPDIR")
    if not path or not appdir or not Path(path).is_file():
        return None
    try:
        (package or _PACKAGE).relative_to(Path(appdir).resolve())
    except ValueError:
        return None
    return os.path.abspath(path)


def restore_host_environment(environ=None) -> None:
    """Give programs Roseblox starts the environment the AppImage was started with.

    AppRun points the loader, Python and GLib at the bundled files and saves the
    previous values. GLib and gdk-pixbuf read some of them lazily, so their caches are
    filled first; after that the variables are only seen by child processes.
    """
    environ = os.environ if environ is None else environ
    names = environ.pop("ROSEBLOX_HOST_VARS", None)
    if names is None:
        return
    import gi

    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf, Gio, GLib

    GLib.get_system_data_dirs()
    GdkPixbuf.Pixbuf.get_formats()
    Gio.Vfs.get_default()
    Gio.SettingsSchemaSource.get_default()
    Gio.SettingsBackend.get_default()
    for name in names.split():
        saved = environ.pop(f"ROSEBLOX_HOST_{name}", None)
        if saved is None:
            environ.pop(name, None)
        else:
            environ[name] = saved
    for name in _RUNTIME_VARIABLES:
        environ.pop(name, None)


def bundled_tool(name: str, environ: Mapping[str, str] | None = None) -> str | None:
    """A helper program shipped inside the AppImage, such as bsdtar."""
    environ = os.environ if environ is None else environ
    appdir = environ.get("ROSEBLOX_APPDIR")
    if not appdir or appimage_path(environ) is None:
        return None
    path = Path(appdir) / "usr" / "bin" / name
    return str(path) if os.access(path, os.X_OK) else None


def which(name: str) -> str | None:
    """``shutil.which``, except that ``roseblox`` is the AppImage file when running from one."""
    if name == "roseblox":
        appimage = appimage_path()
        if appimage:
            return appimage
    return shutil.which(name) or bundled_tool(name)


def _data_home(environ: Mapping[str, str]) -> Path:
    return Path(environ.get("XDG_DATA_HOME") or Path(environ.get("HOME") or Path.home()) / ".local" / "share")


def _quote(path: str) -> str:
    escaped = path.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return f'"{escaped}"'


def desktop_entry(appimage: str) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Roseblox\n"
        "GenericName=Roblox Launcher\n"
        "Comment=Manage Roblox Player and Studio through Rosemary\n"
        f"Exec={_quote(appimage)} %u\n"
        f"TryExec={appimage}\n"
        f"Icon={APP_ID}\n"
        "Terminal=false\n"
        "StartupNotify=true\n"
        f"StartupWMClass={APP_ID}\n"
        "Categories=Game;\n"
        "Keywords=Roblox;Player;Studio;Rosemary;Games;Launcher;\n"
        f"{MARKER}\n"
    )


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def integrate(environ: Mapping[str, str] | None = None) -> bool:
    """Add or refresh the AppImage's menu entry and icon; return whether anything changed.

    A menu entry that another installation wrote (a package or the install script) is
    left alone.
    """
    environ = os.environ if environ is None else environ
    appimage = appimage_path(environ)
    if appimage is None:
        return False
    data_home = _data_home(environ)
    desktop = data_home / "applications" / DESKTOP_ID
    icon = data_home / "icons" / "hicolor" / "scalable" / "apps" / f"{APP_ID}.svg"
    contents = desktop_entry(appimage)
    try:
        current = desktop.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = None
    except (OSError, UnicodeError):
        return False
    if current is not None and MARKER not in current:
        return False
    changed = False
    if current != contents:
        _write_atomic(desktop, contents.encode("utf-8"))
        changed = True
    if not icon.exists():
        _write_atomic(icon, _ICON.read_bytes())
        changed = True
    return changed


def remove_integration(environ: Mapping[str, str] | None = None) -> None:
    environ = os.environ if environ is None else environ
    data_home = _data_home(environ)
    desktop = data_home / "applications" / DESKTOP_ID
    try:
        if MARKER in desktop.read_text(encoding="utf-8"):
            desktop.unlink()
            (data_home / "icons" / "hicolor" / "scalable" / "apps" / f"{APP_ID}.svg").unlink(missing_ok=True)
    except (OSError, UnicodeError):
        pass
