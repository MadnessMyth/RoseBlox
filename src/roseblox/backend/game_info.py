"""Public game details and server locations for activity features.

Only public, unauthenticated endpoints are used, and nothing about the player
is sent: Roblox gets a place ID, the location service gets a Roblox server
address.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .. import __version__
from .activity import public_address

LOCATION_SERVICE = "ipwho.is"
_TIMEOUT = 8.0
_MAX_RESPONSE = 512 * 1024


class GameInfoError(RuntimeError):
    pass


@dataclass(frozen=True)
class GameDetails:
    place_id: int
    universe_id: int
    name: str
    creator: str | None
    icon_url: str | None


def _get_json(url: str, opener=None) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": f"Roseblox/{__version__}", "Accept": "application/json"})
    try:
        with (opener or urllib.request.urlopen)(request, timeout=_TIMEOUT) as response:
            data = response.read(_MAX_RESPONSE + 1)
    except OSError as error:
        raise GameInfoError(f"{urllib.parse.urlsplit(url).hostname} could not be reached: {error}") from error
    if len(data) > _MAX_RESPONSE:
        raise GameInfoError("The response was too large.")
    try:
        return json.loads(data)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise GameInfoError("The response was not valid JSON.") from error


def _first(document: object) -> dict:
    items = document.get("data") if isinstance(document, dict) else None
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        raise GameInfoError("Roblox returned no details for this game.")
    return items[0]


def fetch_game(place_id: int, opener=None) -> GameDetails:
    universe = _get_json(f"https://apis.roblox.com/universes/v1/places/{int(place_id)}/universe", opener)
    universe_id = universe.get("universeId") if isinstance(universe, dict) else None
    if not isinstance(universe_id, int) or universe_id <= 0:
        raise GameInfoError("Roblox returned no universe for this place.")
    game = _first(_get_json(f"https://games.roblox.com/v1/games?universeIds={universe_id}", opener))
    name = game.get("name")
    if not isinstance(name, str) or not name.strip():
        raise GameInfoError("Roblox returned no name for this game.")
    creator = game.get("creator", {}).get("name") if isinstance(game.get("creator"), dict) else None
    icon_url = None
    try:
        icon = _first(_get_json(
            "https://thumbnails.roblox.com/v1/games/icons?"
            f"universeIds={universe_id}&returnPolicy=PlaceHolder&size=512x512&format=Png&isCircular=false",
            opener,
        ))
        candidate = icon.get("imageUrl")
        if isinstance(candidate, str) and candidate.startswith("https://") and icon.get("state") == "Completed":
            icon_url = candidate
    except GameInfoError:
        pass
    return GameDetails(int(place_id), universe_id, name.strip(), creator if isinstance(creator, str) else None, icon_url)


def locate_server(address: str, opener=None) -> str:
    """``City, Region, Country`` for a public Roblox server address."""
    checked = public_address(address)
    if checked is None:
        raise GameInfoError("Only public server addresses are looked up.")
    document = _get_json(f"https://{LOCATION_SERVICE}/{urllib.parse.quote(checked)}", opener)
    if not isinstance(document, dict) or document.get("success") is not True:
        message = document.get("message") if isinstance(document, dict) else None
        raise GameInfoError(f"{LOCATION_SERVICE} could not locate the server{': ' + message if message else ''}.")
    parts = []
    for key in ("city", "region", "country"):
        value = document.get(key)
        if isinstance(value, str) and value.strip() and value.strip() not in parts:
            parts.append(value.strip())
    if not parts:
        raise GameInfoError(f"{LOCATION_SERVICE} returned no location.")
    return ", ".join(parts)
