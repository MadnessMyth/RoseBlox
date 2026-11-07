"""Application entry point."""

import sys

from .application import RosebloxApplication


def main() -> int:
    return RosebloxApplication().run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
