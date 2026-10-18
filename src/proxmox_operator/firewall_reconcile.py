"""FirewallRuleSet CRD handlers: delete cleanup and periodic timer reconcile."""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import urllib.parse
from typing import Any

import kopf

from .client import ProxmoxClient
from .config import load_settings_from_env
from .constants import FIREWALL_RECONCILE_INTERVAL, FIREWALL_RECONCILE_INTERVAL_MIN
from .node_handler import resolve_proxmox_url
from .types import FirewallRuleSetSpec
from .utils import _get_k8s_client

logger = logging.getLogger(__name__)


def _get_reconcile_interval() -> int:
    """Get firewall reconciliation interval from environment or default.

    Returns:
        Reconciliation interval in seconds
    """
    interval_str = os.getenv("FIREWALL_RECONCILE_INTERVAL_SECONDS")
    if interval_str:
        try:
            interval = int(interval_str)
            if interval < FIREWALL_RECONCILE_INTERVAL_MIN:
                logger.warning(
                    "Firewall reconciliation interval %s is less than %d seconds, "
                    "using minimum of %d seconds",
                    interval,
                    FIREWALL_RECONCILE_INTERVAL_MIN,
                    FIREWALL_RECONCILE_INTERVAL_MIN,
                )
                return FIREWALL_RECONCILE_INTERVAL_MIN
            return interval
        except ValueError:
            logger.warning(
                "Invalid FIREWALL_RECONCILE_INTERVAL_SECONDS value: %s, using default: %s",
                interval_str,
                FIREWALL_RECONCILE_INTERVAL,
            )
    return FIREWALL_RECONCILE_INTERVAL


# Calculate reconciliation interval at module load time
RECONCILE_INTERVAL = _get_reconcile_interval()

# --- HELPERS ---


def _get_client(proxmox_url: str) -> ProxmoxClient:
    return ProxmoxClient(load_settings_from_env(proxmox_url))


def _normalize_ipset_name(ruleset_name: str, rule_id: str, suffix: str) -> str:
    """Generate a valid Proxmox ipset name (25 chars) following the hash-based naming concept."""
    r_hash = hashlib.sha256(ruleset_name.encode()).hexdigest()[:8]
    id_hash = hashlib.sha256(rule_id.encode()).hexdigest()[:8]
    ipset_name = f"frs-{r_hash}-{id_hash}-{suffix}"

    # Ensure it starts with a letter (naming concept requires a leading letter)
    if ipset_name and ipset_name[0].isdigit():
        ipset_name = f"ipset-{ipset_name}"
    elif not ipset_name or not ipset_name[0].isalpha():
        ipset_name = f"ipset-{ipset_name}" if ipset_name else "ipset-default"

    return ipset_name


def _get_target_vms_by_proxmox_url(
    ruleset_name: str, namespace: str, logger: kopf.Logger | None = None
) -> dict[str, list[dict]]:
    """Get VMs grouped by their resolved proxmoxUrl that reference this ruleset.

    This function:
    1. Lists all ProxmoxVM CRs in the namespace.
    2. Finds VMs that reference this FirewallRuleSet (via spec.firewallRuleSet.name).
    3. Resolves the Proxmox URL by looking up each VM's owning ProxmoxNode CR
       (spec.proxmoxNodeRef.name -> ProxmoxNode.spec.proxmoxUrl). A per-call
       cache keyed by ProxmoxNode name avoids redundant API lookups when many
       VMs share the same node.
    4. Groups VMs by their resolved proxmoxUrl.

    Best-effort: ProxmoxNode lookup failures (missing ref, 404, transient API
    errors, malformed CRs) just drop that VM from the result and log a debug
    message. The caller continues processing the remaining VMs.

    Returns:
        Dict mapping proxmoxUrl -> list of VM specs (with vmConfig info).
    """
    vms_by_url: dict[str, list[dict]] = {}
    # Per-call cache: ProxmoxNode name -> resolved proxmoxUrl (or None if lookup failed).
    node_url_cache: dict[str, str | None] = {}
    log = logger or logging.getLogger(__name__)

    try:
        _, custom_api = _get_k8s_client()
        resp = custom_api.list_namespaced_custom_object(
            group="vm.proxmox.com", version="v1alpha1", namespace=namespace, plural="proxmoxvms"
        )
        items = resp.get("items", []) if isinstance(resp, dict) else resp.body.get("items", [])

        for vm in items:
            v_spec = vm.get("spec", {})
            firewall_ref = v_spec.get("firewallRuleSet") or {}
            ref_name = firewall_ref.get("name") if isinstance(firewall_ref, dict) else None
            if ref_name != ruleset_name:
                continue

            node_ref = v_spec.get("proxmoxNodeRef") or {}
            node_ref_name = node_ref.get("name") if isinstance(node_ref, dict) else None
            if not node_ref_name:
                log.debug(
                    "Firewall ruleset %s: VM %s/%s missing proxmoxNodeRef.name, skipping",
                    ruleset_name,
                    namespace,
                    vm.get("metadata", {}).get("name", "?"),
                )
                continue

            if node_ref_name not in node_url_cache:
                try:
                    node_url_cache[node_ref_name] = resolve_proxmox_url(
                        node_ref_name, namespace, log
                    )
                except Exception as e:
                    log.debug(
                        "Firewall ruleset %s: failed to resolve proxmoxUrl from ProxmoxNode %s/%s: %s",
                        ruleset_name,
                        namespace,
                        node_ref_name,
                        e,
                    )
                    node_url_cache[node_ref_name] = None

            proxmox_url = node_url_cache[node_ref_name]
            if not proxmox_url:
                continue
            vms_by_url.setdefault(proxmox_url, []).append(v_spec)
    except Exception:
        pass
    return vms_by_url


def _get_vms_for_proxmox_url(
    vm_specs: list[dict], client: ProxmoxClient
) -> list[tuple[str, str, int]]:
    """Get VM tuples (node, vm_name, vmid) for a list of VM specs using the given client."""
    vms = []
    for v_spec in vm_specs:
        v_cfg = v_spec.get("vmConfig", {})
        v_name = v_cfg.get("name")
        n_name = v_cfg.get("nodeName")
        if v_name and n_name:
            try:
                vmid = client.find_vm_by_name(n_name, v_name)
                if vmid:
                    vms.append((n_name, v_name, vmid))
            except Exception:
                pass
    return vms


def _delete_firewall_rules(proxmox: Any, node: str, vmid: int, logger: kopf.Logger) -> None:
    """Delete guest firewall rules for a VM, preserving provider SMTP outbound rules.

    Rules are deleted in reverse order to avoid position shifts.
    SMTP rules (comment prefix noderings:smtp-outbound-block:) are never deleted here;
    they are owned by ProxmoxVM.spec.blockOutboundSmtp.
    """
    from .smtp_outbound import is_smtp_outbound_rule

    try:
        rules = proxmox.nodes(node).qemu(vmid).firewall.rules.get()
        if not rules:
            logger.debug("[CLEANUP] No firewall rules to delete for VM %s (vmid: %s)", node, vmid)
            return

        rules_to_delete = [
            r.get("pos") for r in rules if r.get("pos") is not None and not is_smtp_outbound_rule(r)
        ]

        if not rules_to_delete:
            logger.debug(
                "[CLEANUP] No non-SMTP firewall rules to delete for VM %s (vmid: %s)", node, vmid
            )
            return

        logger.info(
            "[CLEANUP] Deleting %d firewall rule(s) for VM %s (vmid: %s) (preserving SMTP rules)",
            len(rules_to_delete),
            node,
            vmid,
        )
        deleted_count = 0
        for pos in sorted(rules_to_delete, reverse=True):
            try:
                proxmox.nodes(node).qemu(vmid).firewall.rules(pos).delete()
                deleted_count += 1
            except Exception as e:
                logger.warning(
                    "[CLEANUP] Failed to delete firewall rule at position %d for VM %s (vmid: %s): %s",
                    pos,
                    node,
                    vmid,
                    e,
                )

        if deleted_count > 0:
            logger.info(
                "[CLEANUP] Successfully deleted %d firewall rule(s) for VM %s (vmid: %s)",
                deleted_count,
                node,
                vmid,
            )
    except Exception as e:
        logger.error(
            "[CLEANUP] Failed to delete firewall rules for VM %s (vmid: %s): %s",
            node,
            vmid,
            e,
            exc_info=True,
        )


def _delete_ipset_entries(
    proxmox: Any, node: str, vmid: int, ipset_name: str, logger: kopf.Logger
) -> None:
    """Delete all entries (prefixes) from an IPSet.

    Args:
        proxmox: Proxmox connection object
        node: Proxmox node name
        vmid: VM ID
        ipset_name: Name of the IPSet
        logger: Logger instance
    """
    try:
        entries = proxmox.nodes(node).qemu(vmid).firewall.ipset(ipset_name).get()
        if not entries:
            logger.debug(
                "[CLEANUP] No entries to delete from IPSet %s for VM %s (vmid: %s)",
                ipset_name,
                node,
                vmid,
            )
            return

        logger.info(
            "[CLEANUP] Deleting %d entry/entries from IPSet %s for VM %s (vmid: %s)",
            len(entries),
            ipset_name,
            node,
            vmid,
        )
        deleted_count = 0
        for e in entries:
            cidr = e.get("cidr")
            if cidr:
                try:
                    encoded_cidr = urllib.parse.quote(cidr, safe="")
                    proxmox.nodes(node).qemu(vmid).firewall.ipset(ipset_name)(encoded_cidr).delete()
                    deleted_count += 1
                except Exception as e:
                    logger.warning(
                        "[CLEANUP] Failed to delete entry %s from IPSet %s for VM %s (vmid: %s): %s",
                        cidr,
                        ipset_name,
                        node,
                        vmid,
                        e,
                    )

        if deleted_count > 0:
            logger.info(
                "[CLEANUP] Successfully deleted %d entry/entries from IPSet %s for VM %s (vmid: %s)",
                deleted_count,
                ipset_name,
                node,
                vmid,
            )
    except Exception as e:
        logger.warning(
            "[CLEANUP] Failed to delete entries from IPSet %s for VM %s (vmid: %s): %s",
            ipset_name,
            node,
            vmid,
            e,
        )


def _cleanup_firewall_resources(
    client: ProxmoxClient,
    ruleset_name: str,
    target_vm_info: tuple[str, str, int],
    logger: kopf.Logger,
) -> None:
    """Clean up firewall resources (rules and ipsets) for a ruleset on a single VM.

    Args:
        client: Proxmox client
        ruleset_name: Name of the firewall ruleset
        target_vm_info: Tuple of (node, vm_name, vmid)
        logger: Logger instance
    """
    node, vm_name, vmid = target_vm_info
    proxmox = client._get_connection()
    ruleset_hash = hashlib.sha256(ruleset_name.encode()).hexdigest()[:8]

    logger.info(
        "[CLEANUP] Starting cleanup of firewall resources for ruleset %s on VM %s (vmid: %s, node: %s)",
        ruleset_name,
        vm_name,
        vmid,
        node,
    )

    # 1. Delete all firewall rules
    _delete_firewall_rules(proxmox, node, vmid, logger)

    # 2. Delete IPSets and their entries
    try:
        ipsets = proxmox.nodes(node).qemu(vmid).firewall.ipset.get()
        matching_ipsets = []
        for ipset in ipsets:
            ipset_name = ipset.get("name", "")
            # Skip ipfilter- IPSets (they should never be deleted)
            if ipset_name.startswith("ipfilter-"):
                continue
            # Match IPSets with the ruleset hash prefix
            if ipset_name.startswith(f"frs-{ruleset_hash}-"):
                matching_ipsets.append(ipset_name)

        if not matching_ipsets:
            logger.debug(
                "[CLEANUP] No matching IPSets to delete for ruleset %s on VM %s (vmid: %s)",
                ruleset_name,
                vm_name,
                vmid,
            )
        else:
            logger.info(
                "[CLEANUP] Found %d matching IPSet(s) to delete for ruleset %s on VM %s (vmid: %s)",
                len(matching_ipsets),
                ruleset_name,
                vm_name,
                vmid,
            )

            for ips_name in matching_ipsets:
                try:
                    # Delete all entries from the IPSet
                    _delete_ipset_entries(proxmox, node, vmid, ips_name, logger)

                    # Delete the IPSet itself
                    try:
                        proxmox.nodes(node).qemu(vmid).firewall.ipset(ips_name).delete()
                        logger.info(
                            "[CLEANUP] Successfully deleted IPSet %s for VM %s (vmid: %s)",
                            ips_name,
                            vm_name,
                            vmid,
                        )
                    except Exception as e:
                        logger.warning(
                            "[CLEANUP] Failed to delete IPSet %s for VM %s (vmid: %s): %s",
                            ips_name,
                            vm_name,
                            vmid,
                            e,
                        )
                except Exception as e:
                    logger.error(
                        "[CLEANUP] Failed to cleanup IPSet %s for VM %s (vmid: %s): %s",
                        ips_name,
                        vm_name,
                        vmid,
                        e,
                        exc_info=True,
                    )
    except Exception as e:
        logger.error(
            "[CLEANUP] Failed to fetch IPSets for VM %s (vmid: %s): %s",
            vm_name,
            vmid,
            e,
            exc_info=True,
        )

    logger.info(
        "[CLEANUP] Completed cleanup of firewall resources for ruleset %s on VM %s (vmid: %s)",
        ruleset_name,
        vm_name,
        vmid,
    )


# --- CORE HANDLERS ---


def _is_any_only(prefixes: list[str] | None) -> bool:
    """Check if prefixes contain only 'any' (0.0.0.0/0 or ::/0).

    Args:
        prefixes: List of prefix strings or None

    Returns:
        True if prefixes contain only a single "any" prefix, False otherwise
    """
    if not prefixes or len(prefixes) != 1:
        return False
    return prefixes[0] in ("0.0.0.0/0", "::/0")


def _create_ipset_with_prefixes(
    proxmox: Any,
    node: str,
    vmid: int,
    ipset_name: str,
    prefixes: list[str],
    vm_name: str,
    logger: kopf.Logger,
) -> None:
    """Create an IPSet and add all prefixes to it.

    Args:
        proxmox: Proxmox connection object
        node: Proxmox node name
        vmid: VM ID
        ipset_name: Name of the IPSet to create
        prefixes: List of prefixes (CIDR) to add to the IPSet
        vm_name: VM name for logging
        logger: Logger instance
    """
    # Create IPSet
    try:
        proxmox.nodes(node).qemu(vmid).firewall.ipset.post(name=ipset_name)
        logger.debug("[CREATE] Created IPSet %s for VM %s (vmid: %s)", ipset_name, vm_name, vmid)
    except Exception as e:
        logger.warning(
            "[CREATE] Failed to create IPSet %s for VM %s (vmid: %s), may already exist: %s",
            ipset_name,
            vm_name,
            vmid,
            e,
        )

    # Add Prefixes
    added_count = 0
    for prefix in prefixes:
        try:
            proxmox.nodes(node).qemu(vmid).firewall.ipset(ipset_name).post(
                cidr=prefix, comment=f"auto-{prefix}"
            )
            added_count += 1
            logger.debug(
                "[CREATE] Added prefix %s to IPSet %s for VM %s (vmid: %s)",
                prefix,
                ipset_name,
                vm_name,
                vmid,
            )
        except Exception as e:
            logger.warning(
                "[CREATE] Failed to add prefix %s to IPSet %s for VM %s (vmid: %s), may already exist: %s",
                prefix,
                ipset_name,
                vm_name,
                vmid,
                e,
            )

    if added_count > 0:
        logger.debug(
            "[CREATE] Successfully added %d prefix(es) to IPSet %s for VM %s (vmid: %s)",
            added_count,
            ipset_name,
            vm_name,
            vmid,
        )


def _is_any_protocol(protocol: str | None) -> bool:
    """True when CR protocol means match all protocols (omit Proxmox proto=)."""
    return (protocol or "").strip().lower() in ("", "any")


def _proxmox_proto(protocol: str | None) -> str:
    """Map CR protocol to the name Proxmox expects (IPProtocolSelector /etc/protocols)."""
    value = (protocol or "").strip().lower()
    aliases = {
        "icmpv6": "ipv6-icmp",
        "ax25": "ax.25",
        "ax.25": "ax.25",
    }
    return aliases.get(value, value)


def _build_firewall_rule_params(
    rule: Any,
    ruleset_name: str,
    source_is_any_only: bool,
    destination_is_any_only: bool,
) -> dict[str, Any]:
    """Build firewall rule parameters dictionary for Proxmox API.

    Args:
        rule: Firewall rule object from CR
        ruleset_name: Name of the firewall ruleset
        source_is_any_only: Whether source contains only "any" prefix
        destination_is_any_only: Whether destination contains only "any" prefix

    Returns:
        Dictionary of parameters for Proxmox firewall rule creation
    """
    params: dict[str, Any] = {
        "action": rule.action.upper(),
        "type": "in" if rule.direction == "ingress" else "out",
        "enable": 1,
    }

    # Omit proto when protocol is any/empty — Proxmox matches all protocols.
    if not _is_any_protocol(getattr(rule, "protocol", None)):
        params["proto"] = _proxmox_proto(rule.protocol)

    # Omit source/dest when "any" — Proxmox treats explicit 0.0.0.0/0 and ::/0
    # differently from an unset field; unset means any and actually matches.
    if rule.sourcePrefixes and not source_is_any_only:
        src_ipset = _normalize_ipset_name(ruleset_name, rule.id, "src")
        params["source"] = f"+{src_ipset}"

    if rule.destinationPrefixes and not destination_is_any_only:
        dst_ipset = _normalize_ipset_name(ruleset_name, rule.id, "dst")
        params["dest"] = f"+{dst_ipset}"

    if rule.sourcePorts:
        params["sport"] = rule.sourcePorts

    if rule.destinationPorts:
        params["dport"] = rule.destinationPorts

    return params


def _create_firewall_rule(
    proxmox: Any,
    node: str,
    vmid: int,
    params: dict[str, Any],
    rule: Any,
    vm_name: str,
    logger: kopf.Logger,
) -> None:
    """Create a firewall rule on Proxmox.

    Args:
        proxmox: Proxmox connection object
        node: Proxmox node name
        vmid: VM ID
        params: Firewall rule parameters dictionary
        rule: Firewall rule object from CR (for logging)
        vm_name: VM name for logging
        logger: Logger instance
    """
    try:
        proxmox.nodes(node).qemu(vmid).firewall.rules.post(**params)
        logger.debug(
            "[CREATE] Created firewall rule (pos: %s, action: %s, type: %s) for VM %s (vmid: %s)",
            rule.position,
            rule.action,
            rule.direction,
            vm_name,
            vmid,
        )
    except Exception as e:
        logger.warning(
            "[CREATE] Failed to create firewall rule (pos: %s, action: %s, type: %s) for VM %s (vmid: %s): %s",
            rule.position,
            rule.action,
            rule.direction,
            vm_name,
            vmid,
            e,
        )


def _create_firewall_rules_and_ipsets(
    proxmox: Any,
    ruleset_name: str,
    rules: list[Any],
    vms: list[tuple[str, str, int]],
    logger: kopf.Logger,
) -> None:
    """Create firewall rules and IPSets for given VMs.

    This function creates IPSets, prefixes, and firewall rules.
    Used for both creation and reconciliation.

    Args:
        proxmox: Proxmox connection object
        ruleset_name: Name of the firewall ruleset
        rules: List of firewall rules to create
        vms: List of (node, vm_name, vmid) tuples
        logger: Logger instance
    """
    if not rules:
        logger.debug("[CREATE] No rules to create for ruleset %s", ruleset_name)
        return

    if not vms:
        logger.debug("[CREATE] No VMs to create firewall rules for ruleset %s", ruleset_name)
        return

    # Sort rules by position and reverse order for creation
    # Proxmox inserts rules at the top, so we create from highest to lowest position
    # to ensure they end up in the correct order (0, 1, 2, ...)
    sorted_rules = sorted(rules, key=lambda r: r.position if r.position is not None else 999999)
    reversed_rules = list(reversed(sorted_rules))

    logger.info(
        "[CREATE] Creating firewall rules and IPSets for ruleset %s on %d VM(s)",
        ruleset_name,
        len(vms),
    )

    for node, vm_name, vmid in vms:
        logger.debug(
            "[CREATE] Processing VM %s (vmid: %s, node: %s) for ruleset %s",
            vm_name,
            vmid,
            node,
            ruleset_name,
        )

        for rule in reversed_rules:
            # Check if source/destination is "any" only (0.0.0.0/0 or ::/0 and nothing else)
            source_is_any_only = _is_any_only(rule.sourcePrefixes)
            destination_is_any_only = _is_any_only(rule.destinationPrefixes)

            # 1. Create IPSets and Prefixes
            # Only skip creating ipset if it's "any" only (0.0.0.0/0 or ::/0 alone)
            # If "any" is combined with other prefixes, create ipset with all prefixes
            for suffix, prefixes in [
                ("src", rule.sourcePrefixes),
                ("dst", rule.destinationPrefixes),
            ]:
                if not prefixes:
                    continue

                # Skip creating ipset only if it's "any" only (0.0.0.0/0 or ::/0 alone)
                if _is_any_only(prefixes):
                    continue

                ipset_name = _normalize_ipset_name(ruleset_name, rule.id, suffix)
                _create_ipset_with_prefixes(
                    proxmox, node, vmid, ipset_name, prefixes, vm_name, logger
                )

            # 2. Create Firewall Rule
            params = _build_firewall_rule_params(
                rule, ruleset_name, source_is_any_only, destination_is_any_only
            )
            _create_firewall_rule(proxmox, node, vmid, params, rule, vm_name, logger)

    logger.info(
        "[CREATE] Completed creating firewall rules and IPSets for ruleset %s", ruleset_name
    )


@kopf.on.delete("vm.proxmox.com", "v1alpha1", "firewallrulesets")
def on_firewall_ruleset_delete(spec: dict, meta: dict, logger: kopf.Logger, **_: Any) -> None:
    """Function triggered when FirewallRuleSet is deleted. Removes all Proxmox resources."""
    name = meta.get("name", "")
    ns = meta.get("namespace", "default")

    try:
        # Get VMs grouped by proxmoxUrl (handles multiple Proxmox instances)
        vms_by_url = _get_target_vms_by_proxmox_url(name, ns, logger)
        if not vms_by_url:
            logger.warning("[FIREWALL DELETE] No VMs found for ruleset %s", name)
            return

        # Process each Proxmox instance separately
        for proxmox_url, vm_specs in vms_by_url.items():
            try:
                client = _get_client(proxmox_url)
                vms = _get_vms_for_proxmox_url(vm_specs, client)
                if vms:
                    for vm_info in vms:
                        _cleanup_firewall_resources(client, name, vm_info, logger)
            except Exception as e:
                logger.error(
                    "[FIREWALL DELETE] Failed to delete firewall ruleset %s for proxmoxUrl %s: %s",
                    name,
                    proxmox_url,
                    e,
                    exc_info=True,
                )
    except Exception as e:
        logger.error(
            "[FIREWALL DELETE] Failed to delete firewall ruleset %s: %s", name, e, exc_info=True
        )


def _normalize_prefix(prefix: str) -> str:
    """Normalize IP prefix to canonical CIDR form for comparison.

    Converts IP addresses to their canonical CIDR form:
    - "1.1.1.1" -> "1.1.1.1/32" (IPv4)
    - "2001:db8::1" -> "2001:db8::1/128" (IPv6)
    - "1.1.1.1/32" -> "1.1.1.1/32" (already in CIDR form)
    """
    prefix = prefix.strip()

    if not prefix:
        return prefix

    try:
        # If prefix already contains CIDR notation, parse as network
        if "/" in prefix:
            net = ipaddress.ip_network(prefix, strict=False)
            return str(net)

        # Otherwise, treat as a single IP address
        ip = ipaddress.ip_address(prefix)
        # Convert to network with appropriate prefix length
        prefix_len = 32 if ip.version == 4 else 128
        net = ipaddress.ip_network(f"{prefix}/{prefix_len}", strict=False)
        return str(net)

    except (ValueError, ipaddress.AddressValueError):
        return prefix


def _reconcile_firewall_ipsets(
    client: ProxmoxClient,
    ruleset_name: str,
    valid_rules: list[Any],
    target_vm_info: list[tuple[str, str, int]],
    logger: kopf.Logger,
) -> None:
    """Reconcile IPSets and their prefixes to match CR state.

    This function:
    1. Fetches all IPSets and prefixes from Proxmox (batch operation)
    2. Compares with expected state from CR
    3. Creates missing IPSets
    4. Deletes extra IPSets (manually created)
    5. Syncs prefixes (adds missing, removes extra)

    Args:
        client: Proxmox client
        ruleset_name: Name of the firewall ruleset
        valid_rules: List of valid firewall rules from CR
        target_vm_info: List of (node, vm_name, vmid) tuples
        logger: Logger instance
    """
    if not target_vm_info:
        return

    proxmox = client._get_connection()

    # Build expected IPSets from CR rules
    # Skip creating ipset only if it's "any" only (0.0.0.0/0 or ::/0 alone)
    # If "any" is combined with other prefixes, create ipset with all prefixes
    expected_ipsets: dict[str, set[str]] = {}
    for rule in valid_rules:
        if rule.sourcePrefixes:
            # Skip creating ipset only if it's "any" only (0.0.0.0/0 or ::/0 alone)
            source_is_any_only = len(rule.sourcePrefixes) == 1 and (
                rule.sourcePrefixes[0] == "0.0.0.0/0" or rule.sourcePrefixes[0] == "::/0"
            )
            if not source_is_any_only:
                ipset_name = _normalize_ipset_name(ruleset_name, rule.id, "src")
                expected_ipsets[ipset_name] = {_normalize_prefix(p) for p in rule.sourcePrefixes}
        if rule.destinationPrefixes:
            # Skip creating ipset only if it's "any" only (0.0.0.0/0 or ::/0 alone)
            destination_is_any_only = len(rule.destinationPrefixes) == 1 and (
                rule.destinationPrefixes[0] == "0.0.0.0/0" or rule.destinationPrefixes[0] == "::/0"
            )
            if not destination_is_any_only:
                ipset_name = _normalize_ipset_name(ruleset_name, rule.id, "dst")
                expected_ipsets[ipset_name] = {
                    _normalize_prefix(p) for p in rule.destinationPrefixes
                }

    for node, vm_name, vmid in target_vm_info:
        # Step 1: Fetch ALL IPSets on the VM (including manually created ones)
        existing_ipsets: dict[str, set[str]] = {}
        try:
            all_ipsets_raw = proxmox.nodes(node).qemu(vmid).firewall.ipset.get()
            all_ipset_names = [
                ipset.get("name", "") for ipset in all_ipsets_raw if ipset.get("name", "")
            ]

            # Fetch prefixes for each IPSet
            for ipset_name in all_ipset_names:
                try:
                    entries = proxmox.nodes(node).qemu(vmid).firewall.ipset(ipset_name).get()
                    prefixes = set()
                    for entry in entries:
                        cidr = entry.get("cidr", "")
                        if cidr and not cidr.startswith("@"):
                            normalized = _normalize_prefix(cidr)
                            prefixes.add(normalized)
                    existing_ipsets[ipset_name] = prefixes
                except Exception:
                    existing_ipsets[ipset_name] = set()
        except Exception:
            pass

        # Step 2: Find missing IPSets (in CR but not in Proxmox)
        missing_ipsets = set(expected_ipsets.keys()) - set(existing_ipsets.keys())
        if missing_ipsets:
            for ipset_name in missing_ipsets:
                try:
                    proxmox.nodes(node).qemu(vmid).firewall.ipset.post(name=ipset_name)
                    # Initialize empty set for prefix sync
                    existing_ipsets[ipset_name] = set()
                except Exception:
                    pass

        # Step 3: Find extra IPSets (in Proxmox but not in CR - manually created)
        # This includes IPSets with and without the ruleset prefix
        extra_ipsets = set(existing_ipsets.keys()) - set(expected_ipsets.keys())
        if extra_ipsets:
            for ipset_name in extra_ipsets:
                # Skip ipfilter- IPSets (they should never be deleted)
                if ipset_name.startswith("ipfilter-"):
                    continue
                try:
                    # Delete all entries from the IPSet first
                    _delete_ipset_entries(proxmox, node, vmid, ipset_name, logger)

                    # Delete the IPSet itself
                    try:
                        proxmox.nodes(node).qemu(vmid).firewall.ipset(ipset_name).delete()
                        logger.debug(
                            "[IPSET RECONCILE] Successfully deleted extra IPSet %s for VM %s (vmid: %s)",
                            ipset_name,
                            vm_name,
                            vmid,
                        )
                    except Exception as e:
                        logger.warning(
                            "[IPSET RECONCILE] Failed to delete extra IPSet %s for VM %s (vmid: %s): %s",
                            ipset_name,
                            vm_name,
                            vmid,
                            e,
                        )
                except Exception as e:
                    logger.warning(
                        "[IPSET RECONCILE] Failed to cleanup extra IPSet %s for VM %s (vmid: %s): %s",
                        ipset_name,
                        vm_name,
                        vmid,
                        e,
                    )

        # Step 4: Sync prefixes for existing IPSets
        for ipset_name in expected_ipsets.keys():
            expected_prefixes = expected_ipsets[ipset_name]
            existing_prefixes = existing_ipsets.get(ipset_name, set())

            # Find prefixes to add and remove
            prefixes_to_add = expected_prefixes - existing_prefixes
            prefixes_to_remove = existing_prefixes - expected_prefixes

            if prefixes_to_add or prefixes_to_remove:
                # Remove extra prefixes
                if prefixes_to_remove:
                    try:
                        entries = proxmox.nodes(node).qemu(vmid).firewall.ipset(ipset_name).get()
                        deleted_count = 0
                        for entry in entries:
                            entry_cidr = entry.get("cidr", "")
                            if entry_cidr and not entry_cidr.startswith("@"):
                                normalized = _normalize_prefix(entry_cidr)
                                if normalized in prefixes_to_remove:
                                    try:
                                        encoded = urllib.parse.quote(entry_cidr, safe="")
                                        proxmox.nodes(node).qemu(vmid).firewall.ipset(ipset_name)(
                                            encoded
                                        ).delete()
                                        deleted_count += 1
                                        logger.debug(
                                            "[IPSET RECONCILE] Deleted prefix %s from IPSet %s for VM %s (vmid: %s)",
                                            entry_cidr,
                                            ipset_name,
                                            vm_name,
                                            vmid,
                                        )
                                    except Exception as e:
                                        logger.warning(
                                            "[IPSET RECONCILE] Failed to delete prefix %s from IPSet %s for VM %s (vmid: %s): %s",
                                            entry_cidr,
                                            ipset_name,
                                            vm_name,
                                            vmid,
                                            e,
                                        )
                        if deleted_count > 0:
                            logger.debug(
                                "[IPSET RECONCILE] Deleted %d extra prefix(es) from IPSet %s for VM %s (vmid: %s)",
                                deleted_count,
                                ipset_name,
                                vm_name,
                                vmid,
                            )
                    except Exception as e:
                        logger.warning(
                            "[IPSET RECONCILE] Failed to remove extra prefixes from IPSet %s for VM %s (vmid: %s): %s",
                            ipset_name,
                            vm_name,
                            vmid,
                            e,
                        )

                # Add missing prefixes
                if prefixes_to_add:
                    added_count = 0
                    for prefix in prefixes_to_add:
                        try:
                            # Use normalized prefix for creation (canonical CIDR form)
                            proxmox.nodes(node).qemu(vmid).firewall.ipset(ipset_name).post(
                                cidr=prefix, comment=f"auto-{prefix}"
                            )
                            added_count += 1
                            logger.debug(
                                "[IPSET RECONCILE] Added prefix %s to IPSet %s for VM %s (vmid: %s)",
                                prefix,
                                ipset_name,
                                vm_name,
                                vmid,
                            )
                        except Exception as e:
                            logger.warning(
                                "[IPSET RECONCILE] Failed to add prefix %s to IPSet %s for VM %s (vmid: %s): %s",
                                prefix,
                                ipset_name,
                                vm_name,
                                vmid,
                                e,
                            )
                    if added_count > 0:
                        logger.debug(
                            "[IPSET RECONCILE] Added %d missing prefix(es) to IPSet %s for VM %s (vmid: %s)",
                            added_count,
                            ipset_name,
                            vm_name,
                            vmid,
                        )


def _reconcile_ipsets_timer(spec: dict, meta: dict, logger: kopf.Logger) -> None:
    """IPSet reconciliation function called by timer."""
    try:
        name = meta.get("name", "")
        ns = meta.get("namespace", "default")

        # Get VMs grouped by proxmoxUrl (handles multiple Proxmox instances)
        vms_by_url = _get_target_vms_by_proxmox_url(name, ns, logger)
        if not vms_by_url:
            return

        desired = FirewallRuleSetSpec.model_validate(spec)

        # Filter valid rules (only for IPSet reconciliation)
        valid_rules = []
        for rule in desired.rules:
            if rule.sourcePrefixes or rule.destinationPrefixes:
                valid_rules.append(rule)

        if not valid_rules:
            return

        # Process each Proxmox instance separately
        for proxmox_url, vm_specs in vms_by_url.items():
            try:
                client = _get_client(proxmox_url)
                vms = _get_vms_for_proxmox_url(vm_specs, client)
                if vms:
                    # Reconcile IPSets only (no firewall rules reconciliation)
                    _reconcile_firewall_ipsets(client, name, valid_rules, vms, logger)
            except Exception as e:
                logger.error(
                    "[FIREWALL IPSET RECONCILE] Failed for ruleset %s, proxmoxUrl %s: %s",
                    name,
                    proxmox_url,
                    e,
                    exc_info=True,
                )
    except Exception:
        pass


def _build_expected_proxmox_rule(ruleset_name: str, cr_rule: Any) -> dict:
    """Build expected Proxmox rule format from CR rule for comparison.

    Returns a dict with normalized rule properties that can be compared with actual Proxmox rules.
    """
    expected = {
        "action": cr_rule.action.upper(),
        "type": "in" if cr_rule.direction == "ingress" else "out",
        "proto": "" if _is_any_protocol(cr_rule.protocol) else _proxmox_proto(cr_rule.protocol),
        "sport": cr_rule.sourcePorts or "",
        "dport": cr_rule.destinationPorts or "",
        "pos": cr_rule.position if cr_rule.position is not None else 999999,
    }

    # Build expected source (empty = any; do not use literal 0.0.0.0/0 / ::/0)
    expected["source"] = ""
    if cr_rule.sourcePrefixes:
        source_is_any_only = len(cr_rule.sourcePrefixes) == 1 and (
            cr_rule.sourcePrefixes[0] == "0.0.0.0/0" or cr_rule.sourcePrefixes[0] == "::/0"
        )
        if not source_is_any_only:
            expected["source"] = f"+{_normalize_ipset_name(ruleset_name, cr_rule.id, 'src')}"

    # Build expected destination (empty = any)
    expected["dest"] = ""
    if cr_rule.destinationPrefixes:
        destination_is_any_only = len(cr_rule.destinationPrefixes) == 1 and (
            cr_rule.destinationPrefixes[0] == "0.0.0.0/0"
            or cr_rule.destinationPrefixes[0] == "::/0"
        )
        if not destination_is_any_only:
            expected["dest"] = f"+{_normalize_ipset_name(ruleset_name, cr_rule.id, 'dst')}"

    return expected


def _normalize_proxmox_rule(proxmox_rule: dict) -> dict:
    """Normalize Proxmox rule format for comparison.

    Converts Proxmox rule to a comparable format matching expected format.
    """
    # Normalize type (direction) - Proxmox uses "in" for ingress, "out" for egress
    type_value = (proxmox_rule.get("type", "") or "").lower()

    normalized = {
        "action": (proxmox_rule.get("action", "") or "").upper(),
        "type": type_value,  # "in" for ingress, "out" for egress
        "proto": _proxmox_proto(proxmox_rule.get("proto", "") or ""),
        "sport": proxmox_rule.get("sport", "") or "",
        "dport": proxmox_rule.get("dport", "") or "",
        # Keep literal 0.0.0.0/0 / ::/0 as-is so they mismatch expected empty "any"
        # and the timer rewrites them to unset (which Proxmox actually matches).
        "source": proxmox_rule.get("source", "") or "",
        "dest": proxmox_rule.get("dest", "") or "",
    }

    # Normalize position
    pos = proxmox_rule.get("pos")
    if pos is not None:
        try:
            normalized["pos"] = int(pos)
        except (ValueError, TypeError):
            normalized["pos"] = None
    else:
        normalized["pos"] = None

    return normalized


def _rules_match(expected: dict, actual: dict) -> tuple[bool, list[str]]:
    """Compare expected and actual rule properties.

    Returns (match, mismatches) where mismatches is a list of mismatch descriptions.
    """
    mismatches = []

    # Compare all properties
    if expected.get("pos") != actual.get("pos"):
        mismatches.append(f"position: expected={expected.get('pos')}, actual={actual.get('pos')}")
    if expected.get("action") != actual.get("action"):
        mismatches.append(
            f"action: expected={expected.get('action')}, actual={actual.get('action')}"
        )
    # Compare direction (type: "in" = ingress, "out" = egress)
    if expected.get("type") != actual.get("type"):
        expected_dir = "ingress" if expected.get("type") == "in" else "egress"
        actual_dir = "ingress" if actual.get("type") == "in" else "egress"
        mismatches.append(
            f"direction: expected={expected_dir} (type={expected.get('type')}), actual={actual_dir} (type={actual.get('type')})"
        )
    if expected.get("proto") != actual.get("proto"):
        mismatches.append(
            f"protocol: expected={expected.get('proto')}, actual={actual.get('proto')}"
        )
    if expected.get("sport") != actual.get("sport"):
        mismatches.append(
            f"sourcePort: expected={expected.get('sport') or 'none'}, actual={actual.get('sport') or 'none'}"
        )
    if expected.get("dport") != actual.get("dport"):
        mismatches.append(
            f"destinationPort: expected={expected.get('dport') or 'none'}, actual={actual.get('dport') or 'none'}"
        )
    if expected.get("source") != actual.get("source"):
        mismatches.append(
            f"source: expected={expected.get('source') or 'none'}, actual={actual.get('source') or 'none'}"
        )
    if expected.get("dest") != actual.get("dest"):
        mismatches.append(
            f"destination: expected={expected.get('dest') or 'none'}, actual={actual.get('dest') or 'none'}"
        )

    return len(mismatches) == 0, mismatches


def _reconcile_firewall_rules_timer(spec: dict, meta: dict, logger: kopf.Logger) -> None:
    """Firewall rules reconciliation function - checks and fixes mismatches by comparing rule properties."""
    try:
        name = meta.get("name", "")
        ns = meta.get("namespace", "default")

        # Get VMs grouped by proxmoxUrl (handles multiple Proxmox instances)
        vms_by_url = _get_target_vms_by_proxmox_url(name, ns, logger)
        if not vms_by_url:
            return

        desired = FirewallRuleSetSpec.model_validate(spec)

        expected_rules: list[tuple[int, dict[str, Any]]] = []
        # Handle empty ruleset
        if not desired.rules:
            # If CR has no rules, we should ensure Proxmox also has no rules
            # This will be handled in the comparison logic below
            pass
        else:
            # Build expected rules from CR (sorted by position)
            for cr_rule in sorted(
                desired.rules, key=lambda r: r.position if r.position is not None else 999999
            ):
                expected_rule = _build_expected_proxmox_rule(name, cr_rule)
                expected_rules.append(
                    (cr_rule.position if cr_rule.position is not None else 999999, expected_rule)
                )

        # Process each Proxmox instance separately
        for proxmox_url, vm_specs in vms_by_url.items():
            try:
                client = _get_client(proxmox_url)
                proxmox = client._get_connection()
                vms = _get_vms_for_proxmox_url(vm_specs, client)
                if not vms:
                    continue

                # Fetch all firewall rules from Proxmox for each VM
                for node, vm_name, vmid in vms:
                    try:
                        from .smtp_outbound import (
                            is_smtp_outbound_rule,
                            reconcile_vm_smtp_outbound_block,
                        )

                        all_rules = proxmox.nodes(node).qemu(vmid).firewall.rules.get() or []

                        # Build actual rules from Proxmox (sorted by position).
                        # Ignore provider SMTP outbound rules — owned by ProxmoxVM.spec.blockOutboundSmtp.
                        actual_rules = []
                        rules_without_position = []
                        for proxmox_rule in all_rules:
                            if is_smtp_outbound_rule(proxmox_rule):
                                continue
                            normalized = _normalize_proxmox_rule(proxmox_rule)
                            pos = normalized.get("pos")
                            if pos is not None:
                                actual_rules.append((pos, normalized))
                            else:
                                # Rules without position are unexpected - log and include in comparison
                                rules_without_position.append(normalized)

                        # Sort by position
                        actual_rules.sort(key=lambda x: x[0] if x[0] is not None else 999999)
                        expected_rules_sorted = sorted(
                            expected_rules, key=lambda x: x[0] if x[0] is not None else 999999
                        )

                        # Compare rules by relative order among non-SMTP rules (positions may be
                        # offset when SMTP DROPs occupy the top of the chain).
                        all_match = True
                        mismatches = []

                        # If there are rules without position, that's a mismatch
                        if rules_without_position:
                            all_match = False
                            mismatches.append(
                                f"Found {len(rules_without_position)} rule(s) without position in Proxmox"
                            )

                        # Handle empty ruleset case
                        if len(expected_rules_sorted) == 0:
                            if len(actual_rules) > 0:
                                all_match = False
                                mismatches.append(
                                    f"Expected no rules but found {len(actual_rules)} non-SMTP rule(s) in Proxmox"
                                )
                        # Check if we have at least as many rules as expected
                        elif len(actual_rules) < len(expected_rules_sorted):
                            all_match = False
                            mismatches.append(
                                f"Rule count mismatch: expected={len(expected_rules_sorted)}, actual={len(actual_rules)}"
                            )
                        else:
                            # Compare each expected rule with the non-SMTP rule at the same index
                            for idx, (expected_pos, expected_rule) in enumerate(
                                expected_rules_sorted
                            ):
                                if idx < len(actual_rules):
                                    actual_pos, actual_rule = actual_rules[idx]
                                    # Compare properties except absolute position (SMTP may shift positions)
                                    expected_cmp = dict(expected_rule)
                                    actual_cmp = dict(actual_rule)
                                    expected_cmp.pop("pos", None)
                                    actual_cmp.pop("pos", None)
                                    match, rule_mismatches = _rules_match(expected_cmp, actual_cmp)
                                    if not match:
                                        all_match = False
                                        mismatches.append(
                                            f"Position {expected_pos}: {', '.join(rule_mismatches)}"
                                        )
                                else:
                                    all_match = False
                                    mismatches.append(
                                        f"Position {expected_pos}: missing in Proxmox"
                                    )

                            # If there are more non-SMTP rules than expected, that's also a mismatch
                            if len(actual_rules) > len(expected_rules_sorted):
                                all_match = False
                                extra_count = len(actual_rules) - len(expected_rules_sorted)
                                mismatches.append(
                                    f"Found {extra_count} extra non-SMTP rule(s) in Proxmox beyond expected {len(expected_rules_sorted)}"
                                )

                        if not all_match:
                            logger.warning(
                                "[FIREWALL RULES RECONCILE] Mismatch detected for VM %s (vmid: %s): %s",
                                vm_name,
                                vmid,
                                "; ".join(mismatches),
                            )
                            try:
                                _cleanup_firewall_resources(
                                    client, name, (node, vm_name, vmid), logger
                                )
                                _create_firewall_rules_and_ipsets(
                                    proxmox, name, desired.rules, [(node, vm_name, vmid)], logger
                                )
                            except Exception as e:
                                logger.error(
                                    "[FIREWALL RULES RECONCILE] Failed to fix mismatch for VM %s (vmid: %s): %s",
                                    vm_name,
                                    vmid,
                                    e,
                                    exc_info=True,
                                )

                        # Re-ensure provider SMTP rules after ruleset reconcile (preserve priority).
                        try:
                            block_smtp = False
                            for vs in vm_specs:
                                v_cfg = (vs or {}).get("vmConfig") or {}
                                if v_cfg.get("name") == vm_name:
                                    block_smtp = bool((vs or {}).get("blockOutboundSmtp", False))
                                    break
                            reconcile_vm_smtp_outbound_block(
                                proxmox, node, vmid, block_smtp, logger
                            )
                        except Exception as e:
                            logger.warning(
                                "[SMTP] Failed to re-ensure SMTP after ruleset reconcile for vmid %s: %s",
                                vmid,
                                e,
                            )
                    except Exception as e:
                        logger.error(
                            "[FIREWALL RULES RECONCILE] Failed to fetch firewall rules for VM %s (vmid: %s): %s",
                            vm_name,
                            vmid,
                            e,
                            exc_info=True,
                        )

            except Exception as e:
                logger.error(
                    "[FIREWALL RULES RECONCILE] Failed to process proxmoxUrl %s: %s",
                    proxmox_url,
                    e,
                    exc_info=True,
                )
    except Exception as e:
        logger.error(
            "[FIREWALL RULES RECONCILE] Reconciliation failed for ruleset %s: %s",
            meta.get("name", "unknown"),
            e,
            exc_info=True,
        )


@kopf.timer(
    "vm.proxmox.com",
    "v1alpha1",
    "firewallrulesets",
    interval=RECONCILE_INTERVAL,
)
def reconcile_firewall_ruleset(spec: dict, meta: dict, logger: kopf.Logger, **_: Any) -> None:
    """Periodic reconciliation loop for FirewallRuleSet resources.

    Uses ``interval`` only (no ``idle``). Annotation/watch churn on the CR
    resets kopf's idle clock and can prevent the timer from ever running.
    """
    _reconcile_ipsets_timer(spec, meta, logger)
    _reconcile_firewall_rules_timer(spec, meta, logger)
