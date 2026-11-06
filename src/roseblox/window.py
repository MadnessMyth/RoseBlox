"""Main application window and sidebar navigation."""

import threading

import gi
gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gtk

from .backend.installer import InstallerService
from .backend.library import BundleValidationError, InstallationRecord, LibraryStore, validate_bundle
from .backend.preferences import (
    PreferredInstallations,
    PreferencesStore,
    resolve_preferred_installation,
)
from .backend.rosemary import detect_rosemary, save_rosemary_executable
from .backend.setup import SetupState, SetupStore
from .backend.url_handlers import URLHandlerService, URLHandlerStatus
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
        self.preferences_store = PreferencesStore()
        self.version_settings = VersionSettingsStore()
        self.setup_store = SetupStore()
        self.url_handlers = URLHandlerService(self.setup_store)
        self.setup_state = SetupState()
        self.url_handler_status = URLHandlerStatus(False, False, "URL-handler status has not been checked.")
        self.setup_error = None
        self.installer = InstallerService(self.library, version_settings=self.version_settings)
        self.rosemary_status = detect_rosemary()
        self.process_sessions = {}
        self.launching_installations = set()
        self.stopping_installations = set()
        self.library_records: list[InstallationRecord] = []
        self.library_validation: dict[str, str] = {}
        self.preferred_installations = PreferredInstallations()
        self.resolved_preferred_ids = {"player": None, "studio": None}
        self.preference_errors: dict[str, str] = {}
        self.library_error: str | None = None
        self.library_loading = True
        self.download_recovery_jobs = []
        self.download_recovery_error: str | None = None
        self._library_loaded = False
        self._refresh_in_progress = False
        self._refresh_pending_library = False
        self._refresh_pending_recovery = False
        self._refresh_pending_preferences = False
        self._refresh_pending_setup = False
        self._snapshot_generation = 0

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
        self.settings_page = None

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
                setup_store=self.setup_store,
                url_handlers=self.url_handlers,
            )
            self.pages[page_id] = page
            self._stack.add_titled(page, page_id, title)
            if page_id == "settings":
                self.settings_page = page.get_child()

        from .pages.onboarding import OnboardingPage
        self.onboarding_page = OnboardingPage(self, self.setup_store, self.url_handlers)
        setup_scroller = Gtk.ScrolledWindow()
        setup_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        setup_scroller.set_child(self.onboarding_page)
        self._stack.add_named(setup_scroller, "setup")

        sidebar = Gtk.StackSidebar()
        sidebar.set_stack(self._stack)
        sidebar.set_size_request(200, -1)
        sidebar.add_css_class("navigation-sidebar")
        content.append(sidebar)
        content.append(self._stack)
        self.refresh_pages(reload_library=True, reload_recovery=True, reload_preferences=True)

    def refresh_pages(
        self,
        *,
        reload_library: bool = True,
        reload_recovery: bool = False,
        reload_preferences: bool = False,
    ) -> None:
        if reload_library:
            reload_preferences = True
        self._refresh_pending_library |= reload_library
        self._refresh_pending_recovery |= reload_recovery
        self._refresh_pending_preferences |= reload_preferences
        self._refresh_pending_setup = (
            getattr(self, "_refresh_pending_setup", False)
            or reload_preferences
            or reload_library
        )
        if not reload_library and not reload_recovery and not reload_preferences:
            self._apply_shared_state()
            return
        self._snapshot_generation += 1
        if self._refresh_in_progress:
            return
        self._start_pending_refresh()

    def _start_pending_refresh(self) -> None:
        load_library = self._refresh_pending_library
        load_recovery = self._refresh_pending_recovery
        load_preferences = self._refresh_pending_preferences
        load_setup = getattr(self, "_refresh_pending_setup", False)
        self._refresh_pending_library = False
        self._refresh_pending_recovery = False
        self._refresh_pending_preferences = False
        self._refresh_pending_setup = False
        self._refresh_in_progress = True
        generation = self._snapshot_generation
        if load_library and not self._library_loaded:
            self.library_loading = True
            self._apply_shared_state()
        threading.Thread(
            target=self._load_shared_state_worker,
            args=(load_library, load_recovery, load_preferences, generation, load_setup),
            daemon=True,
        ).start()

    def _load_shared_state_worker(
        self,
        load_library: bool,
        load_recovery: bool,
        load_preferences: bool = False,
        generation: int = 0,
        load_setup: bool = False,
    ) -> None:
        records = None
        validation = None
        library_error = None
        recovery_jobs = None
        recovery_error = None
        preferences = None
        resolved_preferred_ids = None
        preference_errors = None
        setup_state = None
        setup_error = None
        handler_status = None
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
        if load_preferences:
            preferences = self.preferences_store.load()
            snapshot_records = records if records is not None else self.library_records
            snapshot_validation = validation if validation is not None else self.library_validation
            resolved_preferred_ids = {
                app_type: resolve_preferred_installation(
                    app_type, preferences, snapshot_records, snapshot_validation
                )
                for app_type in ("player", "studio")
            }
            preference_errors = dict(preferences.errors or {})
        if load_setup:
            try:
                setup_state = self.setup_store.load()
                handler_status = self.url_handlers.check()
            except Exception as error:
                setup_error = str(error)[:500]
        GLib.idle_add(
            self._shared_state_loaded,
            generation,
            load_library,
            records,
            validation,
            library_error,
            load_recovery,
            recovery_jobs,
            recovery_error,
            load_preferences,
            preferences,
            resolved_preferred_ids,
            preference_errors,
            load_setup,
            setup_state,
            setup_error,
            handler_status,
        )

    def _shared_state_loaded(
        self,
        generation,
        load_library,
        records,
        validation,
        library_error,
        load_recovery,
        recovery_jobs,
        recovery_error,
        load_preferences=False,
        preferences=None,
        resolved_preferred_ids=None,
        preference_errors=None,
        load_setup=False,
        setup_state=None,
        setup_error=None,
        handler_status=None,
    ) -> bool:
        if generation != self._snapshot_generation:
            self._refresh_pending_library |= load_library
            self._refresh_pending_recovery |= load_recovery
            self._refresh_pending_preferences |= load_preferences
            self._refresh_pending_setup = getattr(self, "_refresh_pending_setup", False) or load_setup
            self._refresh_in_progress = False
            self._start_pending_refresh()
            return GLib.SOURCE_REMOVE
        if load_library:
            self.library_records = records or []
            self.library_validation = validation or {}
            self.library_error = library_error
            self.library_loading = False
            self._library_loaded = True
        if load_recovery:
            self.download_recovery_jobs = recovery_jobs or []
            self.download_recovery_error = recovery_error
        if load_preferences:
            self.preferred_installations = preferences or PreferredInstallations()
            self.resolved_preferred_ids = resolved_preferred_ids or {"player": None, "studio": None}
            self.preference_errors = preference_errors or {}
        if load_setup:
            if setup_state is not None:
                self.setup_state = setup_state
            self.setup_error = setup_error
            self.url_handler_status = handler_status
            self.onboarding_page.apply_snapshot(
                self.rosemary_status, self.setup_state, setup_error, handler_status,
                self.library_records, self.library_validation,
            )
            if self.settings_page is not None:
                self.settings_page.apply_setup_snapshot(
                    self.rosemary_status, self.setup_state, setup_error, handler_status
                )
            if not getattr(self, "_initial_page_chosen", False):
                self._initial_page_chosen = True
                self._stack.set_visible_child_name(
                    "home" if self.setup_state and self.setup_state.onboarding_completed else "setup"
                )
        self._refresh_in_progress = False
        self._apply_shared_state()
        if (
            self._refresh_pending_library
            or self._refresh_pending_recovery
            or self._refresh_pending_preferences
            or getattr(self, "_refresh_pending_setup", False)
        ):
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
                    preferred_ids={
                        "player": self.preferred_installations.player_id,
                        "studio": self.preferred_installations.studio_id,
                    },
                    resolved_preferred_ids=self.resolved_preferred_ids,
                    preference_errors=self.preference_errors,
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
                    preferred_ids={
                        "player": self.preferred_installations.player_id,
                        "studio": self.preferred_installations.studio_id,
                    },
                    resolved_preferred_ids=self.resolved_preferred_ids,
                    preference_errors=self.preference_errors,
                )

    def navigate_to(self, page_id: str) -> None:
        if page_id == "setup":
            self._stack.set_visible_child_name("setup")
        elif page_id in self.pages:
            self._stack.set_visible_child_name(page_id)

    def choose_rosemary_executable(self) -> None:
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Choose Rosemary executable")
        dialog.open(self, None, self._rosemary_file_chosen, None)

    def _rosemary_file_chosen(self, dialog, result, _data=None) -> None:
        try:
            file = dialog.open_finish(result)
            path = file.get_path()
            if not path:
                raise ValueError("Choose a local Rosemary executable file.")
        except Exception as error:
            if "dismiss" not in str(error).lower() and "cancel" not in str(error).lower():
                self._show_window_error("Could not choose Rosemary", str(error))
            return
        self.save_rosemary_choice(path)

    def save_rosemary_choice(self, path: str | None) -> None:
        threading.Thread(target=self._save_rosemary_worker, args=(path,), daemon=True).start()

    def _save_rosemary_worker(self, path):
        try:
            save_rosemary_executable(path)
            status = detect_rosemary()
            error = None
        except Exception as failure:
            status = None
            error = str(failure)
        GLib.idle_add(self._rosemary_saved, status, error)

    def _rosemary_saved(self, status, error):
        if error:
            self._show_window_error("Could not configure Rosemary", error)
        else:
            self.rosemary_status = status
            for page in self.pages.values():
                if hasattr(page, "rosemary_status"):
                    page.rosemary_status = status
                elif hasattr(page, "get_child") and hasattr(page.get_child(), "apply_setup_snapshot"):
                    page.get_child().apply_setup_snapshot(
                        status,
                        getattr(self, "setup_state", None),
                        None,
                        getattr(self, "url_handler_status", None),
                    )
            self.onboarding_page._status = status
            self.onboarding_page.apply_snapshot(
                status, getattr(self, "setup_state", None), None,
                getattr(self, "url_handler_status", None), self.library_records, self.library_validation,
            )
        return GLib.SOURCE_REMOVE

    def _show_window_error(self, title: str, message: str) -> None:
        dialog = Adw.MessageDialog.new(self, title, message[:1000])
        dialog.add_response("close", "Close")
        dialog.set_default_response("close")
        dialog.present()

    def handle_roblox_url(self, uri: str) -> bool:
        from .backend.url_handlers import parse_roblox_url
        try:
            parsed = parse_roblox_url(uri)
        except ValueError as error:
            self._show_window_error("Unsupported Roblox link", str(error))
            return GLib.SOURCE_REMOVE
        app_type = parsed.app_type
        self.navigate_to(app_type)
        controller = self.pages.get(app_type)
        records = [record for record in self.library_records if record.app_type == app_type]
        preferred = self.resolved_preferred_ids.get(app_type)
        record = next(
            (
                item for item in records
                if item.id == preferred and self.library_validation.get(item.id) == "Valid"
            ),
            None,
        )
        if record is None:
            record = next((item for item in records if self.library_validation.get(item.id) == "Valid"), None)
        if record is None:
            self._show_window_error(
                "Roblox installation required",
                f"No valid {('Player' if app_type == 'player' else 'Studio')} installation is registered. Use the {app_type.title()} page to install or import it, then open the link again.",
            )
            return GLib.SOURCE_REMOVE
        controller.request_launch_url(record, parsed.uri)
        return GLib.SOURCE_REMOVE
