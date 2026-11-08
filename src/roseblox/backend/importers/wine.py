"""Find Windows apps' files inside Wine prefixes (plain Wine, Bottles, Lutris, Steam/Proton)."""

from __future__ import annotations

from pathlib import Path

from .model import home, xdg

MAX_PREFIXES = 64


def prefix_roots(environ: dict[str, str]) -> list[Path]:
    user_home = home(environ)
    data = xdg(environ, "DATA")
    roots = [Path(environ["WINEPREFIX"])] if environ.get("WINEPREFIX") else []
    roots.append(user_home / ".wine")
    for parent in (
        data / "wineprefixes",
        data / "bottles" / "bottles",
        user_home / ".var/app/com.usebottles.bottles/data/bottles/bottles",
        user_home / "Games",
        data / "Steam/steamapps/compatdata",
        user_home / ".steam/steam/steamapps/compatdata",
        data / "grapejuice/prefixes",
    ):
        try:
            children = sorted(child for child in parent.iterdir() if child.is_dir())
        except OSError:
            continue
        for child in children:
            roots.append(child / "pfx" if (child / "pfx").is_dir() else child)
    unique = []
    for root in roots:
        if (root / "drive_c").is_dir() and root not in unique:
            unique.append(root)
    return unique[:MAX_PREFIXES]


def windows_users(prefix: Path) -> list[Path]:
    try:
        return sorted(
            user for user in (prefix / "drive_c" / "users").iterdir()
            if user.is_dir() and user.name.lower() not in {"public", "default"}
        )
    except OSError:
        return []


def find_in_prefixes(environ: dict[str, str], relative: str) -> list[Path]:
    """``relative`` is under a Windows user's profile, e.g. ``AppData/Local/Bloxstrap``."""
    found = []
    for prefix in prefix_roots(environ):
        for user in windows_users(prefix):
            candidate = user / relative
            if candidate.exists():
                found.append(candidate)
    return found


def resolve_in_prefix(path: Path, relative: str) -> Path | None:
    """When the user browses to a prefix or a Windows user folder, look for ``relative`` inside it."""
    if (path / "drive_c").is_dir():
        for user in windows_users(path):
            if (user / relative).exists():
                return user / relative
    if (path / relative).exists():
        return path / relative
    return None
