"""Short-lived panel calls. Status never includes a Wings URL or token."""

from __future__ import annotations

import base64
import threading
from typing import Any

import kopf

from .. import control, files
from ..bridge import register_session, register_transfer
from ..limits import within_file_limit
from ..client import PanelError
from ..constants import GROUP, VERSION
from ..files import UnsafePath
from ..panel import panel_client as _client

PLURAL = "pterodactylactions"
_MAX_BYTES = 256 * 1024
# Liqo can deliver the same action twice before status.phase is visible.
# A second panel call would create another backup, database, or port.
_action_guard = threading.Lock()
_actions_started: set[str] = set()


def action_already_finished(status: dict[str, Any] | None) -> bool:
    if not isinstance(status, dict):
        return False
    return str(status.get("phase") or "") in {"Ready", "Error"}


def begin_action(namespace: str, name: str) -> bool:
    """True only for the first delivery of this action object."""
    key = f"{namespace}/{name}"
    with _action_guard:
        if key in _actions_started:
            return False
        _actions_started.add(key)
        return True


def reset_action_claims() -> None:
    with _action_guard:
        _actions_started.clear()


def _text(payload: dict[str, Any], key: str, default: str = "") -> str:
    value = payload.get(key)
    if value is None:
        return default
    return str(value)


def _decode(payload: dict[str, Any], key: str) -> bytes:
    raw = base64.b64decode(_text(payload, key), validate=False)
    if len(raw) > _MAX_BYTES:
        raise ValueError("file is too large")
    return raw


def _encode(data: bytes) -> str:
    if len(data) > _MAX_BYTES:
        raise ValueError("file is too large")
    return base64.b64encode(data).decode()


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {"url", "socket", "token"} or "signed" in lowered:
                continue
            cleaned[key] = _scrub(item)
        return cleaned
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, str) and (
        "token=" in value or "/upload/file" in value or "/download/file" in value
    ):
        return ""
    return value


def _db(attrs: dict[str, Any]) -> dict[str, Any]:
    host = attrs.get("host") or {}
    if isinstance(host, dict):
        address = str(host.get("address") or "")
        port = int(host.get("port") or 0)
    else:
        address = str(host or "")
        port = int(attrs.get("port") or 0)
    row = {
        "id": str(attrs.get("id") or ""),
        "name": attrs.get("database") or attrs.get("name") or "",
        "username": attrs.get("username") or "",
        "host": address,
        "port": port,
        "connectionsFrom": attrs.get("remote") or attrs.get("connections_from") or "%",
    }
    password = attrs.get("password") or ""
    if password:
        row["password"] = password
    return row


def _cron(attrs: dict[str, Any], name: str, flat: str) -> str:
    cron = attrs.get("cron") or {}
    if attrs.get(flat) not in (None, ""):
        return str(attrs[flat])
    if isinstance(cron, dict) and cron.get(name) not in (None, ""):
        return str(cron[name])
    return "*"


def _schedule_task(attrs: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(attrs.get("id") or ""),
        "sequenceId": int(attrs.get("sequence_id") or 0),
        "action": attrs.get("action") or "",
        "payload": attrs.get("payload") or "",
        "timeOffset": int(attrs.get("time_offset") or 0),
        "continueOnFailure": bool(attrs.get("continue_on_failure")),
    }


def _schedule_tasks(attrs: dict[str, Any]) -> list[dict[str, Any]]:
    relationships = attrs.get("relationships") or {}
    tasks = relationships.get("tasks") if isinstance(relationships, dict) else None
    rows = tasks.get("data") if isinstance(tasks, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        row = item.get("attributes")
        if isinstance(row, dict):
            out.append(_schedule_task(row))
    return out


def _schedule(attrs: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(attrs.get("id") or ""),
        "name": attrs.get("name") or "",
        "minute": _cron(attrs, "minute", "cron_minute"),
        "hour": _cron(attrs, "hour", "cron_hour"),
        "dayOfMonth": _cron(attrs, "day_of_month", "cron_day_of_month"),
        "month": _cron(attrs, "month", "cron_month"),
        "dayOfWeek": _cron(attrs, "day_of_week", "cron_day_of_week"),
        "isActive": bool(attrs.get("is_active", True)),
        "onlyWhenOnline": bool(
            attrs.get("only_when_online", attrs.get("only_when_server_is_online", False))
        ),
        "tasks": _schedule_tasks(attrs),
        "lastRunAt": (
            "" if attrs.get("last_run_at") in (None, "") else str(attrs.get("last_run_at"))
        ),
        "nextRunAt": (
            "" if attrs.get("next_run_at") in (None, "") else str(attrs.get("next_run_at"))
        ),
        "isProcessing": bool(attrs.get("is_processing")),
    }


def _file(attrs: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": attrs.get("name") or "",
        "isFile": bool(attrs.get("is_file")),
        "size": int(attrs.get("size") or 0),
        "mime": attrs.get("mimetype") or "",
    }


def _allocation(attrs: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(attrs.get("id") or 0),
        "ip": attrs.get("ip") or "",
        "port": int(attrs.get("port") or 0),
        "alias": attrs.get("alias") or "",
        "notes": attrs.get("notes") or "",
        "isDefault": bool(attrs.get("is_default")),
    }


def _backup(attrs: dict[str, Any]) -> dict[str, Any]:
    return {
        "uuid": attrs.get("uuid") or "",
        "name": attrs.get("name") or "",
        "isSuccessful": bool(attrs.get("is_successful")),
        "isLocked": bool(attrs.get("is_locked")),
        "bytes": int(attrs.get("bytes") or 0),
        "completedAt": attrs.get("completed_at") or "",
    }


def _run(identifier: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    client = _client()
    if action == "list_files":
        rows = files.list_files(client, identifier, _text(payload, "directory", "/"))
        return {"files": [_file(row) for row in rows]}
    if action == "create_folder":
        files.create_folder(client, identifier, _text(payload, "root", "/"), _text(payload, "name"))
        return {}
    if action == "write_file":
        files.write_file(client, identifier, _text(payload, "path"), _decode(payload, "contentB64"))
        return {}
    if action == "read_file":
        return {"contentB64": _encode(files.read_file(client, identifier, _text(payload, "path")))}
    if action == "copy_file":
        files.copy_file(client, identifier, _text(payload, "location"))
        return {}
    if action == "delete_files":
        names = [str(name) for name in (payload.get("files") or [])]
        files.delete_files(client, identifier, _text(payload, "root", "/"), names)
        return {}
    if action == "upload_file":
        files.upload_file(
            client,
            identifier,
            _text(payload, "directory", "/"),
            _text(payload, "filename"),
            _decode(payload, "contentB64"),
        )
        return {}
    if action == "download_file":
        return {
            "contentB64": _encode(files.download_file(client, identifier, _text(payload, "path")))
        }
    if action == "compress_files":
        names = [str(name) for name in (payload.get("files") or [])]
        if not names:
            raise ValueError("Choose a folder to archive.")
        archive = files.compress_files(client, identifier, _text(payload, "root", "/"), names)
        return {"name": str(archive.get("name") or "")}
    if action == "rename_file":
        files.rename_file(
            client,
            identifier,
            _text(payload, "root", "/"),
            _text(payload, "source"),
            _text(payload, "dest"),
        )
        return {}
    if action == "resources":
        attrs = control.resources(client, identifier)
        usage = attrs.get("resources") or {}
        return {
            "state": attrs.get("current_state") or "",
            "cpuAbsolute": float(usage.get("cpu_absolute") or 0),
            "memoryBytes": int(usage.get("memory_bytes") or 0),
            "diskBytes": int(usage.get("disk_bytes") or 0),
            "networkRxBytes": int(usage.get("network_rx_bytes") or 0),
            "networkTxBytes": int(usage.get("network_tx_bytes") or 0),
            "uptimeMillis": int(usage.get("uptime") or 0),
        }
    if action == "list_databases":
        return {"databases": [_db(row) for row in control.list_databases(client, identifier)]}
    if action == "create_database":
        created = control.create_database(
            client, identifier, _text(payload, "database"), _text(payload, "remote", "%")
        )
        return {"database": _db(created)}
    if action == "rotate_database":
        rotated = control.rotate_database_password(client, identifier, _text(payload, "databaseId"))
        return {"database": _db(rotated)}
    if action == "delete_database":
        control.delete_database(client, identifier, _text(payload, "databaseId"))
        return {}
    if action == "list_schedules":
        return {"schedules": [_schedule(row) for row in control.list_schedules(client, identifier)]}
    if action == "create_schedule":
        if not _text(payload, "month"):
            raise ValueError("month is required")
        created = control.create_schedule(
            client,
            identifier,
            name=_text(payload, "name"),
            minute=_text(payload, "minute", "*"),
            hour=_text(payload, "hour", "*"),
            day_of_month=_text(payload, "dayOfMonth", "*"),
            month=_text(payload, "month"),
            day_of_week=_text(payload, "dayOfWeek", "*"),
            only_when_online=bool(payload.get("onlyWhenOnline")),
            is_active=True if "isActive" not in payload else bool(payload.get("isActive")),
        )
        return {"schedule": _schedule(created)}
    if action == "update_schedule":
        if not _text(payload, "month"):
            raise ValueError("month is required")
        updated = control.update_schedule(
            client,
            identifier,
            int(_text(payload, "scheduleId")),
            name=_text(payload, "name"),
            minute=_text(payload, "minute", "*"),
            hour=_text(payload, "hour", "*"),
            day_of_month=_text(payload, "dayOfMonth", "*"),
            month=_text(payload, "month"),
            day_of_week=_text(payload, "dayOfWeek", "*"),
            only_when_online=bool(payload.get("onlyWhenOnline")),
            is_active=bool(payload.get("isActive")),
        )
        return {"schedule": _schedule(updated)}
    if action == "create_schedule_task":
        control.create_schedule_task(
            client,
            identifier,
            int(_text(payload, "scheduleId")),
            action=_text(payload, "taskAction"),
            payload=_text(payload, "taskPayload"),
            time_offset=int(payload.get("timeOffset") or 0),
            continue_on_failure=bool(payload.get("continueOnFailure")),
        )
        return {}
    if action == "update_schedule_task":
        sequence = payload.get("sequenceId")
        control.update_schedule_task(
            client,
            identifier,
            int(_text(payload, "scheduleId")),
            int(_text(payload, "taskId")),
            action=_text(payload, "taskAction"),
            payload=_text(payload, "taskPayload"),
            time_offset=int(payload.get("timeOffset") or 0),
            continue_on_failure=bool(payload.get("continueOnFailure")),
            sequence_id=None if sequence in (None, "") else int(sequence),
        )
        return {}
    if action == "delete_schedule_task":
        control.delete_schedule_task(
            client,
            identifier,
            int(_text(payload, "scheduleId")),
            int(_text(payload, "taskId")),
        )
        return {}
    if action == "execute_schedule":
        state = (control.resources(client, identifier).get("current_state") or "").lower()
        if state != "running":
            raise ValueError("A schedule can run only while the server is running")
        control.execute_schedule(client, identifier, int(_text(payload, "scheduleId")))
        return {}
    if action == "delete_schedule":
        control.delete_schedule(client, identifier, int(_text(payload, "scheduleId")))
        return {}
    if action == "startup":
        payload_body = control.startup(client, identifier)
        meta = payload_body.get("meta") or {}
        current = str(control.server(client, identifier).get("docker_image") or "")
        variables = []
        for item in payload_body.get("data") or []:
            attrs = item.get("attributes") or item
            variables.append(
                {
                    "envVariable": attrs.get("env_variable") or "",
                    "name": attrs.get("name") or "",
                    "description": attrs.get("description") or "",
                    "serverValue": str(attrs.get("server_value") or ""),
                    "userViewable": bool(attrs.get("user_viewable", True)),
                    "userEditable": bool(attrs.get("is_editable", False)),
                    "rules": attrs.get("rules") or "",
                }
            )
        return {
            "startupCommand": meta.get("startup_command") or "",
            "rawStartupCommand": meta.get("raw_startup_command") or "",
            "dockerImage": current,
            "dockerImages": control.docker_image_choices(meta.get("docker_images")),
            "variables": variables,
        }
    if action == "update_startup":
        control.update_startup_variable(
            client, identifier, _text(payload, "key"), _text(payload, "value")
        )
        return {}
    if action == "update_docker_image":
        control.update_docker_image(client, identifier, _text(payload, "dockerImage"))
        return {}
    if action == "list_allocations":
        return {
            "allocations": [
                _allocation(row) for row in control.list_allocations(client, identifier)
            ]
        }
    if action == "create_allocation":
        return {"allocation": _allocation(control.create_allocation(client, identifier))}
    if action == "update_allocation":
        updated = control.update_allocation_notes(
            client, identifier, int(payload.get("allocationId") or 0), _text(payload, "notes")
        )
        return {"allocation": _allocation(updated)}
    if action == "set_primary":
        control.set_primary_allocation(client, identifier, int(payload.get("allocationId") or 0))
        return {}
    if action == "delete_allocation":
        control.delete_allocation(client, identifier, int(payload.get("allocationId") or 0))
        return {}
    if action == "list_backups":
        return {"backups": [_backup(row) for row in control.list_backups(client, identifier)]}
    if action == "create_backup":
        return {
            "backup": _backup(control.create_backup(client, identifier, _text(payload, "name")))
        }
    if action == "restore_backup":
        control.restore_backup(
            client,
            identifier,
            _text(payload, "backupUuid"),
            truncate=bool(payload.get("truncate")),
        )
        return {}
    if action == "lock_backup":
        control.toggle_backup_lock(client, identifier, _text(payload, "backupUuid"))
        return {}
    if action == "delete_backup":
        control.delete_backup(client, identifier, _text(payload, "backupUuid"))
        return {}
    if action == "rename":
        control.rename_server(client, identifier, _text(payload, "name"))
        return {}
    if action == "open_console":
        return {"session": register_session(identifier)}
    if action == "file_grant":
        op = _text(payload, "op")
        if op not in {"write", "upload", "read", "download", "stream"}:
            raise ValueError("unsupported file operation")
        size = int(payload.get("size") or 0)
        if not within_file_limit(size):
            raise ValueError("file is too large")
        return {
            "transfer": register_transfer(
                identifier,
                op,
                {
                    "path": _text(payload, "path"),
                    "directory": _text(payload, "directory", "/"),
                    "filename": _text(payload, "filename"),
                },
            )
        }
    raise ValueError(f"unknown action {action}")


@kopf.on.create(GROUP, VERSION, PLURAL)
def on_action(
    spec: dict[str, Any],
    patch: kopf.Patch,
    status: dict[str, Any] | None = None,
    name: str | None = None,
    namespace: str | None = None,
    logger: Any = None,
    **_: Any,
) -> None:
    if action_already_finished(status):
        return
    identifier = str(spec.get("identifier") or "")
    action = str(spec.get("action") or "")
    payload = spec.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}
    if not identifier or not action:
        patch.status["phase"] = "Error"
        patch.status["message"] = "identifier and action are required"
        return
    if not begin_action(str(namespace or ""), str(name or "")):
        if logger is not None:
            logger.info("skipping %s/%s; this action was already handled", namespace, name)
        return
    try:
        result = _scrub(_run(identifier, action, payload))
    except UnsafePath:
        patch.status["phase"] = "Error"
        patch.status["message"] = "That path is not allowed"
        return
    except PanelError as exc:
        patch.status["phase"] = "Error"
        patch.status["message"] = exc.detail[:300]
        return
    except (ValueError, RuntimeError) as exc:
        patch.status["phase"] = "Error"
        patch.status["message"] = str(exc)[:300]
        return
    patch.status["phase"] = "Ready"
    patch.status["message"] = ""
    patch.status["result"] = result
