"""VirtFusion server lifecycle operations."""

import logging
import time
from datetime import datetime
from typing import Any

from .client import VirtFusionClient, as_mapping
from .constants import QUEUE_POLL_INTERVAL, QUEUE_POLL_TIMEOUT
from .errors import VirtFusionHTTPError
from .guest_userdata import prepare_virtfusion_user_data
from .queue import poll_queue
from .user_api import VirtFusionUserClient, poll_user_task

logger = logging.getLogger(__name__)


def _extract_queue_id(result: dict[str, Any] | None) -> int | None:
    """Extract queueId from a VF API response.

    ipv4Qty returns ``data`` as a list of addresses (no queueId). Treat that
    as no queue rather than crashing on ``list.get``.
    """
    if not result:
        return None
    for blob in (result, result.get("data")):
        if not isinstance(blob, dict):
            continue
        qid = blob.get("queueId")
        if qid:
            return int(qid)
    return None


def ipv4_from_qty_result(result: dict[str, Any] | None) -> list[str]:
    """Parse addresses from POST /ipv4Qty (data is a list of IPs or objects)."""
    if not result:
        return []
    data = result.get("data", result)
    items: list[Any]
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        raw = data.get("ip") or data.get("ipv4") or data.get("addresses")
        items = raw if isinstance(raw, list) else []
    else:
        return []
    out: list[str] = []
    for item in items:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        elif isinstance(item, dict):
            addr = item.get("address") or item.get("ip")
            if addr:
                out.append(str(addr).strip())
    return out


def create_server(
    client: VirtFusionClient,
    *,
    package_id: int,
    user_id: int,
    hypervisor_id: int,
    cpu_cores: int | None = None,
    memory_mb: int | None = None,
    disk_gb: int | None = None,
    network_speed_in: int | None = None,
    network_speed_out: int | None = None,
) -> dict[str, Any]:
    """POST /servers and return server data (id, uuid, queueId)."""
    body: dict[str, Any] = {
        "packageId": package_id,
        "userId": user_id,
        "hypervisorId": hypervisor_id,
        "ipv4": 1,
    }
    if cpu_cores is not None:
        body["cpuCores"] = cpu_cores
    if memory_mb is not None:
        body["memory"] = memory_mb
    if disk_gb is not None:
        body["storage"] = disk_gb
    if network_speed_in is not None:
        body["networkSpeedInbound"] = network_speed_in
    if network_speed_out is not None:
        body["networkSpeedOutbound"] = network_speed_out

    result = client.create_server(body)
    if not result:
        raise VirtFusionHTTPError("Empty response from POST /servers")
    qid = _extract_queue_id(result)
    if qid:
        poll_queue(client, qid)
    data = result.get("data", result)
    if not isinstance(data, dict) or not data.get("id"):
        raise VirtFusionHTTPError("POST /servers did not return data.id")
    logger.info("Created server id=%s uuid=%s", data.get("id"), data.get("uuid"))
    return data


def add_ipv4(client: VirtFusionClient, server_id: int, addresses: list[str]) -> None:
    """POST /servers/{id}/ipv4. Poll hypervisor queue when VF returns queueId."""
    if not addresses:
        return
    result = client.add_ipv4(server_id, addresses)
    qid = _extract_queue_id(result)
    if qid:
        poll_queue(client, qid)
    logger.info("Added IPv4 %s to server %s", addresses, server_id)


def add_ipv4_from_pool(client: VirtFusionClient, server_id: int, quantity: int = 1) -> list[str]:
    """Ask VirtFusion to assign ``quantity`` IPv4s from the hypervisor IP block.

    Lab (and production) blocks only contain the advertised pool. Exact
    ``POST /ipv4`` with a NodeRings-picked LAN address returns 422. ``ipv4Qty``
    is how VF reserves the 2nd/3rd address and updates NIC anti-spoof
    (ipFilter) for those IPs.
    """
    if quantity < 1:
        return []
    before = set(ipv4_addresses_from_server(client.get_server(server_id)))
    result = client.add_ipv4_qty(server_id, quantity)
    qid = _extract_queue_id(result)
    if qid:
        poll_queue(client, qid)
    assigned = ipv4_from_qty_result(result)
    if len(assigned) < quantity:
        after = ipv4_addresses_from_server(client.get_server(server_id))
        assigned = [ip for ip in after if ip not in before]
    if not assigned:
        raise VirtFusionHTTPError(f"VirtFusion ipv4Qty assigned no addresses on server {server_id}")
    logger.info("Added %s IPv4 from VF pool to server %s: %s", quantity, server_id, assigned)
    return assigned


def remove_ipv4(client: VirtFusionClient, server_id: int, addresses: list[str]) -> None:
    """DELETE /servers/{id}/ipv4. Poll hypervisor queue when VF returns queueId."""
    if not addresses:
        return
    result = client.remove_ipv4(server_id, addresses)
    qid = _extract_queue_id(result)
    if qid:
        poll_queue(client, qid)
    logger.info("Removed IPv4 %s from server %s", addresses, server_id)


def _server_data(server: dict[str, Any] | None) -> dict[str, Any]:
    if not server:
        return {}
    data = server.get("data", server)
    return data if isinstance(data, dict) else {}


def built_stamp(server: dict[str, Any] | None) -> str:
    """VirtFusion `built` timestamp. Empty when the guest has never been installed."""
    return str(_server_data(server).get("built") or "")


def _parse_vf_time(raw: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _rebuild_pending(server: dict[str, Any] | None) -> bool:
    val = _server_data(server).get("rebuild")
    if val is True or val == 1:
        return True
    return isinstance(val, str) and val.strip().lower() in {"true", "yes", "1"}


def _qemu_os(server: dict[str, Any] | None) -> dict[str, Any]:
    qa = _server_data(server).get("qemuAgent")
    if not isinstance(qa, dict):
        return {}
    osinfo = qa.get("os")
    return osinfo if isinstance(osinfo, dict) else {}


def _qemu_os_seen_after_build(server: dict[str, Any] | None) -> bool:
    """True when qemu-guest-agent OS info is from this install, not the previous OS.

    VF sets `built` when a rebuild *starts*. A leftover Ubuntu qemuAgent.os after
    a Rocky rebuild would look "ready" if we only required a non-empty name.
    Panels that omit qemuAgent entirely (unit fake) do not block.
    """
    data = _server_data(server)
    if "qemuAgent" not in data:
        return True
    osinfo = _qemu_os(server)
    if not str(osinfo.get("name") or osinfo.get("dist") or "").strip():
        return False
    built = _parse_vf_time(built_stamp(server))
    updated = _parse_vf_time(str(osinfo.get("updated") or ""))
    if built is None or updated is None:
        return True
    return updated >= built


def _guest_install_ready(server: dict[str, Any] | None, previous_built: str | None) -> bool:
    if previous_built is not None:
        stamp = built_stamp(server)
        if not stamp or stamp == previous_built:
            return False
    elif not server_is_built(server):
        return False
    if _rebuild_pending(server):
        return False
    return _qemu_os_seen_after_build(server)


def wait_until_server_built(
    client: VirtFusionClient,
    server_id: int,
    *,
    timeout: int = QUEUE_POLL_TIMEOUT,
    interval: int = QUEUE_POLL_INTERVAL,
    previous_built: str | None = None,
) -> None:
    """Poll GET /servers/{id} until the guest OS is actually up.

    The User API task and VirtFusion `built` stamp both complete when a rebuild
    *starts* (lab: ~16s). qemu-guest-agent OS `updated` at-or-after that stamp
    means the new guest has booted. Password cloud-init may still be in
    `dnf upgrade`; bootcmd keeps applying the hash during that window.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = client.get_server(server_id)
        if _guest_install_ready(current, previous_built):
            return
        time.sleep(interval)
    raise VirtFusionHTTPError(
        f"Server {server_id} was not built within {timeout}s after User API build"
    )


def server_is_built(server: dict[str, Any] | None) -> bool:
    """True when GET /servers/{id} shows the guest has already been installed."""
    data = _server_data(server)
    if not data:
        return False
    if data.get("built"):
        return True
    state = str(data.get("state") or "").lower()
    return state in {"complete", "complete_pending", "running"}


def wait_for_assigned_ipv4(
    client: VirtFusionClient,
    server_id: int,
    timeout: int = 60,
    interval: float = 2.0,
) -> dict[str, Any] | None:
    """GET /servers/{id} until VF has published at least one IPv4, or timeout."""
    deadline = time.time() + timeout
    last: dict[str, Any] | None = None
    while time.time() < deadline:
        last = client.get_server(server_id)
        if ipv4_addresses_from_server(last):
            return last
        time.sleep(interval)
    return last


def ipv4_addresses_from_server(server: dict[str, Any] | None) -> list[str]:
    """Collect IPv4 strings from GET /servers/{id} (no remoteState)."""
    if not server:
        return []
    data = server.get("data", server)
    if not isinstance(data, dict):
        return []
    found: list[str] = []

    def add(addr: Any) -> None:
        if isinstance(addr, str) and addr.strip():
            found.append(addr.strip())
        elif isinstance(addr, dict):
            a = addr.get("address") or addr.get("ip")
            if a:
                found.append(str(a).strip())

    ipv4 = data.get("ipv4")
    if isinstance(ipv4, list):
        for item in ipv4:
            add(item)
    network = as_mapping(data.get("network"))
    interfaces = network.get("interfaces") or data.get("networkInterfaces") or []
    if isinstance(interfaces, list):
        for iface in interfaces:
            if not isinstance(iface, dict):
                continue
            for item in iface.get("ipv4") or []:
                add(item)
    seen: set[str] = set()
    out: list[str] = []
    for ip in found:
        if ip not in seen:
            seen.add(ip)
            out.append(ip)
    return out


def _iter_ip_objects(server: dict[str, Any] | None, key: str) -> list[dict[str, Any] | str]:
    data = _server_data(server)
    found: list[dict[str, Any] | str] = []
    raw = data.get(key)
    if isinstance(raw, list):
        found.extend(item for item in raw if isinstance(item, (dict, str)))
    elif isinstance(raw, (dict, str)):
        found.append(raw)
    network = as_mapping(data.get("network"))
    interfaces = network.get("interfaces") or data.get("networkInterfaces") or []
    if isinstance(interfaces, list):
        for iface in interfaces:
            if not isinstance(iface, dict):
                continue
            nested = iface.get(key) or []
            if isinstance(nested, list):
                found.extend(item for item in nested if isinstance(item, (dict, str)))
            elif isinstance(nested, (dict, str)):
                found.append(nested)
    return found


def _ipv6_token(val: Any) -> str:
    """First usable IPv6 address from a VF string, list, or address object."""
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return ""
        if "/" in s:
            s = s.split("/", 1)[0].strip()
        return s
    if isinstance(val, dict):
        return _ipv6_token(val.get("address") or val.get("ip"))
    if isinstance(val, list):
        for item in val:
            got = _ipv6_token(item)
            if got:
                return got
    return ""


def ipv6_address_from_server(server: dict[str, Any] | None) -> str:
    """Primary IPv6 string from GET /servers/{id}.

    VirtFusion nested objects carry a delegated ``subnet`` plus generated
    host ``addresses``. Prefer the host so NodeRings records what the guest
    actually uses.
    """
    for item in _iter_ip_objects(server, "ipv6"):
        if isinstance(item, str):
            got = _ipv6_token(item)
            if got:
                return got
        if isinstance(item, dict):
            for key in ("address", "ip", "addresses", "addressesDetailed"):
                got = _ipv6_token(item.get(key))
                if got:
                    return got
            got = _ipv6_token(item.get("subnet"))
            if got:
                return got
    data = _server_data(server)
    for key in ("ipv6Address", "primaryIpv6"):
        got = _ipv6_token(data.get(key))
        if got:
            return got
    return ""


def _gateway_from_items(items: list[dict[str, Any] | str], extra_keys: tuple[str, ...]) -> str:
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in extra_keys:
            val = item.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def _prefix_from_ip_item(item: dict[str, Any] | str, *, version: int) -> str:
    from noderings_hooks.net import normalize_prefix

    if isinstance(item, str):
        return normalize_prefix(item, version=version)
    if not isinstance(item, dict):
        return ""
    for key in ("cidr", "prefix", "prefixlen", "prefix_len"):
        if item.get(key) is not None:
            got = normalize_prefix(item.get(key), version=version)
            if got:
                return got
    for key in ("netmask", "mask"):
        if item.get(key) is not None:
            got = normalize_prefix(item.get(key), version=version)
            if got:
                return got
    for key in ("address", "ip", "subnet"):
        got = normalize_prefix(item.get(key), version=version)
        if got:
            return got
    return ""


def ipv4_prefix_from_server(server: dict[str, Any] | None) -> str:
    for item in _iter_ip_objects(server, "ipv4"):
        got = _prefix_from_ip_item(item, version=4)
        if got:
            return got
    return ""


def ipv6_prefix_from_server(server: dict[str, Any] | None) -> str:
    for item in _iter_ip_objects(server, "ipv6"):
        got = _prefix_from_ip_item(item, version=6)
        if got:
            return got
    return ""


def ipv4_gateway_from_server(server: dict[str, Any] | None) -> str:
    gw = _gateway_from_items(_iter_ip_objects(server, "ipv4"), ("gateway", "gw"))
    if gw:
        return gw
    data = _server_data(server)
    network = as_mapping(data.get("network"))
    for src in (data, network):
        val = src.get("gateway") or src.get("ipv4Gateway")
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def ipv6_gateway_from_server(server: dict[str, Any] | None) -> str:
    gw = _gateway_from_items(_iter_ip_objects(server, "ipv6"), ("gateway", "gw"))
    if gw:
        return gw
    data = _server_data(server)
    network = as_mapping(data.get("network"))
    val = network.get("ipv6Gateway") or data.get("ipv6Gateway")
    if isinstance(val, str) and val.strip():
        return val.strip()
    return ""


def dns_servers_from_server(server: dict[str, Any] | None) -> list[str]:
    found: list[str] = []

    def add(val: Any) -> None:
        if isinstance(val, str) and val.strip() and val.strip() not in found:
            found.append(val.strip())
        elif isinstance(val, list):
            for item in val:
                add(item)

    data = _server_data(server)
    network = as_mapping(data.get("network"))
    for src in (*_iter_ip_objects(server, "ipv4"), *_iter_ip_objects(server, "ipv6")):
        if isinstance(src, dict):
            add(src.get("dns1"))
            add(src.get("dns2"))
            add(src.get("dns"))
            add(src.get("nameservers"))
    add(data.get("dns"))
    add(data.get("dnsServers"))
    add(data.get("nameservers"))
    add(network.get("dns"))
    add(network.get("dnsServers"))
    add(network.get("nameservers"))
    return found


def network_status_from_server(server: dict[str, Any] | None) -> dict[str, Any]:
    """CR status fields for hypervisor-assigned addressing."""
    ipv4 = ipv4_addresses_from_server(server)
    ipv6 = ipv6_address_from_server(server)
    out: dict[str, Any] = {"ipv4": ipv4}
    if ipv6:
        out["ipv6"] = ipv6
    gw4 = ipv4_gateway_from_server(server)
    if gw4:
        out["ipv4_gateway"] = gw4
    gw6 = ipv6_gateway_from_server(server)
    if gw6:
        out["ipv6_gateway"] = gw6
    p4 = ipv4_prefix_from_server(server)
    if p4:
        out["ipv4_prefix"] = p4
    p6 = ipv6_prefix_from_server(server)
    if p6:
        out["ipv6_prefix"] = p6
    dns = dns_servers_from_server(server)
    if dns:
        out["dns_servers"] = dns
    return out


def sync_extra_ipv4(client: VirtFusionClient, server_id: int, desired: list[str] | None) -> None:
    """POST IPv4 addresses in desired that are not already on the server."""
    want = [ip for ip in (desired or []) if ip]
    current = set(ipv4_addresses_from_server(client.get_server(server_id)))
    to_add = [ip for ip in want if ip not in current]
    add_ipv4(client, server_id, to_add)


def sync_remove_ipv4(client: VirtFusionClient, server_id: int, desired: list[str] | None) -> None:
    """DELETE IPv4 addresses on the server that are not in desired (NodeRings IPAM)."""
    want = set(ip for ip in (desired or []) if ip)
    current = ipv4_addresses_from_server(client.get_server(server_id))
    to_remove = [ip for ip in current if ip not in want]
    remove_ipv4(client, server_id, to_remove)


def build_server(
    client: VirtFusionClient,
    server_id: int,
    *,
    os_id: int,
    hostname: str | None = None,
    name: str | None = None,
    ssh_key_ids: list[int] | None = None,
    reinstall: bool = False,
    ipv6: bool = False,
) -> int | None:
    """POST /servers/{id}/build and return the queueId."""
    body: dict[str, Any] = {
        "operatingSystemId": os_id,
        "vnc": True,
        "email": False,
    }
    if hostname:
        body["hostname"] = hostname
    if name:
        body["name"] = name
    if ssh_key_ids:
        body["sshKeys"] = ssh_key_ids
    if reinstall:
        body["reinstall"] = True
    if ipv6:
        body["ipv6"] = True

    result = client.build(server_id, body)
    queue_id = _extract_queue_id(result)
    logger.info("Build server %s queued (queueId=%s)", server_id, queue_id)
    return queue_id


def _extract_user_task_id(result: dict[str, Any] | None) -> int | None:
    if not result:
        return None
    data = result.get("data", result)
    if not isinstance(data, dict):
        return None
    task = data.get("task") if isinstance(data.get("task"), dict) else data
    tid = task.get("id") if isinstance(task, dict) else None
    return int(tid) if tid else None


def build_server_with_userdata(
    admin: VirtFusionClient,
    user_client: VirtFusionUserClient,
    server_id: int,
    server_uuid: str,
    *,
    os_id: int,
    password: str = "",
    guest_user: str = "root",
    hostname: str | None = None,
    name: str | None = None,
    ssh_key_ids: list[int] | None = None,
    ipv6: bool = False,
    extra_user_data: str | None = None,
    reinstall: bool = False,
) -> None:
    """User API POST /api/server/{uuid}/build with cloud-init userData.

    Paths:

    - Instant App + SSH: compiled Instant App YAML is userData (hostname lifted).
    - Instant App + form password: merge password cloud-config into the app YAML.
    - OS + form password: password cloud-config only.
    - OS + SSH only: caller uses Global /build instead of this function.

    Reinstall of an already-built guest must set reinstall=True or VF no-ops.
    """
    previous_built = built_stamp(admin.get_server(server_id)) if reinstall else None
    user_data, api_hostname = prepare_virtfusion_user_data(
        password=password or "",
        guest_user=guest_user,
        extra=extra_user_data,
        hostname_fallback=hostname,
    )
    body: dict[str, Any] = {
        "method": "template",
        "templateId": os_id,
        "userData": user_data,
    }
    if api_hostname:
        body["hostname"] = api_hostname
    if name:
        body["name"] = name
    if ssh_key_ids:
        body["sshKeys"] = ssh_key_ids
    if ipv6:
        body["ipv6"] = True
    if reinstall:
        body["reinstall"] = True

    result = user_client.build(server_uuid, body)
    task_id = _extract_user_task_id(result)
    logger.info(
        "User API build server %s queued (taskId=%s reinstall=%s)",
        server_id,
        task_id,
        reinstall,
    )
    if task_id:
        poll_user_task(
            user_client,
            server_uuid,
            task_id,
            timeout=QUEUE_POLL_TIMEOUT,
            interval=QUEUE_POLL_INTERVAL,
        )
    wait_until_server_built(admin, server_id, previous_built=previous_built)


def power_action(client: VirtFusionClient, server_id: int, action: str) -> int | None:
    """POST /servers/{id}/power/{action} and return queueId if present."""
    result = client.power(server_id, action)
    return _extract_queue_id(result)


def suspend_server(client: VirtFusionClient, server_id: int) -> None:
    client.suspend(server_id)
    logger.info("Suspended server %s", server_id)


def unsuspend_server(client: VirtFusionClient, server_id: int) -> None:
    client.unsuspend(server_id)
    logger.info("Unsuspended server %s", server_id)


def is_not_found(err: VirtFusionHTTPError) -> bool:
    if err.status_code == 404:
        return True
    msg = (err.message or str(err)).lower()
    return "not found" in msg or "server not found" in msg


def delete_server(client: VirtFusionClient, server_id: int) -> None:
    try:
        client.delete_server(server_id, delay=0)
    except VirtFusionHTTPError as e:
        if is_not_found(e):
            logger.info("VF server %s already gone", server_id)
            return
        raise
    logger.info("Deleted server %s", server_id)


def reset_password(client: VirtFusionClient, server_id: int, user: str = "root") -> str:
    """POST /resetPassword and return expectedPassword. Never log the password."""
    result = client.reset_password(server_id, user)
    if not result:
        raise VirtFusionHTTPError(f"Empty response from resetPassword for server {server_id}")
    data = result.get("data", result)
    queue_id = data.get("queueId")
    if queue_id:
        poll_queue(client, int(queue_id))
    password = data.get("expectedPassword", "")
    if not password:
        raise VirtFusionHTTPError(f"No expectedPassword returned for server {server_id}")
    return password


def get_vnc_details(client: VirtFusionClient, server_id: int) -> dict[str, Any]:
    """POST vnc enable then GET vnc details. Returns ip, port, password."""
    client.enable_vnc(server_id)
    result = client.get_vnc(server_id)
    if not result:
        raise VirtFusionHTTPError(f"Empty VNC response for server {server_id}")
    data = result.get("data", result)
    vnc = data.get("vnc", data)
    ip = vnc.get("ip")
    port = vnc.get("port")
    if not ip or port is None:
        raise VirtFusionHTTPError(f"Missing VNC ip/port for server {server_id}")
    return {
        "ip": ip,
        "port": int(port),
        "password": vnc.get("password", ""),
    }


def change_package(client: VirtFusionClient, server_id: int, package_id: int) -> None:
    client.change_package(server_id, package_id)
    logger.info("Changed server %s to package %s", server_id, package_id)


def modify_resources(
    client: VirtFusionClient,
    server_id: int,
    *,
    cpu_cores: int | None = None,
    memory_mb: int | None = None,
    cpu_throttle_percent: int | None = None,
) -> None:
    if cpu_cores is not None:
        client.modify_cpu(server_id, cpu_cores)
        logger.info("Modified server %s CPU to %s cores", server_id, cpu_cores)
    if memory_mb is not None:
        client.modify_memory(server_id, memory_mb)
        logger.info("Modified server %s memory to %s MB", server_id, memory_mb)
    if cpu_throttle_percent is not None:
        client.modify_cpu_throttle(server_id, cpu_throttle_percent)
        logger.info("Modified server %s CPU throttle to %s%%", server_id, cpu_throttle_percent)
