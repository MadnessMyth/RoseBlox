"""Compare an import with Roseblox's current settings, validate the chosen changes, then write them."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

from ..fast_flags import MAX_FLAGS
from ..launch_config import MAX_ENTRIES, AppLaunchConfig, LaunchConfigError, LaunchConfigStore
from ..mods import ModsError, ModSettings, ModsStore, font_kind
from ..setup import SetupSettingsError, SetupState, SetupStore, WhilePlaying
from .model import APP_SETTINGS, PLAYER, ROSEBLOX, STUDIO, ImportItem, ImportProblem, ImportResult, SKIPPED, INVALID

ADD = "add"
SAME = "same"
REPLACE = "replace"
MAPPINGS = ("fast_flags", "rosemary_environment", "guest_environment")


class ImportApplyError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlannedChange:
    item: ImportItem
    status: str
    current: object
    selected: bool

    @property
    def id(self) -> tuple[str, str]:
        return self.item.target, self.item.key


@dataclass
class ImportPlan:
    result: ImportResult
    changes: list[PlannedChange] = field(default_factory=list)
    problems: list[ImportProblem] = field(default_factory=list)

    @property
    def actionable(self) -> list[PlannedChange]:
        return [change for change in self.changes if change.status != SAME]

    @property
    def unchanged(self) -> list[PlannedChange]:
        return [change for change in self.changes if change.status == SAME]


@dataclass(frozen=True)
class Stores:
    launch: LaunchConfigStore
    setup: SetupStore
    mods: ModsStore

    @classmethod
    def default(cls) -> "Stores":
        return cls(LaunchConfigStore(), SetupStore(), ModsStore())


@dataclass(frozen=True)
class Current:
    apps: dict[str, AppLaunchConfig]
    mods: dict[str, ModSettings]
    setup: SetupState

    @classmethod
    def load(cls, stores: Stores) -> "Current":
        return cls(
            {app: stores.launch.load(app) for app in (PLAYER, STUDIO)},
            {app: stores.mods.load(app) for app in (PLAYER, STUDIO)},
            stores.setup.load(),
        )

    def value(self, target: str, key: str) -> tuple[object, bool]:
        """The current value, and whether it differs from Roseblox's default (False: nothing to overwrite)."""
        if target == ROSEBLOX:
            if key == "plain_ctrl_click":
                return self.setup.plain_ctrl_click, self.setup.plain_ctrl_click != SetupState().plain_ctrl_click
            value = getattr(self.setup.while_playing, key)
            return value, value != getattr(WhilePlaying(), key)
        if key.startswith("mods."):
            field_name = key.removeprefix("mods.")
            value = getattr(self.mods[target], field_name)
            return value, value != getattr(ModSettings(), field_name)
        config = self.apps[target]
        mapping, _, name = key.partition(".")
        if mapping in MAPPINGS:
            entries = getattr(config, mapping)
            return entries.get(name), name in entries
        value = getattr(config, key)
        return value, value != getattr(AppLaunchConfig(), key)


def plan(result: ImportResult, stores: Stores) -> ImportPlan:
    current = Current.load(stores)
    outcome = ImportPlan(result, problems=list(result.problems))
    latest: dict[tuple[str, str], ImportItem] = {}
    for item in result.items:
        previous = latest.get((item.target, item.key))
        if previous is not None and previous.source_key != item.source_key:
            outcome.problems.append(ImportProblem(previous.source_key, SKIPPED, f"Replaced by {item.source_key}."))
        latest[(item.target, item.key)] = item
    for item in latest.values():
        if item.key == "mods.font":
            problem = _font_problem(item)
            if problem:
                outcome.problems.append(ImportProblem(item.source_key, INVALID, problem))
                continue
        value, is_set = current.value(item.target, item.key)
        if _same(item, value):
            status = SAME
        elif is_set:
            status = REPLACE
        else:
            status = ADD
        outcome.changes.append(PlannedChange(item, status, value, item.selected and status == ADD))
    return outcome


def _font_problem(item: ImportItem) -> str | None:
    try:
        font_kind(Path(item.value))
    except ModsError as error:
        return str(error)
    return None


def _same(item: ImportItem, current: object) -> bool:
    if item.key != "mods.font":
        return item.value == current
    try:
        return current is not None and Path(current).read_bytes() == Path(item.value).read_bytes()
    except OSError:
        return False


@dataclass(frozen=True)
class Proposed:
    apps: dict[str, AppLaunchConfig]
    mods: dict[str, ModSettings]
    fonts: dict[str, Path]
    setup: SetupState


def build(chosen: list[PlannedChange], stores: Stores) -> Proposed:
    """Every chosen change applied in memory and validated; nothing is written."""
    current = Current.load(stores)
    apps = dict(current.apps)
    mods = dict(current.mods)
    fonts: dict[str, Path] = {}
    while_playing = current.setup.while_playing
    plain_ctrl_click = current.setup.plain_ctrl_click
    problems: list[str] = []
    for change in chosen:
        item = change.item
        if item.target == ROSEBLOX:
            if item.key == "plain_ctrl_click":
                plain_ctrl_click = item.value
            else:
                while_playing = replace(while_playing, **{item.key: item.value})
        elif item.key == "mods.font":
            fonts[item.target] = Path(item.value)
        elif item.key.startswith("mods."):
            mods[item.target] = replace(mods[item.target], **{item.key.removeprefix("mods."): item.value})
        else:
            mapping, _, name = item.key.partition(".")
            config = apps[item.target]
            if mapping in MAPPINGS:
                apps[item.target] = replace(config, **{mapping: {**getattr(config, mapping), name: item.value}})
            elif mapping in APP_SETTINGS:
                apps[item.target] = replace(config, **{mapping: item.value})
            else:
                problems.append(f"Roseblox has no setting {item.key}.")
    for app, config in apps.items():
        title = "Studio" if app == STUDIO else "Player"
        if len(config.fast_flags) > MAX_FLAGS:
            problems.append(f"{title} would have {len(config.fast_flags)} FastFlags; Roseblox keeps at most {MAX_FLAGS}.")
        for mapping in ("rosemary_environment", "guest_environment"):
            if len(getattr(config, mapping)) > MAX_ENTRIES:
                problems.append(f"{title} would have more than {MAX_ENTRIES} environment variables in one list.")
        problems.extend(f"{title}: {problem}" for problem in config.problems())
    try:
        while_playing.validate()
    except (SetupSettingsError, ValueError) as error:
        problems.append(str(error))
    for path in fonts.values():
        try:
            font_kind(path)
        except ModsError as error:
            problems.append(str(error))
    if problems:
        raise ImportApplyError("\n".join(dict.fromkeys(problems)))
    return Proposed(apps, mods, fonts, replace(current.setup, while_playing=while_playing, plain_ctrl_click=plain_ctrl_click))


def apply(chosen: list[PlannedChange], stores: Stores, source: str) -> int:
    """Validate everything first, then write. Returns how many settings were written."""
    chosen = [change for change in chosen if change.status != SAME]
    if not chosen:
        return 0
    proposed = build(chosen, stores)
    current = Current.load(stores)
    targets = {change.item.target for change in chosen}
    try:
        for app in (PLAYER, STUDIO):
            if proposed.apps[app] != current.apps[app]:
                stores.launch.save(app, proposed.apps[app], f"Before importing from {source}")
            if proposed.mods[app] != current.mods[app]:
                stores.mods.save(app, proposed.mods[app])
            if app in proposed.fonts:
                stores.mods.set_font(app, proposed.fonts[app])
        if ROSEBLOX in targets:
            if proposed.setup.while_playing != current.setup.while_playing:
                stores.setup.save_while_playing(proposed.setup.while_playing)
            if proposed.setup.plain_ctrl_click != current.setup.plain_ctrl_click:
                stores.setup.save_plain_ctrl_click(proposed.setup.plain_ctrl_click)
    except (LaunchConfigError, ModsError, SetupSettingsError, OSError, ValueError) as error:
        raise ImportApplyError(f"Could not save the imported settings: {error}") from error
    return len(chosen)
