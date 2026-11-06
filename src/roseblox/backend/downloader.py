"""Cancelable streaming downloads for Roblox archives."""

from __future__ import annotations

import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class DownloadError(RuntimeError):
    """Raised when a Roblox archive cannot be downloaded completely."""


class DownloadCancelled(DownloadError):
    """Raised when the user cancels an active download."""


@dataclass(frozen=True)
class DownloadProgress:
    downloaded: int
    total: int | None


ProgressCallback = Callable[[DownloadProgress], None]


def download_archive(
    url: str,
    temporary_directory: Path,
    cancel_event: threading.Event,
    progress_callback: ProgressCallback | None = None,
    *,
    timeout: float = 30.0,
    chunk_size: int = 1024 * 1024,
    partial_path: Path | None = None,
    resume: bool = False,
    etag: str | None = None,
    last_modified: str | None = None,
    metadata_callback: Callable[[str | None, str | None, int | None], None] | None = None,
    preserve_partial: bool = False,
) -> Path:
    """Stream an archive; optionally retain and resume a validated partial file."""
    temporary_directory = Path(temporary_directory)
    temporary_directory.mkdir(parents=True, exist_ok=True)
    if partial_path is None:
        file_descriptor, raw_path = tempfile.mkstemp(
            prefix="roseblox-", suffix=".part", dir=temporary_directory
        )
        temporary_path = Path(raw_path)
        os.close(file_descriptor)
    else:
        temporary_path = Path(partial_path).absolute()
        try:
            temporary_path.relative_to(temporary_directory.absolute())
        except ValueError as error:
            raise DownloadError("Partial download path must remain inside Roseblox staging storage.") from error
        temporary_path.parent.mkdir(parents=True, exist_ok=True)
        if temporary_path.is_symlink():
            raise DownloadError("Partial download path cannot be a symbolic link.")
    downloaded = 0
    complete = False
    try:
        offset = temporary_path.stat().st_size if resume and temporary_path.is_file() else 0
        for attempt in range(2):
            headers = {"User-Agent": "Roseblox/0.1"}
            if offset:
                headers["Range"] = f"bytes={offset}-"
                validator = etag or last_modified
                if validator:
                    headers["If-Range"] = validator
            request = Request(url, headers=headers)
            try:
                response = urlopen(request, timeout=timeout)
            except HTTPError as error:
                if offset and error.code == 416 and attempt == 0:
                    error.close()
                    temporary_path.unlink(missing_ok=True)
                    offset = 0
                    continue
                error.close()
                raise DownloadError(f"Archive server returned HTTP {error.code}.") from error
            except (URLError, TimeoutError, OSError) as error:
                raise DownloadError(f"Could not download Roblox archive: {error}") from error
            with response:
                status = getattr(response, "status", None)
                if status is None:
                    getcode = getattr(response, "getcode", None)
                    status = getcode() if getcode else 200
                response_etag = response.headers.get("ETag")
                response_modified = response.headers.get("Last-Modified")
                content_range = response.headers.get("Content-Range", "")
                if offset and status == 206:
                    range_match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", content_range)
                    valid_range = range_match is not None and int(range_match.group(1)) == offset
                    if etag and response_etag and etag != response_etag:
                        valid_range = False
                    if last_modified and response_modified and last_modified != response_modified:
                        valid_range = False
                    if not valid_range:
                        response.close()
                        if attempt == 0:
                            temporary_path.unlink(missing_ok=True)
                            offset = 0
                            continue
                        raise DownloadError("Archive server returned an invalid range for the saved partial download.")
                    range_total = range_match.group(3)
                    total = int(range_total) if range_total != "*" else None
                    mode = "ab"
                elif status == 200:
                    offset = 0
                    downloaded = 0
                    raw_total = response.headers.get("Content-Length")
                    try:
                        total = int(raw_total) if raw_total is not None else None
                        if total is not None and total < 0:
                            total = None
                    except (TypeError, ValueError):
                        total = None
                    mode = "wb"
                else:
                    raise DownloadError(f"Archive server returned unexpected HTTP status {status} for range resume.")
                if metadata_callback:
                    if offset and status == 206:
                        metadata_callback(response_etag or etag, response_modified or last_modified, total)
                    else:
                        metadata_callback(response_etag, response_modified, total)
                if progress_callback:
                    progress_callback(DownloadProgress(offset, total))
                with temporary_path.open(mode) as output:
                    while True:
                        if cancel_event.is_set():
                            raise DownloadCancelled("Download cancelled.")
                        chunk = response.read(chunk_size)
                        if not chunk:
                            break
                        output.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback:
                            progress_callback(DownloadProgress(offset + downloaded, total))
                    if cancel_event.is_set():
                        raise DownloadCancelled("Download cancelled.")
                    expected_bytes = None if total is None else total - offset
                    if expected_bytes is not None and downloaded != expected_bytes:
                        raise DownloadError(
                            f"The archive download was incomplete ({offset + downloaded} of {total} bytes)."
                        )
                    output.flush()
                    os.fsync(output.fileno())
                complete = True
                return temporary_path
        raise DownloadError("Could not restart the partial download safely.")
    except DownloadError:
        raise
    except (OSError, TimeoutError) as error:
        raise DownloadError(f"Could not write the Roblox archive: {error}") from error
    finally:
        if not complete and not preserve_partial:
            temporary_path.unlink(missing_ok=True)
