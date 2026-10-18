"""Queue polling for asynchronous VirtFusion operations."""

import logging
import time

from .client import VirtFusionClient
from .constants import QUEUE_POLL_INTERVAL, QUEUE_POLL_TIMEOUT
from .errors import VirtFusionHTTPError

logger = logging.getLogger(__name__)


def _queue_fail_message(queue_id: int, data: dict) -> str:
    """Keep CR status readable — VF queue payloads include full hypervisor traces."""
    job = data.get("job") or data.get("action") or ""
    errors = data.get("errors") if isinstance(data.get("errors"), dict) else {}
    exception = errors.get("exception") if isinstance(errors, dict) else {}
    detail = ""
    if isinstance(exception, dict):
        detail = str(exception.get("message") or "")[:200]
    suffix = f" ({job})" if job else ""
    extra = f": {detail}" if detail and detail != "[]" else ""
    return f"Queue {queue_id} failed{suffix}{extra}"


def is_noop_power_failure(err: VirtFusionHTTPError, action: str) -> bool:
    """True when VF queued a power job that is already satisfied (guest running/stopped).

    Nested KVM often returns a failed Boot/Shutdown/Restart queue with an empty
    hypervisor exception instead of success when the domain is already in that state.
    A real failure includes extra exception text after the job name.
    """
    msg = err.message or str(err)
    needle = {
        "boot": "Boot",
        "shutdown": "Shutdown",
        "restart": "Restart",
    }.get(action)
    if not needle or needle not in msg:
        return False
    suffix = msg.split("failed", 1)[-1]
    return ": " not in suffix


def poll_queue(client: VirtFusionClient, queue_id: int, timeout: int = QUEUE_POLL_TIMEOUT) -> None:
    """Poll GET /queue/{id} until finished or failed.

    Raises:
        VirtFusionHTTPError: On queue failure or timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = client.get_queue(queue_id)
        if not result:
            time.sleep(QUEUE_POLL_INTERVAL)
            continue

        data = result.get("data", result)
        if not isinstance(data, dict):
            time.sleep(QUEUE_POLL_INTERVAL)
            continue
        if data.get("failed"):
            raise VirtFusionHTTPError(_queue_fail_message(queue_id, data))
        if data.get("finished"):
            logger.info("Queue %s finished", queue_id)
            return

        progress = data.get("progress", 0)
        logger.debug("Queue %s progress: %s%%", queue_id, progress)
        time.sleep(QUEUE_POLL_INTERVAL)

    raise VirtFusionHTTPError(f"Queue {queue_id} timed out after {timeout}s")
