"""Application entry point."""

from .application import RosebloxApplication


def main() -> int:
    return RosebloxApplication().run(None)


if __name__ == "__main__":
    raise SystemExit(main())
