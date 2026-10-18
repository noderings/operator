"""Publish one platform's panel nests and eggs onto PterodactylGameCatalog status."""

from __future__ import annotations

import os
from typing import Any

import kopf

from ..client import PanelError
from ..constants import GROUP, PHASE_ERROR, PHASE_READY, VERSION
from ..panel import panel_client as _client
from ..errors import is_permanent
from ..servers import list_eggs

CATALOG_PLURAL = "pterodactylgamecatalogs"
# How often the operator lists panel nests and eggs and writes the catalog CR.
DEFAULT_CATALOG_REFRESH_SECONDS = 900.0


def catalog_refresh_interval(raw: str | None = None) -> float:
    """Seconds between catalog refreshes. A bare number is seconds; s, m, and h are accepted."""
    if raw is None:
        raw = os.environ.get("PTERODACTYL_CATALOG_REFRESH_INTERVAL", "")
    text = (raw or "").strip().lower()
    if not text:
        return DEFAULT_CATALOG_REFRESH_SECONDS
    multiplier = 1.0
    if text[-1:] in {"s", "m", "h"}:
        multiplier = {"s": 1.0, "m": 60.0, "h": 3600.0}[text[-1]]
        text = text[:-1].strip()
    try:
        seconds = float(text) * multiplier
    except ValueError:
        return DEFAULT_CATALOG_REFRESH_SECONDS
    if seconds <= 0:
        return DEFAULT_CATALOG_REFRESH_SECONDS
    return seconds


def catalog_games(eggs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per nest and egg. The panel ids are the identity, not the name."""
    games: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for egg in eggs:
        nest_id = int(egg.get("nest_id") or 0)
        egg_id = int(egg.get("id") or 0)
        if nest_id <= 0 or egg_id <= 0 or (nest_id, egg_id) in seen:
            continue
        seen.add((nest_id, egg_id))
        row: dict[str, Any] = {
            "nestId": nest_id,
            "nestName": str(egg.get("nest_name") or "").strip(),
            "eggId": egg_id,
            "eggName": str(egg.get("name") or "").strip(),
            "eula": egg_requires_eula(egg),
        }
        panel_egg = egg.get("panel_egg")
        if isinstance(panel_egg, dict):
            row["variables"] = variable_defs(panel_egg)
        games.append(row)
    return games


def egg_requires_eula(egg: dict[str, Any]) -> bool:
    """True when the panel marks this egg as needing eula.txt.

    feature_tags may contain eula. This panel leaves those tags empty and
    instead puts the prompt in config.startup.userInteraction.
    """
    sources: list[dict[str, Any]] = [egg]
    panel_egg = egg.get("panel_egg")
    if isinstance(panel_egg, dict):
        sources.append(panel_egg)
    for source in sources:
        tags = source.get("feature_tags")
        if tags is None:
            tags = source.get("featureTags")
        if isinstance(tags, list) and any("eula" in str(tag).casefold() for tag in tags):
            return True
        config = source.get("config")
        startup = config.get("startup") if isinstance(config, dict) else None
        if not isinstance(startup, dict):
            continue
        interactions = startup.get("userInteraction")
        if interactions is None:
            interactions = startup.get("user_interaction")
        if isinstance(interactions, str):
            interactions = [interactions]
        if isinstance(interactions, list) and any(
            "eula" in str(item).casefold() for item in interactions
        ):
            return True
    return False


def _flag(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes"}:
        return True
    if text in {"0", "false", "no", ""}:
        return False
    return default


def rules_required(rules: str) -> bool:
    """True when the panel validation rules contain required."""
    for part in str(rules or "").split("|"):
        token = part.strip().lower()
        if token == "required" or token.startswith("required:"):
            return True
    return False


def variable_defs(egg: dict[str, Any]) -> list[dict[str, Any]]:
    """Variable definitions from an egg fetched with include=variables."""
    variables = ((egg.get("relationships") or {}).get("variables") or {}).get("data") or []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in variables:
        if not isinstance(item, dict):
            continue
        attrs = item.get("attributes") if isinstance(item.get("attributes"), dict) else item
        if not isinstance(attrs, dict):
            continue
        env = str(attrs.get("env_variable") or "").strip()
        if not env or env in seen:
            continue
        seen.add(env)
        description = str(attrs.get("description") or "")
        if len(description) > 2000:
            description = description[:2000]
        default = "" if attrs.get("default_value") is None else str(attrs.get("default_value"))
        if len(default) > 4096:
            default = default[:4096]
        name = str(attrs.get("name") or env).strip() or env
        if len(name) > 255:
            name = name[:255]
        rules = str(attrs.get("rules") or "")
        rows.append(
            {
                "envVariable": env,
                "name": name,
                "description": description,
                "defaultValue": default,
                "rules": rules,
                "userViewable": _flag(attrs.get("user_viewable"), True),
                "userEditable": _flag(attrs.get("user_editable"), False),
                "required": rules_required(rules),
            }
        )
    return rows


def catalog_is_current(status: dict[str, Any] | None) -> bool:
    """True when a previous list already succeeded. The timer still refreshes it."""
    if not isinstance(status, dict):
        return False
    if status.get("phase") != PHASE_READY:
        return False
    return isinstance(status.get("games"), list)


def publish(patch: kopf.Patch, logger: kopf.Logger | None = None) -> None:
    client = _client()
    try:
        games = catalog_games(list_eggs(client))
    except PanelError as exc:
        if is_permanent(exc):
            patch.status["phase"] = PHASE_ERROR
            patch.status["message"] = exc.detail
            return
        raise kopf.TemporaryError(exc.detail, delay=30) from exc
    patch.status["phase"] = PHASE_READY
    patch.status["games"] = games
    patch.status["message"] = ""


@kopf.on.create(GROUP, VERSION, CATALOG_PLURAL)
@kopf.on.update(GROUP, VERSION, CATALOG_PLURAL)
def on_catalog(
    patch: kopf.Patch, logger: kopf.Logger, status: dict[str, Any] | None = None, **_: Any
) -> None:
    # A lost "already handled" mark used to run this on every watch event.
    # Skip the panel when the catalog is already published; the timer refreshes it.
    if catalog_is_current(status):
        return
    publish(patch, logger)


@kopf.timer(GROUP, VERSION, CATALOG_PLURAL, interval=catalog_refresh_interval(), initial_delay=30)
def refresh_catalog(patch: kopf.Patch, logger: kopf.Logger, **_: Any) -> None:
    """Keep the CR current when the provider adds an egg between database syncs."""
    publish(patch, logger)
