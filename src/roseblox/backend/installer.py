"""Managed Roblox download, archive import, extraction, and registration."""

from __future__ import annotations

import os
import errno
import re
import shutil
import tempfile
import threading
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .archive import ArchiveTypeUndetermined, extract_archive, inspect_archive
from .downloader import DownloadCancelled, DownloadProgress, download_archive
from .download_state import DownloadJob, DownloadJobStore
from .library import (
    APP_BUNDLE_NAMES,
    InstallationRecord,
    LibraryStore,
    default_data_directory,
    managed_install_root,
    validate_bundle,
)
from .versions import VersionCache, VersionClient, VersionInfo, archive_url
from .version_settings import VersionSettingsStore


class InstallationBusyError(RuntimeError):
    """Raised when an installation of this application type is already active."""


class InstallationConflictError(RuntimeError):
    """Raised rather than replacing an existing version or managed destination."""


@dataclass(frozen=True)
class InstallProgress:
    phase: str
    downloaded: int = 0
    total: int | None = None


@dataclass(frozen=True)
class InstallResult:
    record: InstallationRecord
    archive_path: Path | None
    already_installed: bool = False


ProgressCallback = Callable[[InstallProgress], None]
_ARCHIVE_VERSION_PATTERNS = {
    "player": re.compile(r"^(version-[0-9a-f]{16})-RobloxPlayer\.zip$", re.IGNORECASE),
    "studio": re.compile(r"^(version-[0-9a-f]{16})-RobloxStudioApp\.zip$", re.IGNORECASE),
}


class InstallerService:
    def __init__(
        self,
        library: LibraryStore,
        *,
        version_client: VersionClient | None = None,
        version_cache: VersionCache | None = None,
        version_settings: VersionSettingsStore | None = None,
        data_directory: Path | None = None,
        bsdtar_path: str | None = None,
    ) -> None:
        self.library = library
        self.version_client = version_client or VersionClient()
        self.version_cache = version_cache or VersionCache()
        self.version_settings = version_settings or VersionSettingsStore()
        self.data_directory = Path(data_directory) if data_directory else default_data_directory()
        self.bsdtar_path = bsdtar_path
        self._locks = {"player": threading.Lock(), "studio": threading.Lock()}
        self.download_jobs = DownloadJobStore(self.data_directory)

    @property
    def install_root(self) -> Path:
        if self.data_directory == default_data_directory():
            return managed_install_root()
        return self.data_directory / "installations"

    def download_latest(
        self,
        app_type: str,
        cancel_event: threading.Event,
        progress_callback: ProgressCallback | None = None,
    ) -> InstallResult:
        with self._application_lock(app_type):
            self._emit(progress_callback, InstallProgress("checking-version"))
            if cancel_event.is_set():
                raise DownloadCancelled("Download cancelled.")
            source = self.version_settings.load_for(app_type)
            info = self.version_client.fetch(app_type, source)
            self.version_cache.update(app_type, info)
            if cancel_event.is_set():
                raise DownloadCancelled("Download cancelled before the archive download began.")

            matching = [
                record
                for record in self.library.load()
                if record.app_type == app_type and record.version == info.version
            ]
            for available in matching:
                try:
                    validate_bundle(available.bundle_path, available.app_type)
                except (ValueError, OSError):
                    continue
                return InstallResult(
                    available,
                    Path(available.archive_path) if available.archive_path else None,
                    already_installed=True,
                )

            saved = [
                job
                for job in self.download_jobs.load(app_type)
                if job.version == info.version and job.state in {"interrupted", "downloaded"}
            ]
            if saved:
                raise InstallationConflictError(
                    f"A saved {app_type.title()} download for {info.version} already exists. Resume or install it "
                    "from the recovery controls before starting another download."
                )

            version_directory = self.install_root / app_type / info.version
            if version_directory.exists():
                raise InstallationConflictError(
                    f"A {app_type.title()} installation directory already exists for {info.version}; "
                    "Roseblox will not overwrite it."
                )
            url = archive_url(app_type, info.version)
            archive_root = self.data_directory / "archives" / app_type
            archive_root.mkdir(parents=True, exist_ok=True)
            archive_name = (
                f"{info.version}-RobloxPlayer-{uuid.uuid4().hex[:8]}.zip"
                if app_type == "player"
                else f"{info.version}-RobloxStudioApp-{uuid.uuid4().hex[:8]}.zip"
            )
            job = self.download_jobs.create(
                app_type,
                info.version,
                uuid.uuid4().hex,
                archive_root / archive_name,
            )
            return self._download_and_install(job, cancel_event, progress_callback)

    def recoverable_downloads(self, app_type: str | None = None) -> list[DownloadJob]:
        return [job for job in self.download_jobs.load(app_type) if job.state in {"interrupted", "downloaded", "invalid"}]

    def retry_download(
        self,
        job_id: str,
        cancel_event: threading.Event,
        progress_callback: ProgressCallback | None = None,
    ) -> InstallResult:
        job = self.download_jobs.get(job_id)
        with self._application_lock(job.app_type):
            if job.state not in {"interrupted", "downloaded"}:
                raise ValueError(f"This saved download cannot be resumed (state: {job.state}).")
            if job.state == "interrupted":
                job = self._resume_job(job, cancel_event, progress_callback)
            result = self._install_archive(
                archive_path=Path(job.archive_destination),
                app_type=job.app_type,
                version=job.version,
                source="download",
                display_name=f"Roblox {job.app_type.title()} {job.version}",
                progress_callback=progress_callback,
            )
            self.download_jobs.update(job.id, state="installed", error=None)
            return result

    def discard_download(self, job_id: str) -> None:
        self.download_jobs.discard(job_id)

    def _download_and_install(
        self,
        job: DownloadJob,
        cancel_event: threading.Event,
        progress_callback: ProgressCallback | None,
    ) -> InstallResult:
        job = self._resume_job(job, cancel_event, progress_callback)
        result = self._install_archive(
            archive_path=Path(job.archive_destination),
            app_type=job.app_type,
            version=job.version,
            source="download",
            display_name=f"Roblox {job.app_type.title()} {job.version}",
            progress_callback=progress_callback,
        )
        self.download_jobs.update(job.id, state="installed", error=None)
        return result

    def _resume_job(
        self,
        job: DownloadJob,
        cancel_event: threading.Event,
        progress_callback: ProgressCallback | None,
    ) -> DownloadJob:
        temporary = self.download_jobs._safe_temp_path(job)
        destination = self.download_jobs._safe_archive_path(job)
        existing_size = temporary.stat().st_size if temporary.is_file() else 0
        job = self.download_jobs.update(
            job.id,
            state="downloading",
            downloaded=existing_size,
            error=None,
        )
        last_persisted = job.downloaded

        def on_metadata(etag, modified, total):
            nonlocal job
            job = self.download_jobs.update(
                job.id,
                etag=etag,
                last_modified=modified,
                total=total,
            )

        def on_download(progress: DownloadProgress) -> None:
            nonlocal job, last_persisted
            if progress.downloaded - last_persisted >= 4 * 1024 * 1024 or progress.downloaded == progress.total:
                job = self.download_jobs.update(
                    job.id,
                    downloaded=progress.downloaded,
                    total=progress.total,
                )
                last_persisted = progress.downloaded
            self._emit(
                progress_callback,
                InstallProgress("downloading", progress.downloaded, progress.total),
            )

        try:
            if cancel_event.is_set():
                raise DownloadCancelled("Download cancelled before transfer began.")
            downloaded = download_archive(
                job.url,
                temporary.parent,
                cancel_event,
                on_download,
                partial_path=temporary,
                resume=temporary.exists(),
                etag=job.etag,
                last_modified=job.last_modified,
                metadata_callback=on_metadata,
                preserve_partial=True,
            )
            if cancel_event.is_set():
                raise DownloadCancelled("Download cancelled after transfer; the partial file was retained for recovery.")
            if not zipfile.is_zipfile(downloaded):
                raise RuntimeError("The completed transfer is not a readable ZIP archive; it remains available for retry.")
            size = downloaded.stat().st_size
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Linking is atomic and refuses to replace any previous archive.
            try:
                os.link(downloaded, destination)
            except FileExistsError as error:
                raise InstallationConflictError(
                    f"Download archive destination already exists: {destination}"
                ) from error
            downloaded.unlink()
            job = self.download_jobs.update(
                job.id,
                state="downloaded",
                downloaded=size,
                total=size,
                error=None,
            )
            self._emit(progress_callback, InstallProgress("downloaded", size, size))
            return job
        except Exception as error:
            size = temporary.stat().st_size if temporary.is_file() else 0
            try:
                self.download_jobs.update(
                    job.id,
                    state="interrupted",
                    downloaded=size,
                    error=str(error),
                )
            except Exception as persistence_error:
                raise RuntimeError(
                    f"{error} The partial download remains at {temporary}, but recovery metadata could not be updated: "
                    f"{persistence_error}"
                ) from persistence_error
            raise

    def import_archive(
        self,
        archive_path: str | Path,
        *,
        app_type: str | None = None,
        display_name: str | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> InstallResult:
        archive_path = Path(archive_path).expanduser().absolute()
        if not archive_path.is_file():
            raise FileNotFoundError(f"Archive does not exist: {archive_path}")
        inspection = inspect_archive(archive_path)
        if app_type is None:
            if inspection.detected_type is None:
                raise ArchiveTypeUndetermined(
                    "Roseblox could not determine whether this ZIP contains Player or Studio. Select the application type."
                )
            app_type = inspection.detected_type
        elif app_type not in APP_BUNDLE_NAMES:
            raise ValueError(f"Unsupported application type: {app_type}")
        if inspection.app_types and app_type not in inspection.app_types:
            raise ArchiveTypeUndetermined(
                f"The archive does not appear to contain a {APP_BUNDLE_NAMES[app_type]} bundle."
            )

        version = self._version_from_archive_name(archive_path, app_type)
        display_name = display_name or self._default_display_name(app_type, version)
        with self._application_lock(app_type):
            return self._install_archive(
                archive_path=archive_path,
                app_type=app_type,
                version=version,
                source="archive_import",
                display_name=display_name,
                progress_callback=progress_callback,
            )

    def _install_archive(
        self,
        *,
        app_type: str,
        version: str | None,
        source: str,
        display_name: str,
        progress_callback: ProgressCallback | None,
        archive_path: Path | None = None,
        url: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> InstallResult:
        if (archive_path is None) == (url is None):
            raise ValueError("Provide exactly one archive path or download URL.")

        self.data_directory.mkdir(parents=True, exist_ok=True)
        staging_root = self.data_directory / ".staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        downloaded_archive: Path | None = None
        committed_archive: Path | None = None
        version_directory: Path | None = None
        registered = False
        preserve_stage = False
        stage = Path(tempfile.mkdtemp(prefix="install-", dir=staging_root))

        try:
            if url is not None:
                if cancel_event is None:
                    raise ValueError("A cancellation event is required for downloads.")
                self._emit(progress_callback, InstallProgress("downloading"))

                def on_download(progress: DownloadProgress) -> None:
                    self._emit(
                        progress_callback,
                        InstallProgress("downloading", progress.downloaded, progress.total),
                    )

                downloaded_archive = download_archive(
                    url,
                    stage,
                    cancel_event,
                    on_download,
                )
                archive_path = downloaded_archive
                if cancel_event.is_set():
                    raise DownloadCancelled("Download completed, but installation was cancelled before extraction.")

            if archive_path is None:
                raise ValueError("Archive path was not resolved.")
            self._emit(progress_callback, InstallProgress("extracting"))
            bundle = extract_archive(
                archive_path,
                stage / "extracted",
                app_type,
                bsdtar_path=self.bsdtar_path,
            )
            bundle = validate_bundle(bundle, app_type)
            if version is None:
                version = self._version_from_archive_name(archive_path, app_type)

            install_root = self.install_root
            app_install_root = install_root / app_type
            app_install_root.mkdir(parents=True, exist_ok=True)
            segment = version or f"unknown-{uuid.uuid4().hex}"
            version_directory = app_install_root / segment
            try:
                version_directory.mkdir(exist_ok=False)
            except FileExistsError as error:
                raise InstallationConflictError(
                    f"Installation destination already exists: {version_directory}. Roseblox will not overwrite it."
                ) from error
            installed_bundle = version_directory / APP_BUNDLE_NAMES[app_type]
            os.rename(bundle, installed_bundle)

            if downloaded_archive is not None:
                self._emit(progress_callback, InstallProgress("finalizing-archive"))
                committed_archive = self._preserve_download(downloaded_archive, app_type, version)
                archive_path = committed_archive

            self._emit(progress_callback, InstallProgress("registering"))
            record = self.library.add(
                app_type,
                installed_bundle,
                display_name,
                version=version,
                managed=True,
                source=source,
                archive_path=archive_path,
            )
            registered = True
            self._emit(progress_callback, InstallProgress("complete"))
            return InstallResult(record, Path(archive_path))
        except Exception as error:
            preserved_archive = committed_archive
            preservation_error: OSError | None = None
            if downloaded_archive is not None and downloaded_archive.exists() and committed_archive is None:
                try:
                    preserved_archive = self._preserve_download(downloaded_archive, app_type, version)
                except OSError as write_error:
                    preserved_archive = downloaded_archive
                    preserve_stage = True
                    preservation_error = write_error
            if version_directory is not None and not registered:
                shutil.rmtree(version_directory, ignore_errors=True)
            if preserved_archive is not None:
                message = f"{error} The completed archive was preserved at {preserved_archive}."
                if preservation_error:
                    message = (
                        f"{error} Roseblox could not move the complete archive to managed storage: "
                        f"{preservation_error}. It remains at {preserved_archive}."
                    )
                if isinstance(error, DownloadCancelled):
                    raise DownloadCancelled(message) from error
                raise RuntimeError(message) from error
            raise
        finally:
            if not preserve_stage:
                shutil.rmtree(stage, ignore_errors=True)

    def _preserve_download(self, source: Path, app_type: str, version: str | None) -> Path:
        archive_root = self.data_directory / "archives" / app_type
        archive_root.mkdir(parents=True, exist_ok=True)
        bundle_stem = "RobloxPlayer" if app_type == "player" else "RobloxStudioApp"
        base = version or "unversioned"
        while True:
            destination = archive_root / f"{base}-{bundle_stem}-{uuid.uuid4().hex[:8]}.zip"
            try:
                # A hard link is an atomic no-overwrite move on the shared XDG filesystem.
                os.link(source, destination)
                source.unlink()
                return destination
            except FileExistsError:
                continue
            except OSError as error:
                if error.errno == errno.EXDEV:
                    break
                raise

        destination = archive_root / f"{base}-{bundle_stem}-{uuid.uuid4().hex[:8]}.zip"
        try:
            with source.open("rb") as input_stream, destination.open("xb") as output_stream:
                shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
                output_stream.flush()
                os.fsync(output_stream.fileno())
        except OSError:
            destination.unlink(missing_ok=True)
            raise
        source.unlink()
        return destination

    def _application_lock(self, app_type: str):
        if app_type not in self._locks:
            raise ValueError(f"Unsupported application type: {app_type}")
        lock = self._locks[app_type]
        if not lock.acquire(blocking=False):
            raise InstallationBusyError(f"A {app_type.title()} download or import is already in progress.")

        class _LockContext:
            def __enter__(self_nonlocal):
                return lock

            def __exit__(self_nonlocal, _type, _value, _traceback):
                lock.release()

        return _LockContext()

    @staticmethod
    def _emit(callback: ProgressCallback | None, progress: InstallProgress) -> None:
        if callback is not None:
            callback(progress)

    @staticmethod
    def _version_from_archive_name(archive_path: Path, app_type: str) -> str | None:
        pattern = _ARCHIVE_VERSION_PATTERNS[app_type]
        match = pattern.fullmatch(archive_path.name)
        return match.group(1) if match else None

    @staticmethod
    def _default_display_name(app_type: str, version: str | None) -> str:
        label = "Player" if app_type == "player" else "Studio"
        return f"Roblox {label} {version}" if version else f"Imported Roblox {label}"
