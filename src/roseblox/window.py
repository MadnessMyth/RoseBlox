"""Main application window and sidebar navigation."""

import threading
import time

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
from .backend.rosemary import RosemaryStatus, detect_rosemary, save_rosemary_executable
from .backend.rosemary_install import (
    RosemaryInstallError,
    RosemaryInstallState,
    plan_rosemary_install,
    start_rosemary_install,
)
from .backend.setup import SetupSettingsError, SetupState, SetupStore
from .backend.updates import UpdateCheckerService
from .backend.url_handlers import URLHandlerService, URLHandlerStatus
from .backend.version_settings import VersionSettingsStore
from .pages import PAGE_DEFINITIONS, create_page

# Building the AUR package compiles Rosemary from source, which can take a long time.
_ROSEMARY_INSTALL_WAIT_SECONDS = 2 * 60 * 60
# Gives Rosemary time to fail visibly before the launcher closes behind Roblox.
CLOSE_AFTER_LAUNCH_DELAY_SECONDS = 5


def _scrolled_content(widget: Gtk.Widget) -> Gtk.Widget:
    """Return the page inside a ScrolledWindow, past the Viewport GTK may insert."""
    child = widget.get_child() if isinstance(widget, Gtk.ScrolledWindow) else widget
    if isinstance(child, Gtk.Viewport):
        child = child.get_child()
    return child


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
        self.update_checker = UpdateCheckerService(self.setup_store)
        self.update_checker_status = None
        self.update_check_rows = []
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
        self._pending_roblox_url: str | None = None
        self._pending_update: str | None = None
        self.rosemary_install = RosemaryInstallState()
        self._rosemary_install_process = None
        self._rosemary_install_deadline = 0.0

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(root)

        header = Adw.HeaderBar()
        header.add_css_class("main-header")
        self._window_title = Adw.WindowTitle(title="Roseblox", subtitle="Roblox through Rosemary")
        header.set_title_widget(self._window_title)
        self._header = header
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
        self.performance_page = None
        self.mods_page = None

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
            if hasattr(page, "set_rosemary_status"):
                page.set_rosemary_status(self.rosemary_status)
            if page_id == "settings":
                self.settings_page = _scrolled_content(page)
            elif page_id == "performance":
                self.performance_page = _scrolled_content(page)
            elif page_id == "mods":
                self.mods_page = _scrolled_content(page)

        from .pages.notification_center import NotificationCenter
        self.notification_center = NotificationCenter(self)
        self._header.pack_end(self.notification_center)
        for page in self.pages.values():
            if hasattr(page, "add_operation_listener"):
                page.add_operation_listener(self.notification_center.operation_changed)

        from .pages.onboarding import OnboardingPage
        self.onboarding_page = OnboardingPage(self, self.setup_store, self.url_handlers)
        setup_scroller = Gtk.ScrolledWindow()
        setup_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        setup_scroller.set_child(self.onboarding_page)
        self._stack.add_named(setup_scroller, "setup")

        self._sidebar = Gtk.StackSidebar()
        self._sidebar.set_stack(self._stack)
        self._sidebar.set_size_request(200, -1)
        self._sidebar.add_css_class("navigation-sidebar")
        content.append(self._sidebar)
        content.append(self._stack)
        self._stack.connect("notify::visible-child-name", self._visible_page_changed)
        try:
            self.setup_state = self.setup_store.load()
        except SetupSettingsError:
            pass
        else:
            self._initial_page_chosen = True
            self._stack.set_visible_child_name("home" if self.setup_state.onboarding_completed else "setup")
        self._visible_page_changed(self._stack)
        self.refresh_pages(reload_library=True, reload_recovery=True, reload_preferences=True)

    def _visible_page_changed(self, stack, _pspec=None) -> None:
        in_setup = stack.get_visible_child_name() == "setup"
        self._sidebar.set_visible(not in_setup)
        self._window_title.set_subtitle("First-time setup" if in_setup else "Roblox through Rosemary")

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
        update_status = None
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
            update_status = self.update_checker.status()
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
            update_status,
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
        update_status=None,
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
            if update_status is not None:
                self.apply_update_checker_status(update_status, reload=False)
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
        if self._library_loaded:
            self.notification_center.library_ready()
        pending_url = getattr(self, "_pending_roblox_url", None)
        if pending_url is not None and not self.library_loading:
            self._pending_roblox_url = None
            GLib.idle_add(self.handle_roblox_url, pending_url)
        pending_update = getattr(self, "_pending_update", None)
        if pending_update is not None and not self.library_loading:
            self._pending_update = None
            GLib.idle_add(self.install_update, pending_update)
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
            self._apply_rosemary_status(status)
        return GLib.SOURCE_REMOVE

    def _apply_rosemary_status(self, status: RosemaryStatus) -> None:
        self.rosemary_status = status
        for page in self.pages.values():
            if hasattr(page, "rosemary_status"):
                page.rosemary_status = status
            if hasattr(page, "set_rosemary_status"):
                page.set_rosemary_status(status)
        if self.settings_page is not None:
            self.settings_page.apply_setup_snapshot(
                status,
                getattr(self, "setup_state", None),
                None,
                getattr(self, "url_handler_status", None),
            )
        self.onboarding_page.apply_snapshot(
            status, getattr(self, "setup_state", None), None,
            getattr(self, "url_handler_status", None), self.library_records, self.library_validation,
        )
        if status.available and self.rosemary_install.waiting:
            self._finish_rosemary_install(RosemaryInstallState(False, f"Rosemary is ready at {status.executable}."))

    def recheck_rosemary(self) -> None:
        threading.Thread(target=self._recheck_rosemary_worker, daemon=True).start()

    def _recheck_rosemary_worker(self) -> None:
        status = detect_rosemary()
        GLib.idle_add(self._rosemary_rechecked, status)

    def _rosemary_rechecked(self, status: RosemaryStatus) -> bool:
        if status != self.rosemary_status or self.rosemary_install.waiting:
            self._apply_rosemary_status(status)
        return GLib.SOURCE_REMOVE

    def install_rosemary(self) -> None:
        """Open a terminal running the AUR helper, then watch for Rosemary to appear."""
        if self.rosemary_install.waiting:
            return
        try:
            plan = plan_rosemary_install()
            self._rosemary_install_process = start_rosemary_install(plan)
        except RosemaryInstallError as error:
            self._set_rosemary_install_state(RosemaryInstallState(False, str(error), error=True))
            return
        self._rosemary_install_deadline = time.monotonic() + _ROSEMARY_INSTALL_WAIT_SECONDS
        self._set_rosemary_install_state(
            RosemaryInstallState(
                True,
                f"Continue in the terminal running “{plan.display_command}”. "
                "Roseblox will detect Rosemary as soon as the installation finishes.",
            )
        )
        GLib.timeout_add_seconds(2, self._poll_rosemary_install)

    def stop_waiting_for_rosemary(self) -> None:
        if self.rosemary_install.waiting:
            self._finish_rosemary_install(
                RosemaryInstallState(False, "Stopped waiting. Use Check Again after the installation finishes.")
            )

    def _poll_rosemary_install(self) -> bool:
        if not self.rosemary_install.waiting:
            return GLib.SOURCE_REMOVE
        process = self._rosemary_install_process
        exit_status = process.poll() if process is not None else None
        if exit_status not in (None, 0):
            self._finish_rosemary_install(
                RosemaryInstallState(
                    False,
                    "The installation did not finish. Review the terminal output, then try again.",
                    error=True,
                )
            )
            self.recheck_rosemary()
            return GLib.SOURCE_REMOVE
        if time.monotonic() > self._rosemary_install_deadline:
            self.stop_waiting_for_rosemary()
            return GLib.SOURCE_REMOVE
        self.recheck_rosemary()
        return GLib.SOURCE_CONTINUE

    def _finish_rosemary_install(self, state: "RosemaryInstallState") -> None:
        self._rosemary_install_process = None
        self._set_rosemary_install_state(state)

    def _set_rosemary_install_state(self, state: "RosemaryInstallState") -> None:
        self.rosemary_install = state
        self.onboarding_page.apply_rosemary_install_state(state)
        if self.settings_page is not None:
            self.settings_page.apply_rosemary_install_state(state)

    def operation_in_progress(self) -> bool:
        """Whether closing the window now would interrupt work the user started."""
        return self.rosemary_install.waiting or any(
            getattr(page, "operation_busy", False) for page in self.pages.values()
        )

    def running_session(self, app_type: str):
        """The ``(record, session)`` of a running Player or Studio that Roseblox started, if any."""
        records = {record.id: record for record in self.library_records}
        for page in self.pages.values():
            sessions = getattr(page, "_sessions", None)
            if sessions is None and isinstance(page, Gtk.ScrolledWindow):
                sessions = getattr(_scrolled_content(page), "_sessions", None)
            for installation_id, session in dict(sessions or {}).items():
                record = records.get(installation_id)
                if record is not None and record.app_type == app_type and session.process.poll() is None:
                    return record, session
        return None

    def application_started(self, app_type: str, session, from_link: bool) -> None:
        try:
            preference = self.setup_store.load().close_preference(app_type)
        except SetupSettingsError:
            return
        if preference.applies(from_link):
            GLib.timeout_add(
                int(CLOSE_AFTER_LAUNCH_DELAY_SECONDS * 1000), self._close_after_launch, session
            )

    def _close_after_launch(self, session) -> bool:
        if session.process.poll() is None and not self.operation_in_progress():
            self.close()
        return GLib.SOURCE_REMOVE

    def _show_window_error(self, title: str, message: str) -> None:
        dialog = Adw.MessageDialog.new(self, title, message[:1000])
        dialog.add_response("close", "Close")
        dialog.set_default_response("close")
        dialog.present()

    def apply_appearance(self, appearance) -> str | None:
        """Save and apply an appearance; returns an error message if it could not be saved."""
        theme = getattr(self.get_application(), "theme", None)
        if theme is not None:
            theme.apply(appearance)
        try:
            self.setup_state = self.setup_store.save_appearance(appearance)
        except (SetupSettingsError, ValueError) as error:
            return str(error)[:300]
        return None

    def apply_update_checker_status(self, status, message=None, *, source=None, reload=True) -> None:
        self.update_checker_status = status
        for row in self.update_check_rows:
            row.apply_status(status, message if row is source else None)
        if self.settings_page is not None:
            self.settings_page.apply_update_status(status)
        if reload:
            self.refresh_pages(reload_library=False, reload_preferences=True)

    def show_page(self, app_type: str) -> bool:
        self.navigate_to(app_type)
        self.present()
        return GLib.SOURCE_REMOVE

    def install_update(self, app_type: str) -> bool:
        """Install the latest version, as requested from an update notification."""
        if self.library_loading:
            self._pending_update = app_type
            return GLib.SOURCE_REMOVE
        self.navigate_to(app_type)
        page = self.pages.get(app_type)
        if page is None or page.operation_busy:
            return GLib.SOURCE_REMOVE
        if not page.start_install_from_setup(make_preferred=True):
            self._show_window_error(
                "Could not update Roblox",
                page.library_error or "Roseblox could not start the installation.",
            )
        return GLib.SOURCE_REMOVE

    def handle_roblox_url(self, uri: str) -> bool:
        from .backend.url_handlers import parse_roblox_url
        if self.library_loading:
            self._pending_roblox_url = uri
            return GLib.SOURCE_REMOVE
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
