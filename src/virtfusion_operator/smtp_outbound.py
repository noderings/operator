"""Provider outbound SMTP block for VirtFusion via an existing panel firewall ruleset.

NodeRings does not create VirtFusion firewall rulesets (no Global create API). The
provider names a panel ruleset containing "smtp" (case-insensitive). When
VirtFusionVM.spec.blockOutboundSmtp is true the operator attaches the first matching
ruleset; when false it removes that ruleset if it is already on the VM.

Official Global API cannot list rulesets by name (GET /firewall/rulesets 404s on
current control planes). The fake API implements that catalog. On live VF we also
scan ruleset ids (apply + GET) and treat a ruleset as SMTP when its name contains
smtp or its expanded rules DENY outbound TCP 25/465/587.
"""

from __future__ import annotations

from typing import Any

import kopf

from .client import VirtFusionClient
from .errors import VirtFusionHTTPError

SMTP_OUTBOUND_PORTS: tuple[int, ...] = (25, 465, 587)
SMTP_NAME_NEEDLE = "smtp"
FIREWALL_INTERFACE = "primary"
# VF enums from the panel UI: protocol 1=TCP, direction 1=BOTH 3=OUT, action 1=DENY.
PROTO_TCP = 1
DIRECTION_BOTH = 1
DIRECTION_OUT = 3
ACTION_DENY = 1
RULESET_ID_SCAN_MAX = 32

_smtp_ruleset_id: int | None = None
_applied_ruleset_ids: dict[int, list[int]] = {}


def clear_smtp_ruleset_cache() -> None:
    """Reset process-wide catalog / last-applied cache (tests)."""
    global _smtp_ruleset_id
    _smtp_ruleset_id = None
    _applied_ruleset_ids.clear()


def ruleset_name_is_smtp(name: str | None) -> bool:
    return SMTP_NAME_NEEDLE in str(name or "").lower()


def pick_first_smtp_ruleset(rulesets: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """Return the first ruleset whose name contains smtp (case-insensitive)."""
    for rs in rulesets or []:
        if isinstance(rs, dict) and ruleset_name_is_smtp(str(rs.get("name") or "")):
            return rs
    return None


def _positive_int(value: Any) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _firewall_data(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {}
    data = payload.get("data", payload)
    return data if isinstance(data, dict) else {}


def firewall_enabled(payload: dict[str, Any] | None) -> bool:
    return bool(_firewall_data(payload).get("enabled"))


def expanded_rules(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    rules = _firewall_data(payload).get("rules") or []
    return [r for r in rules if isinstance(r, dict)]


def rulesets_from_firewall_payload(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Best-effort `{id, name}` list from GET firewall (names are often absent on live VF)."""
    data = _firewall_data(payload)
    out: list[dict[str, Any]] = []
    seen: set[int] = set()

    def add(rid: int | None, name: str | None) -> None:
        if not rid or rid in seen:
            return
        seen.add(rid)
        item: dict[str, Any] = {"id": rid}
        if name:
            item["name"] = name
        out.append(item)

    for rs in data.get("rulesets") or []:
        if isinstance(rs, dict):
            add(
                _positive_int(rs.get("id") or rs.get("rulesetId") or rs.get("ruleset_id")),
                str(rs.get("name") or "") or None,
            )
        else:
            add(_positive_int(rs), None)
    for rule in expanded_rules(payload):
        add(
            _positive_int(
                rule.get("rulesetId")
                or rule.get("ruleset_id")
                or rule.get("set_id")
                or rule.get("firewallRulesetId")
            ),
            str(rule.get("name") or rule.get("rulesetName") or rule.get("ruleset") or "") or None,
        )
    return out


def ruleset_ids_from_firewall(payload: dict[str, Any] | None) -> list[int]:
    ids: list[int] = []
    seen: set[int] = set()
    data = _firewall_data(payload)
    for raw in data.get("rulesetIds") or []:
        n = _positive_int(raw)
        if n and n not in seen:
            seen.add(n)
            ids.append(n)
    for rs in rulesets_from_firewall_payload(payload):
        n = _positive_int(rs.get("id"))
        if n and n not in seen:
            seen.add(n)
            ids.append(n)
    return ids


def _rule_covers_smtp_port(rule: dict[str, Any]) -> bool:
    start = _positive_int(rule.get("port_dst_start") or rule.get("dstPortStart"))
    end = _positive_int(rule.get("port_dst_end") or rule.get("dstPortEnd"))
    if start is None:
        dport = rule.get("dport")
        start = _positive_int(dport)
        end = start
    if start is None:
        return False
    if end is None or end < start:
        end = start
    return any(start <= port <= end for port in SMTP_OUTBOUND_PORTS)


def rule_looks_like_smtp_deny(rule: dict[str, Any] | None) -> bool:
    if not rule:
        return False
    proto = _positive_int(rule.get("protocol")) or 0
    if proto not in (0, PROTO_TCP):
        return False
    direction = _positive_int(rule.get("direction")) or 0
    if direction not in (DIRECTION_OUT, DIRECTION_BOTH):
        return False
    action = _positive_int(rule.get("action")) or 0
    if action != ACTION_DENY:
        return False
    return _rule_covers_smtp_port(rule)


def merge_ruleset_ids(current: list[int], smtp_id: int | None, block: bool) -> list[int]:
    """Keep non-SMTP ids; add or drop the SMTP ruleset. Replace-all API."""
    others = [i for i in current if smtp_id is None or i != smtp_id]
    if block and smtp_id is not None and smtp_id not in others:
        return others + [smtp_id]
    return others


def _current_applied_ids(client: VirtFusionClient, server_id: int) -> list[int]:
    cached = _applied_ruleset_ids.get(server_id)
    if cached is not None:
        return list(cached)
    fw = client.get_firewall(server_id, FIREWALL_INTERFACE)
    return ruleset_ids_from_firewall(fw)


def _scan_for_smtp_ruleset(
    client: VirtFusionClient, server_id: int, logger: kopf.Logger
) -> int | None:
    """Probe sequential ruleset ids when Global API has no catalog (live VF)."""
    fw_before = client.get_firewall(server_id, FIREWALL_INTERFACE)
    original_ids = list(_applied_ruleset_ids.get(server_id) or ruleset_ids_from_firewall(fw_before))
    original_enabled = firewall_enabled(fw_before)
    found: int | None = None
    try:
        client.enable_firewall(server_id, FIREWALL_INTERFACE)
    except VirtFusionHTTPError as e:
        logger.warning("[SMTP] enable firewall before ruleset scan failed: %s", e)

    for rid in range(1, RULESET_ID_SCAN_MAX + 1):
        try:
            client.apply_firewall_rulesets(server_id, [rid], FIREWALL_INTERFACE)
        except VirtFusionHTTPError as e:
            if e.status_code == 422:
                continue
            logger.warning("[SMTP] ruleset id scan failed at %s: %s", rid, e)
            break
        fw = client.get_firewall(server_id, FIREWALL_INTERFACE)
        named = pick_first_smtp_ruleset(rulesets_from_firewall_payload(fw))
        if named:
            found = _positive_int(named.get("id")) or rid
            break
        if any(rule_looks_like_smtp_deny(r) for r in expanded_rules(fw)):
            found = rid
            break

    try:
        if original_ids:
            client.apply_firewall_rulesets(server_id, original_ids, FIREWALL_INTERFACE)
            _applied_ruleset_ids[server_id] = list(original_ids)
        else:
            client.apply_firewall_rulesets(server_id, [], FIREWALL_INTERFACE)
            _applied_ruleset_ids[server_id] = []
            if not original_enabled:
                client.disable_firewall(server_id, FIREWALL_INTERFACE)
    except VirtFusionHTTPError as e:
        logger.warning("[SMTP] failed to restore firewall after ruleset scan: %s", e)
    return found


def resolve_smtp_ruleset_id(
    client: VirtFusionClient,
    server_id: int,
    logger: kopf.Logger,
    *,
    allow_scan: bool,
) -> int | None:
    global _smtp_ruleset_id
    if _smtp_ruleset_id:
        return _smtp_ruleset_id

    picked = pick_first_smtp_ruleset(client.list_firewall_rulesets())
    if picked:
        rid = _positive_int(picked.get("id"))
        if rid:
            _smtp_ruleset_id = rid
            return rid

    fw = client.get_firewall(server_id, FIREWALL_INTERFACE)
    picked = pick_first_smtp_ruleset(rulesets_from_firewall_payload(fw))
    if picked:
        rid = _positive_int(picked.get("id"))
        if rid:
            _smtp_ruleset_id = rid
            return rid

    if not allow_scan:
        return None
    found = _scan_for_smtp_ruleset(client, server_id, logger)
    if found:
        _smtp_ruleset_id = found
    return found


def _apply_desired(
    client: VirtFusionClient,
    server_id: int,
    desired: list[int],
    logger: kopf.Logger,
) -> None:
    if desired:
        client.enable_firewall(server_id, FIREWALL_INTERFACE)
        client.apply_firewall_rulesets(server_id, desired, FIREWALL_INTERFACE)
        _applied_ruleset_ids[server_id] = list(desired)
        logger.info(
            "[SMTP] Applied firewall rulesets %s on server %s",
            desired,
            server_id,
        )
        return
    fw = client.get_firewall(server_id, FIREWALL_INTERFACE)
    if firewall_enabled(fw) or ruleset_ids_from_firewall(fw) or _applied_ruleset_ids.get(server_id):
        client.apply_firewall_rulesets(server_id, [], FIREWALL_INTERFACE)
        client.disable_firewall(server_id, FIREWALL_INTERFACE)
        logger.info("[SMTP] Removed SMTP firewall ruleset from server %s", server_id)
    _applied_ruleset_ids[server_id] = []


def reconcile_vm_smtp_outbound_block(
    client: VirtFusionClient,
    server_id: int,
    enabled: bool,
    logger: kopf.Logger,
) -> None:
    """Attach or detach the SMTP-named VirtFusion firewall ruleset on a server."""
    try:
        smtp_id = resolve_smtp_ruleset_id(client, server_id, logger, allow_scan=True)
        if enabled and smtp_id is None:
            logger.warning(
                "[SMTP] No VirtFusion firewall ruleset name containing %r; "
                "outbound SMTP is not blocked on server %s",
                SMTP_NAME_NEEDLE,
                server_id,
            )
            return

        current = _current_applied_ids(client, server_id)
        desired = merge_ruleset_ids(current, smtp_id, enabled)
        if desired == current:
            if enabled and desired:
                fw = client.get_firewall(server_id, FIREWALL_INTERFACE)
                if not firewall_enabled(fw):
                    client.enable_firewall(server_id, FIREWALL_INTERFACE)
                    logger.info(
                        "[SMTP] Enabled firewall on server %s for SMTP ruleset %s",
                        server_id,
                        smtp_id,
                    )
                return
            if not enabled:
                fw = client.get_firewall(server_id, FIREWALL_INTERFACE)
                if not firewall_enabled(fw) and not desired:
                    return
        _apply_desired(client, server_id, desired, logger)
    except Exception as e:
        logger.warning(
            "[SMTP] Failed to reconcile SMTP outbound block for server %s: %s",
            server_id,
            e,
            exc_info=True,
        )
