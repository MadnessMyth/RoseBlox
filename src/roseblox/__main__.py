"""Application entry point."""

import sys


def main() -> int:
    # The update checker, activity watcher, playtime tracker and microphone command run in the background,
    # so they never load GTK.
    if sys.argv[1:2] in (["--update-service"], ["--activity-watch"], ["--playtime"], ["--mic"]):
        from .backend.appimage import restore_host_environment

        restore_host_environment()
    if sys.argv[1:2] == ["--update-service"]:
        from .update_service import main as run_update_service

        return run_update_service(sys.argv)
    if sys.argv[1:2] == ["--activity-watch"]:
        from .activity_service import main as run_activity_watch

        return run_activity_watch(sys.argv)
    if sys.argv[1:2] == ["--playtime"]:
        from .playtime_service import main as run_playtime

        return run_playtime(sys.argv)
    if sys.argv[1:2] == ["--mic"]:
        from .microphone_service import command_main

        return command_main(sys.argv)
    from .application import RosebloxApplication

    application = RosebloxApplication()
    status = application.run(sys.argv)
    application.restart_if_requested()
    return status


if __name__ == "__main__":
    raise SystemExit(main())
