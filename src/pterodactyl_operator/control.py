"""Client API control: power, resources, databases, schedules, startup."""

from __future__ import annotations

from typing import Any

from .client import PanelClient


def _server(identifier: str) -> str:
    return f"/servers/{identifier}"


def power(client: PanelClient, identifier: str, signal: str) -> None:
    if signal not in {"start", "stop", "restart", "kill"}:
        raise ValueError(signal)
    client.client_api("POST", _server(identifier) + "/power", json_body={"signal": signal})


def resources(client: PanelClient, identifier: str) -> dict[str, Any]:
    _, payload = client.client_api("GET", _server(identifier) + "/resources")
    return payload["attributes"]


def list_databases(client: PanelClient, identifier: str) -> list[dict[str, Any]]:
    _, payload = client.client_api("GET", _server(identifier) + "/databases")
    return [item["attributes"] for item in payload.get("data") or []]


def _database_attributes(payload: dict[str, Any]) -> dict[str, Any]:
    attrs = dict(payload["attributes"])
    # Panel 1.15 nests fractal includes inside attributes, not beside them.
    included = ((attrs.get("relationships") or {}).get("password") or {}).get("attributes") or {}
    password = included.get("password")
    attrs.pop("relationships", None)
    if password:
        attrs["password"] = password
    return attrs


def create_database(
    client: PanelClient, identifier: str, database: str, remote: str = "%"
) -> dict[str, Any]:
    _, payload = client.client_api(
        "POST",
        _server(identifier) + "/databases",
        json_body={"database": database, "remote": remote},
    )
    return _database_attributes(payload)


def rotate_database_password(
    client: PanelClient, identifier: str, database_id: str
) -> dict[str, Any]:
    _, payload = client.client_api(
        "POST",
        _server(identifier) + f"/databases/{database_id}/rotate-password",
    )
    return _database_attributes(payload)


def delete_database(client: PanelClient, identifier: str, database_id: str) -> None:
    client.client_api("DELETE", _server(identifier) + f"/databases/{database_id}")


def list_schedules(client: PanelClient, identifier: str) -> list[dict[str, Any]]:
    _, payload = client.client_api("GET", _server(identifier) + "/schedules")
    rows: list[dict[str, Any]] = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        attrs = dict(item.get("attributes") or {})
        # Tasks arrive as a sibling of attributes, not inside them.
        relationships = item.get("relationships")
        if isinstance(relationships, dict):
            attrs["relationships"] = relationships
        rows.append(attrs)
    return rows


def create_schedule(
    client: PanelClient,
    identifier: str,
    *,
    name: str,
    minute: str = "0",
    hour: str = "*",
    day_of_month: str = "*",
    month: str = "*",
    day_of_week: str = "*",
    only_when_online: bool = False,
    is_active: bool = True,
) -> dict[str, Any]:
    _, payload = client.client_api(
        "POST",
        _server(identifier) + "/schedules",
        json_body={
            "name": name,
            "is_active": is_active,
            "only_when_online": only_when_online,
            "minute": minute,
            "hour": hour,
            "day_of_month": day_of_month,
            "month": month,
            "day_of_week": day_of_week,
        },
    )
    return payload["attributes"]


def create_schedule_task(
    client: PanelClient,
    identifier: str,
    schedule_id: int,
    *,
    action: str,
    payload: str,
    time_offset: int = 0,
    continue_on_failure: bool = False,
) -> dict[str, Any]:
    _, body = client.client_api(
        "POST",
        _server(identifier) + f"/schedules/{schedule_id}/tasks",
        json_body={
            "action": action,
            "payload": payload,
            "time_offset": time_offset,
            "continue_on_failure": continue_on_failure,
        },
    )
    return body["attributes"]


def update_schedule(
    client: PanelClient,
    identifier: str,
    schedule_id: int,
    *,
    name: str,
    minute: str,
    hour: str,
    day_of_month: str,
    month: str,
    day_of_week: str,
    only_when_online: bool,
    is_active: bool,
) -> dict[str, Any]:
    _, payload = client.client_api(
        "POST",
        _server(identifier) + f"/schedules/{schedule_id}",
        json_body={
            "name": name,
            "is_active": is_active,
            "only_when_online": only_when_online,
            "minute": minute,
            "hour": hour,
            "day_of_month": day_of_month,
            "month": month,
            "day_of_week": day_of_week,
        },
    )
    return payload["attributes"]


def update_schedule_task(
    client: PanelClient,
    identifier: str,
    schedule_id: int,
    task_id: int,
    *,
    action: str,
    payload: str,
    time_offset: int,
    continue_on_failure: bool,
    sequence_id: int | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "action": action,
        "payload": payload,
        "time_offset": time_offset,
        "continue_on_failure": continue_on_failure,
    }
    if sequence_id is not None:
        body["sequence_id"] = sequence_id
    _, response = client.client_api(
        "POST",
        _server(identifier) + f"/schedules/{schedule_id}/tasks/{task_id}",
        json_body=body,
    )
    return response["attributes"]


def delete_schedule(client: PanelClient, identifier: str, schedule_id: int) -> None:
    client.client_api("DELETE", _server(identifier) + f"/schedules/{schedule_id}")


def delete_schedule_task(
    client: PanelClient, identifier: str, schedule_id: int, task_id: int
) -> None:
    client.client_api("DELETE", _server(identifier) + f"/schedules/{schedule_id}/tasks/{task_id}")


def startup(client: PanelClient, identifier: str) -> dict[str, Any]:
    _, payload = client.client_api("GET", _server(identifier) + "/startup")
    return payload


def server(client: PanelClient, identifier: str) -> dict[str, Any]:
    _, payload = client.client_api("GET", _server(identifier))
    return payload.get("attributes") or {}


def docker_image_choices(images: Any) -> list[dict[str, str]]:
    """Egg image map from the panel startup response, one row per image."""
    if not isinstance(images, dict):
        return []
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for name, image in images.items():
        image_text = str(image or "").strip()
        if not image_text or image_text in seen:
            continue
        seen.add(image_text)
        label = str(name or "").strip() or image_text
        rows.append({"name": label, "image": image_text})
    return rows


def update_docker_image(client: PanelClient, identifier: str, image: str) -> None:
    image = image.strip()
    if not image:
        raise ValueError("Choose a Docker image.")
    client.client_api(
        "PUT",
        _server(identifier) + "/settings/docker-image",
        json_body={"docker_image": image},
    )


def update_startup_variable(
    client: PanelClient, identifier: str, key: str, value: str
) -> dict[str, Any]:
    _, payload = client.client_api(
        "PUT",
        _server(identifier) + "/startup/variable",
        json_body={"key": key, "value": value},
    )
    return payload["attributes"]


def rename_server(client: PanelClient, identifier: str, name: str) -> None:
    client.client_api(
        "POST",
        _server(identifier) + "/settings/rename",
        json_body={"name": name, "description": ""},
    )


def list_allocations(client: PanelClient, identifier: str) -> list[dict[str, Any]]:
    _, payload = client.client_api("GET", _server(identifier) + "/network/allocations")
    return [item["attributes"] for item in payload.get("data") or []]


def create_allocation(client: PanelClient, identifier: str) -> dict[str, Any]:
    _, payload = client.client_api("POST", _server(identifier) + "/network/allocations")
    return payload["attributes"]


def update_allocation_notes(
    client: PanelClient, identifier: str, allocation_id: int, notes: str
) -> dict[str, Any]:
    _, payload = client.client_api(
        "POST",
        _server(identifier) + f"/network/allocations/{allocation_id}",
        json_body={"notes": notes},
    )
    return payload["attributes"]


def set_primary_allocation(client: PanelClient, identifier: str, allocation_id: int) -> None:
    client.client_api("POST", _server(identifier) + f"/network/allocations/{allocation_id}/primary")


def delete_allocation(client: PanelClient, identifier: str, allocation_id: int) -> None:
    client.client_api("DELETE", _server(identifier) + f"/network/allocations/{allocation_id}")


def list_backups(client: PanelClient, identifier: str) -> list[dict[str, Any]]:
    _, payload = client.client_api("GET", _server(identifier) + "/backups")
    return [item["attributes"] for item in payload.get("data") or []]


def create_backup(client: PanelClient, identifier: str, name: str) -> dict[str, Any]:
    _, payload = client.client_api(
        "POST",
        _server(identifier) + "/backups",
        json_body={"name": name, "is_locked": False},
    )
    return payload["attributes"]


def toggle_backup_lock(client: PanelClient, identifier: str, backup_id: str) -> None:
    client.client_api("POST", _server(identifier) + f"/backups/{backup_id}/lock")


def restore_backup(
    client: PanelClient, identifier: str, backup_id: str, *, truncate: bool = False
) -> None:
    """Restore a backup. truncate deletes every current file before the extract."""
    client.client_api(
        "POST",
        _server(identifier) + f"/backups/{backup_id}/restore",
        json_body={"truncate": bool(truncate)},
    )


def delete_backup(client: PanelClient, identifier: str, backup_id: str) -> None:
    client.client_api("DELETE", _server(identifier) + f"/backups/{backup_id}")


def execute_schedule(client: PanelClient, identifier: str, schedule_id: int) -> None:
    client.client_api("POST", _server(identifier) + f"/schedules/{schedule_id}/execute")


def jdbc_url(username: str, password: str, host: str, port: int, database: str) -> str:
    from urllib.parse import quote

    return f"jdbc:mysql://{quote(username, safe='')}:{quote(password, safe='')}@{host}:{port}/{database}"
