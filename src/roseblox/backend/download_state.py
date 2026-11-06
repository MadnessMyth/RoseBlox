"""Durable metadata for resumable Roblox archive downloads."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .library import APP_BUNDLE_NAMES, default_data_directory
from .versions import VersionLookupError, archive_url, validate_version


class DownloadStateError(RuntimeError):
    """Raised when persistent download metadata cannot safely be used."""


@dataclass(frozen=True)
class DownloadJob:
    id: str
    app_type: str
    version: str
    url: str
    archive_destination: str
    temporary_path: str
    state: str
    downloaded: int = 0
    total: int | None = None
    etag: str | None = None
    last_modified: str | None = None
    updated_at: str = ""
    error: str | None = None


_JOB_LOCK = threading.RLock()
_VALID_STATES = {"downloading", "interrupted", "downloaded", "installed", "invalid"}


class DownloadJobStore:
    def __init__(self, data_directory: Path | None = None) -> None:
        self.data_directory = Path(data_directory) if data_directory else default_data_directory()
        self.path = self.data_directory / "downloads.json"
        self.temporary_root = self.data_directory / ".staging" / "downloads"
        self.archive_root = self.data_directory / "archives"

    def load(self, app_type: str | None = None) -> list[DownloadJob]:
        with _JOB_LOCK:
            records = self._read()
            recovered: list[DownloadJob] = []
            changed = False
            for job in records:
                if job.state == "downloading":
                    try:
                        temporary = self._safe_temp_path(job)
                        destination = self._safe_archive_path(job)
                        if (
                            destination.is_file()
                            and (job.total is None or destination.stat().st_size == job.total)
                            and zipfile.is_zipfile(destination)
                        ):
                            size = destination.stat().st_size
                            temporary.unlink(missing_ok=True)
                            job = self._replace(job, state="downloaded", downloaded=size, error=None)
                            recovered.append(job)
                            changed = True
                            continue
                        size = temporary.stat().st_size if temporary.is_file() else 0
                        if (
                            temporary.is_file()
                            and job.total is not None
                            and size == job.total
                            and zipfile.is_zipfile(temporary)
                        ):
                            try:
                                os.link(temporary, destination)
                            except FileExistsError:
                                if not (
                                    (job.total is None or destination.stat().st_size == job.total)
                                    and zipfile.is_zipfile(destination)
                                ):
                                    job = self._replace(
                                        job,
                                        state="invalid",
                                        downloaded=size,
                                        error="A different file already occupies the saved archive destination.",
                                    )
                                    recovered.append(job)
                                    changed = True
                                    continue
                            temporary.unlink(missing_ok=True)
                            job = self._replace(job, state="downloaded", downloaded=size, error=None)
                            recovered.append(job)
                            changed = True
                            continue
                        job = self._replace(job, state="interrupted", downloaded=size)
                    except DownloadStateError as error:
                        job = self._replace(job, state="invalid", error=str(error))
                    changed = True
                elif job.state in {"interrupted", "downloaded"}:
                    try:
                        temporary = self._safe_temp_path(job)
                        destination = self._safe_archive_path(job)
                    except DownloadStateError:
                        job = self._replace(job, state="invalid", error="Unsafe persisted file path.")
                        changed = True
                    else:
                        target = destination if job.state == "downloaded" else temporary
                        if not target.is_file():
                            job = self._replace(job, state="invalid", error="Download file is missing.")
                            changed = True
                        elif job.state == "downloaded" and not zipfile.is_zipfile(target):
                            job = self._replace(job, state="invalid", error="Completed archive is not a readable ZIP file.")
                            changed = True
                        elif job.state == "interrupted":
                            size = target.stat().st_size
                            if size != job.downloaded:
                                job = self._replace(job, downloaded=size)
                                changed = True
                recovered.append(job)
            if changed:
                self._write(recovered)
            self._remove_stale_orphans(recovered)
            return [job for job in recovered if app_type is None or job.app_type == app_type]

    def get(self, job_id: str) -> DownloadJob:
        for job in self.load():
            if job.id == job_id:
                return job
        raise KeyError(f"No saved download job with ID {job_id}.")

    def create(self, app_type: str, version: str, job_id: str, archive_destination: Path) -> DownloadJob:
        if app_type not in APP_BUNDLE_NAMES:
            raise DownloadStateError(f"Unsupported application type: {app_type}")
        version = validate_version(version)
        self.temporary_root.mkdir(parents=True, exist_ok=True)
        self.archive_root.joinpath(app_type).mkdir(parents=True, exist_ok=True)
        job = DownloadJob(
            id=job_id,
            app_type=app_type,
            version=version,
            url=archive_url(app_type, version),
            archive_destination=str(archive_destination.absolute()),
            temporary_path=str((self.temporary_root / f"{job_id}.part").absolute()),
            state="downloading",
            updated_at=self._now(),
        )
        with _JOB_LOCK:
            jobs = self._read()
            if any(existing.id == job_id for existing in jobs):
                raise DownloadStateError("A download job with this ID already exists.")
            self._write([*jobs, job])
        return job

    def update(self, job_id: str, **changes: Any) -> DownloadJob:
        with _JOB_LOCK:
            jobs = self._read()
            updated: list[DownloadJob] = []
            found: DownloadJob | None = None
            for job in jobs:
                if job.id == job_id:
                    if "state" in changes and changes["state"] not in _VALID_STATES:
                        raise DownloadStateError("Invalid download state.")
                    found = self._replace(job, **changes)
                    updated.append(found)
                else:
                    updated.append(job)
            if found is None:
                raise KeyError(f"No saved download job with ID {job_id}.")
            self._write(updated)
            return found

    def discard(self, job_id: str) -> None:
        with _JOB_LOCK:
            jobs = self._read()
            job = next((item for item in jobs if item.id == job_id), None)
            if job is None:
                return
            if job.state in {"installed", "downloaded"}:
                raise DownloadStateError("A completed archive is retained and cannot be discarded as an interrupted download.")
            temporary = self._safe_temp_path(job)
            destination = self._safe_archive_path(job)
            temporary.unlink(missing_ok=True)
            self._write([item for item in jobs if item.id != job_id])

    def _read(self) -> list[DownloadJob]:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise DownloadStateError(f"Could not read saved downloads: {error}") from error
        if not isinstance(document, dict) or not isinstance(document.get("downloads"), list):
            raise DownloadStateError("Saved download metadata has an invalid format; it was not changed.")
        try:
            jobs = [DownloadJob(**self._validate_record(record)) for record in document["downloads"]]
        except (TypeError, ValueError, KeyError) as error:
            raise DownloadStateError(f"Saved download metadata contains invalid data: {error}") from error
        return jobs

    def _validate_record(self, record: Any) -> dict[str, Any]:
        if not isinstance(record, dict):
            raise ValueError("download record must be an object")
        app_type = record.get("app_type")
        if app_type not in APP_BUNDLE_NAMES:
            raise ValueError("unsupported application type")
        try:
            version = validate_version(record.get("version"))
            expected_url = archive_url(app_type, version)
        except (TypeError, VersionLookupError) as error:
            raise ValueError("invalid saved Roblox version") from error
        if record.get("url") != expected_url:
            raise ValueError("URL does not match the established application/version pattern")
        if record.get("state") not in _VALID_STATES:
            raise ValueError("invalid download state")
        if not isinstance(record.get("id"), str) or not record["id"]:
            raise ValueError("missing download ID")
        for field in ("archive_destination", "temporary_path"):
            if not isinstance(record.get(field), str) or not Path(record[field]).is_absolute():
                raise ValueError(f"{field} must be an absolute path")
        self._contained_path(record["temporary_path"], self.temporary_root)
        self._contained_path(record["archive_destination"], self.archive_root / app_type)
        if not isinstance(record.get("downloaded", 0), int) or record.get("downloaded", 0) < 0:
            raise ValueError("invalid downloaded byte count")
        total = record.get("total")
        if total is not None and (not isinstance(total, int) or total < 0):
            raise ValueError("invalid total byte count")
        for field in ("etag", "last_modified", "updated_at", "error"):
            value = record.get(field)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{field} must be a string or null")
        return {
            **record,
            "version": version,
            "downloaded": record.get("downloaded", 0),
            "total": total,
            "etag": record.get("etag"),
            "last_modified": record.get("last_modified"),
            "updated_at": record.get("updated_at") or "",
            "error": record.get("error"),
        }

    def _remove_stale_orphans(self, jobs: list[DownloadJob]) -> None:
        if not self.temporary_root.is_dir():
            return
        referenced = set()
        for job in jobs:
            try:
                referenced.add(self._safe_temp_path(job))
            except DownloadStateError:
                continue
        cutoff = time.time() - 30 * 24 * 60 * 60
        for path in self.temporary_root.iterdir():
            if path in referenced or path.is_symlink() or not path.is_file():
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue

    def _safe_temp_path(self, job: DownloadJob) -> Path:
        return self._contained_path(job.temporary_path, self.temporary_root)

    def _safe_archive_path(self, job: DownloadJob) -> Path:
        return self._contained_path(job.archive_destination, self.archive_root / job.app_type)

    @staticmethod
    def _contained_path(raw_path: str, root: Path) -> Path:
        path = Path(raw_path).absolute()
        root = root.absolute()
        try:
            path.relative_to(root)
        except ValueError as error:
            raise DownloadStateError("Saved download file points outside Roseblox-managed storage.") from error
        if path.is_symlink():
            raise DownloadStateError("Saved download file must not be a symbolic link.")
        return path

    @staticmethod
    def _replace(job: DownloadJob, **changes: Any) -> DownloadJob:
        changes.setdefault("updated_at", DownloadJobStore._now())
        return DownloadJob(**{**asdict(job), **changes})

    def _write(self, jobs: list[DownloadJob]) -> None:
        temporary: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({"version": 1, "downloads": [asdict(job) for job in jobs]}, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as error:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise DownloadStateError(f"Could not save download recovery data: {error}") from error

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
