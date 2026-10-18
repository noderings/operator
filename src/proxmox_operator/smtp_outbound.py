"""Provider outbound SMTP block helpers for Proxmox guest firewall.

SMTP DROP rules are owned by ProxmoxVM.spec.blockOutboundSmtp, not FirewallRuleSet.
They are identified by a stable comment prefix so ruleset reconcile can preserve them.
"""

from __future__ import annotations

from typing import Any

import kopf

SMTP_OUTBOUND_PORTS: tuple[int, ...] = (25, 465, 587)
SMTP_OUTBOUND_COMMENT_PREFIX = "noderings:smtp-outbound-block:"


def smtp_outbound_comment(port: int) -> str:
    return f"{SMTP_OUTBOUND_COMMENT_PREFIX}{port}"


def is_smtp_outbound_rule(rule: dict[str, Any] | None) -> bool:
    if not rule:
        return False
    comment = rule.get("comment") or ""
    return str(comment).startswith(SMTP_OUTBOUND_COMMENT_PREFIX)


def _list_rules(proxmox: Any, node: str, vmid: int) -> list[dict[str, Any]]:
    rules = proxmox.nodes(node).qemu(vmid).firewall.rules.get() or []
    return [r for r in rules if isinstance(r, dict)]


def _delete_smtp_rules(proxmox: Any, node: str, vmid: int, logger: kopf.Logger) -> int:
    rules = _list_rules(proxmox, node, vmid)
    positions = sorted(
        (int(r["pos"]) for r in rules if is_smtp_outbound_rule(r) and r.get("pos") is not None),
        reverse=True,
    )
    deleted = 0
    for pos in positions:
        try:
            proxmox.nodes(node).qemu(vmid).firewall.rules(pos).delete()
            deleted += 1
        except Exception as e:
            logger.warning(
                "[SMTP] Failed to delete SMTP rule at pos %s for vmid %s: %s",
                pos,
                vmid,
                e,
            )
    return deleted


def _create_smtp_rule(proxmox: Any, node: str, vmid: int, port: int, logger: kopf.Logger) -> None:
    params = {
        "action": "DROP",
        "type": "out",
        "proto": "tcp",
        "dport": str(port),
        "enable": 1,
        "comment": smtp_outbound_comment(port),
    }
    try:
        # Proxmox inserts at the top by default when pos is omitted / 0.
        proxmox.nodes(node).qemu(vmid).firewall.rules.post(**params)
        logger.info(
            "[SMTP] Created outbound SMTP DROP tcp/%s for vmid %s",
            port,
            vmid,
        )
    except Exception as e:
        logger.warning(
            "[SMTP] Failed to create outbound SMTP DROP tcp/%s for vmid %s: %s",
            port,
            vmid,
            e,
        )


def _existing_smtp_ports(rules: list[dict[str, Any]]) -> set[int]:
    found: set[int] = set()
    for r in rules:
        if not is_smtp_outbound_rule(r):
            continue
        comment = str(r.get("comment") or "")
        suffix = comment[len(SMTP_OUTBOUND_COMMENT_PREFIX) :]
        try:
            found.add(int(suffix))
        except ValueError:
            # Legacy/malformed comment — treat as present but don't map to a port.
            pass
    return found


def reconcile_vm_smtp_outbound_block(
    proxmox: Any,
    node: str,
    vmid: int,
    enabled: bool,
    logger: kopf.Logger,
) -> None:
    """Ensure or remove provider SMTP outbound DROP rules on a guest firewall."""
    try:
        if not enabled:
            deleted = _delete_smtp_rules(proxmox, node, vmid, logger)
            if deleted:
                logger.info(
                    "[SMTP] Removed %d SMTP outbound rule(s) for vmid %s",
                    deleted,
                    vmid,
                )
            return

        rules = _list_rules(proxmox, node, vmid)
        existing_ports = _existing_smtp_ports(rules)

        # Create missing ports. Proxmox posts insert at top; create high→low so
        # final top-of-chain order is 25, 465, 587 when all are newly created.
        for port in reversed(SMTP_OUTBOUND_PORTS):
            if port not in existing_ports:
                _create_smtp_rule(proxmox, node, vmid, port, logger)

        # If some existed and some were missing, reorder by delete+recreate all SMTP
        # rules so they sit above customer rules (best-effort priority).
        rules_after = _list_rules(proxmox, node, vmid)
        smtp_rules = [r for r in rules_after if is_smtp_outbound_rule(r)]
        non_smtp = [r for r in rules_after if not is_smtp_outbound_rule(r)]
        if non_smtp and smtp_rules:
            # If any SMTP rule is not among the first N positions, rebuild SMTP at top.
            smtp_positions = sorted(int(r["pos"]) for r in smtp_rules if r.get("pos") is not None)
            expected_top = list(range(len(smtp_positions)))
            if smtp_positions != expected_top:
                logger.info(
                    "[SMTP] Reordering SMTP rules to top for vmid %s (was positions %s)",
                    vmid,
                    smtp_positions,
                )
                _delete_smtp_rules(proxmox, node, vmid, logger)
                for port in reversed(SMTP_OUTBOUND_PORTS):
                    _create_smtp_rule(proxmox, node, vmid, port, logger)

    except Exception as e:
        logger.warning(
            "[SMTP] Failed to reconcile SMTP outbound block for vmid %s: %s",
            vmid,
            e,
            exc_info=True,
        )


def delete_non_smtp_firewall_rules(
    proxmox: Any,
    node: str,
    vmid: int,
    logger: kopf.Logger,
) -> int:
    """Delete all guest firewall rules except provider SMTP outbound rules."""
    try:
        rules = _list_rules(proxmox, node, vmid)
        positions = sorted(
            (
                int(r["pos"])
                for r in rules
                if r.get("pos") is not None and not is_smtp_outbound_rule(r)
            ),
            reverse=True,
        )
        deleted = 0
        for pos in positions:
            try:
                proxmox.nodes(node).qemu(vmid).firewall.rules(pos).delete()
                deleted += 1
            except Exception as e:
                logger.warning(
                    "[CLEANUP] Failed to delete non-SMTP rule at pos %s for vmid %s: %s",
                    pos,
                    vmid,
                    e,
                )
        return deleted
    except Exception as e:
        logger.error(
            "[CLEANUP] Failed listing/deleting non-SMTP rules for vmid %s: %s",
            vmid,
            e,
            exc_info=True,
        )
        return 0
