"""SolusVM 2 server lifecycle operations.

WHMCS field names. Parse IPv4 from ``ip_addresses.ipv4[]``, never SDK ``ips[]``.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlparse

from .client import SolusVMClient, management_host, unwrap_data
from .constants import (
    EXTRA_IPV4_POLL_INTERVAL,
    EXTRA_IPV4_POLL_TIMEOUT,
    TASK_FAILED_STATUSES,
    TASK_FINISHED_STATUSES,
    TASK_POLL_INTERVAL,
    TASK_POLL_TIMEOUT,
)
from .errors import SolusVMHTTPError, TransientError
from .tasks import extract_task_id, poll_if_task

logger = logging.getLogger(__name__)


def parse_ipv4(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Parse WHMCS ``data.ip_addresses.ipv4[]`` of ``{id, ip, is_primary}``.

    Do **not** read SDK ``ips[]``.
    """
    if not data:
        return []
    payload = data.get("data", data) if isinstance(data, dict) else data
    if not isinstance(payload, dict):
        return []
    ip_addresses = payload.get("ip_addresses")
    if not isinstance(ip_addresses, dict):
        return []
    raw = ip_addresses.get("ipv4")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        ip = item.get("ip")
        if not ip:
            continue
        entry: dict[str, Any] = {
            "id": item.get("id"),
            "ip": str(ip).strip(),
            "is_primary": bool(item.get("is_primary")),
        }
        out.append(entry)
    return out


def parse_ipv4_set(data: dict[str, Any] | None) -> set[str]:
    return {item["ip"] for item in parse_ipv4(data) if item.get("ip")}


def parse_ipv4_ordered(data: dict[str, Any] | None) -> list[str]:
    """Primary first, then extras. Do not lexicographically sort (that can swap roles)."""
    rows = parse_ipv4(data)
    primary = [r["ip"] for r in rows if r.get("is_primary") and r.get("ip")]
    extra = [r["ip"] for r in rows if not r.get("is_primary") and r.get("ip")]
    seen: set[str] = set()
    out: list[str] = []
    for ip in primary + extra:
        if ip in seen:
            continue
        seen.add(ip)
        out.append(ip)
    return out


def parse_ipv6_primary(data: dict[str, Any] | None) -> str:
    """Parse ``data.ip_addresses.ipv6.primary_ip``."""
    if not data:
        return ""
    payload = data.get("data", data) if isinstance(data, dict) else data
    if not isinstance(payload, dict):
        return ""
    ip_addresses = payload.get("ip_addresses")
    if not isinstance(ip_addresses, dict):
        return ""
    ipv6 = ip_addresses.get("ipv6")
    if isinstance(ipv6, dict):
        return str(ipv6.get("primary_ip") or "")
    return ""


def _ip_addresses_block(data: dict[str, Any] | None) -> dict[str, Any]:
    if not data:
        return {}
    payload = data.get("data", data) if isinstance(data, dict) else data
    if not isinstance(payload, dict):
        return {}
    block = payload.get("ip_addresses")
    return block if isinstance(block, dict) else {}


def _prefix_from_item(item: dict[str, Any], *, version: int) -> str:
    from noderings_hooks.net import normalize_prefix

    for key in ("cidr", "prefix", "prefixlen", "prefix_len", "netmask", "mask"):
        if item.get(key) is not None:
            got = normalize_prefix(item.get(key), version=version)
            if got:
                return got
    for key in ("ip", "address", "subnet"):
        got = normalize_prefix(item.get(key), version=version)
        if got:
            return got
    return ""


def parse_ipv4_prefix(data: dict[str, Any] | None) -> str:
    from noderings_hooks.net import normalize_prefix

    block = _ip_addresses_block(data)
    raw = block.get("ipv4")
    if isinstance(raw, list):
        primary = None
        extras: list[dict[str, Any]] = []
        for row in raw:
            if not isinstance(row, dict):
                continue
            if row.get("is_primary"):
                primary = row
            else:
                extras.append(row)
        for row in ([primary] if primary else []) + extras:
            got = _prefix_from_item(row, version=4)
            if got:
                return got
    return normalize_prefix(
        block.get("cidr") or block.get("prefix") or block.get("netmask"), version=4
    )


def parse_ipv6_prefix(data: dict[str, Any] | None) -> str:
    from noderings_hooks.net import normalize_prefix

    block = _ip_addresses_block(data)
    ipv6 = block.get("ipv6")
    if isinstance(ipv6, dict):
        got = _prefix_from_item(ipv6, version=6)
        if got:
            return got
    return normalize_prefix(block.get("ipv6_prefix") or block.get("ipv6_cidr"), version=6)


def parse_ipv4_gateway(data: dict[str, Any] | None) -> str:
    """Best-effort IPv4 gateway from GET /servers/{id}."""
    block = _ip_addresses_block(data)
    raw = block.get("ipv4")
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            gw = item.get("gateway") or item.get("gw")
            if gw:
                return str(gw).strip()
    for key in ("gateway", "ipv4_gateway"):
        val = block.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def parse_ipv6_gateway(data: dict[str, Any] | None) -> str:
    block = _ip_addresses_block(data)
    ipv6 = block.get("ipv6")
    if isinstance(ipv6, dict):
        gw = ipv6.get("gateway") or ipv6.get("gw")
        if gw:
            return str(gw).strip()
    return ""


def parse_dns_servers(data: dict[str, Any] | None) -> list[str]:
    """Guest DNS from SolusVM IP block (ns_1 / ns_2) or address objects."""
    found: list[str] = []

    def add(val: Any) -> None:
        if isinstance(val, str) and val.strip() and val.strip() not in found:
            found.append(val.strip())
        elif isinstance(val, list):
            for item in val:
                add(item)

    block = _ip_addresses_block(data)
    add(block.get("ns_1"))
    add(block.get("ns_2"))
    add(block.get("nameservers"))
    add(block.get("dns"))
    raw = block.get("ipv4")
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            add(item.get("ns_1"))
            add(item.get("ns_2"))
            add(item.get("dns1"))
            add(item.get("dns2"))
            add(item.get("nameservers"))
    ipv6 = block.get("ipv6")
    if isinstance(ipv6, dict):
        add(ipv6.get("ns_1"))
        add(ipv6.get("ns_2"))
        add(ipv6.get("nameservers"))
    return found


def parse_mac(data: dict[str, Any] | None) -> str:
    """Best-effort MAC from GET /servers/{id} (field name varies until lab dump)."""
    if not data:
        return ""
    payload = data.get("data", data) if isinstance(data, dict) else data
    if not isinstance(payload, dict):
        return ""
    for key in ("mac", "mac_address", "macAddress"):
        val = payload.get(key)
        if val:
            return str(val)
    network = payload.get("network")
    if isinstance(network, dict):
        for key in ("mac", "mac_address"):
            val = network.get(key)
            if val:
                return str(val)
    settings = payload.get("settings")
    if isinstance(settings, dict):
        val = settings.get("mac") or settings.get("mac_address")
        if val:
            return str(val)
    return ""


def get_vnc_password(data: dict[str, Any] | None) -> str:
    """settings.vnc_password or settings.vnc.password. Never log."""
    if not data:
        return ""
    payload = data.get("data", data) if isinstance(data, dict) else data
    if not isinstance(payload, dict):
        return ""
    settings = payload.get("settings")
    if not isinstance(settings, dict):
        return ""
    pwd = settings.get("vnc_password")
    if pwd:
        return str(pwd)
    vnc = settings.get("vnc")
    if isinstance(vnc, dict) and vnc.get("password"):
        return str(vnc["password"])
    return ""


def compute_resource_id_from_server(data: dict[str, Any] | None) -> int | None:
    if not data:
        return None
    payload = data.get("data", data) if isinstance(data, dict) else data
    if not isinstance(payload, dict):
        return None
    cr = payload.get("compute_resource")
    if isinstance(cr, dict) and cr.get("id") is not None:
        return int(cr["id"])
    if payload.get("compute_resource_id") is not None:
        return int(payload["compute_resource_id"])
    return None


def is_not_found(err: SolusVMHTTPError) -> bool:
    if err.status_code == 404:
        return True
    msg = (err.message or str(err)).lower()
    return "not found" in msg


def require_not_processing(client: SolusVMClient, server_id: int) -> dict[str, Any]:
    """GET /servers/{id}; raise TransientError if is_processing."""
    result = client.get_server(server_id)
    data = unwrap_data(result)
    if data.get("is_processing"):
        raise TransientError(
            f"Server {server_id} is_processing",
            status_code=409,
        )
    return data


def wait_for_ipv4(
    client: SolusVMClient,
    server_id: int,
    timeout: int = TASK_POLL_TIMEOUT,
) -> dict[str, Any]:
    """Poll GET /servers/{id} until ip_addresses.ipv4 is non-empty and not processing."""
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            result = client.get_server(server_id)
        except SolusVMHTTPError as e:
            if e.status_code == 404:
                last_error = e
                time.sleep(TASK_POLL_INTERVAL)
                continue
            raise
        data = unwrap_data(result)
        if data.get("is_processing"):
            time.sleep(TASK_POLL_INTERVAL)
            continue
        if parse_ipv4(data):
            return data
        time.sleep(TASK_POLL_INTERVAL)
    suffix = f": {last_error}" if last_error else ""
    raise SolusVMHTTPError(f"Server {server_id} has no IPv4 after {timeout}s{suffix}")


def wait_for_ipv6(
    client: SolusVMClient,
    server_id: int,
    timeout: int = TASK_POLL_TIMEOUT,
) -> dict[str, Any]:
    """Poll GET /servers/{id} until a primary IPv6 is present and not processing."""
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            result = client.get_server(server_id)
        except SolusVMHTTPError as e:
            if e.status_code == 404:
                last_error = e
                time.sleep(TASK_POLL_INTERVAL)
                continue
            raise
        data = unwrap_data(result)
        if data.get("is_processing"):
            time.sleep(TASK_POLL_INTERVAL)
            continue
        if parse_ipv6_primary(data):
            return data
        time.sleep(TASK_POLL_INTERVAL)
    suffix = f": {last_error}" if last_error else ""
    raise SolusVMHTTPError(f"Server {server_id} has no IPv6 after {timeout}s{suffix}")


def create_server(client: SolusVMClient, body: dict[str, Any]) -> dict[str, Any]:
    """POST /servers (WHMCS field names). Poll task if present; wait for IPv4."""
    result = client.create_server(body)
    if not result:
        raise SolusVMHTTPError("Empty response from POST /servers")
    data = unwrap_data(result)
    server_id = data.get("id")
    if not server_id:
        raise SolusVMHTTPError("POST /servers did not return data.id")
    server_id = int(server_id)
    logger.info("Created server id=%s uuid=%s", server_id, data.get("uuid"))
    poll_if_task(client, result)
    ready = wait_for_ipv4(client, server_id)
    ready.setdefault("id", server_id)
    if not ready.get("uuid"):
        ready["uuid"] = data.get("uuid", "")
    return ready


def wait_for_extra_ipv4(
    client: SolusVMClient,
    server_id: int,
    before: set[str],
    *,
    task_result: dict[str, Any] | None = None,
    timeout: int = EXTRA_IPV4_POLL_TIMEOUT,
    interval: float = EXTRA_IPV4_POLL_INTERVAL,
) -> dict[str, Any]:
    """Wait until GET /servers/{id} shows more IPv4 than ``before``.

    SolusVM 2 often assigns the address while the POST /ips task stays
    ``pending``. Succeed as soon as the extra IP is visible; only fail the
    task if it finishes without a new address.
    """
    task_id = extract_task_id(task_result)
    deadline = time.time() + timeout
    last: dict[str, Any] = {}
    while time.time() < deadline:
        last = unwrap_data(client.get_server(server_id))
        got = parse_ipv4_set(last)
        if got - before:
            logger.info(
                "Extra IPv4 on server %s: %s (task %s still in flight is ok)",
                server_id,
                sorted(got - before),
                task_id,
            )
            return last
        if task_id is not None:
            task = unwrap_data(client.get_task(task_id))
            status = str(task.get("status") or "").lower()
            if status in TASK_FAILED_STATUSES:
                output = task.get("output") or ""
                raise SolusVMHTTPError(
                    f"Task {task_id} {status}: {output}".strip(),
                )
            if status in TASK_FINISHED_STATUSES:
                last = unwrap_data(client.get_server(server_id))
                got = parse_ipv4_set(last)
                if got - before:
                    return last
                raise SolusVMHTTPError(
                    f"Task {task_id} finished ({status}) but server {server_id} has no extra IPv4"
                )
        time.sleep(interval)
    raise SolusVMHTTPError(
        f"Server {server_id} has no extra IPv4 after {timeout}s (had {sorted(before)})"
    )


def add_ips(client: SolusVMClient, server_id: int, count: int) -> dict[str, Any]:
    """POST /servers/{id}/ips {count, delayed: true}; wait until extra IPv4 appears."""
    before_data = unwrap_data(client.get_server(server_id))
    if count <= 0:
        return before_data
    before = parse_ipv4_set(before_data)
    result = client.add_ips(server_id, {"count": count, "delayed": True})
    after = wait_for_extra_ipv4(client, server_id, before, task_result=result)
    logger.info("Added %s extra IPv4 to server %s", count, server_id)
    return after


def delete_ips(client: SolusVMClient, server_id: int, ids: list[int]) -> None:
    """DELETE /servers/{id}/ips {ids, delayed: true}."""
    if not ids:
        return
    result = client.delete_ips(server_id, {"ids": ids, "delayed": True})
    poll_if_task(client, result)
    logger.info("Removed IP ids %s from server %s", ids, server_id)


def start_server(client: SolusVMClient, server_id: int) -> None:
    data = require_not_processing(client, server_id)
    if str(data.get("status") or "").lower() == "started":
        logger.info("Server %s already started", server_id)
        return
    result = client.start(server_id)
    poll_if_task(client, result)


def stop_server(client: SolusVMClient, server_id: int) -> None:
    data = require_not_processing(client, server_id)
    if str(data.get("status") or "").lower() == "stopped":
        logger.info("Server %s already stopped", server_id)
        return
    result = client.stop(server_id, force=False)
    poll_if_task(client, result)
    after = unwrap_data(client.get_server(server_id))
    if str(after.get("status") or "").lower() == "started":
        logger.info("Server %s still started after stop; sending force stop", server_id)
        forced = client.stop(server_id, force=True)
        poll_if_task(client, forced)


def restart_server(client: SolusVMClient, server_id: int) -> None:
    require_not_processing(client, server_id)
    result = client.restart(server_id)
    poll_if_task(client, result)


def suspend_server(client: SolusVMClient, server_id: int) -> None:
    require_not_processing(client, server_id)
    result = client.suspend(server_id)
    poll_if_task(client, result)
    logger.info("Suspended server %s", server_id)


def resume_server(client: SolusVMClient, server_id: int) -> None:
    require_not_processing(client, server_id)
    result = client.resume(server_id)
    poll_if_task(client, result)
    logger.info("Resumed server %s", server_id)


def reinstall_server(
    client: SolusVMClient,
    server_id: int,
    *,
    os: int,
    ssh_keys: list[int] | None = None,
    user_data: str | None = None,
    password: str | None = None,
) -> None:
    require_not_processing(client, server_id)
    body: dict[str, Any] = {"os": os}
    if ssh_keys:
        body["ssh_keys"] = ssh_keys
    if user_data:
        body["user_data"] = user_data
    if password:
        body["password"] = password
    result = client.reinstall(server_id, body)
    poll_if_task(client, result)
    logger.info("Reinstalled server %s", server_id)


def reset_password(client: SolusVMClient, server_id: int, user_id: int | None = None) -> str:
    """POST /reset_password empty. On 403, one-time user token then never store it.

    Returns the new password string. Never log it.
    """
    try:
        result = client.reset_password(server_id)
    except SolusVMHTTPError as e:
        if e.status_code != 403 or not user_id:
            raise
        one_time = client.create_one_time_user_token(user_id)
        try:
            result = client.reset_password(server_id, bearer=one_time)
        finally:
            del one_time

    poll_if_task(client, result)
    data = unwrap_data(result)
    password = data.get("password") or (result or {}).get("password") or ""
    if not password:
        raise SolusVMHTTPError(f"No password returned for server {server_id}")
    return str(password)


def change_hostname(client: SolusVMClient, server_id: int, hostname: str) -> None:
    result = client.patch_server(server_id, {"name": hostname, "fqdns": [hostname]})
    poll_if_task(client, result)
    logger.info("Changed hostname on server %s", server_id)


def resize_server(
    client: SolusVMClient,
    server_id: int,
    plan_id: int,
    *,
    custom_plan: dict[str, Any] | None = None,
    preserve_disk: bool = True,
) -> None:
    require_not_processing(client, server_id)
    body: dict[str, Any] = {
        "plan_id": plan_id,
        "preserve_disk": preserve_disk,
        "delayed": True,
    }
    if custom_plan:
        body["custom_plan"] = custom_plan
    result = client.resize(server_id, body)
    poll_if_task(client, result)
    logger.info("Resized server %s to plan %s", server_id, plan_id)


def delete_server(client: SolusVMClient, server_id: int) -> None:
    try:
        result = client.delete_server(server_id)
    except SolusVMHTTPError as e:
        if is_not_found(e):
            logger.info("SolusVM server %s already gone", server_id)
            return
        raise
    poll_if_task(client, result)
    deadline = time.time() + TASK_POLL_TIMEOUT
    while time.time() < deadline:
        try:
            client.get_server(server_id)
        except SolusVMHTTPError as e:
            if is_not_found(e):
                logger.info("Deleted server %s", server_id)
                return
            raise
        time.sleep(TASK_POLL_INTERVAL)
    logger.info("Deleted server %s (GET still returning after poll window)", server_id)


def vnc_up(client: SolusVMClient, server_id: int) -> dict[str, Any]:
    """POST /vnc_up. Returns url from top-level or data.url / vnc_proxy_url."""
    result = client.vnc_up(server_id) or {}
    data = unwrap_data(result)
    url = (
        result.get("url")
        or data.get("url")
        or result.get("vnc_proxy_url")
        or data.get("vnc_proxy_url")
        or ""
    )
    if not url:
        raise SolusVMHTTPError(f"vnc_up for server {server_id} returned no url")
    return {"url": str(url)}


def build_ws_url(vnc_url: str, endpoint: str) -> str:
    """wss://{mn host}/vnc?url={url} unless url is already ws(s)://."""
    raw = (vnc_url or "").strip()
    if raw.lower().startswith("ws://") or raw.lower().startswith("wss://"):
        return raw
    host = management_host(endpoint)
    if not host:
        parsed = urlparse(endpoint if "://" in endpoint else f"https://{endpoint}")
        host = parsed.netloc or endpoint
    return f"wss://{host}/vnc?url={raw}"


def custom_plan_from_spec(
    cpu_cores: int | None,
    memory_mb: int | None,
    disk_gb: int | None,
) -> dict[str, Any] | None:
    """RAM in bytes (MB × 1024²). Disk in GB. Omit if nothing to send."""
    params: dict[str, Any] = {}
    if cpu_cores is not None:
        params["vcpu"] = cpu_cores
    if memory_mb is not None:
        params["ram"] = int(memory_mb) * 1024 * 1024
    if disk_gb is not None:
        params["disk"] = disk_gb
    if not params:
        return None
    return {"params": params}
