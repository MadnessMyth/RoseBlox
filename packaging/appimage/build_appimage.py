#!/usr/bin/env python3
"""Build dist/Roseblox-x86_64.AppImage from this checkout.

Run on Ubuntu 24.04 (x86-64) with the packages the "Build packages" step of
.github/workflows/release.yml installs, and appimagetool. The AppImage carries the build machine's Python, PyGObject, GTK 4 and
libadwaita with every library they need, except the ones every desktop has and that
must match the system (glibc, graphics drivers, X11/Wayland clients, fontconfig).
It therefore runs on systems with glibc 2.39 or newer.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_ID = "io.github.roseblox.Roseblox"
OUTPUT_NAME = "Roseblox-x86_64.AppImage"
UPDATE_INFORMATION = f"gh-releases-zsync|MadnessMyth|RoseBlox|latest|{OUTPUT_NAME}.zsync"
MULTIARCH = "/usr/lib/x86_64-linux-gnu"
PYTHON = f"python{sys.version_info.major}.{sys.version_info.minor}"
TYPELIBS = ("Adw-1", "Gtk-4.0", "Gdk-4.0", "Gio-2.0", "GLib-2.0", "Pango-1.0", "GdkPixbuf-2.0")
SCHEMAS = "org.gtk.gtk4.Settings.*.gschema.xml"
STDLIB_SKIP = {
    "test", "idlelib", "tkinter", "turtledemo", "ensurepip", "lib2to3", "pydoc_data",
    f"config-{sys.version_info.major}.{sys.version_info.minor}-x86_64-linux-gnu", "site-packages",
    "dist-packages",
}

# Libraries taken from the system: the C runtime, graphics drivers and what they load,
# display clients, and fonts, following the AppImage project's exclude list.
EXCLUDED = {
    "ld-linux-x86-64.so.2", "libc.so.6", "libm.so.6", "libdl.so.2", "libpthread.so.0", "librt.so.1",
    "libresolv.so.2", "libutil.so.1", "libanl.so.1", "libmvec.so.1", "libnsl.so.1", "libthread_db.so.1",
    "libBrokenLocale.so.1", "libgcc_s.so.1", "libstdc++.so.6",
    "libGL.so.1", "libGLX.so.0", "libGLdispatch.so.0", "libOpenGL.so.0", "libEGL.so.1", "libGLESv2.so.2",
    "libgbm.so.1", "libdrm.so.2", "libvulkan.so.1",
    "libX11.so.6", "libX11-xcb.so.1", "libxcb.so.1", "libXau.so.6", "libXdmcp.so.6",
    "libwayland-client.so.0", "libwayland-cursor.so.0", "libwayland-egl.so.1", "libwayland-server.so.0",
    "libfontconfig.so.1", "libfreetype.so.6", "libharfbuzz.so.0", "libexpat.so.1", "libz.so.1",
    "libasound.so.2", "libdbus-1.so.3", "libsystemd.so.0", "libudev.so.1",
    "libgpg-error.so.0", "libgcrypt.so.20", "libcom_err.so.2",
}
_LDD_LINE = re.compile(r"^\s*(\S+)\s+=>\s+(\S+)\s+\(0x[0-9a-f]+\)$")
_LIBRARY_NAME = re.compile(rb"lib[A-Za-z0-9_+.-]+?\.so\.\d+")
_NAMESPACE = re.compile(rb"([A-Z][A-Za-z0-9]*-\d+\.\d+)")


def run(*command: str | Path, **options) -> subprocess.CompletedProcess:
    return subprocess.run([str(part) for part in command], check=True, **options)


def version() -> str:
    text = (ROOT / "src" / "roseblox" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__\s*=\s*"([^"]+)"', text).group(1)


class AppDir:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.usr = path / "usr"
        self.libs = self.usr / "lib" / "libs"
        self.elves: list[Path] = []

    def copy(self, source: str | Path, target: str | Path, *, elf: bool = False) -> Path:
        destination = self.path / target
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(source).resolve(), destination)
        if elf:
            self.elves.append(destination)
        return destination

    def copy_tree(self, source: Path, target: str, skip=frozenset()) -> None:
        destination = self.path / target

        def ignore(directory: str, names: list[str]) -> set[str]:
            return {name for name in names if name in skip or name == "__pycache__" and target.startswith("usr/share")}

        shutil.copytree(source, destination, symlinks=False, ignore=ignore, dirs_exist_ok=True)
        for path in destination.rglob("*.so*"):
            if path.is_file():
                self.elves.append(path)

    def bundle_libraries(self) -> list[str]:
        """Copy every shared library the bundled programs need, transitively."""
        self.libs.mkdir(parents=True, exist_ok=True)
        done: set[Path] = set()
        bundled: list[str] = []
        pending = list(self.elves)
        while pending:
            binary = pending.pop()
            if binary in done or not binary.exists():
                continue
            done.add(binary)
            result = subprocess.run(["ldd", str(binary)], capture_output=True, text=True)
            output = result.stdout
            if result.returncode != 0 and "not a dynamic executable" not in result.stdout + result.stderr:
                raise SystemExit(f"ldd failed for {binary}:\n{result.stderr}")
            if "not found" in output:
                raise SystemExit(f"{binary} needs a library that is not installed:\n{output}")
            for line in output.splitlines():
                match = _LDD_LINE.match(line)
                if not match or match.group(1) in EXCLUDED:
                    continue
                name, source = match.groups()
                target = self.libs / name
                if not target.exists():
                    shutil.copy2(Path(source).resolve(), target)
                    bundled.append(name)
                    pending.append(target)
        return sorted(bundled)


def find_library(name: str) -> Path:
    for directory in (MULTIARCH, "/usr/lib", "/lib/x86_64-linux-gnu"):
        candidate = Path(directory) / name
        if candidate.exists():
            return candidate
    raise SystemExit(f"Shared library {name} is not installed.")


def typelib_closure() -> list[Path]:
    directory = Path(MULTIARCH) / "girepository-1.0"
    seen: set[str] = set()
    pending = list(TYPELIBS)
    while pending:
        namespace = pending.pop()
        if namespace in seen or not (directory / f"{namespace}.typelib").exists():
            continue
        seen.add(namespace)
        data = (directory / f"{namespace}.typelib").read_bytes()
        pending.extend(match.decode() for match in _NAMESPACE.findall(data))
    return [directory / f"{namespace}.typelib" for namespace in sorted(seen)]


def build_appdir(appdir: AppDir) -> None:
    usr = appdir.usr
    # Python and the parts of its standard library a desktop app can use.
    appdir.copy(f"/usr/bin/{PYTHON}", "usr/bin/python3", elf=True)
    appdir.copy_tree(Path(f"/usr/lib/{PYTHON}"), f"usr/lib/{PYTHON}", STDLIB_SKIP)
    for path in (usr / "lib" / PYTHON / "lib-dynload").glob("_tkinter*"):
        path.unlink()
    appdir.copy_tree(Path("/usr/lib/python3/dist-packages/gi"), "usr/lib/python3/dist-packages/gi")

    # GObject introspection data and the libraries it names.
    for typelib in typelib_closure():
        appdir.copy(typelib, f"usr/lib/girepository-1.0/{typelib.name}")
        for name in sorted(set(_LIBRARY_NAME.findall(typelib.read_bytes()))):
            library = name.decode()
            if library not in EXCLUDED:
                appdir.copy(find_library(library), f"usr/lib/libs/{library}", elf=True)

    # gdk-pixbuf for SVG icons, GTK's settings schemas and icon themes.
    pixbuf = Path(MULTIARCH) / "gdk-pixbuf-2.0"
    appdir.copy(pixbuf / "gdk-pixbuf-query-loaders", "usr/lib/gdk-pixbuf/gdk-pixbuf-query-loaders", elf=True)
    for loader in (pixbuf / "2.10.0" / "loaders").glob("libpixbufloader-*.so"):
        if loader.name in {"libpixbufloader-svg.so", "libpixbufloader-png.so", "libpixbufloader-jpeg.so"}:
            appdir.copy(loader, f"usr/lib/gdk-pixbuf/loaders/{loader.name}", elf=True)
    (usr / "lib" / "gio" / "modules").mkdir(parents=True, exist_ok=True)
    schemas = usr / "share" / "glib-2.0" / "schemas"
    for schema in Path("/usr/share/glib-2.0/schemas").glob(SCHEMAS):
        appdir.copy(schema, f"usr/share/glib-2.0/schemas/{schema.name}")
    run("glib-compile-schemas", schemas)
    appdir.copy_tree(Path("/usr/share/icons/Adwaita"), "usr/share/icons/Adwaita")
    appdir.copy("/usr/share/icons/hicolor/index.theme", "usr/share/icons/hicolor/index.theme")

    # bsdtar for unpacking Roblox, for systems that do not have it.
    appdir.copy("/usr/bin/bsdtar", "usr/lib/roseblox-tools/bsdtar", elf=True)
    wrapper = usr / "bin" / "bsdtar"
    wrapper.write_text(
        "#!/bin/sh\n"
        'here=$(dirname "$(readlink -f "$0")")/..\n'
        'LD_LIBRARY_PATH="$here/lib/libs${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" '
        'exec "$here/lib/roseblox-tools/bsdtar" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    # Roseblox itself, compiled ahead so the read-only image starts quickly.
    appdir.copy_tree(ROOT / "src" / "roseblox", "usr/share/roseblox/roseblox")
    run(usr / "bin" / "python3", "-m", "compileall", "-q", "-d", "/usr/share/roseblox", usr / "share" / "roseblox",
        env={**os.environ, "PYTHONHOME": str(usr), "LD_LIBRARY_PATH": str(appdir.libs)})

    icon = ROOT / "packaging" / "arch" / f"{APP_ID}.svg"
    appdir.copy(icon, f"usr/share/icons/hicolor/scalable/apps/{APP_ID}.svg")
    appdir.copy(icon, f"{APP_ID}.svg")
    (appdir.path / ".DirIcon").symlink_to(f"{APP_ID}.svg")
    desktop = (ROOT / "packaging" / "arch" / f"{APP_ID}.desktop").read_text(encoding="utf-8")
    desktop = re.sub(r"(?m)^Exec=.*$", "Exec=roseblox %u", desktop)
    desktop = re.sub(r"(?m)^TryExec=.*\n", "", desktop)
    (appdir.path / f"{APP_ID}.desktop").write_text(desktop, encoding="utf-8")
    apprun = appdir.copy(ROOT / "packaging" / "appimage" / "AppRun", "AppRun")
    apprun.chmod(0o755)
    appdir.copy(ROOT / "LICENSE", "usr/share/doc/roseblox/LICENSE")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--appimagetool", default=os.environ.get("APPIMAGETOOL", "appimagetool"))
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--keep-appdir", action="store_true", help="leave the AppDir in the output folder")
    arguments = parser.parse_args()

    if os.uname().machine != "x86_64":
        raise SystemExit("The Roseblox AppImage is built on x86-64.")
    arguments.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        appdir = AppDir(Path(temporary) / "Roseblox.AppDir")
        build_appdir(appdir)
        bundled = appdir.bundle_libraries()
        print(f"Bundled {len(bundled)} libraries.")
        output = arguments.output / OUTPUT_NAME
        run(arguments.appimagetool, "--no-appstream", "-u", UPDATE_INFORMATION, appdir.path, output,
            env={**os.environ, "ARCH": "x86_64", "VERSION": version()}, cwd=arguments.output)
        if arguments.keep_appdir:
            kept = arguments.output / "Roseblox.AppDir"
            shutil.rmtree(kept, ignore_errors=True)
            shutil.copytree(appdir.path, kept, symlinks=True)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
