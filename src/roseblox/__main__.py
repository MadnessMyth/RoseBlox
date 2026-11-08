"""Application entry point."""

import sys


def main() -> int:
    # The update checker and activity watcher run in the background, so they never load GTK.
    if sys.argv[1:2] in (["--update-service"], ["--activity-watch"]):
        from .backend.appimage import restore_host_environment

        restore_host_environment()
    if sys.argv[1:2] == ["--update-service"]:
        from .update_service import main as run_update_service

        return run_update_service(sys.argv)
    if sys.argv[1:2] == ["--activity-watch"]:
        from .activity_service import main as run_activity_watch

        return run_activity_watch(sys.argv)
    from .application import RosebloxApplication

    application = RosebloxApplication()
    status = application.run(sys.argv)
    application.restart_if_requested()
    return status


if __name__ == "__main__":
    raise SystemExit(main())
