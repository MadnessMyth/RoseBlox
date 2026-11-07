"""Application entry point."""

import sys


def main() -> int:
    # The update checker runs all session long, so it never loads GTK.
    if sys.argv[1:2] == ["--update-service"]:
        from .update_service import main as run_update_service

        return run_update_service(sys.argv)
    from .application import RosebloxApplication

    return RosebloxApplication().run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
