"""A short banner drawn inside the Roblox Player by MangoHud's Vulkan layer.

Some desktops hold notifications back while a game is fullscreen; MangoHud draws on the
game's own frames, so the banner shows there too. Roseblox gives the launch a config file
of its own (``MANGOHUD_CONFIGFILE``), which MangoHud watches and re-reads when it changes:

- resting: the user's own MangoHud config when the MangoHud overlay is on, otherwise a hidden HUD;
- banner: only one line of text at the top, for ``BANNER_SECONDS``, then back to resting;
- ping: resting with a ``PING`` row added, while the constant ping overlay is on.

MangoHud draws one HUD in one place, so the banner briefly replaces a user's HUD instead of
being added to it.

Every config of one launch keeps the font settings of its resting config: a reloaded config with
other ones makes MangoHud wait for the GPU and build a new font texture in the middle of the game.
So a line of text is MangoHud's clock line with the text as its format: that line is drawn in the
HUD's stat font, where custom text is drawn at about half that size.

An unchanged file is left alone, so MangoHud only re-reads its config when the HUD changes.
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
PING_COLOR = "FDF909"
FONT_OPTIONS = (
    "font_size", "font_size_text", "font_size_secondary", "font_scale", "font_file", "font_file_text",
    "font_glyph_ranges", "no_small_font",
)
DEFAULT_FONT_SIZE = 24
# MangoHud's own font is monospaced, each character about half as wide as the font size. A HUD does not
# widen for a long line, so its width is set to fit, with MangoHud's 5 pixel margin on both sides.
CHARACTER_WIDTH = 0.51
LINE_MARGINS = 14
CLOCK_OPTIONS = ("time", "time_format", "time_no_label")
DEFAULT_TIME_FORMAT = "%T"
MEDIA_OPTIONS = (
    "media_player", "media_player_name", "media_player_color", "media_player_format", "font_scale_media_player",
)
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


def banner_config(text: str, resting: str = HIDDEN) -> str:
    return _line_config(clean_text(text), resting, "position=top-center", "background_alpha=0.7", "round_corners=10")


def _line_config(text: str, resting: str, *settings: str) -> str:
    """A HUD of one line, ``text``, in the stat font of ``resting``."""
    fonts = [line for line in resting.splitlines() if _option(line)[0] in FONT_OPTIONS]
    options = dict(_option(line) for line in fonts)
    size = _size(options.get("font_size"), DEFAULT_FONT_SIZE) * _size(options.get("font_scale"), 1.0)
    width = round(len(text) * size * CHARACTER_WIDTH) + LINE_MARGINS
    # The format goes through strftime, where only "%" stands for something.
    return "\n".join((
        "legacy_layout=0", *settings, "table_columns=1", f"width={width}", *fonts,
        "time", "time_no_label", f"time_format={text.replace('%', '%%')}",
    )) + "\n"


def _size(value: str | None, default: float) -> float:
    try:
        size = float(value) if value is not None else default
    except ValueError:
        return default
    return size if 0 < size < 10_000 else default


def ping_text(ping_ms: int) -> str:
    return f"PING {max(1, int(ping_ms))} ms"


def media_row_fits(resting: str) -> bool:
    """Whether the ``PING`` row can be MangoHud's media-player line: in a HUD of the user's that shows no player."""
    if resting == HIDDEN:
        return False
    options = dict(_option(line) for line in resting.splitlines())
    return not _switched_on(options.get("media_player", options.get("full", "0")))


def ping_config(resting: str, ping_ms: int, player: str | None = None) -> str:
    """MangoHud config with a ``PING`` row added to ``resting``.

    MangoHud has one text color for the whole HUD and its default layout ignores custom text. So in
    a user's HUD the row is MangoHud's clock line with the ping as its text: the first row, at stat
    size, in the HUD's own text color, with every other row as it was. A HUD the user already shows
    the clock in keeps it, in front of the ping. Only a HUD that holds nothing but the ping is yellow.

    The one line MangoHud colors by itself is its media player's. ``player`` names an MPRIS player
    whose track title is the ping (``ping_player``); the row in a user's HUD is then that line, in
    yellow at stat size. It is the last row, which is where MangoHud's default layout draws it, and
    the config itself no longer changes with the ping.
    """
    text = ping_text(ping_ms)
    if resting == HIDDEN:
        return _line_config(text, resting, f"text_color={PING_COLOR}")
    lines = [(line, *_option(line)) for line in resting.splitlines()]
    options = {name: value for _line, name, value in lines}
    if player and media_row_fits(resting):
        kept = [line for line, name, _value in lines if name not in MEDIA_OPTIONS]
        return "\n".join((
            *kept, "media_player", f"media_player_name={player}", f"media_player_color={PING_COLOR}",
            "media_player_format={title}", "font_scale_media_player=1",
        )) + "\n"
    if _switched_on(options.get("time", options.get("full", "0"))):
        text = f"{options.get('time_format') or DEFAULT_TIME_FORMAT}  {text}"
    kept = [line for line, name, _value in lines if name not in CLOCK_OPTIONS]
    return "\n".join(("time", "time_no_label", f"time_format={text}", *kept)) + "\n"


def _option(line: str) -> tuple[str, str]:
    """``(name, value)`` of a config line as MangoHud reads it: ``#`` starts a comment, no value means on."""
    name, separator, value = line.split("#", 1)[0].partition("=")
    return name.strip(), value.strip() if separator else "1"


def _switched_on(value: str) -> bool:
    # MangoHud reads a switch as a number, so "true" is off and only a number other than zero is on.
    return value.lstrip("+-0")[:1].isdigit()


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

    def configures(self, pid: int) -> bool:
        """Whether process ``pid`` was started with this file as its MangoHud config, as the game was.

        Also true for a process whose environment cannot be read, so that only what is known to
        be another program is left out.
        """
        try:
            environment = (Path("/proc") / str(pid) / "environ").read_bytes()
        except OSError:
            return True
        return os.fsencode(f"{CONFIG_VARIABLE}={self.path}") in environment.split(b"\0")

    def show(self, text: str) -> None:
        self._write(self.path, banner_config(text, self.resting()))

    def media_row_fits(self) -> bool:
        return media_row_fits(self.resting())

    def show_ping(self, ping_ms: int, player: str | None = None) -> None:
        self._write(self.path, ping_config(self.resting(), ping_ms, player))

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
        try:
            if path.read_text(encoding="utf-8") == contents:
                return
        except (OSError, ValueError):
            pass
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
