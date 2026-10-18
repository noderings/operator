"""Task polling for asynchronous SolusVM 2 operations."""

from __future__ import annotations

import logging
import time
from typing import Any

from .client import SolusVMClient, as_mapping, unwrap_data
from .constants import (
    TASK_FAILED_STATUSES,
    TASK_FINISHED_STATUSES,
    TASK_POLL_INTERVAL,
    TASK_POLL_TIMEOUT,
)
from .errors import SolusVMHTTPError

logger = logging.getLogger(__name__)


def extract_task_id(result: dict[str, Any] | None) -> int | None:
    """Pull a task id from WHMCS/SDK-shaped responses.

    Create often returns ``meta.task_id`` beside server ``data``. Power/reinstall
    wrap a Task as ``data`` (id + status pending/queued/running/done/…).
    """
    if not result:
        return None
    meta = as_mapping(result.get("meta"))
    if meta.get("task_id"):
        return int(meta["task_id"])
    data = result.get("data", result)
    if not isinstance(data, dict):
        return None
    if data.get("task_id"):
        return int(data["task_id"])
    nested = data.get("task")
    if isinstance(nested, dict) and nested.get("id"):
        return int(nested["id"])
    status = str(data.get("status") or "").lower()
    if data.get("id") and status in TASK_FINISHED_STATUSES | {
        "pending",
        "queued",
        "running",
    }:
        return int(data["id"])
    return None


def poll_task(
    client: SolusVMClient, task_id: int, timeout: int = TASK_POLL_TIMEOUT
) -> dict[str, Any]:
    """Poll GET /tasks/{id} until status is finished.

    Finished: done / done_with_errors / failed / canceled.
    Timeout 1800s, interval 5s. Surfaces ``output`` on failure.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = client.get_task(task_id)
        data = unwrap_data(result)
        status = str(data.get("status") or "").lower()
        if status in TASK_FAILED_STATUSES:
            output = data.get("output") or ""
            raise SolusVMHTTPError(
                f"Task {task_id} {status}: {output}".strip(),
            )
        if status in TASK_FINISHED_STATUSES:
            logger.info("Task %s finished (%s)", task_id, status)
            return data
        logger.debug("Task %s status=%s progress=%s", task_id, status, data.get("progress"))
        time.sleep(TASK_POLL_INTERVAL)

    raise SolusVMHTTPError(f"Task {task_id} timed out after {timeout}s")


def poll_if_task(client: SolusVMClient, result: dict[str, Any] | None) -> int | None:
    """If the response contains a task id, poll it. Return the id or None."""
    task_id = extract_task_id(result)
    if task_id:
        poll_task(client, task_id)
    return task_id
