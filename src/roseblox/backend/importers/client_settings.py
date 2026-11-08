"""Any app that keeps FastFlags as Roblox's own ``ClientAppSettings.json``: one JSON object of names and values."""

from __future__ import annotations

from pathlib import Path

from .model import PLAYER, STUDIO, Importer, ImportResult, ImportSourceError, read_json

STUDIO_COPY = "Tick to give Studio the same flag."


def detect(environ: dict[str, str]) -> list[Path]:
    return []


def read(path: Path) -> ImportResult:
    if path.is_dir():
        candidate = path / "ClientAppSettings.json"
        if not candidate.is_file():
            candidate = path / "ClientSettings" / "ClientAppSettings.json"
        path = candidate
    document = read_json(path)
    if not isinstance(document, dict):
        raise ImportSourceError(f"{path.name} is not a JSON object of FastFlag names and values.")
    result = ImportResult("FastFlags file", path)
    result.flags(PLAYER, document, path.name)
    result.flags(STUDIO, document, path.name, STUDIO_COPY, selected=False)
    return result


IMPORTER = Importer(
    id="client_settings",
    title="Other app (FastFlags file)",
    description="A ClientAppSettings.json or exported FastFlags JSON from any other launcher.",
    detect=detect,
    read=read,
    browse_hint="a JSON file of FastFlag names and values",
)
