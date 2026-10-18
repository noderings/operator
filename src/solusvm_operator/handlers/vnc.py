"""Kopf handlers for SolusVMVNCRequest resources."""

from __future__ import annotations

import logging
from typing import Any

import kopf
import kubernetes

from ..client import SolusVMClient
from ..config import load_credentials
from ..constants import GROUP, TRANSIENT_RETRY_DELAY_SECONDS, VERSION
from ..errors import SolusVMHTTPError, TransientError
from ..servers import build_ws_url, get_vnc_password, vnc_up
from ..types import CONTROL_PLURAL, NODE_PLURAL, VM_PLURAL, VNC_PLURAL, SolusVMVNCRequestSpec

logger = logging.getLogger(__name__)


def _resolve_server(vm_name: str, namespace: str) -> tuple[int, str]:
    """Look up SolusVMVM → status.serverId and control endpoint."""
    api = kubernetes.client.CustomObjectsApi()
    vm = api.get_namespaced_custom_object(
        group=GROUP,
        version=VERSION,
        namespace=namespace,
        plural=VM_PLURAL,
        name=vm_name,
    )
    server_id = vm.get("status", {}).get("serverId")
    if not server_id:
        raise SolusVMHTTPError(f"SolusVMVM {vm_name} has no serverId")

    node_name = vm.get("spec", {}).get("solusvmNodeRef", {}).get("name", "")
    node = api.get_namespaced_custom_object(
        group=GROUP,
        version=VERSION,
        namespace=namespace,
        plural=NODE_PLURAL,
        name=node_name,
    )
    control_name = node.get("spec", {}).get("controlRef", {}).get("name", "")
    ctrl = api.get_namespaced_custom_object(
        group=GROUP,
        version=VERSION,
        namespace=namespace,
        plural=CONTROL_PLURAL,
        name=control_name,
    )
    endpoint = ctrl.get("spec", {}).get("endpoint", "")
    return int(server_id), endpoint


@kopf.on.create(GROUP, VERSION, VNC_PLURAL)
@kopf.on.update(GROUP, VERSION, VNC_PLURAL)
def on_vnc_request(
    spec: dict[str, Any],
    meta: dict[str, Any],
    patch: kopf.Patch,
    logger: kopf.Logger,
    **_: Any,
) -> None:
    parsed = SolusVMVNCRequestSpec.model_validate(spec)
    namespace = meta.get("namespace", "default")

    try:
        server_id, endpoint = _resolve_server(parsed.vmRef.name, namespace)
        creds = load_credentials(endpoint)
        client = SolusVMClient(creds.url, creds.token)

        server = client.get_server(server_id)
        password = get_vnc_password(server)
        vnc = vnc_up(client, server_id)
        ws_url = build_ws_url(vnc["url"], endpoint)
        if not password or not ws_url:
            raise TransientError("VNC password or websocket URL is not ready yet")

        patch.status["ready"] = True
        patch.status["wsUrl"] = ws_url
        patch.status["password"] = password
        logger.info("VNC ready for server %s", server_id)

    except SolusVMHTTPError as e:
        patch.status["ready"] = False
        patch.status["error"] = "token rejected" if e.status_code in (401, 403) else str(e)
        if e.status_code in (401, 403):
            raise kopf.PermanentError("token rejected") from e
        if isinstance(e, TransientError):
            raise kopf.TemporaryError(str(e), delay=TRANSIENT_RETRY_DELAY_SECONDS) from e
        raise kopf.PermanentError(str(e)) from e
    except Exception as e:
        patch.status["ready"] = False
        patch.status["error"] = str(e)
        raise kopf.TemporaryError(str(e), delay=TRANSIENT_RETRY_DELAY_SECONDS) from e
