"""Content mods: replacement fonts, cursors, sounds and textures inside managed Roblox bundles.

Mods are applied to installations Roseblox manages, right before they start.
Every replaced file is backed up next to the bundle the first time, so turning
a mod off puts Roblox's own file back. Only media files under
``Contents/Resources`` can be replaced: never code, scripts, models or places
other than the reviewed avatar editor background below.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import tempfile
import threading
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Mapping

from .. import __version__

APP_TYPES = ("player", "studio")
RESOURCES = "Contents/Resources"
FONTS = f"{RESOURCES}/content/fonts"
FAMILIES = f"{FONTS}/families"
CUSTOM_FONT_STEM = "RosebloxCustomFont"
# The CJK fallback keeps other scripts readable; monospace families keep code aligned in Studio.
KEPT_FONT_FAMILIES = frozenset({"NotoSansCJKFallback.json", "BuilderMono.json", "RobotoMono.json", "Inconsolata.json"})
CURSORS = (f"{RESOURCES}/content/textures/Cursors/KeyboardMouse/ArrowCursor.png",
           f"{RESOURCES}/content/textures/Cursors/KeyboardMouse/ArrowFarCursor.png")
DEATH_SOUND = f"{RESOURCES}/content/sounds/ouch.ogg"
CLASSIC_DEATH_SOUND = f"{RESOURCES}/content/sounds/oof.ogg"
AVATAR_BACKGROUND = f"{RESOURCES}/ExtraContent/places/Mobile.rbxl"
# Bloxstrap's pre-2024 avatar editor scene (MIT), pinned to one reviewed file. Its only
# scripts are the same as in Roblox's own Mobile.rbxl.
AVATAR_BACKGROUND_URL = (
    "https://raw.githubusercontent.com/bloxstraplabs/bloxstrap/81161be984f35c5a4b0216b186dc97513f4878ca/"
    "Bloxstrap/Resources/Mods/OldAvatarBackground.rbxl"
)
AVATAR_BACKGROUND_SHA256 = "70604d44c691309d79035b2695a0ce2efcdc41adcc764e78542eefb52319e20f"
MEDIA_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".dds", ".tex", ".ktx", ".ogg", ".mp3", ".wav", ".ttf", ".otf", ".mesh"})
MAX_FILE_SIZE = 64 * 1024 * 1024
MAX_FONT_SIZE = 32 * 1024 * 1024
MAX_CURSOR_SIZE = 1024 * 1024
MAX_FILES = 5000
STATE_FILE = ".roseblox-mods.json"
ORIGINALS = ".roseblox-originals"


class ModsError(RuntimeError):
    pass


def _data_home(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("XDG_DATA_HOME") or Path(env.get("HOME") or Path.home()) / ".local" / "share")


def default_mods_directory(app_type: str, env: Mapping[str, str] | None = None) -> Path:
    """The folder users drop files into; it mirrors ``Contents/Resources`` of the app."""
    return _data_home(env) / "roseblox" / "mods" / app_type


def default_settings_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    config = Path(env.get("XDG_CONFIG_HOME") or Path(env.get("HOME") or Path.home()) / ".config")
    return config / "roseblox" / "mods.json"


def default_cache_directory(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("XDG_CACHE_HOME") or Path(env.get("HOME") or Path.home()) / ".cache") / "roseblox" / "mods"


def check_relative(relative: str) -> str:
    """Reject anything outside Contents/Resources or that is not a media file."""
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or not relative.startswith(RESOURCES + "/"):
        raise ModsError(f"{relative} is outside the app's resources.")
    suffix = path.suffix.lower()
    if relative == AVATAR_BACKGROUND:
        return relative
    if suffix == ".json" and str(path.parent) == FAMILIES:
        return relative
    if suffix not in MEDIA_SUFFIXES:
        raise ModsError(f"{path.name}: only images, textures, sounds, fonts and meshes can be replaced.")
    return relative


def _sha256(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def font_kind(path: Path) -> str:
    """``.ttf`` or ``.otf`` from the file's signature."""
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            magic = stream.read(4)
    except OSError as error:
        raise ModsError(f"Could not read the font: {error}") from error
    if size > MAX_FONT_SIZE:
        raise ModsError("The font is larger than 32 MiB.")
    if magic in (b"\x00\x01\x00\x00", b"true"):
        return ".ttf"
    if magic == b"OTTO":
        return ".otf"
    raise ModsError("Choose a TrueType (.ttf) or OpenType (.otf) font.")


def png_size(path: Path) -> tuple[int, int]:
    try:
        if path.stat().st_size > MAX_CURSOR_SIZE:
            raise ModsError("The cursor image is larger than 1 MiB.")
        with path.open("rb") as stream:
            header = stream.read(24)
    except OSError as error:
        raise ModsError(f"Could not read the image: {error}") from error
    if header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ModsError("Choose a PNG image.")
    width, height = int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")
    if not (8 <= width <= 256 and 8 <= height <= 256):
        raise ModsError("A cursor image must be between 8 and 256 pixels wide and high.")
    return width, height


@dataclass(frozen=True)
class ModSettings:
    font: str | None = None
    cursor: str | None = None
    classic_death_sound: bool = False
    classic_avatar_background: bool = False

    @property
    def is_default(self) -> bool:
        return self == ModSettings()


@dataclass(frozen=True)
class FolderFile:
    relative: str
    path: Path
    size: int
    problem: str | None = None


@dataclass
class ApplyResult:
    written: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    unchanged: int = 0
    skipped: list[str] = field(default_factory=list)


_LOCKS: dict[Path, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(path.resolve(), threading.RLock())


class ModsStore:
    """Per-application mod choices; chosen files are copied so the originals may move."""

    def __init__(self, path: Path | None = None, *, assets_directory: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_settings_path()
        self.assets_directory = (
            Path(assets_directory) if assets_directory is not None else _data_home() / "roseblox" / "mod-assets"
        )
        self._lock = _lock_for(self.path)

    def load(self, app_type: str) -> ModSettings:
        document = self._read()
        entry = document.get("apps", {}).get(app_type, {})
        if not isinstance(entry, dict):
            raise ModsError("Saved mod settings are damaged.")
        settings = ModSettings(
            font=entry.get("font"), cursor=entry.get("cursor"),
            classic_death_sound=entry.get("classic_death_sound", False),
            classic_avatar_background=entry.get("classic_avatar_background", False),
        )
        for value in (settings.font, settings.cursor):
            if value is not None and not isinstance(value, str):
                raise ModsError("Saved mod settings are damaged.")
        if not isinstance(settings.classic_death_sound, bool) or not isinstance(settings.classic_avatar_background, bool):
            raise ModsError("Saved mod settings are damaged.")
        return settings

    def save(self, app_type: str, settings: ModSettings) -> ModSettings:
        if app_type not in APP_TYPES:
            raise ModsError(f"Unknown application {app_type!r}.")
        with self._lock, self._file_lock():
            document = self._read()
            apps = dict(document.get("apps") or {})
            apps[app_type] = {
                "font": settings.font, "cursor": settings.cursor,
                "classic_death_sound": settings.classic_death_sound,
                "classic_avatar_background": settings.classic_avatar_background,
            }
            _write_json(self.path, {"version": 1, "apps": apps})
        return self.load(app_type)

    def set_font(self, app_type: str, source: Path | None) -> ModSettings:
        current = self.load(app_type)
        if source is None:
            return self.save(app_type, replace(current, font=None))
        suffix = font_kind(Path(source))
        stored = self._store_asset(app_type, Path(source), "font", suffix)
        return self.save(app_type, replace(current, font=str(stored)))

    def set_cursor(self, app_type: str, source: Path | None) -> ModSettings:
        current = self.load(app_type)
        if source is None:
            return self.save(app_type, replace(current, cursor=None))
        png_size(Path(source))
        stored = self._store_asset(app_type, Path(source), "cursor", ".png")
        return self.save(app_type, replace(current, cursor=str(stored)))

    def _store_asset(self, app_type: str, source: Path, kind: str, suffix: str) -> Path:
        """Copy a chosen file under its own name, replacing the previous choice of this kind."""
        directory = self.assets_directory / app_type / kind
        directory.mkdir(parents=True, exist_ok=True)
        stem = "".join(character for character in source.stem if character.isalnum() or character in " ._-")[:80]
        target = directory / f"{stem.strip() or kind}{suffix}"
        _copy_atomic(source, target)
        for old in directory.iterdir():
            if old != target and old.is_file():
                old.unlink(missing_ok=True)
        return target

    @contextlib.contextmanager
    def _file_lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_name(self.path.name + ".lock"), "a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def _read(self) -> dict:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "apps": {}}
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ModsError(f"Could not read mod settings: {error}") from error
        if not isinstance(document, dict) or document.get("version") != 1 or not isinstance(document.get("apps", {}), dict):
            raise ModsError("Saved mod settings are damaged.")
        return document


def _write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        json.dump(document, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(stream.name, path)


def _copy_atomic(source: Path, target: Path, data: bytes | None = None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            if data is not None:
                stream.write(data)
            else:
                with source.open("rb") as reader:
                    shutil.copyfileobj(reader, stream, 1024 * 1024)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def folder_files(directory: Path) -> list[FolderFile]:
    """Files in a mods folder, each mapped to ``Contents/Resources/...`` with any reason it is refused."""
    files: list[FolderFile] = []
    if not directory.is_dir():
        return files
    for root, directories, names in os.walk(directory):
        directories[:] = sorted(name for name in directories if not (Path(root) / name).is_symlink())
        for name in sorted(names):
            path = Path(root) / name
            relative = f"{RESOURCES}/{path.relative_to(directory).as_posix()}"
            problem = None
            try:
                size = path.lstat().st_size
            except OSError as error:
                files.append(FolderFile(relative, path, 0, str(error)))
                continue
            if path.is_symlink():
                problem = "Links are not followed."
            elif size > MAX_FILE_SIZE:
                problem = "Larger than 64 MiB."
            elif relative == AVATAR_BACKGROUND:
                problem = "Use Classic avatar editor background instead."
            else:
                try:
                    check_relative(relative)
                except ModsError as error:
                    problem = str(error)
            files.append(FolderFile(relative, path, size, problem))
            if len(files) >= MAX_FILES:
                return files
    return files


def _original(bundle: Path, relative: str) -> Path:
    """Roblox's own copy of a file, from the backup when Roseblox already replaced it."""
    backup = bundle.parent / ORIGINALS / relative
    return backup if backup.is_file() else bundle / relative


def custom_font_files(bundle: Path, font: Path) -> dict[str, bytes | Path]:
    suffix = font_kind(font)
    asset = f"rbxasset://fonts/{CUSTOM_FONT_STEM}{suffix}"
    files: dict[str, bytes | Path] = {f"{FONTS}/{CUSTOM_FONT_STEM}{suffix}": font}
    families = bundle / FAMILIES
    if not families.is_dir():
        return files
    for family in sorted(families.glob("*.json")):
        if family.name in KEPT_FONT_FAMILIES:
            continue
        relative = f"{FAMILIES}/{family.name}"
        try:
            document = json.loads(_original(bundle, relative).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        faces = document.get("faces") if isinstance(document, dict) else None
        if not isinstance(faces, list):
            continue
        for face in faces:
            if isinstance(face, dict) and "assetId" in face:
                face["assetId"] = asset
        files[relative] = (json.dumps(document, indent="\t") + "\n").encode("utf-8")
    return files


def avatar_background_path(cache_directory: Path | None = None) -> Path:
    return (cache_directory or default_cache_directory()) / f"OldAvatarBackground-{AVATAR_BACKGROUND_SHA256[:12]}.rbxl"


def fetch_avatar_background(cache_directory: Path | None = None, opener=None) -> Path:
    """Download the pinned avatar editor background once and verify it."""
    target = avatar_background_path(cache_directory)
    if _sha256(target) == AVATAR_BACKGROUND_SHA256:
        return target
    request = urllib.request.Request(AVATAR_BACKGROUND_URL, headers={"User-Agent": f"Roseblox/{__version__}"})
    try:
        with (opener or urllib.request.urlopen)(request, timeout=30) as response:
            data = response.read(4 * 1024 * 1024 + 1)
    except OSError as error:
        raise ModsError(f"Could not download the avatar editor background: {error}") from error
    if hashlib.sha256(data).hexdigest() != AVATAR_BACKGROUND_SHA256:
        raise ModsError("The downloaded avatar editor background did not match the reviewed file; it was not used.")
    _copy_atomic(target, target, data)
    return target


def desired_files(
    bundle: Path, app_type: str, settings: ModSettings, folder: Path | None, *, cache_directory: Path | None = None,
) -> tuple[dict[str, bytes | Path], list[str]]:
    """Every file the bundle should have replaced, and why any choice could not be used."""
    files: dict[str, bytes | Path] = {}
    problems: list[str] = []
    for item in folder_files(folder) if folder is not None else ():
        if item.problem is None:
            files[item.relative] = item.path
    if settings.font:
        try:
            files.update(custom_font_files(bundle, Path(settings.font)))
        except ModsError as error:
            problems.append(f"Font: {error}")
    if settings.cursor:
        cursor = Path(settings.cursor)
        if cursor.is_file():
            for relative in CURSORS:
                files[relative] = cursor
        else:
            problems.append("Cursor: the chosen image is missing; choose it again.")
    if settings.classic_death_sound and app_type == "player":
        classic = _original(bundle, CLASSIC_DEATH_SOUND)
        if classic.is_file():
            files[DEATH_SOUND] = classic
        else:
            problems.append("Classic death sound: this Roblox version has no oof.ogg.")
    if settings.classic_avatar_background and app_type == "player":
        background = avatar_background_path(cache_directory)
        if (bundle / AVATAR_BACKGROUND).is_file() and _sha256(background) == AVATAR_BACKGROUND_SHA256:
            files[AVATAR_BACKGROUND] = background
        else:
            problems.append("Classic avatar editor background: the file is not downloaded yet; turn it off and on again.")
    return files, problems


def _safe_target(bundle: Path, relative: str) -> Path:
    check_relative(relative)
    target = bundle / relative
    current = bundle
    for part in PurePosixPath(relative).parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ModsError(f"{relative}: the bundle contains a link on this path.")
    if target.is_symlink():
        raise ModsError(f"{relative}: the bundle file is a link.")
    return target


def apply_mods(bundle: Path, files: Mapping[str, bytes | Path]) -> ApplyResult:
    """Make the bundle's replaced files match ``files`` and restore any no longer wanted."""
    bundle = Path(bundle)
    state_path = bundle.parent / STATE_FILE
    originals = bundle.parent / ORIGINALS
    result = ApplyResult()
    with _lock_for(state_path):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = {"version": 1, "files": {}}
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ModsError(f"The record of applied mods is damaged: {error}") from error
        applied: dict = dict(state.get("files") or {})
        for relative in sorted(set(applied) - set(files)):
            entry = applied.pop(relative)
            try:
                target = _safe_target(bundle, relative)
            except ModsError:
                continue
            if _sha256(target) != entry.get("sha256"):
                continue
            backup = originals / relative
            if entry.get("original") and backup.is_file():
                _copy_atomic(backup, target)
                backup.unlink()
            elif not entry.get("original"):
                target.unlink(missing_ok=True)
            result.restored.append(relative)
        for relative, source in sorted(files.items()):
            target = _safe_target(bundle, relative)
            if isinstance(source, bytes):
                data, digest = source, hashlib.sha256(source).hexdigest()
            else:
                data, digest = None, _sha256(Path(source))
                if digest is None:
                    result.skipped.append(relative)
                    continue
            if relative in applied and _sha256(target) == digest == applied[relative].get("sha256"):
                result.unchanged += 1
                continue
            if relative not in applied:
                had_original = target.is_file()
                if had_original:
                    _copy_atomic(target, originals / relative)
                applied[relative] = {"original": had_original}
            _copy_atomic(Path(source) if data is None else target, target, data)
            applied[relative]["sha256"] = digest
            result.written.append(relative)
        _write_json(state_path, {"version": 1, "files": applied})
    return result


def applied_files(bundle: Path) -> dict[str, dict]:
    try:
        state = json.loads((Path(bundle).parent / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    files = state.get("files") if isinstance(state, dict) else None
    return dict(files) if isinstance(files, dict) else {}


def prepare_launch(record, *, store: ModsStore | None = None, env: Mapping[str, str] | None = None) -> str | None:
    """Apply the app's mods to a managed installation; return a message for the user, if any."""
    app_type = getattr(record, "app_type", None)
    bundle_path = getattr(record, "bundle_path", None)
    if app_type not in APP_TYPES or not bundle_path:
        return None
    try:
        settings = (store or ModsStore()).load(app_type)
    except ModsError as error:
        return f"Mods were not applied: {error}"
    folder = default_mods_directory(app_type, env)
    bundle = Path(bundle_path)
    wanted = not settings.is_default or any(item.problem is None for item in folder_files(folder))
    if not getattr(record, "managed", False):
        return "Mods apply only to installations Roseblox installed; this one was left unchanged." if wanted else None
    if not wanted and not applied_files(bundle):
        return None
    files, problems = desired_files(bundle, app_type, settings, folder)
    try:
        apply_mods(bundle, files)
    except (ModsError, OSError) as error:
        return f"Mods were not applied: {error}"
    return " ".join(problems) or None
