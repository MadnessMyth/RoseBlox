"""Main application window and sidebar navigation."""

import threading

import gi
gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gtk

from .backend.installer import InstallerService
from .backend.library import BundleValidationError, InstallationRecord, LibraryStore, validate_bundle
from .backend.rosemary import detect_rosemary
from .backend.version_settings import VersionSettingsStore
from .pages import PAGE_DEFINITIONS, create_page


class RosebloxWindow(Adw.ApplicationWindow):
    """Main Roseblox window with a native stack sidebar."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.set_title("Roseblox")
        self.set_default_size(960, 640)
        self.set_size_request(680, 480)
        self.library = LibraryStore()
        self.version_settings = VersionSettingsStore()
        self.installer = InstallerService(self.library, version_settings=self.version_settings)
        self.rosemary_status = detect_rosemary()
        self.process_sessions = {}
        self.launching_installations = set()
        self.stopping_installations = set()
        self.library_records: list[InstallationRecord] = []
        self.library_validation: dict[str, str] = {}
        self.library_error: str | None = None
        self.library_loading = True
        self.download_recovery_jobs = []
        self.download_recovery_error: str | None = None
        self._library_loaded = False
        self._refresh_in_progress = False
        self._refresh_pending_library = False
        self._refresh_pending_recovery = False

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(root)

        header = Adw.HeaderBar()
        header.add_css_class("main-header")
        header.set_title_widget(Adw.WindowTitle(title="Roseblox", subtitle="Roblox through Rosemary"))
        root.append(header)

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        content.add_css_class("window-content")
        root.append(content)

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_transition_duration(160)
        self._stack.set_hexpand(True)
        self._stack.set_vexpand(True)
        self.pages = {}

        for page_id, title, icon_name, description in PAGE_DEFINITIONS:
            page = create_page(
                page_id,
                title,
                icon_name,
                description,
                window=self,
                store=self.library,
                installer=self.installer,
                rosemary_status=self.rosemary_status,
                library_error=self.library_error,
                version_settings=self.version_settings,
            )
            self.pages[page_id] = page
            self._stack.add_titled(page, page_id, title)

        sidebar = Gtk.StackSidebar()
        sidebar.set_stack(self._stack)
        sidebar.set_size_request(200, -1)
        sidebar.add_css_class("navigation-sidebar")
        content.append(sidebar)
        content.append(self._stack)
        self.refresh_pages(reload_library=True, reload_recovery=True)

    def refresh_pages(self, *, reload_library: bool = True, reload_recovery: bool = False) -> None:
        self._refresh_pending_library |= reload_library
        self._refresh_pending_recovery |= reload_recovery
        if not reload_library and not reload_recovery:
            self._apply_shared_state()
            return
        if self._refresh_in_progress:
            return
        self._start_pending_refresh()

    def _start_pending_refresh(self) -> None:
        load_library = self._refresh_pending_library
        load_recovery = self._refresh_pending_recovery
        self._refresh_pending_library = False
        self._refresh_pending_recovery = False
        self._refresh_in_progress = True
        if load_library and not self._library_loaded:
            self.library_loading = True
            self._apply_shared_state()
        threading.Thread(
            target=self._load_shared_state_worker,
            args=(load_library, load_recovery),
            daemon=True,
        ).start()

    def _load_shared_state_worker(self, load_library: bool, load_recovery: bool) -> None:
        records = None
        validation = None
        library_error = None
        recovery_jobs = None
        recovery_error = None
        if load_library:
            try:
                records = self.library.load()
                validation = {}
                for record in records:
                    try:
                        validate_bundle(record.bundle_path, record.app_type)
                        validation[record.id] = "Valid"
                    except (BundleValidationError, OSError):
                        validation[record.id] = "Missing or invalid"
            except Exception as error:
                records = []
                validation = {}
                library_error = (
                    f"{error} The library is read-only until the file is repaired; its contents were not changed."
                )
        if load_recovery:
            try:
                recovery_jobs = self.installer.recoverable_downloads()
            except Exception as error:
                recovery_jobs = []
                recovery_error = str(error)
        GLib.idle_add(
            self._shared_state_loaded,
            load_library,
            records,
            validation,
            library_error,
            load_recovery,
            recovery_jobs,
            recovery_error,
        )

    def _shared_state_loaded(
        self,
        load_library,
        records,
        validation,
        library_error,
        load_recovery,
        recovery_jobs,
        recovery_error,
    ) -> bool:
        if load_library:
            self.library_records = records or []
            self.library_validation = validation or {}
            self.library_error = library_error
            self.library_loading = False
            self._library_loaded = True
        if load_recovery:
            self.download_recovery_jobs = recovery_jobs or []
            self.download_recovery_error = recovery_error
        self._refresh_in_progress = False
        self._apply_shared_state()
        if self._refresh_pending_library or self._refresh_pending_recovery:
            self._start_pending_refresh()
        return GLib.SOURCE_REMOVE

    def _apply_shared_state(self) -> None:
        for page in self.pages.values():
            apply_library = getattr(page, "apply_library_snapshot", None)
            if apply_library is not None:
                apply_library(
                    self.library_records,
                    self.library_validation,
                    self.library_error,
                    loading=self.library_loading,
                )
            set_recovery = getattr(page, "set_recovery_jobs", None)
            if set_recovery is not None:
                set_recovery(self.download_recovery_jobs, self.download_recovery_error)
            apply_home = getattr(page, "apply_snapshot", None)
            if apply_home is not None:
                apply_home(
                    self.library_records,
                    self.library_validation,
                    self.library_error,
                    loading=self.library_loading,
                )

    def navigate_to(self, page_id: str) -> None:
        if page_id in self.pages:
            self._stack.set_visible_child_name(page_id)
