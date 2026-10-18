"""Application API: allocations, eggs, create, delete, install status."""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from .client import PanelClient, PanelError


def list_allocations(client: PanelClient, node_id: int) -> list[dict[str, Any]]:
    page = 1
    rows: list[dict[str, Any]] = []
    while True:
        _, payload = client.application(
            "GET",
            f"/nodes/{node_id}/allocations",
            params={"per_page": 100, "page": page},
        )
        data = payload.get("data") or []
        for item in data:
            rows.append(item["attributes"])
        meta = (payload.get("meta") or {}).get("pagination") or {}
        total_pages = int(meta.get("total_pages") or 1)
        if page >= total_pages:
            return rows
        page += 1


def newest_free_allocation(client: PanelClient, node_id: int) -> dict[str, Any]:
    free = [row for row in list_allocations(client, node_id) if not row.get("assigned")]
    if not free:
        raise RuntimeError(f"node {node_id} has no free allocation")
    return max(free, key=lambda row: int(row["id"]))


def deployable_ids_from_payload(payload: dict[str, Any] | None) -> set[int]:
    """Panel node ids from a GET /nodes/deployable body."""
    ids: set[int] = set()
    if not isinstance(payload, dict):
        return ids
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        attrs = item.get("attributes")
        if not isinstance(attrs, dict):
            attrs = item
        raw = attrs.get("id")
        if raw is None:
            continue
        try:
            node_id = int(raw)
        except (TypeError, ValueError):
            continue
        if node_id > 0:
            ids.add(node_id)
    return ids


def list_deployable_node_ids(client: PanelClient, memory: int, disk: int) -> set[int]:
    """Node ids the panel would accept for this memory and disk, in MiB.

    Page is omitted so the panel returns every match. A 400 means none qualify.
    """
    try:
        _, payload = client.application(
            "GET",
            "/nodes/deployable",
            params={"memory": int(memory), "disk": int(disk)},
        )
    except PanelError as exc:
        if exc.status == 400:
            return set()
        raise
    return deployable_ids_from_payload(payload)


def create_placement_block(client: PanelClient, node_id: int, memory: int, disk: int) -> str | None:
    """Deployable first, then panel maintenance. None means create may continue."""
    if node_id not in list_deployable_node_ids(client, memory, disk):
        return "node is not deployable"
    _, payload = client.application("GET", f"/nodes/{node_id}")
    attrs = payload.get("attributes") if isinstance(payload, dict) else None
    if isinstance(attrs, dict) and attrs.get("maintenance_mode"):
        return "node is in maintenance"
    return None


class EggMatchError(RuntimeError):
    """No unique egg is close enough to the requested name."""


def _normalize_name(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def _edit_distance(left: str, right: str) -> int:
    if left == right:
        return 0
    if abs(len(left) - len(right)) > 2:
        return 3
    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, 1):
        current = [i]
        for j, right_char in enumerate(right, 1):
            cost = 0 if left_char == right_char else 1
            current.append(
                min(
                    current[-1] + 1,
                    previous[j] + 1,
                    previous[j - 1] + cost,
                )
            )
        previous = current
    return previous[-1]


def _paged_attributes(client: PanelClient, path: str, include: str = "") -> list[dict[str, Any]]:
    page = 1
    rows: list[dict[str, Any]] = []
    while True:
        params: dict[str, Any] = {"per_page": 100, "page": page}
        if include:
            params["include"] = include
        _, payload = client.application("GET", path, params=params)
        for item in payload.get("data") or []:
            rows.append(item["attributes"])
        meta = (payload.get("meta") or {}).get("pagination") or {}
        total_pages = int(meta.get("total_pages") or 1)
        if page >= total_pages:
            return rows
        page += 1


def _relationship_rows(attributes: dict[str, Any], name: str) -> list[dict[str, Any]] | None:
    """Rows already embedded on a resource. None when that include was not returned."""
    relationships = attributes.get("relationships")
    if not isinstance(relationships, dict) or name not in relationships:
        return None
    data = (relationships.get(name) or {}).get("data") or []
    rows: list[dict[str, Any]] = []
    for item in data:
        if isinstance(item, dict) and isinstance(item.get("attributes"), dict):
            rows.append(item["attributes"])
    return rows


def _egg_record(egg: dict[str, Any], nest_id: int, nest_name: str) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": int(egg["id"]),
        "nest_id": int(egg.get("nest") or nest_id),
        "nest_name": nest_name,
        "name": str(egg.get("name") or ""),
    }
    # Keep the egg attributes when the list already included variables.
    # The catalog reads them from this one payload. It does not fetch each egg.
    if _relationship_rows(egg, "variables") is not None:
        record["panel_egg"] = egg
    return record


def list_eggs(client: PanelClient) -> list[dict[str, Any]]:
    """Nests, eggs, and variables from the nest list.

    include=eggs.variables is one paginated list. When that response fails,
    each nest is loaded on its own. One nest whose eggs cannot be serialized
    is skipped, and the other nests are still returned.
    """
    try:
        nests = _paged_attributes(client, "/nests", include="eggs.variables")
    except PanelError as exc:
        logging.getLogger(__name__).warning(
            "egg catalog include failed (%s); loading nests separately", exc
        )
        return _list_eggs_per_nest(client)
    return _eggs_from_nests(client, nests)


def _eggs_from_nests(client: PanelClient, nests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    eggs: list[dict[str, Any]] = []
    for nest in nests:
        try:
            nest_id = int(nest["id"])
            nest_name = str(nest.get("name") or "")
            included = _relationship_rows(nest, "eggs")
            if included is None:
                egg_rows = _eggs_for_nest(client, nest_id, nest_name)
            else:
                egg_rows = included
                # Eggs without variables get one list for the nest, not one request per egg.
                if egg_rows and all(
                    _relationship_rows(egg, "variables") is None for egg in egg_rows
                ):
                    listed = _eggs_for_nest(client, nest_id, nest_name)
                    if listed:
                        egg_rows = listed
            eggs.extend(_egg_records(egg_rows, nest_id, nest_name))
        except (KeyError, TypeError, ValueError) as exc:
            logging.getLogger(__name__).warning("skipping nest %s: %s", nest.get("id"), exc)
    return eggs


def _list_eggs_per_nest(client: PanelClient) -> list[dict[str, Any]]:
    eggs: list[dict[str, Any]] = []
    for nest in _paged_attributes(client, "/nests"):
        try:
            nest_id = int(nest["id"])
            nest_name = str(nest.get("name") or "")
        except (KeyError, TypeError, ValueError) as exc:
            logging.getLogger(__name__).warning("skipping nest %s: %s", nest.get("id"), exc)
            continue
        egg_rows = _eggs_for_nest(client, nest_id, nest_name)
        eggs.extend(_egg_records(egg_rows, nest_id, nest_name))
    return eggs


def _eggs_for_nest(client: PanelClient, nest_id: int, nest_name: str) -> list[dict[str, Any]]:
    """Eggs for one nest. An empty list means this nest was skipped."""
    try:
        return _paged_attributes(client, f"/nests/{nest_id}/eggs", include="variables")
    except PanelError as exc:
        logging.getLogger(__name__).warning("skipping nest %s (%s): %s", nest_id, nest_name, exc)
        return []


def _egg_records(
    egg_rows: list[dict[str, Any]], nest_id: int, nest_name: str
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for egg in egg_rows:
        try:
            records.append(_egg_record(egg, nest_id, nest_name))
        except (KeyError, TypeError, ValueError) as exc:
            logging.getLogger(__name__).warning(
                "skipping egg in nest %s (%s): %s", nest_id, nest_name, exc
            )
    return records


def choose_egg(eggs: list[dict[str, Any]], wanted: str) -> tuple[int, int]:
    """Pick an exact name, or the single egg within two character edits."""
    target = _normalize_name(wanted)
    if not target:
        raise EggMatchError("egg name is empty")
    scored: list[tuple[int, dict[str, Any]]] = []
    for egg in eggs:
        scored.append((_edit_distance(target, _normalize_name(str(egg.get("name") or ""))), egg))
    exact = [egg for distance, egg in scored if distance == 0]
    if len(exact) == 1:
        return int(exact[0]["nest_id"]), int(exact[0]["id"])
    if len(exact) > 1:
        raise EggMatchError(f"more than one egg is named {wanted.strip()}")
    close = [(distance, egg) for distance, egg in scored if 0 < distance <= 2]
    if not close:
        raise EggMatchError(f"no egg named {wanted.strip()}")
    best = min(distance for distance, _egg in close)
    winners = [egg for distance, egg in close if distance == best]
    if len(winners) != 1:
        names = ", ".join(str(egg.get("name") or "") for egg in winners)
        raise EggMatchError(f"several eggs are close to {wanted.strip()}: {names}")
    return int(winners[0]["nest_id"]), int(winners[0]["id"])


def get_egg(client: PanelClient, nest_id: int, egg_id: int) -> dict[str, Any]:
    _, payload = client.application(
        "GET",
        f"/nests/{nest_id}/eggs/{egg_id}",
        params={"include": "variables"},
    )
    return payload["attributes"]


def environment_from_egg(egg: dict[str, Any]) -> dict[str, str]:
    variables = ((egg.get("relationships") or {}).get("variables") or {}).get("data") or []
    env: dict[str, str] = {}
    for item in variables:
        attrs = item["attributes"]
        env[attrs["env_variable"]] = (
            "" if attrs.get("default_value") is None else str(attrs["default_value"])
        )
    return env


def merged_environment(egg: dict[str, Any], overrides: dict[str, str] | None) -> dict[str, str]:
    """Egg defaults fill required panel variables. Caller values override them.

    An empty override does not wipe a non-empty egg default. The panel rejects
    a create when a required variable such as Maximum Users is missing.
    """
    merged = environment_from_egg(egg)
    for key, value in (overrides or {}).items():
        if value is None:
            continue
        text = str(value)
        if text.strip() == "" and str(merged.get(key, "")).strip():
            continue
        merged[str(key)] = text
    return merged


def docker_image(egg: dict[str, Any]) -> str:
    image = egg.get("docker_image")
    if isinstance(image, str) and image:
        return image
    images = egg.get("docker_images") or {}
    if isinstance(images, dict) and images:
        return next(iter(images.values()))
    raise RuntimeError(f"egg {egg.get('id')} has no docker image")


def parse_thread_set(value: Any) -> set[int]:
    """Pterodactyl stores pins as '0,1', '0-3', or null."""
    if value is None:
        return set()
    text = str(value).strip()
    if not text or text.lower() == "null":
        return set()
    used: set[int] = set()
    for part in text.split(","):
        piece = part.strip()
        if not piece:
            continue
        if "-" in piece:
            start_text, end_text = piece.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if end < start:
                raise ValueError(f"thread range {piece} is reversed")
            used.update(range(start, end + 1))
            continue
        used.add(int(piece))
    return used


def choose_pinned_threads(cpu_count: int, used: set[int], cores: int) -> str:
    if cores <= 0:
        raise RuntimeError("dedicated plan needs at least one core")
    if cpu_count <= 0:
        raise RuntimeError("node reported no cpu cores")
    free = [index for index in range(cpu_count) if index not in used]
    if len(free) < cores:
        raise RuntimeError(f"node has {len(free)} free cpu cores, need {cores}")
    return ",".join(str(index) for index in free[:cores])


def cores_from_cpu_percent(cpu_percent: int) -> int:
    if cpu_percent <= 0:
        raise RuntimeError("cpu limit must be positive")
    if cpu_percent % 100 != 0:
        raise RuntimeError(f"cpu limit {cpu_percent} is not a whole number of cores")
    return cpu_percent // 100


def list_node_servers(client: PanelClient, node_id: int) -> list[dict[str, Any]]:
    page = 1
    rows: list[dict[str, Any]] = []
    while True:
        _, payload = client.application(
            "GET",
            "/servers",
            params={"per_page": 100, "page": page, "filter[node_id]": node_id},
        )
        for item in payload.get("data") or []:
            attrs = item["attributes"]
            if int(attrs.get("node") or 0) != node_id:
                continue
            rows.append(attrs)
        meta = (payload.get("meta") or {}).get("pagination") or {}
        total_pages = int(meta.get("total_pages") or 1)
        if page >= total_pages:
            return rows
        page += 1


def wings_cpu_count(client: PanelClient, node_id: int) -> int:
    _, node_payload = client.application("GET", f"/nodes/{node_id}")
    attrs = node_payload["attributes"]
    _, cfg = client.application("GET", f"/nodes/{node_id}/configuration")
    token_id = str(cfg.get("token_id") or "")
    token = str(cfg.get("token") or "")
    if not token_id or not token:
        raise RuntimeError(f"node {node_id} has no wings token")
    fqdn = str(attrs.get("fqdn") or "")
    port = int(attrs.get("daemon_listen") or 0)
    if not fqdn or port <= 0:
        raise RuntimeError(f"node {node_id} has no wings address")
    scheme = str(attrs.get("scheme") or "https")
    # Current Wings accepts the node token alone. Older Wings wants token_id.token.
    response = None
    for authorization in (f"Bearer {token}", f"Bearer {token_id}.{token}"):
        response = requests.get(
            f"{scheme}://{fqdn}:{port}/api/system",
            headers={"Authorization": authorization, "Accept": "application/json"},
            timeout=15,
            verify=False,
        )
        if response.status_code in {401, 403}:
            continue
        break
    if response is None or response.status_code >= 400:
        code = response.status_code if response is not None else 0
        raise RuntimeError(f"wings on node {node_id} returned {code}")
    count = int((response.json() or {}).get("cpu_count") or 0)
    if count <= 0:
        raise RuntimeError(f"node {node_id} reported no cpu cores")
    return count


def pinned_threads_for_node(client: PanelClient, node_id: int, cpu_percent: int) -> str:
    cores = cores_from_cpu_percent(cpu_percent)
    used: set[int] = set()
    for server in list_node_servers(client, node_id):
        used |= parse_thread_set((server.get("limits") or {}).get("threads"))
    return choose_pinned_threads(wings_cpu_count(client, node_id), used, cores)


def noderings_external_id(external_id: str) -> str:
    """NR id stored on the panel external_id. The panel name is the customer hostname."""
    external_id = (external_id or "").strip()
    if not external_id:
        raise RuntimeError("external id is required")
    if external_id.upper().startswith("NR-"):
        external_id = external_id[3:]
    return "NR-" + external_id


def panel_server_name(external_id: str) -> str:
    return noderings_external_id(external_id)


def create_server(
    client: PanelClient,
    *,
    name: str,
    user_id: int,
    nest_id: int,
    egg_id: int,
    allocation_id: int,
    external_id: str,
    memory: int = 512,
    disk: int = 1024,
    cpu: int = 100,
    threads: str | None = None,
    databases: int = 1,
    backups: int = 1,
    extra_allocations: int = 0,
    start_on_completion: bool = True,
    skip_scripts: bool = False,
    environment: dict[str, str] | None = None,
    startup: str | None = None,
    image: str | None = None,
) -> dict[str, Any]:
    egg = get_egg(client, nest_id, egg_id)
    # The panel shows the customer hostname. The NR id is the external id.
    name = (name or "").strip()
    if not name:
        raise RuntimeError("hostname is required")
    external_id = noderings_external_id(external_id)
    body: dict[str, Any] = {
        "name": name,
        "user": user_id,
        "egg": egg_id,
        "docker_image": image or docker_image(egg),
        "startup": startup or egg["startup"],
        "environment": merged_environment(egg, environment),
        "limits": {
            "memory": memory,
            "swap": 0,
            "disk": disk,
            "io": 500,
            "cpu": cpu,
        },
        "oom_disabled": True,
        "feature_limits": {
            "databases": databases,
            "allocations": extra_allocations + 1,
            "backups": backups,
        },
        "allocation": {"default": allocation_id},
        "external_id": external_id,
        "start_on_completion": start_on_completion,
        "skip_scripts": skip_scripts,
    }
    if threads:
        body["limits"]["threads"] = threads
    _, payload = client.application("POST", "/servers", json_body=body)
    return payload["attributes"]


def get_server(client: PanelClient, server_id: int) -> dict[str, Any]:
    _, payload = client.application("GET", f"/servers/{server_id}")
    return payload["attributes"]


def get_server_by_external_id(client: PanelClient, external_id: str) -> dict[str, Any] | None:
    """Return the panel server for this order, or None when the panel has never seen it."""
    external_id = external_id.strip()
    if not external_id:
        return None
    try:
        _, payload = client.application(
            "GET",
            f"/servers/external/{external_id}",
            params={"include": "allocations"},
        )
    except PanelError as exc:
        if exc.status == 404:
            return None
        raise
    return payload["attributes"]


def allocation_from_server(server: dict[str, Any]) -> dict[str, Any]:
    """Primary allocation on a panel server that included allocations."""
    relationships = server.get("relationships") or {}
    rows = ((relationships.get("allocations") or {}).get("data")) or []
    primary = server.get("allocation")
    chosen: dict[str, Any] | None = None
    for item in rows:
        attrs = item.get("attributes") or item
        if not isinstance(attrs, dict):
            continue
        if primary is None or int(attrs.get("id") or 0) == int(primary):
            chosen = attrs
            break
        if chosen is None:
            chosen = attrs
    if chosen is None and server.get("allocation"):
        return {"id": server.get("allocation"), "ip": "", "port": 0, "alias": ""}
    if chosen is None:
        return {}
    return {
        "id": chosen.get("id"),
        "ip": chosen.get("ip") or "",
        "port": chosen.get("port") or 0,
        "alias": chosen.get("alias") or "",
    }


def delete_server(client: PanelClient, server_id: int) -> None:
    client.application("DELETE", f"/servers/{server_id}")


def update_server_startup(
    client: PanelClient,
    server_id: int,
    egg_id: int,
    startup: str,
    image: str,
    environment: dict[str, str],
) -> None:
    """Point an existing server at another egg and replace startup, image, and environment."""
    client.application(
        "PATCH",
        f"/servers/{int(server_id)}/startup",
        json_body={
            "startup": startup,
            "environment": environment,
            "egg": int(egg_id),
            "image": image,
            "skip_scripts": False,
        },
    )


# Wings sets one of these and stops. Waiting again will not clear them.
INSTALL_FAILED_STATUSES = frozenset({"install_failed", "reinstall_failed"})


class InstallFailed(RuntimeError):
    """The panel has given up on this install. Retrying the wait does not help."""

    def __init__(self, server_id: int, panel_status: str) -> None:
        self.panel_status = panel_status
        super().__init__(f"server {server_id} {panel_status}")


def raise_if_install_failed(server_id: int, row_status: Any) -> None:
    if row_status in INSTALL_FAILED_STATUSES:
        raise InstallFailed(server_id, str(row_status))


def wait_until_installed(
    client: PanelClient, server_id: int, timeout: float = 600.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        attrs = get_server(client, server_id)
        # Panel 1.x stores install state on the server row as status null once installed,
        # and "installing" / "install_failed" while the installer runs. WitherPanel
        # uses "reinstall_failed" for a reinstall that Wings already marked failed.
        row_status = attrs.get("status")
        last = str(row_status)
        if row_status is None:
            return attrs
        raise_if_install_failed(server_id, row_status)
        time.sleep(3)
    raise TimeoutError(f"server {server_id} still {last!r} after {timeout}s")


def wait_until_reinstall_finished(
    client: PanelClient,
    server_id: int,
    timeout: float = 600.0,
    poll: float = 2.0,
    start_grace: float = 20.0,
) -> dict[str, Any]:
    """Wait for a reinstall that was just requested.

    A freshly installed server already has status null. Returning on the first
    null read would start the new game before the installer runs, and the
    installer would delete eula.txt. Wait until the panel reports installing,
    then until that state clears. If the install finishes inside the grace
    window, accept null after the grace.
    """
    deadline = time.monotonic() + timeout
    grace_deadline = time.monotonic() + start_grace
    saw_installing = False
    last = ""
    while time.monotonic() < deadline:
        attrs = get_server(client, server_id)
        row_status = attrs.get("status")
        last = str(row_status)
        raise_if_install_failed(server_id, row_status)
        if row_status == "installing":
            saw_installing = True
        elif row_status is None and (saw_installing or time.monotonic() >= grace_deadline):
            return attrs
        time.sleep(poll)
    raise TimeoutError(f"server {server_id} still {last!r} after {timeout}s")
