"""Import settings from other Roblox launchers and FastFlag tools.

Each source app has its own module that finds and reads its files (never
writing them) and returns an ``ImportResult``; ``planner`` compares that with
Roseblox's settings and writes only what the user ticked.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import bloxstrap, client_settings, fastflag_manager, quadra, roblox_manager, roseline, sober, vinegar
from .model import Importer, ImportResult, ImportSourceError

IMPORTERS: tuple[Importer, ...] = (
    roseline.IMPORTER,
    quadra.IMPORTER,
    sober.IMPORTER,
    vinegar.IMPORTER,
    bloxstrap.IMPORTER,
    fastflag_manager.IMPORTER,
    roblox_manager.IMPORTER,
    client_settings.IMPORTER,
)


@dataclass(frozen=True)
class Detection:
    importer: Importer
    locations: tuple[Path, ...]


def importer(importer_id: str) -> Importer:
    for candidate in IMPORTERS:
        if candidate.id == importer_id:
            return candidate
    raise KeyError(importer_id)


def detect_all(environ: dict[str, str] | None = None) -> list[Detection]:
    environ = dict(os.environ if environ is None else environ)
    found = []
    for candidate in IMPORTERS:
        try:
            locations = tuple(candidate.detect(environ))
        except OSError:
            locations = ()
        found.append(Detection(candidate, locations))
    return found


def read_source(importer_id: str, path: Path) -> ImportResult:
    """Read a source without changing it; ImportSourceError explains files that cannot be used."""
    try:
        return importer(importer_id).read(Path(path))
    except ImportSourceError:
        raise
    except (OSError, ValueError) as error:
        raise ImportSourceError(f"Could not read {path}: {error}") from error
