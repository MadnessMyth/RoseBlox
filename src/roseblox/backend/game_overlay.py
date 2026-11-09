"""A short banner drawn inside the Roblox Player by MangoHud's Vulkan layer.

Desktop notifications are held back while a game is fullscreen; MangoHud draws on the
game's own frames, so the banner shows there too. Roseblox gives the launch a config file
of its own (``MANGOHUD_CONFIGFILE``), which MangoHud watches and re-reads when it changes:

- resting: the user's own MangoHud config when the MangoHud overlay is on, otherwise a hidden HUD;
- banner: only one line of text at the top, for ``BANNER_SECONDS``, then back to resting.

MangoHud's default ("legacy") layout ignores custom text, so the banner briefly replaces a
user's HUD instead of being added to it.
"""

from __future__ import annotations

import os
import secrets
import time
from collections.abc import Mapping
from pathlib import Path

from . import mangohud

CONFIG_VARIABLE = "MANGOHUD_CONFIGFILE"
BANNER_SECONDS = 10
BANNER_FONT_SIZE = 18
MIN_BANNER_WIDTH = 140
BANNER_POSITION = "bottom-left"
BANNER_ALPHA = 0.35
HIDDEN = "legacy_layout=0\nno_display\n"
MAX_TEXT = 100
MAX_USER_CONFIG = 64 * 1024
STALE_SECONDS = 2 * 24 * 3600
RESTING_SUFFIX = ".resting"


def overlay_directory(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    runtime = env.get("XDG_RUNTIME_DIR")
    if runtime and Path(runtime).is_dir():
        return Path(runtime) / "roseblox" / "overlay"
    cache = Path(env.get("XDG_CACHE_HOME") or Path(env.get("HOME") or Path.home()) / ".cache")
    return cache / "roseblox" / "overlay"


def clean_text(text: str) -> str:
    """One line MangoHud can parse: ``#`` starts a comment and ``=`` splits a config line."""
    cleaned = "".join(" " if not character.isprintable() or character in "#=" else character for character in text)
    cleaned = " ".join(cleaned.split())
    return cleaned if len(cleaned) <= MAX_TEXT else cleaned[: MAX_TEXT - 1].rstrip() + "…"


def banner_config(text: str) -> str:
    line = clean_text(text)
    # MangoHud does not widen the HUD for custom text, so a long line would be cut off. Its custom text
    # font is about 0.28 × font_size wide per character.
    width = max(MIN_BANNER_WIDTH, round(len(line) * BANNER_FONT_SIZE * 0.3) + 28)
    return (
        f"legacy_layout=0\nposition={BANNER_POSITION}\nfont_size={BANNER_FONT_SIZE}\nbackground_alpha={BANNER_ALPHA}\n"
        f"round_corners=8\nwidth={width}\ncustom_text_center={line}\n"
    )


def user_config(env: Mapping[str, str] | None = None, program: str | None = None) -> str:
    """The MangoHud config this launch would read without Roseblox: per-program first, as MangoHud does."""
    env = os.environ if env is None else env
    config_home = Path(env.get("XDG_CONFIG_HOME") or Path(env.get("HOME") or Path.home()) / ".config")
    candidates = [config_home / "MangoHud" / f"{program}.conf"] if program else []
    candidates.append(config_home / "MangoHud" / "MangoHud.conf")
    for candidate in candidates:
        if candidate.is_file():
            return _read_config(candidate)
    return ""


class GameOverlay:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.resting_path = self.path.with_name(self.path.name + RESTING_SUFFIX)

    @classmethod
    def create(cls, directory: Path, resting: str) -> "GameOverlay":
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        _remove_stale(directory)
        overlay = cls(directory / f"player-{secrets.token_hex(6)}.conf")
        overlay._write(overlay.resting_path, resting)
        overlay._write(overlay.path, resting)
        return overlay

    def resting(self) -> str:
        try:
            return self.resting_path.read_text(encoding="utf-8")
        except OSError:
            return HIDDEN

    def show(self, text: str) -> None:
        self._write(self.path, banner_config(text))

    def hide(self) -> None:
        self._write(self.path, self.resting())

    def remove(self) -> None:
        for path in (self.path, self.resting_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _write(path: Path, contents: str) -> None:
        # Rewritten in place: MangoHud watches this inode and re-reads 100 ms after a change.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(contents)


def _remove_stale(directory: Path) -> None:
    cutoff = time.time() - STALE_SECONDS
    for path in directory.glob("player-*"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


def _mangohud_on(environment: Mapping[str, str]) -> bool:
    return environment.get(mangohud.ENABLE_VARIABLE, "") not in ("", "0")


def prepare_launch(
    environment: Mapping[str, str],
    program: str | None = None,
    directory: Path | None = None,
) -> tuple[GameOverlay | None, dict[str, str], str | None]:
    """Set up the banner for one Player launch: ``(overlay, environment changes, problem)``.

    ``environment`` is what Rosemary would start with; if it already turns MangoHud on, that
    HUD (including a ``MANGOHUD_CONFIGFILE`` the user set) is what the banner returns to.
    Without MangoHud's layer there is no overlay, and ``problem`` says why.
    """
    if not mangohud.layer_installed(environment):
        return None, {}, NO_MANGOHUD
    resting = HIDDEN
    if _mangohud_on(environment):
        own = environment.get(CONFIG_VARIABLE)
        resting = _read_config(Path(own)) if own else user_config(environment, program)
    try:
        overlay = GameOverlay.create(directory or overlay_directory(environment), resting)
    except OSError as error:
        return None, {}, f"Could not prepare the in-game banner: {error}"
    return overlay, {mangohud.ENABLE_VARIABLE: "1", CONFIG_VARIABLE: str(overlay.path)}, None


def _read_config(path: Path) -> str:
    try:
        if path.stat().st_size <= MAX_USER_CONFIG:
            return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    return ""


NO_MANGOHUD = "MangoHud is not installed, so the server location is shown as a desktop notification instead."
