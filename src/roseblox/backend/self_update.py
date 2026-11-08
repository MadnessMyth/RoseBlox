"""New Roseblox versions: how this copy was installed, what GitHub's latest release is,
and replacing a Roseblox AppImage with the new one."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from .. import __version__
from .appimage import appimage_path

REPOSITORY = "MadnessMyth/RoseBlox"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases/latest"
UPDATE_GUIDE_URL = f"https://github.com/{REPOSITORY}/blob/master/docs/INSTALL.md#update"
APPIMAGE_ASSET = "Roseblox-x86_64.AppImage"
CHECKSUMS_ASSET = "SHA256SUMS"
CHECK_INTERVAL_SECONDS = 24 * 60 * 60
_TIMEOUT_SECONDS = 15
_MAX_RELEASE_BYTES = 1024 * 1024
_MAX_APPIMAGE_BYTES = 1024 * 1024 * 1024
_VERSION_TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

INSTALL_APPIMAGE = "appimage"
INSTALL_DEB = "deb"
INSTALL_SCRIPT = "script"
INSTALL_PACKAGE = "package"
INSTALL_SOURCE = "source"


class SelfUpdateError(RuntimeError):
    """Raised when the latest release cannot be read or an AppImage update fails."""


@dataclass(frozen=True)
class Release:
    version: str
    page_url: str
    appimage_url: str | None = None
    appimage_size: int | None = None
    checksums_url: str | None = None


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = _VERSION_TAG.match(text.strip())
    return tuple(int(part) for part in match.groups()) if match else None


def is_newer(candidate: str, current: str = __version__) -> bool:
    new, old = parse_version(candidate), parse_version(current)
    return new is not None and old is not None and new > old


def install_kind(
    environ: Mapping[str, str] | None = None,
    package_directory: Path | None = None,
    dpkg_list: Path = Path("/var/lib/dpkg/info/roseblox.list"),
) -> str:
    """How this copy of Roseblox was installed, which decides how it is updated."""
    package_directory = package_directory or Path(__file__).resolve().parent.parent
    if appimage_path(environ, package_directory):
        return INSTALL_APPIMAGE
    if (package_directory.parent / ".installed-by-roseblox").exists():
        return INSTALL_SCRIPT
    parts = package_directory.parts
    if "dist-packages" in parts and str(package_directory).startswith("/usr/") and dpkg_list.exists():
        return INSTALL_DEB
    if str(package_directory).startswith("/usr/"):
        return INSTALL_PACKAGE
    return INSTALL_SOURCE


def parse_release(document: object) -> Release:
    if not isinstance(document, dict):
        raise SelfUpdateError("GitHub's release information has an unexpected format.")
    tag = document.get("tag_name")
    if not isinstance(tag, str) or parse_version(tag) is None:
        raise SelfUpdateError("GitHub's latest release has no valid version tag.")
    page = document.get("html_url")
    page = page if isinstance(page, str) and page.startswith("https://github.com/") else RELEASES_URL
    appimage_url = appimage_size = checksums_url = None
    for asset in document.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        url = asset.get("browser_download_url")
        if not isinstance(url, str) or not url.startswith("https://github.com/"):
            continue
        if asset.get("name") == APPIMAGE_ASSET:
            appimage_url = url
            size = asset.get("size")
            appimage_size = size if isinstance(size, int) and size > 0 else None
        elif asset.get("name") == CHECKSUMS_ASSET:
            checksums_url = url
    return Release(tag.lstrip("v"), page, appimage_url, appimage_size, checksums_url)


def _request(url: str, accept: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": f"Roseblox/{__version__}", "Accept": accept})


def fetch_latest_release(opener: Callable = urllib.request.urlopen) -> Release:
    try:
        with opener(_request(LATEST_RELEASE_API, "application/vnd.github+json"), timeout=_TIMEOUT_SECONDS) as response:
            body = response.read(_MAX_RELEASE_BYTES + 1)
    except OSError as error:
        raise SelfUpdateError(f"Could not reach GitHub: {error}") from error
    if len(body) > _MAX_RELEASE_BYTES:
        raise SelfUpdateError("GitHub's release information is unexpectedly large.")
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise SelfUpdateError("GitHub's release information is not valid JSON.") from error
    return parse_release(document)


def expected_checksum(checksums: str, name: str = APPIMAGE_ASSET) -> str | None:
    for line in checksums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name and re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            return parts[0]
    return None


def update_appimage(
    release: Release,
    target: str | Path,
    progress: Callable[[int, int | None], None] = lambda received, total: None,
    cancelled: Callable[[], bool] = lambda: False,
    opener: Callable = urllib.request.urlopen,
) -> Path:
    """Download the release's AppImage next to ``target``, check it, and swap it in.

    The running AppImage keeps working from its mounted copy until Roseblox restarts.
    """
    if not release.appimage_url or not release.checksums_url:
        raise SelfUpdateError(f"Release {release.version} has no Roseblox AppImage to update to.")
    target = Path(target)
    try:
        with opener(_request(release.checksums_url, "text/plain"), timeout=_TIMEOUT_SECONDS) as response:
            checksum = expected_checksum(response.read(_MAX_RELEASE_BYTES).decode("utf-8", "replace"))
    except OSError as error:
        raise SelfUpdateError(f"Could not download the release checksums: {error}") from error
    if checksum is None:
        raise SelfUpdateError(f"Release {release.version} does not list a checksum for {APPIMAGE_ASSET}.")

    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".Roseblox-update-", dir=target.parent)
    except OSError as error:
        raise SelfUpdateError(f"Roseblox cannot write to {target.parent}: {error}") from error
    temporary = Path(temporary_name)
    digest = hashlib.sha256()
    received = 0
    try:
        with os.fdopen(descriptor, "wb") as stream:
            with opener(_request(release.appimage_url, "application/octet-stream"), timeout=_TIMEOUT_SECONDS) as response:
                while True:
                    if cancelled():
                        raise SelfUpdateError("The update was cancelled.")
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > _MAX_APPIMAGE_BYTES:
                        raise SelfUpdateError("The downloaded AppImage is unexpectedly large.")
                    digest.update(chunk)
                    stream.write(chunk)
                    progress(received, release.appimage_size)
        if digest.hexdigest() != checksum:
            raise SelfUpdateError("The downloaded AppImage does not match the release checksum; nothing was changed.")
        temporary.chmod(0o755)
        os.replace(temporary, target)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise SelfUpdateError(f"Could not replace {target.name}: {error}") from error
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target


def restart_command(environ: Mapping[str, str] | None = None) -> list[str]:
    """Start the replaced AppImage once this instance has let go of its D-Bus name."""
    appimage = appimage_path(environ)
    if appimage is None:
        raise SelfUpdateError("Only the Roseblox AppImage restarts itself after an update.")
    return ["/bin/sh", "-c", 'sleep 1; exec "$0"', appimage]
